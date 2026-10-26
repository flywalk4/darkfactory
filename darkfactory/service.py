"""Authorization, dependency graph and atomic scheduler invariants."""

import hashlib
import json
import math
import os
import secrets
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import case, func, select

from . import agents, registry
from .db import (
    ROOT,
    Connection,
    Link,
    Organization,
    Pool,
    Principal,
    Project,
    Task,
    Token,
    decrypt,
    event,
    now,
    uid,
)

ROLES = {"observer": 0, "operator": 1, "owner": 2}
TERMINAL = {"merged", "review_required", "cancelled", "duplicate", "needs_human", "checked"}
READY = {"queued", "waiting_pool", "waiting_dependency", "waiting_integration"}
# Phases that hold a model connection; only these count against org and pool limits.
AGENT_PHASES = {"plan", "implement", "review"}


@dataclass
class Access:
    principal: Principal
    token: Token

    @property
    def org_id(self):
        return self.principal.org_id

    def require(self, role):
        if min(ROLES[self.principal.role], ROLES[self.token.role]) < ROLES[role]:
            raise HTTPException(403, "Недостаточно прав")

    def permits(self, project):
        return project.org_id == self.org_id and all(
            not ids or project.id in ids for ids in (self.principal.project_ids, self.token.project_ids)
        )

    def require_scope(self, project_ids):
        for allowed in (self.principal.project_ids, self.token.project_ids):
            if allowed and (not project_ids or not set(project_ids) <= set(allowed)):
                raise HTTPException(403, "Нельзя расширить область доступа")

    def organization_owner(self):
        self.require("owner")
        self.require_scope([])

    def project(self, db, project_id):
        project = db.get(Project, project_id)
        if not project or not self.permits(project):
            raise HTTPException(404, "Проект не найден")
        return project

    def task(self, db, task_id):
        task = db.get(Task, task_id)
        if not task:
            raise HTTPException(404, "Задача не найдена")
        self.project(db, task.project_id)
        return task


def authenticate(db, raw):
    token = db.scalar(select(Token).where(Token.digest == hashlib.sha256(raw.encode()).hexdigest()))
    principal = db.get(Principal, token.principal_id) if token and not token.revoked else None
    if not principal or not principal.enabled:
        raise HTTPException(401, "Неверный или отозванный токен")
    return Access(principal, token)


def issue_token(db, principal, role, project_ids):
    if role not in ROLES or ROLES[role] > ROLES[principal.role]:
        raise HTTPException(400, "Токен не может расширять права пользователя")
    if principal.project_ids and (not project_ids or not set(project_ids) <= set(principal.project_ids)):
        raise HTTPException(400, "Токен не может расширять доступ к проектам")
    raw = secrets.token_urlsafe(32)
    token = Token(
        principal_id=principal.id,
        digest=hashlib.sha256(raw.encode()).hexdigest(),
        role=role,
        project_ids=project_ids,
    )
    db.add(token)
    db.flush()
    return {"id": token.id, "token": raw}


def add_link(db, access, task_id, other_id, kind, artifact=False):
    left, right = access.task(db, task_id), access.task(db, other_id)
    if kind not in {"depends_on", "parent", "related_to", "duplicate"} or left.id == right.id:
        raise HTTPException(400, "Недопустимая связь")
    if kind != "related_to" and left.state not in READY | {"needs_human"}:
        raise HTTPException(409, "Менять ожидания можно только до запуска или после остановки")
    if db.scalar(select(Link).where(Link.task_id == left.id, Link.other_id == right.id, Link.kind == kind)):
        return
    if kind == "parent" and db.scalar(select(Link).where(Link.kind == "parent", Link.other_id == right.id)):
        raise HTTPException(409, "У подзадачи уже есть родитель")
    edges = {}
    for link in db.scalars(select(Link).where(Link.kind.in_(["depends_on", "parent"]))):
        edges.setdefault(link.task_id, []).append(link.other_id)
    if kind in {"depends_on", "parent"}:
        if registry.reaches(db, right.id, left.id):
            raise HTTPException(409, "Цикл в графе ожидания")
        pending, visited = [right.id], set()
        while pending:
            current = pending.pop()
            if current == left.id:
                raise HTTPException(409, "Цикл в графе ожидания")
            if current not in visited:
                visited.add(current)
                pending.extend(edges.get(current, []))
    if kind == "parent":
        try:
            active = any(
                member.state in {"running", "recovering"}
                for task in (left, right)
                for member in execution_limits(db, task)["members"]
            )
        except ValueError as exc:
            raise HTTPException(409, f"Повреждён граф подзадач: {exc}") from None
        if active:
            raise HTTPException(409, "Нельзя объединять бюджеты деревьев с работающими задачами")
    db.add(Link(task_id=left.id, other_id=right.id, kind=kind, artifact=artifact))
    project = db.get(Project, left.project_id)
    if kind in {"depends_on", "parent"}:
        left.verified_sha = left.reviewed_sha = None
        if left.phase != "prepare":
            left.phase = "implement"
    if kind == "duplicate":
        left.state, left.reason = "duplicate", f"Дубликат {right.id}"
        cancel_delegated(db, left)
    if kind != "related_to" and project.integration_task == left.id:
        project.integration_task = None
    event(db, left, "relationship", f"{kind}: {right.id}")


def in_incident_tree(db, task, incident_id):
    """Match a specific incident ancestor, never an unrelated sibling under the same root."""
    incident = db.get(Task, incident_id) if incident_id else None
    if incident is None or incident.org_id != task.org_id:
        return False
    current, visited = task, set()
    while current is not None and current.id not in visited:
        if current.id == incident_id:
            return True
        visited.add(current.id)
        parents = list(db.scalars(select(Link).where(Link.kind == "parent", Link.other_id == current.id)))
        if len(parents) != 1:
            return False
        current = db.get(Task, parents[0].task_id)
        if current is not None and current.org_id != task.org_id:
            return False
    return False


def resume(
    db,
    task,
    *,
    message="",
    extend_seconds=0,
    extra_corrections=0,
    extra_api_budget_usd=0,
    rework=False,
    author="Уточнение владельца",
    keep_phase=False,
):
    """Requeue a stopped task; extensions are task-local credits, project defaults stay unchanged."""
    if task.reason == "token_limit":
        # Another budget on top of what was spent: the owner chose to keep going.
        from .usage import spent_tokens

        task.role_config = {**(task.role_config or {}), "token_credit": spent_tokens(task.id)}
    task.time_credit += extend_seconds
    task.correction_credit += extra_corrections
    task.api_budget_usd += extra_api_budget_usd
    if task.switches >= 8:
        task.switch_credit += 8
    task.state, task.reason, task.cancel_requested = "queued", None, False
    if (task.role_config or {}).get("workspace_swept"):
        # The finished workspace was swept; a resumed task starts again from a fresh clone.
        task.phase = "prepare"
    task.role_config = {
        k: v for k, v in (task.role_config or {}).items() if k not in {"merge_approved", "workspace_swept"}
    }
    if message:
        task.request += f"\n\n{author}:\n" + message
    if (message and not keep_phase) or rework:
        task.verified_sha = task.reviewed_sha = None
        if task.phase != "prepare":
            task.phase = "implement"
        project = db.get(Project, task.project_id)
        if project.integration_task == task.id:
            project.integration_task = None


def cancel_delegated(db, task):
    """Cancel only descendants created by delegation, preserving independently linked work."""
    pending, visited = [task], {task.id}
    while pending:
        parent = pending.pop()
        for link in db.scalars(select(Link).where(Link.task_id == parent.id, Link.kind == "parent")):
            child = db.get(Task, link.other_id)
            if (
                child is None
                or child.id in visited
                or child.project_id != parent.project_id
                or child.plan_depth <= parent.plan_depth
            ):
                continue
            visited.add(child.id)
            pending.append(child)
            if child.state in TERMINAL - {"needs_human"}:
                continue
            child.cancel_requested = True
            if child.state not in {"running", "recovering"}:
                child.state, child.reason = "cancelled", f"Отменена родительская задача {task.id}"
                project = db.get(Project, child.project_id)
                if project.integration_task == child.id:
                    project.integration_task = None
            event(db, child, "delegated_cancelled", f"Отмена родительской задачи {task.id}", parent=parent.id)


def dependency_reason(db, task, ancestors=()):
    if task.id in ancestors:
        return "Повреждён граф зависимостей: цикл"
    if reason := registry.input_reason(db, task, ancestors):
        return reason
    for link in db.scalars(
        select(Link).where(Link.task_id == task.id, Link.kind.in_(["depends_on", "parent"]))
    ):
        other = db.get(Task, link.other_id)
        source = db.get(Project, other.project_id)
        if source.incident_id and not (
            in_incident_tree(db, task, source.incident_id) and in_incident_tree(db, other, source.incident_id)
        ):
            return f"Ожидает восстановления preprod проекта {source.name}"
        if not accepted_dependency(other, link.artifact):
            return f"Ожидает {other.title}: {other.state}"
        if dependency_reason(db, other, (*ancestors, task.id)) or dependency_commits(db, other) != (
            other.dependency_commits or {}
        ):
            return f"Ожидает повторной приёмки зависимостей задачи {other.title}"
    return None


def accepted_dependency(task, artifact):
    if artifact:
        return (
            task.candidate_sha
            if task.state in {"merged", "review_required"} and task.reviewed_sha == task.candidate_sha
            else None
        )
    return task.result_sha if task.state == "merged" else None


def dependency_commits(db, task):
    return {
        **registry.snapshot(db, task),
        **{
            link.id: accepted_dependency(db.get(Task, link.other_id), link.artifact)
            for link in db.scalars(
                select(Link).where(Link.task_id == task.id, Link.kind.in_(["depends_on", "parent"]))
            )
        },
    }


def waiting_on(db, task):
    return list(
        db.scalars(select(Link).where(Link.other_id == task.id, Link.kind.in_(["depends_on", "parent"])))
    )


def release_dropped_dependencies(db, task):
    """A cancelled input never arrives: the waiting task takes over its goal instead of waiting for a person."""
    for link in list(
        db.scalars(select(Link).where(Link.task_id == task.id, Link.kind.in_(["depends_on", "parent"])))
    ):
        other = db.get(Task, link.other_id)
        if other.state not in {"cancelled", "duplicate"}:
            continue
        db.delete(link)
        task.request += (
            f"\n\nФабрика:\nЗависимость «{other.title}» не будет выполнена ({other.reason or other.state}). "
            "Если без её результата задачу не закрыть, сделай нужную часть сама. Её цель:\n"
            + other.request[:4000]
        )
        event(
            db,
            task,
            "dependency_dropped",
            f"Снята отменённая зависимость «{other.title}»; её цель передана этой задаче",
            other=other.id,
            link=link.kind,
        )
    db.flush()


def dependencies_changed(db, task):
    return bool(dependency_reason(db, task)) or dependency_commits(db, task) != (
        task.dependency_commits or {}
    )


def refresh_dependencies(db, task):
    """Bind work to accepted inputs; retain work and budgets when those inputs change."""
    release_dropped_dependencies(db, task)
    reason = dependency_reason(db, task)
    commits = dependency_commits(db, task)
    if reason or commits != (task.dependency_commits or {}):
        task.verified_sha = task.reviewed_sha = None
        if task.phase not in {"prepare", "plan", "collect"}:
            task.phase = "collect"
        project = db.get(Project, task.project_id)
        if project.integration_task == task.id:
            project.integration_task = None
        if not reason:
            previous = task.dependency_commits or {}
            task.dependency_commits = commits
            event(
                db,
                task,
                "dependencies_changed",
                "Принятые входы обновлены; приёмка будет повторена",
                previous=previous,
                commits=commits,
            )
        if reason:
            task.state, task.reason = "waiting_dependency", reason
        elif task.reason:
            task.reason = None
    return reason


def execution_limits(db, task):
    """Return shared, cumulative budgets for one parent tree; malformed trees fail closed."""
    root, ancestors = task, set()
    while True:
        if root.id in ancestors:
            raise ValueError("Cycle in parent task graph")
        ancestors.add(root.id)
        parents = list(db.scalars(select(Link).where(Link.kind == "parent", Link.other_id == root.id)))
        if not parents:
            break
        if len(parents) != 1:
            raise ValueError("Task has multiple parents")
        root = db.get(Task, parents[0].task_id)
        if root is None or root.org_id != task.org_id:
            raise ValueError("Missing or unauthorized parent task")
    members, visited, pending = [], set(), [root]
    while pending:
        current = pending.pop()
        if current.id in visited:
            raise ValueError("Cycle or multiple parents in task tree")
        visited.add(current.id)
        members.append(current)
        for link in db.scalars(select(Link).where(Link.kind == "parent", Link.task_id == current.id)):
            child = db.get(Task, link.other_id)
            if child is None or child.org_id != root.org_id:
                raise ValueError("Missing or unauthorized child task")
            parents = list(db.scalars(select(Link).where(Link.kind == "parent", Link.other_id == child.id)))
            if len(parents) != 1:
                raise ValueError("Task has multiple parents")
            pending.append(child)
    project = db.get(Project, root.project_id)
    if project is None:
        raise ValueError("Missing parent task project")
    return {
        "root": root,
        "members": members,
        "active_seconds": sum(member.active_seconds for member in members),
        "time_reserved": sum(member.time_reserved_seconds for member in members),
        "time_budget": project.max_seconds + sum(member.time_credit for member in members),
        # Cancelled members already stopped looping; their cycles must not starve the rest of the tree.
        "corrections": sum(
            member.corrections for member in members if member.state not in ("cancelled", "duplicate")
        ),
        "correction_limit": project.max_corrections + sum(member.correction_credit for member in members),
        "switches": sum(member.switches for member in members),
        "switch_credit": sum(member.switch_credit for member in members),
        "api_budget": sum(member.api_budget_usd for member in members),
        "api_spent": sum(member.api_spent_usd for member in members),
        "api_reserved": sum(member.api_reserved_usd for member in members),
    }


def _routing(task, project):
    role = "reviewer" if task.phase == "review" else "writer"
    override = (task.role_config or {}).get(role)
    config = json.loads(decrypt(project.secret) or "{}")
    profile = agents.resolve(config, task.phase, (task.role_config or {}).get("profiles"))
    profile_provider = (profile or {}).get("provider")
    if profile_provider == "project":
        profile_provider = None
    preferred = "api" if task.explicit_api else override or profile_provider or getattr(project, role)
    return preferred, preferred == "api"


def _subscription_quota_rank(connection, policy, timestamp, *, respect_recheck=True):
    """Rank usable observations; expired or absent reports remain unverified."""
    details = connection.quota_details or {}
    remaining, reset = details.get("remaining_percent"), details.get("reset_at")
    observed = connection.quota_observed

    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    known = (
        number(remaining)
        and 0 <= remaining <= 100
        and number(observed)
        and 0 <= timestamp - observed <= policy.get("quota_max_age_seconds", 300)
        and (reset is None or (number(reset) and reset > timestamp))
        and (not respect_recheck or connection.quota_source != "recheck_due")
    )
    if known:
        return (
            (0, -remaining)
            if remaining > 0 and remaining >= policy.get("min_remaining_percent", 10)
            else None
        )
    return None if policy.get("require_known_quota", False) else (1, 0)


def sessions_per_connection(pool):
    """Parallel task sessions one account may run; each executor has its own CLI home."""
    value = (pool.policy or {}).get("sessions_per_connection") or os.getenv(
        "DARKFACTORY_SESSIONS_PER_CONNECTION", "3"
    )
    try:
        return max(1, min(10, int(value)))
    except ValueError:
        return 3


def recheck_seconds():
    return max(60, int(os.getenv("DARKFACTORY_QUOTA_RECHECK_SECONDS", "3600")))


def quota_retry(reset_at=None, delay=300):
    """When to probe an exhausted subscription again: the provider's reset, but at least hourly.

    Limits are often reset by hand or reported late; an hourly probe recovers without the owner.
    """
    recheck = recheck_seconds()
    return (
        now() + 5 if reset_at and reset_at <= now() + 5 else min(reset_at or now() + delay, now() + recheck)
    )


def connection_sessions(db, exclude=None):
    rows = db.execute(
        select(Task.connection_id, func.count())
        .where(Task.connection_id.is_not(None), Task.state.in_(("running", "recovering")), Task.id != exclude)
        .group_by(Task.connection_id)
    )
    return dict(rows.all())


def choose_connection(db, task, project, limits=None, *, allow_quota_probe=False, shared=False):
    """Subscriptions run several sessions up to the pool limit; shared=True (director) ignores the limit."""
    preferred, api_only = _routing(task, project)
    config = json.loads(decrypt(project.secret) or "{}")
    profile = agents.resolve(config, task.phase, (task.role_config or {}).get("profiles"))
    pool_id = project.reviewer_pool if task.phase == "review" else project.writer_pool
    if not pool_id:
        return None
    pool = db.get(Pool, pool_id)
    if not pool or pool.org_id != task.org_id:
        return None
    policy = pool.policy or {}
    limits = execution_limits(db, task) if limits is None else limits
    paid_allowed = limits["api_budget"] > 0 and (
        api_only or json.loads(decrypt(project.secret) or "{}").get("allow_paid_fallback", False) is True
    )
    cheap = agents.light(task) and not api_only
    choices, quota_ranks = [], {}
    timestamp = now()
    sessions, session_limit = connection_sessions(db, task.id), sessions_per_connection(pool)
    for conn in db.scalars(select(Connection).where(Connection.org_id == task.org_id)):
        if pool_id not in conn.pool_ids or (not shared and sessions.get(conn.id, 0) >= session_limit):
            continue
        if (
            conn.status == "quota"
            and conn.retry_at
            and min(conn.retry_at, (conn.quota_observed or conn.retry_at) + recheck_seconds()) <= timestamp
        ):
            conn.status, conn.quota_source = "ready", "recheck_due"
        if conn.status != "ready" or (conn.provider == "api" and not paid_allowed):
            continue
        if conn.provider == "api" and (profile or {}).get("mcp_definitions"):
            continue
        if conn.provider not in policy.get("allowed_providers", ["codex", "claude", "api"]):
            continue
        model = profile.get("model") if profile and profile["provider"] == conn.provider else None
        if cheap and not model:
            model = agents.light_model(conn.provider, policy)
        if policy.get("allowed_models") and (model or conn.model) not in policy["allowed_models"]:
            continue
        quota_group = json.loads(decrypt(conn.secret)).get("quota_group")
        if quota_group and any(
            other.status == "quota"
            and (not other.retry_at or other.retry_at > timestamp)
            and json.loads(decrypt(other.secret)).get("quota_group") == quota_group
            for other in db.scalars(select(Connection).where(Connection.org_id == task.org_id))
        ):
            continue
        if api_only and conn.provider != "api":
            continue
        # Light work may take any subscription in the pool: cheap models are interchangeable.
        if not pool.cross_runtime and not cheap and conn.provider != preferred and conn.provider != "api":
            continue
        quota_rank = (0, 0) if conn.provider == "api" else _subscription_quota_rank(conn, policy, timestamp)
        if (
            quota_rank is None
            and allow_quota_probe
            and policy.get("require_known_quota", False)
            and os.getenv("DARKFACTORY_PERSONAL_MODE") == "1"
            and conn.provider == "codex"
            and json.loads(decrypt(conn.secret)).get("auth_method") == "codex_local"
        ):
            # Runner must read rateLimits before creating a thread or model turn.
            # An expired cooldown alone must not bypass a fresh low observation.
            observed_rank = _subscription_quota_rank(
                conn, {**policy, "require_known_quota": False}, timestamp, respect_recheck=False
            )
            if observed_rank is not None:
                quota_rank = (2, 0)
        if quota_rank is None:
            continue
        quota_ranks[conn.id] = quota_rank
        choices.append(conn)
    choices.sort(
        key=lambda c: (
            quota_ranks[c.id][0] == 2,
            c.provider == "api" and not api_only,
            sessions.get(c.id, 0),
            not cheap and c.provider != preferred,
            sessions.get(c.id, 0) if cheap else 0,
            quota_ranks[c.id],
            c.priority,
            c.id == task.last_connection,
            c.id,
        )
    )
    return choices[0] if choices else None


REMOTE_BACKOFF = 300


def remote_down(project):
    return (project.health or {}).get("remote_down_until", 0) > now()


HOLD = ROOT / "system" / "hold"


def held():
    """True while the hold file exists: `touch /data/system/hold` before a deploy, remove it after."""
    return HOLD.exists()


def claim(db, concurrency, only=None):
    """Caller holds queue advisory lock; stale owners must first be stopped by recovery. `only` claims that
    one task if it is ready, with every limit applied as usual."""
    if held():
        # An operator or a deploy script is draining the worker: running steps finish, nothing new starts.
        return None
    running = list(db.scalars(select(Task).where(Task.state.in_(["running", "recovering"]))))
    if len(running) >= concurrency:
        return None
    priority_rank = case({"critical": 0, "high": 1, "normal": 2, "low": 3}, value=Task.priority, else_=2)
    tasks = list(
        db.scalars(
            select(Task)
            .where(Task.state.in_(READY), *((Task.id == only,) if only else ()))
            # Background preprod checks yield to work someone is waiting for.
            .order_by(Task.incident.desc(), Task.system_check, priority_rank, Task.created, Task.id)
        )
    )
    projects = {}

    def project_of(item):
        if item.project_id not in projects:
            projects[item.project_id] = db.get(Project, item.project_id)
        return projects[item.project_id]

    # The task holding a project's integration lock goes first: every other integrator waits on it.
    tasks.sort(key=lambda item: (not item.incident, project_of(item).integration_task != item.id))
    # Org and pool limits count model sessions only. Checks, integration and merges hold no connection;
    # counting them let a monitor plus one checks run leave no slot for any model work.
    agents = [active for active in running if active.phase in AGENT_PHASES]
    pool_load = {}
    for active in agents:
        active_project = project_of(active)
        active_pool = active_project.reviewer_pool if active.phase == "review" else active_project.writer_pool
        pool_load[active_pool] = pool_load.get(active_pool, 0) + 1
    monitoring = any(active.system_check for active in running)
    for task in tasks:
        project = project_of(task)
        if remote_down(project):
            continue
        if task.system_check and monitoring:
            # One preprod monitor at a time; it is background work.
            continue
        if (task.role_config or {}).get("wait_files_until", 0) > now():
            # Waiting for an earlier task that changes the same files.
            continue
        pool_id = project.reviewer_pool if task.phase == "review" else project.writer_pool
        pool = db.get(Pool, pool_id) if pool_id else None
        policy = (pool.policy or {}) if pool else {}
        if task.phase in AGENT_PHASES:
            organization = db.get(Organization, task.org_id)
            if sum(active.org_id == task.org_id for active in agents) >= organization.max_active:
                continue
            if pool_id and pool_load.get(pool_id, 0) >= policy.get("max_active", 2):
                continue
        try:
            limits = execution_limits(db, task)
        except ValueError as exc:
            task.state, task.reason = "needs_human", f"Повреждён граф подзадач: {exc}"
            event(db, task, "limit", task.reason)
            continue
        if refresh_dependencies(db, task):
            continue
        if (
            limits["active_seconds"] >= limits["time_budget"]
            or limits["switches"] >= policy.get("max_switches", 8) + limits["switch_credit"]
        ):
            task.state, task.reason = "needs_human", "Достигнут предел времени или смен подключений"
            # Without an event the stop is silent: no notification, and the supervisor never looks.
            event(db, task, "limit", task.reason)
            continue
        repair = in_incident_tree(db, task, project.incident_id)
        if (
            project.incident_id
            and not repair
            and task.phase in {"integrate", "verify", "review", "publish", "merge"}
        ):
            task.state, task.reason = "waiting_integration", "Интеграция остановлена: инцидент preprod"
            continue
        if task.phase in {"integrate", "verify", "publish", "merge"}:
            if repair and project.integration_task != task.id:
                previous = db.get(Task, project.integration_task) if project.integration_task else None
                if previous is None or previous.state not in {"running", "recovering"}:
                    if previous and previous.state not in TERMINAL:
                        previous.verified_sha = previous.reviewed_sha = None
                        previous.phase, previous.state = "integrate", "waiting_integration"
                        previous.reason = "Ожидает восстановления preprod"
                        event(db, previous, "integration_preempted", f"Приоритет инцидента {task.id}")
                    project.integration_task = None
            if project.integration_task not in (None, task.id):
                task.state, task.reason = (
                    "waiting_integration",
                    "Другая задача проверяет объединённый результат",
                )
                continue
            project.integration_task = task.id
        connection = None
        if task.phase in {"plan", "implement", "review"}:
            # API reservations are already charged to api_spent until confirmed settlement.
            temporary_api = any(
                member.id != task.id
                and member.state in {"running", "recovering"}
                and member.api_reserved_usd > 0
                for member in limits["members"]
            )
            if _routing(task, project)[1] and limits["api_spent"] >= limits["api_budget"]:
                task.state = "waiting_pool" if temporary_api else "needs_human"
                task.reason = (
                    "Бюджет API зарезервирован другой подзадачей" if temporary_api else "Исчерпан бюджет API"
                )
                if task.state == "needs_human":
                    event(db, task, "limit", task.reason)
                if project.integration_task == task.id:
                    project.integration_task = None
                continue
            connection = choose_connection(db, task, project, limits)
            if not connection:
                connection = choose_connection(db, task, project, limits, allow_quota_probe=True)
            if not connection:
                task.state, task.reason = "waiting_pool", "Нет доступного подключения в пуле"
                if project.integration_task == task.id:
                    project.integration_task = None
                continue
            if connection.provider == "api" and limits["api_spent"] >= limits["api_budget"]:
                task.state = "waiting_pool" if temporary_api else "needs_human"
                task.reason = (
                    "Бюджет API зарезервирован другой подзадачей" if temporary_api else "Исчерпан бюджет API"
                )
                if task.state == "needs_human":
                    event(db, task, "limit", task.reason)
                if project.integration_task == task.id:
                    project.integration_task = None
                continue
        available_seconds = limits["time_budget"] - limits["active_seconds"] - limits["time_reserved"]
        if available_seconds <= 0:
            temporary = any(
                member.id != task.id
                and member.state in {"running", "recovering"}
                and member.time_reserved_seconds > 0
                for member in limits["members"]
            )
            task.state = "waiting_pool" if temporary else "needs_human"
            task.reason = (
                "Время зарезервировано другой подзадачей" if temporary else "Исчерпан предел времени"
            )
            if task.state == "needs_human":
                event(db, task, "limit", task.reason)
            if project.integration_task == task.id:
                project.integration_task = None
            continue
        contenders = 0
        for member in limits["members"]:
            if member.state not in READY or dependency_reason(db, member):
                continue
            member_project = db.get(Project, member.project_id)
            integration = member.phase in {"integrate", "verify", "review", "publish", "merge"}
            if integration and (
                (member_project.incident_id and not in_incident_tree(db, member, member_project.incident_id))
                or member_project.integration_task not in {None, member.id}
            ):
                continue
            contenders += 1
        task.time_reserved_seconds = available_seconds / max(1, contenders)
        task.state, task.reason, task.lease_owner = "running", None, uid()
        task.heartbeat, task.lease_until = now(), now() + 45
        task.execution_deadline = now() + task.time_reserved_seconds
        if connection:
            connection.lease_task, connection.lease_until = task.id, task.lease_until
            task.connection_id = connection.id
        event(db, task, "phase_started", task.phase, connection=task.connection_id)
        return task.id, task.lease_owner
    return None
