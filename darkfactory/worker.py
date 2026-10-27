"""Persistent phase runner. All acceptance evidence belongs to an immutable commit."""

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from logging.handlers import RotatingFileHandler

from sqlalchemy import func, select

from . import (
    agents,
    backlog,
    cleanup,
    deploys,
    disk,
    environment,
    initialization,
    logs,
    mainsync,
    promotions,
    schedules,
    selfreport,
    service,
    standbuild,
    standqa,
    toolchains,
    usage,
)
from .auth_sessions import advance_logins
from .chat_commands import exchange
from .coordinator import delegate, needs_plan
from .db import (
    ROOT,
    Connection,
    Event,
    Link,
    Pool,
    Project,
    Session,
    Task,
    decrypt,
    event,
    initialize,
    now,
    transaction,
    uid,
)
from .director import advance_dialogues
from .director_stops import awaits_director, handle_stop, latest_transcript
from .execution import LATER_PREFIX, DockerExecutor, Stalled, Stopped, controller_lock, write_handoff
from .gitops import GitError, Repository
from .notifications import completion_summary
from .telegram_bot import listen as listen_telegram

log = logging.getLogger("darkfactory.worker")


def charge_time(task):
    if task.heartbeat and task.execution_deadline:
        elapsed = max(0, min(now(), task.execution_deadline) - task.heartbeat)
        task.active_seconds += elapsed
        task.time_reserved_seconds = max(0, task.time_reserved_seconds - elapsed)
        task.heartbeat = now()


def release(db, task):
    charge_time(task)
    task.time_reserved_seconds = 0
    if task.connection_id:
        connection = db.get(Connection, task.connection_id)
        if connection and connection.lease_task == task.id:
            # Other sessions may still run on this account; keep it marked busy for one of them.
            other = db.scalar(
                select(Task).where(
                    Task.connection_id == connection.id,
                    Task.id != task.id,
                    Task.state.in_(("running", "recovering")),
                )
            )
            connection.lease_task, connection.lease_until = (
                (other.id, other.lease_until) if other else (None, None)
            )
    task.last_connection = task.connection_id or task.last_connection
    task.connection_id = task.lease_owner = task.lease_until = None
    task.api_reserved_usd = 0
    task.execution_deadline = None
    project = db.get(Project, task.project_id)
    if task.state in service.TERMINAL or task.phase not in {
        "integrate",
        "verify",
        "review",
        "publish",
        "merge",
    }:
        if project.integration_task == task.id:
            project.integration_task = None


def egress_blocker(db, task):
    """Hosts refused since the current implementation round began."""
    started = db.scalar(
        select(func.max(Event.at)).where(
            Event.task_id == task.id, Event.kind == "phase_started", Event.message == "implement"
        )
    )
    hosts = set()
    for row in db.scalars(
        select(Event).where(
            Event.task_id == task.id, Event.kind == "egress_denied", Event.at >= (started or 0)
        )
    ):
        hosts.update((row.data or {}).get("hosts", []))
    return sorted(hosts)


NETWORK_ERRORS = re.compile(
    r"ERR_(?:CONNECTION|NAME_NOT_RESOLVED|TUNNEL|PROXY)|ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|ETIMEDOUT"
    r"|getaddrinfo|403 Forbidden|Could not resolve host|Name or service not known",
)


def network_cited(message, hosts):
    """Did the rejection fail on the network, or for reasons another cycle can fix?"""
    return bool(NETWORK_ERRORS.search(message)) or any(host.rsplit(":", 1)[0] in message for host in hosts)


ENV_PREFIX = "[ENV]"


def review_findings(message):
    try:
        findings = json.loads(message).get("findings") or []
    except (ValueError, AttributeError):
        return []
    return [item for item in findings if isinstance(item, str)]


def blocking(message):
    """Findings that need another cycle; [LATER] improvements stay on the task as notes."""
    return [item for item in review_findings(message) if not item.lstrip().startswith(LATER_PREFIX)]


def without_later(message):
    """The writer's handoff carries only what blocks; deferred improvements would pull it off course."""
    try:
        review = json.loads(message)
        review["findings"] = blocking(message)
        return json.dumps(review, ensure_ascii=False)
    except (ValueError, TypeError, AttributeError):
        return message


def checks_mutation(repo, output=""):
    """A failed result when the checks left tracked files changed: the writer needs the paths to fix it."""
    changed = repo.git("diff", "--name-only", "HEAD", cwd=repo.work, check=False).stdout.split()
    if not changed:
        return None
    text = "Required checks modified tracked source files:\n" + "\n".join(changed[:20])
    if len(changed) > 20:
        text += f"\n… and {len(changed) - 20} more"
    text += (
        "\n.git is read-only in the sandbox, so `git checkout` cannot restore them inside a check: "
        "keep tests from writing tracked files, or copy them aside and back."
    )
    if output:
        text += "\n\nCheck output:\n" + output[-2000:]
    return {"status": "checks_failed", "text": text}


def unbacked_claims(repo, claimed):
    """The writer's handoff names files its commit does not change. An empty change is left to review: the
    work may already be in preprod."""
    try:
        diff = (repo.artifacts / "diff.patch").read_text(errors="replace")
    except OSError:
        return None
    # Both sides of every header: a renamed or deleted file is still a file the commit changed.
    changed = {path for pair in re.findall(r"(?m)^diff --git a/(\S+) b/(\S+)", diff) for path in pair}
    if not changed:
        return None
    missing = [
        path
        for path in claimed or []
        # Files outside the repository (the factory's own notes in /run/cache, evidence) are never committed.
        if not (path.startswith("/") and not path.startswith("/workspace/"))
        and not any(item == path.removeprefix("/workspace/") or item.endswith("/" + path) for item in changed)
    ]
    if not missing:
        return None
    return (
        "В отчёте (handoff.json) заявлены изменения, которых нет в коммите: "
        + ", ".join(missing[:10])
        + ". Внеси эти правки или убери их из отчёта, если они не нужны."
    )


def commit_title(result):
    """The writer's title for its commit: handoff.json "commit", else its final comment's first line."""
    if result.get("commit_title"):
        return result["commit_title"]
    lines = [line for line in (result.get("text") or "").splitlines() if line.strip()]
    return lines[0] if lines and not lines[0].lstrip().startswith(("[darkfactory]", LATER_PREFIX)) else None


def deferred_lines(text):
    """[LATER] lines a writer left in its final comment."""
    return [
        line.strip().lstrip("-*• ").strip()
        for line in (text or "").splitlines()
        if line.strip().lstrip("-*• ").startswith(LATER_PREFIX)
    ]


def later_notes(db, task, items, source):
    """[LATER] notes stay on the task as comments, as review tools do; only the owner turns one into
    backlog work. Filing them automatically made the factory generate its own work from every review round."""
    notes = [
        item.lstrip()[len(LATER_PREFIX) :].strip()[:2000]
        for item in items
        if item.lstrip().startswith(LATER_PREFIX)
    ]
    # A bare checklist item says nothing anyone could act on.
    notes = [note for note in notes if note and not note.startswith("Чеклист ревью:")]
    if notes:
        event(db, task, "later_notes", f"Заметки на потом ({source}): {len(notes)}", items=notes[:30])


def environment_only(message):
    """Findings that only another environment can close, never another cycle."""
    try:
        findings = json.loads(message).get("findings") or []
    except (ValueError, AttributeError):
        return []
    if findings and all(isinstance(item, str) and item.lstrip().startswith(ENV_PREFIX) for item in findings):
        return findings
    return []


def finding_words(message):
    try:
        findings = json.loads(message).get("findings") or []
        message = " ".join(str(item) for item in findings) or message
    except (ValueError, AttributeError):
        pass
    return {word for word in re.findall(r"\w{4,}", message.lower()) if not word.isdigit()}


def repeated_review(db, task, message):
    """The reviewer rejects for the same reason as last cycle: another identical cycle only burns tokens.

    Only review is compared with review: two build logs share most of their words and read as a repeat."""
    previous = next(
        (
            item.message
            for item in db.scalars(
                select(Event)
                .where(Event.task_id == task.id, Event.kind == "correction")
                .order_by(Event.id.desc())
                .limit(10)
            )
            if (item.data or {}).get("source", "review") == "review"
        ),
        None,
    )
    if not previous:
        return False
    now_words, then_words = finding_words(message), finding_words(previous)
    if not now_words or not then_words:
        return False
    return len(now_words & then_words) / len(now_words | then_words) >= REPEAT_SIMILARITY


REPEAT_SIMILARITY = 0.5


def environment_fix(db, task, project, gaps):
    """Queue (or join) the project's environment task for these gaps; once per task, never for itself."""
    if environment.active(task) or (task.role_config or {}).get("environment_requested"):
        return None
    fixer = environment.queue(db, project, (task.role_config or {}).get("environment_author"))
    if fixer.state in {"running", "recovering"}:
        return None  # Its request is already fixed for this run; the owner decides.
    fixer.request += (
        f"\n\nЗадача «{task.title}» не может выполнить проверки в песочнице. Ревью нашло пробелы среды:\n"
        + "\n".join("- " + item.lstrip()[len(ENV_PREFIX) :].strip()[:500] for item in gaps[:20])
        + "\nДобавь недостающее в окружение (toolchains, apt, feeds, setup), если это возможно без "
        "внешних секретов и стендов; иначе опиши, что должен сделать владелец."
    )
    db.add(Link(task_id=task.id, other_id=fixer.id, kind="depends_on"))
    task.role_config = {**(task.role_config or {}), "environment_requested": fixer.id}
    db.flush()
    return fixer


CONFLICT_LIMIT = 3


def correction(db, task, project, message, source="checks"):
    """Send the task back to its writer. `source` is review, checks or conflict: only review findings are
    compared for repeats, and conflicts that other tasks' merges caused do not use the writer's cycles."""
    write_handoff(task, message)
    task.verified_sha = task.reviewed_sha = None
    task.phase = "implement"
    limits = service.execution_limits(db, task)
    budget, spent = usage.token_budget(project, task), usage.spent_tokens(task.id)
    blocked = egress_blocker(db, task)
    if blocked and network_cited(message, blocked):
        # Another cycle cannot reach what the network refuses; ask the owner instead of burning corrections.
        task.state, task.reason = "needs_human", "network_blocked"
        event(
            db,
            task,
            "network_blocked",
            "Сеть закрыта для: " + ", ".join(blocked) + ". Разрешите адреса и продолжите задачу.",
            hosts=blocked,
            finding=message[-4000:],
        )
    elif (gaps := environment_only(message)) and (fixer := environment_fix(db, task, project, gaps)):
        # The sandbox lacks something: the environment agent adds it, then this task resumes on it.
        service.refresh_dependencies(db, task)
        event(
            db,
            task,
            "environment_requested",
            "Среде агента не хватает инструментов; задача ждёт исправления окружения: " + fixer.title,
            environment_task=fixer.id,
            findings=gaps[:20],
        )
    elif gaps:
        task.state, task.reason = "needs_human", "environment_blocked"
        event(
            db,
            task,
            "environment_blocked",
            "Среде агента не хватает: "
            + "; ".join(item.lstrip()[len(ENV_PREFIX) :].strip()[:300] for item in gaps),
            findings=gaps[:20],
        )
    elif source == "conflict" and (task.role_config or {}).get("conflicts", 0) < CONFLICT_LIMIT:
        task.role_config = {
            **(task.role_config or {}),
            "conflicts": (task.role_config or {}).get("conflicts", 0) + 1,
            # The next writer run only resolves the merge; execution gives it a short leash.
            "conflict_pending": True,
        }
        task.state = "queued"
        event(db, task, "correction", message, source=source, conflict=task.role_config["conflicts"])
    elif source == "review" and repeated_review(db, task, message):
        # The writer could not act on this finding once; the supervisor or owner must change the approach.
        task.state, task.reason = "needs_human", "review_repeated"
        event(
            db,
            task,
            "review_repeated",
            "Ревью повторно отклонило задачу по той же причине",
            finding=message[-4000:],
        )
    elif budget and spent >= budget:
        # Retrying a task that already burned its budget repeats the same expensive approach.
        task.state, task.reason = "needs_human", "token_limit"
        event(
            db,
            task,
            "limit",
            f"Исчерпан бюджет токенов: {spent:,} из {budget:,}",
            tokens=spent,
            budget=budget,
        )
    elif (
        limits["corrections"] >= limits["correction_limit"]
        # One subtask must not eat the whole tree budget by retrying the same approach.
        or task.corrections >= task_correction_limit() + task.correction_credit
    ):
        task.state, task.reason = "needs_human", "correction_limit"
        event(db, task, "limit", "Исчерпаны циклы исправления; нужно решение человека")
    else:
        task.corrections += 1
        task.state = "queued"
        event(db, task, "correction", message, cycle=task.corrections, source=source)
        warned = db.scalar(select(Event.id).where(Event.task_id == task.id, Event.kind == "token_warning"))
        if budget and spent >= usage.WARN_SHARE * budget and not warned:
            event(
                db,
                task,
                "token_warning",
                f"Задача израсходовала {spent:,} из {budget:,} токенов",
                tokens=spent,
                budget=budget,
            )


def environment_retry(db, task, project, message):
    """Checks failed on a missing tool or a dependency install: no code change fixes that, so the
    project environment is rebuilt and the same checks rerun once before the owner is asked."""
    write_handoff(task, message)
    if (task.role_config or {}).get("environment_retry") != task.candidate_sha:
        task.role_config = {**(task.role_config or {}), "environment_retry": task.candidate_sha}
        toolchains.invalidate(project.id)
        task.state = "queued"
        event(
            db,
            task,
            "environment_retry",
            "Проверки упали на окружении, а не на коде; окружение пересобирается, проверки повторятся",
            finding=message[-4000:],
        )
        return
    task.state, task.reason = "needs_human", "environment_blocked"
    event(
        db,
        task,
        "environment_blocked",
        "Проверки снова упали на окружении после пересборки; нужен владелец, код не меняется",
        finding=message[-4000:],
    )


EXTENSION_SECONDS = 1800


def task_correction_limit():
    return max(1, int(os.getenv("DARKFACTORY_TASK_CORRECTIONS", "2")))


PROGRESS_PHASES = ("plan", "implement", "checks", "verify", "review", "integrate", "publish", "merge")


def tree_ceiling(project):
    config = json.loads(decrypt(project.secret) or "{}")
    return int(config.get("max_tree_seconds") or os.getenv("DARKFACTORY_MAX_TREE_SECONDS", "43200"))


EXTEND_AHEAD_SECONDS = 60


def extend_on_progress(db, task, limits):
    """Long work keeps going while it advances; only a tree without progress waits for a human."""
    members = [member.id for member in limits["members"]]
    since = db.scalar(
        select(func.max(Event.at)).where(Event.task_id.in_(members), Event.kind == "time_extended")
    ) or min(member.created for member in limits["members"])
    progressed = db.scalar(
        select(func.count(Event.id)).where(
            Event.task_id.in_(members),
            Event.kind == "phase_finished",
            Event.message.in_(PROGRESS_PHASES),
            Event.at > since,
        )
    )
    ceiling = tree_ceiling(db.get(Project, task.project_id))
    grant = min(EXTENSION_SECONDS, ceiling - limits["time_budget"])
    if not progressed or grant <= 0:
        return False
    task.time_credit += grant
    event(db, task, "time_extended", f"Продвижение есть: дереву добавлено {grant // 60} мин", seconds=grant)
    return grant


def warn_budget(db, task):
    """Say once per budget that a tree is long-running; work does not stop here."""
    # The window is wider than a beat, so some beat of every 30 s lands in it.
    if task.active_seconds < 300 or int(task.active_seconds) % 30 > BEAT_SECONDS:
        return
    limits = service.execution_limits(db, task)
    if limits["active_seconds"] < 0.8 * limits["time_budget"]:
        return
    members = [member.id for member in limits["members"]]
    warned = db.scalars(select(Event.data).where(Event.task_id.in_(members), Event.kind == "budget_warning"))
    if any((data or {}).get("budget") == limits["time_budget"] for data in warned):
        return
    event(
        db,
        task,
        "budget_warning",
        f"Задача долгая: использовано {int(limits['active_seconds']) // 60} из {limits['time_budget'] // 60} мин;"
        " при продвижении время продлится автоматически",
        budget=limits["time_budget"],
    )


def finish_time_slice(db, task, message):
    charge_time(task)
    limits = service.execution_limits(db, task)
    if limits["active_seconds"] >= limits["time_budget"] and extend_on_progress(db, task, limits):
        limits = service.execution_limits(db, task)
    task.state = "queued" if limits["active_seconds"] < limits["time_budget"] else "needs_human"
    task.reason = (
        message
        if task.state == "needs_human"
        else "Продолжение из сохранённой работы после временного резерва"
    )
    write_handoff(task, task.reason)
    event(db, task, "time_slice" if task.state == "queued" else "limit", task.reason)


def stall_restart(db, task, message):
    """Restart a silent run from handoff; scheduling prefers a different account."""
    limit = max(0, int(os.getenv("DARKFACTORY_STALL_RESTARTS", "2")))
    restarts = db.scalar(
        select(func.count(Event.id)).where(Event.task_id == task.id, Event.kind == "stall_restart")
    )
    write_handoff(task, f"Предыдущая попытка зависла: {message}. Продолжи с сохранённой работы.")
    if restarts >= limit:
        task.state, task.reason = "needs_human", f"Агент зависал {restarts + 1} раз: {message}"
        event(db, task, "stopped", task.reason)
    else:
        task.state, task.reason = "queued", None
        event(db, task, "stall_restart", f"Перезапуск после зависания: {message}", attempt=restarts + 1)


def publish_phase(task, project):
    """Publish, or go straight to merge when publishing adds nothing: with a plain git remote the change
    record is the task page, and the pushed branch would be deleted again right after the merge."""
    if project.forge == "git" and merge_allowed(task, project):
        task.change_url = f"/api/tasks/{task.id}"
        return "merge"
    return "publish"


def merge_allowed(task, project):
    """Merge needs review plus checks, a documentation-only initialization, owner approval,
    or a project that trusts approved review alone for preprod (`merge_without_checks`)."""
    if not task.candidate_sha or task.reviewed_sha != task.candidate_sha:
        return False
    if (task.role_config or {}).get("merge_approved") or initialization.active(task):
        return project.target == "preprod"
    if environment.active(task):
        return task.verified_sha == task.candidate_sha and project.target == "preprod"
    if project.checks:
        return task.verified_sha == task.candidate_sha
    trusted = json.loads(decrypt(project.secret) or "{}").get("merge_without_checks", True)
    return trusted and project.target == "preprod"


def accept_merge(db, task):
    task.state, task.result_sha = "merged", task.candidate_sha
    project = db.get(Project, task.project_id)
    health = project.health or {}
    if (
        task.verified_sha == task.candidate_sha
        and health.get("state") == "healthy"
        and health.get("sha") == task.target_sha
    ):
        # The merge fast-forwarded a checked preprod to a checked candidate: the new head is known healthy,
        # so the monitor does not re-run every check on it. The periodic full re-check keeps its schedule.
        project.health = {**health, "sha": task.result_sha}
    # Factory bookkeeping (index, environment) rides to the seed branch with the next real change.
    root = (
        not task.system_check
        and not initialization.active(task)
        and not environment.active(task)
        and not db.scalar(select(Link.id).where(Link.kind == "parent", Link.other_id == task.id))
    )
    event(
        db,
        task,
        "accepted",
        f"Слито в preprod. Перенос в {db.get(Project, task.project_id).seed_branch} — вручную в карточке задачи"
        if root
        else "Проверенный результат слит в preprod",
        commit=task.result_sha,
        promotion=bool(root),
        checks=list(db.get(Project, task.project_id).checks),
    )


def disjoint_preprod(repo, project, before, candidate, target):
    """True when preprod moved from `before` to a healthy, already checked `target` without touching any
    file the task changed in `candidate`."""
    health = project.health or {}
    if health.get("state") != "healthy" or health.get("sha") != target:
        return False
    return untouched_by_preprod(repo, before, candidate, target)


def untouched_by_preprod(repo, before, candidate, target):
    """True when preprod moved from `before` to `target` without touching any file the task changed."""
    if not (before and candidate and target) or before == target:
        return False
    try:
        ours = repo.git("diff", "--name-only", before, candidate, cwd=repo.work).split()
        theirs = repo.git("diff", "--name-only", before, target, cwd=repo.work).split()
    except Exception:
        return False
    return bool(theirs) and not set(ours) & set(theirs)


def unchanged_preprod(project):
    """A healthy preprod whose mirror head has not moved needs no full re-run until PREPROD_RECHECK_SECONDS."""
    health = project.health or {}
    recheck = int(os.getenv("PREPROD_RECHECK_SECONDS", "21600"))
    if (
        health.get("state") != "healthy"
        or not health.get("sha")
        or now() - health.get("checked_at", 0) >= recheck
    ):
        return False
    mirror = ROOT / "repositories" / (project.id + ".git")
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{project.target}"],
            cwd=mirror,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return head == health["sha"]


class Worker:
    def __init__(self, executor=None):
        self.executor = executor or DockerExecutor()

    def schedule_health(self):
        interval = self.health_interval()
        with transaction() as db:
            for project in db.scalars(select(Project)):
                incident = db.get(Task, project.incident_id) if project.incident_id else None
                if incident and incident.state == "cancelled":
                    # A cancelled incident must not block the project forever: re-check preprod instead.
                    project.incident_id = None
                    project.health = {**(project.health or {}), "scheduled_at": 0}
                if (
                    not project.checks
                    or project.incident_id
                    or service.remote_down(project)
                    or service.repository_missing(project)
                    or now() - (project.health or {}).get("scheduled_at", 0) < interval
                ):
                    continue
                if not db.scalar(
                    select(Task.id).where(
                        Task.project_id == project.id, Task.state == "merged", Task.system_check.is_(False)
                    )
                ):
                    continue
                if unchanged_preprod(project):
                    continue
                pending = db.scalar(
                    select(Task.id).where(
                        Task.project_id == project.id,
                        Task.system_check.is_(True),
                        Task.state.in_([*service.READY, "running", "recovering"]),
                    )
                )
                if pending:
                    continue
                ident = uid()
                task = Task(
                    id=ident,
                    project_id=project.id,
                    org_id=project.org_id,
                    title="Проверка preprod",
                    request="Run required project checks against current preprod.",
                    branch="darkfactory/" + ident,
                    phase="monitor",
                    system_check=True,
                )
                db.add(task)
                db.flush()
                project.health = {**(project.health or {}), "scheduled_at": now(), "task_id": ident}
                event(db, task, "health_check", "Запланирована проверка текущей preprod")

    NETWORK_FAILURE = re.compile(
        r"ProxyError|RemoteDisconnected|Connection reset by peer|ECONNRESET|ETIMEDOUT|EAI_AGAIN|"
        r"Temporary failure in name resolution|Could not resolve host|DO NOT MATCH THE HASHES|"
        r"Connection timed out|Read timed out|Unable to load the service index|Network is unreachable"
    )
    NETWORK_PATIENCE = 6
    ENVIRONMENT_PATIENCE = 3
    ENVIRONMENT_RETRY = 900
    INSTALL_STEP = re.compile(
        r"\b(?:dotnet restore|npm (?:ci|install)|pnpm install|yarn(?: install)?|pip install|-m pip|venv|uv sync|"
        r"poetry install|bundle install|go mod download|cargo fetch|composer install|apt-get)\b"
    )

    @staticmethod
    def health_interval():
        return max(30, int(os.getenv("PREPROD_CHECK_INTERVAL", "300")))

    @classmethod
    def environment_failure(cls, diagnosis):
        """The failing check is a dependency install or its tool is missing (exit 127)."""
        failed = re.findall(r"^\$ (.*)\nexit=(\d+)", diagnosis, re.M)
        failed = [(command, code) for command, code in failed if code != "0"]
        if not failed:
            return False
        command, code = failed[-1]
        return code == "127" or bool(cls.INSTALL_STEP.search(command))

    # git/ssh errors when the project's own remote (VPN, forge outage) cannot be reached.
    REMOTE_FAILURE = re.compile(
        r"unable to access '|Failed to connect to|Could not connect to server|Could not resolve host|"
        r"Connection timed out|Connection refused|Network is unreachable|No route to host|"
        r"Could not read from remote repository|RPC failed|early EOF|"
        # The repository itself is gone (deleted /repos path, removed project): back off like an outage.
        r"repository '[^']*' does not exist|does not appear to be a git repository|Repository not found"
    )

    def remote_unreachable(self, db, task, project, message):
        """The forge is down, not the work: park the project with growing backoff instead of asking a human."""
        health = project.health or {}
        failures = health.get("remote_failures", 0) + 1
        pause = min(service.REMOTE_BACKOFF * 2 ** (failures - 1), 3600)
        project.health = {
            **health,
            "remote_failures": failures,
            "remote_down_until": now() + pause,
            "remote_error": message[-500:],
        }
        if task.system_check:
            task.state, task.reason = "cancelled", "Git-сервер проекта недоступен; проверка повторится"
        else:
            task.state, task.reason = "queued", None
        event(
            db, task, "remote_unreachable", f"Git-сервер проекта недоступен; повтор через {pause // 60} мин"
        )
        if failures == self.NETWORK_PATIENCE:
            event(
                db,
                task,
                "network_unstable",
                "Git-сервер проекта недоступен уже давно: проверьте VPN или доступ",
            )

    def record_health(self, db, task, project, result):
        success = result.get("status") == "ok"
        project.health = {
            **(project.health or {}),
            "checked_at": now(),
            "sha": result.get("target"),
            "state": "healthy" if success else "broken",
        }
        if success:
            project.health.pop("network_failures", None)
            project.health.pop("environment_failures", None)
            # A distinct state: counted as cancelled, passed preprod checks looked like abandoned work.
            task.state, task.reason = "checked", "Системная проверка preprod прошла; код не изменялся"
            return
        if not (task.role_config or {}).get("health_retried"):
            # One failing run can be a flaky network or registry; open an incident only when it repeats.
            project.health["state"] = "unknown"
            task.role_config = {**(task.role_config or {}), "health_retried": True}
            task.state, task.phase = "queued", "monitor"
            event(db, task, "health_retry", "Проверка preprod не прошла; повторяем перед открытием инцидента")
            return
        diagnosis = result.get("text", "Required preprod checks failed")
        if self.environment_failure(diagnosis):
            # A missing tool or a failed dependency install says nothing about the code: retry on schedule,
            # and only a failure that keeps repeating becomes an incident.
            failures = project.health.get("environment_failures", 0) + 1
            if failures < self.ENVIRONMENT_PATIENCE:
                project.health = {**project.health, "state": "unknown", "environment_failures": failures}
                project.health["scheduled_at"] = now() - self.health_interval() + self.ENVIRONMENT_RETRY
                task.state, task.reason = "cancelled", "Проверка упала на установке или окружении; повторится"
                event(
                    db, task, "health_environment", f"Проверка preprod упала на окружении ({failures} подряд)"
                )
                return
        project.health.pop("environment_failures", None)
        if self.NETWORK_FAILURE.search(diagnosis[-16000:]):
            # Registry outages are not code defects: re-check on schedule instead of opening an incident.
            failures = project.health.get("network_failures", 0) + 1
            project.health = {**project.health, "state": "unknown", "network_failures": failures}
            task.state, task.reason = "cancelled", "Сеть до реестров пакетов нестабильна; проверка повторится"
            event(db, task, "health_network", f"Проверка preprod упала на сетевом сбое ({failures} подряд)")
            if failures == self.NETWORK_PATIENCE:
                event(
                    db,
                    task,
                    "network_unstable",
                    "Проверки preprod подряд падают на сети: проверьте доступ к реестрам",
                )
            return
        fingerprint = hashlib.sha256(
            (str(project.checks) + re.sub(r"\d+(?:\.\d+)?", "N", diagnosis)).encode()
        ).hexdigest()
        previous = db.scalar(
            select(Task)
            .where(Task.project_id == project.id, Task.incident_key == fingerprint, Task.incident.is_(True))
            .order_by(Task.created)
        )
        if project.incident_id and (not previous or project.incident_id != previous.id):
            task.state, task.reason = "cancelled", "Инцидент уже открыт"
            return
        if previous:
            task.state, task.reason = "cancelled", "Повтор известного инцидента: " + previous.id
            project.incident_id = previous.id
            if previous.state == "merged":
                previous.state, previous.phase = "queued", "prepare"
                previous.verified_sha = previous.reviewed_sha = None
            # Cancelled/limit-stopped incidents remain stopped until explicit human action.
            write_handoff(previous, diagnosis)
            event(db, previous, "incident_reopened", "Повтор поломки; прежние время и циклы сохранены")
        else:
            task.incident, task.incident_key, task.system_check = True, fingerprint, False
            task.title = "Исправить поломку preprod"
            task.request = (
                "Required checks detected a broken preprod. Diagnose and fix without weakening tests.\n"
                + logs.condense(diagnosis, 16000)
            )
            task.state, task.phase, task.reason = "queued", "plan", None
            task.target_sha = result.get("target")
            project.incident_id = task.id
            # The failing output is already in the request; repeating it in the handoff doubles every prompt.
            write_handoff(task, "Обязательные проверки preprod упали; вывод проверок — в описании задачи.")
            event(db, task, "incident_detected", "Обязательные проверки обнаружили поломку preprod")

    def locate(self, task, project, connection, beat, remaining):
        """Best effort: a failed or empty search only means the writer searches itself."""
        from .execution import LOCATE_FILE, LOCATE_SCHEMA, locate_prompt

        try:
            found = self.executor.run(
                task,
                project,
                connection,
                "locate",
                lambda: beat(),
                min(remaining, LOCATE_SECONDS),
                prompt_override=locate_prompt(task),
                output_schema=LOCATE_SCHEMA,
            )
            if found.get("status") == "ok":
                value = json.loads(found.get("text") or "{}")
                (ROOT / "artifacts" / task.id / LOCATE_FILE).write_text(json.dumps(value))
        except Stopped:
            raise
        except Exception:
            log.warning("Locate step failed for task %s", task.id, exc_info=True)

    def heartbeat(self, task_id, owner):
        with transaction() as db:
            task = db.get(Task, task_id)
            if task.lease_owner != owner or task.state != "running":
                return False
            stamp = now()
            elapsed = max(0, min(stamp, task.execution_deadline or stamp) - (task.heartbeat or stamp))
            task.active_seconds += elapsed
            task.time_reserved_seconds = max(0, task.time_reserved_seconds - elapsed)
            task.heartbeat, task.lease_until = stamp, stamp + 45
            warn_budget(db, task)
            if task.connection_id:
                db.get(Connection, task.connection_id).lease_until = task.lease_until
            limits = service.execution_limits(db, task)
            near = task.execution_deadline and stamp >= task.execution_deadline - EXTEND_AHEAD_SECONDS
            if (near or limits["active_seconds"] >= limits["time_budget"] - EXTEND_AHEAD_SECONDS) and (
                grant := extend_on_progress(db, task, limits)
            ):
                # Extending a live run keeps the agent's context; stopping it re-reads everything from a handoff.
                task.execution_deadline = (task.execution_deadline or stamp) + grant
                task.time_reserved_seconds += grant
                limits = service.execution_limits(db, task)
            alive = (
                not task.cancel_requested
                and not service.dependencies_changed(db, task)
                and stamp < (task.execution_deadline or stamp)
                and limits["active_seconds"] < limits["time_budget"]
            )
            return max(1.0, task.execution_deadline - stamp) if alive and task.execution_deadline else alive

    def recover(self):
        with Session() as db:
            stale = [
                (t.id, t.lease_owner)
                for t in db.scalars(
                    select(Task).where(Task.state.in_(["running", "recovering"]), Task.lease_until < now())
                )
            ]
        for task_id, owner in stale:
            with transaction() as db:
                task = db.get(Task, task_id)
                if task.lease_owner != owner or task.lease_until > now():
                    continue
                task.state = "recovering"  # Revoke heartbeat/start authority before touching Docker.
            self.executor.stop(task_id, owner)
            try:
                with controller_lock(task_id), transaction() as db:
                    task = db.get(Task, task_id)
                    if task.lease_owner != owner or task.state != "recovering":
                        continue
                    end = min(now(), task.execution_deadline or now())
                    task.active_seconds += max(0, end - (task.heartbeat or end))
                    task.heartbeat = end
                    task.execution_deadline = None
                    task.state = "cancelled" if task.cancel_requested else "queued"
                    if task.state == "queued" and (recent := latest_transcript(task_id, 40)):
                        write_handoff(
                            task,
                            "Выполнение прервано перезапуском фабрики. Последние действия агента:\n" + recent,
                        )
                    event(db, task, "recovered", "Старое выполнение остановлено; рабочие файлы сохранены")
                    release(db, task)
            except BlockingIOError:
                continue  # Old controller still owns workspace; never publish a second writer.

    def run(self, task_id, owner, concurrency=None):
        """Run the claimed phase; mechanical phases that follow it run on, without a trip through the queue
        where every other task competes for the freed slot again."""
        try:
            with controller_lock(task_id):
                while owner:
                    self._run(task_id, owner)
                    owner = self.chain(task_id, concurrency)
        except BlockingIOError:
            log.warning("Workspace still owned by another controller: %s", task_id)

    def chain(self, task_id, concurrency):
        if not concurrency:
            return None
        with transaction() as db:
            task = db.get(Task, task_id)
            if task.state != "queued" or task.phase not in CHAINED_PHASES or task.cancel_requested:
                return None
            claimed = service.claim(db, concurrency, only=task_id)
        return claimed[1] if claimed else None

    def _run(self, task_id, owner):
        stopped = threading.Event()
        live = {}

        def beat():
            alive = self.heartbeat(task_id, owner)
            # An extension moves the deadline in the database; git and phase timeouts read this copy.
            if alive and not isinstance(alive, bool) and (current := live.get("task")) is not None:
                current.execution_deadline = now() + alive
            return alive

        def pulse():
            # A transient database error must not end the heartbeat: the lease would expire and recovery
            # would stop a run that is still working. Only a revoked lease or repeated failure ends it.
            failures, delay = 0, BEAT_SECONDS
            while not stopped.wait(delay):
                try:
                    if not beat():
                        return
                    failures, delay = 0, BEAT_SECONDS
                except Exception:
                    failures += 1
                    log.exception("Heartbeat failed for task %s (%d in a row)", task_id, failures)
                    if failures >= HEARTBEAT_FAILURES:
                        return
                    delay = min(10, 2 * failures)

        heart = threading.Thread(target=pulse, daemon=True)
        heart.start()
        try:
            with Session() as db:
                task = db.get(Task, task_id)
                project = db.get(Project, task.project_id)
                connection = db.get(Connection, task.connection_id) if task.connection_id else None
                if task.lease_owner != owner or task.state != "running":
                    return
            live["task"] = task
            repo = Repository(project, task)
            phase = task.phase
            result = {}
            if not beat():
                raise Stopped("Cancelled or time limit")
            if phase == "prepare":
                result["target"] = repo.prepare()
                previous = toolchains.state(project.id)
                result["toolchain"] = toolchains.provision(
                    getattr(self.executor, "client", None),
                    project.id,
                    repo.work,
                    os.getenv("EXECUTOR_IMAGE", "darkfactory-executor:local"),
                    lambda: beat(),
                )
                result["toolchain_changed"] = {k: previous.get(k) for k in ("spec", "feeds", "status")} != {
                    k: result["toolchain"][k] for k in ("spec", "feeds", "status")
                }
                if initialization.active(task):
                    initialization.prepare(repo, result["target"])
                if task.incident and task.result_sha:
                    result["target"], _ = repo.integrate()
                if (merge_from := (task.role_config or {}).get("merge_from")) and not repo._pending_merge():
                    if conflicts := repo.merge_main(merge_from):
                        write_handoff(task, conflicts.replace("with preprod", "of main into preprod"))
            elif phase == "monitor":
                result["target"] = repo.prepare()
                # Checks run in the project toolchain image; a pruned or missing one is rebuilt, not skipped.
                toolchains.provision(
                    getattr(self.executor, "client", None),
                    project.id,
                    repo.work,
                    os.getenv("EXECUTOR_IMAGE", "darkfactory-executor:local"),
                    lambda: beat(),
                )
                repo.git("fetch", "--no-tags", str(repo.mirror), result["target"], cwd=repo.work)
                repo.git("reset", "--hard", result["target"], cwd=repo.work)
                # Ignored dependency folders (node_modules, bin, obj, .venv) stay: wiping them made every
                # monitor a cold install plus a full build. Untracked sources are still removed.
                repo.git("clean", "-fd", cwd=repo.work)
                repo.own_files()
                checked = self.executor.run(
                    task,
                    project,
                    None,
                    "checks",
                    lambda: beat(),
                    max(1, task.execution_deadline - now()),
                )
                checked = checks_mutation(repo, checked.get("text", "")) or checked
                (repo.artifacts / "checks.txt").write_text(checked.get("text", ""))
                result.update(checked)
            elif phase == "collect":
                result["target"], result["sha"] = repo.integrate()
            elif phase in {"plan", "implement", "review", "checks", "verify"}:
                remaining = max(1, task.execution_deadline - now())
                if phase in {"checks", "verify"} and environment.active(task):
                    environment_state = toolchains.provision(
                        getattr(self.executor, "client", None),
                        project.id,
                        repo.work,
                        os.getenv("EXECUTOR_IMAGE", "darkfactory-executor:local"),
                        lambda: beat(),
                    )
                    if environment_state["status"] == "failed":
                        result = {
                            "status": "checks_failed",
                            "text": "Образ по environment.json не собрался:\n" + environment_state["error"],
                        }
                    elif not environment.commands(repo.work):
                        result = {
                            "status": "checks_failed",
                            "text": f"{environment.SPEC}: нет команд setup/checks",
                        }
                    else:
                        result = self.executor.run(
                            task,
                            project,
                            connection,
                            "checks",
                            lambda: beat(),
                            remaining,
                        )
                elif phase in {"checks", "verify"} and not project.checks:
                    result = {"status": "ok", "text": "No checks configured; automatic merge forbidden"}
                else:
                    role = "checks" if phase in {"checks", "verify"} else phase
                    if role == "review" and task.candidate_sha and task.target_sha:
                        # Built from the candidate itself on every review: whatever path led here (a
                        # correction, a resolved conflict, a resume), review never reads a stale or
                        # foreign diff.
                        repo.write_diff(task.target_sha, task.candidate_sha)
                    if connection and connection.provider == "api":
                        with transaction() as db:
                            current = db.get(Task, task_id)
                            if current.lease_owner != owner or current.state != "running":
                                raise Stopped("Lease revoked before API budget reservation")
                            limits = service.execution_limits(db, current)
                            available = max(0, limits["api_budget"] - limits["api_spent"])
                            if not available:
                                current.state = "waiting_pool" if limits["api_reserved"] else "needs_human"
                                current.reason = (
                                    "API-бюджет занят другой подзадачей"
                                    if limits["api_reserved"]
                                    else "API-бюджет исчерпан"
                                )
                                release(db, current)
                                return
                            current.api_reserved_usd = available
                            current.api_spent_usd += current.api_reserved_usd
                            task.api_reserved_usd = current.api_reserved_usd
                            task.api_spent_usd = current.api_spent_usd
                            event(
                                db,
                                current,
                                "budget_reserved",
                                "Бюджет попытки API зарезервирован",
                                usd=current.api_reserved_usd,
                            )
                    if role in {"plan", "implement"} and should_locate(task, connection):
                        self.locate(task, project, connection, beat, remaining)
                        remaining = max(1, task.execution_deadline - now())
                    if role == "implement" and not task.candidate_sha and (shared := overlapping_work(task)):
                        # Two writers on the same files end in a merge conflict, a re-integration and a
                        # second review for both; the later one waits until the other has merged.
                        result = {"status": "deferred", "text": ", ".join(sorted(shared)[:10])}
                    elif (
                        role == "implement"
                        and not task.candidate_sha
                        and (task.role_config or {}).get("wait_files_until")
                    ):
                        # It waited for another task's merge: start from today's preprod, or the change it
                        # waited for comes back as a conflict. No writer has run, so nothing is lost.
                        target = repo.refresh()
                        repo.git("fetch", "--no-tags", str(repo.mirror), target, cwd=repo.work)
                        repo.git("reset", "--hard", target, cwd=repo.work)
                        repo.own_files()
                        with transaction() as db:
                            current = db.get(Task, task_id)
                            current.target_sha = task.target_sha = target
                            current.role_config = {
                                key: value
                                for key, value in (current.role_config or {}).items()
                                if key != "wait_files_until"
                            }
                        result = self.executor.run(task, project, connection, role, lambda: beat(), remaining)
                    else:
                        result = self.executor.run(task, project, connection, role, lambda: beat(), remaining)
                if phase in {"checks", "verify"}:
                    (repo.artifacts / "checks.txt").write_text(result.get("text", ""))
                    result = checks_mutation(repo, result.get("text", "")) or result
                    if phase == "verify" and initialization.active(task) and result.get("status") == "ok":
                        try:
                            initialization.documents(repo, task.candidate_sha)
                        except (ValueError, UnicodeError) as exc:
                            result = {"status": "checks_failed", "text": str(exc)}
                if phase == "implement" and (inner := result.get("inner_checks")):
                    (repo.artifacts / "checks.txt").write_text(inner.get("text", ""))
                # A writer that stopped to ask or to split leaves its workspace as it is.
                if phase == "implement" and result.get("status") == "ok" and not result.get("request"):
                    try:
                        if initialization.active(task):
                            initialization.validate(repo)
                        if environment.active(task):
                            environment.validate(repo)
                    except (ValueError, UnicodeError) as exc:
                        result = {"status": "checks_failed", "text": str(exc)}
                    else:
                        if tidied := repo.tidy():
                            listed = ", ".join(tidied[:10]) + (
                                f" и ещё {len(tidied) - 10}" if len(tidied) > 10 else ""
                            )
                            with transaction() as db:
                                event(
                                    db,
                                    db.get(Task, task_id),
                                    "tidied",
                                    f"Фабрика убрала из изменения вывод прогонов и временные файлы: {listed}",
                                    files=tidied[:200],
                                )
                            result["text"] = (
                                result.get("text", "")
                                + f"\n\n[darkfactory] Вывод прогонов и временные файлы убраны из коммита "
                                f"и сохранены в артефактах задачи (run-output/): {listed}"
                            )
                        if junk := repo.junk():
                            result = {"status": "checks_failed", "text": junk}
                        else:
                            result["sha"] = repo.commit(commit_title(result))
                            if task.target_sha:
                                # Review now runs before integration: without this it read the diff of
                                # the last integration and rejected fixes it could not see.
                                repo.write_diff(task.target_sha, result["sha"])
                                # First runs only: a correction may revert a file its earlier handoff named,
                                # and must not loop on a stale claim.
                                if not task.corrections and (
                                    unbacked := unbacked_claims(repo, result.get("handoff_files"))
                                ):
                                    # The report claims a change the commit does not have: a review run
                                    # would only find that out at a much higher price.
                                    result = {
                                        "status": "checks_failed",
                                        "text": unbacked,
                                        "handoff": result.get("handoff"),
                                    }
                        write_handoff(task, result.get("text", ""), result.get("handoff"))
                        if result.get("sha"):
                            # The writer's whole final answer: for a research task it is the result itself.
                            (repo.artifacts / "report.md").write_text(result.get("text", ""))
                if phase == "review":
                    (repo.artifacts / "review.txt").write_text(result.get("text", ""))
                    if result.get("review"):
                        (repo.artifacts / "review.json").write_text(
                            json.dumps(result["review"], ensure_ascii=False)
                        )
                    else:
                        # A failed run must not leave the previous round's verdict on the task page.
                        (repo.artifacts / "review.json").unlink(missing_ok=True)
                if phase == "plan" and result.get("status") == "ok":
                    (repo.artifacts / "plan.md").write_text(result["plan"]["plan"])
            elif phase == "integrate":
                before = task.target_sha
                result["target"], result["sha"] = repo.integrate()
                result["disjoint"] = disjoint_preprod(
                    repo, project, before, task.candidate_sha, result["target"]
                )
                result["untouched"] = untouched_by_preprod(repo, before, task.candidate_sha, result["target"])
            elif phase == "publish":
                result["url"] = repo.publish(task.candidate_sha)
            elif phase == "merge":
                if not merge_allowed(task, project):
                    raise RuntimeError("Acceptance evidence does not match the candidate commit")
                if initialization.active(task) and not (task.role_config or {}).get("merge_approved"):
                    initialization.only_documents(repo, task.target_sha, task.candidate_sha)
                if task.dependency_commits:
                    # Accepted inputs are checked right before the merge. The push itself runs outside the
                    # transaction: it holds the global lock, and a slow remote would expire every lease.
                    with transaction() as db:
                        current = db.get(Task, task_id)
                        if (
                            current.lease_owner != owner
                            or current.state != "running"
                            or current.cancel_requested
                            or service.dependencies_changed(db, current)
                        ):
                            raise Stopped("Dependencies or execution authority changed before merge")
                result["merged"] = repo.merge(task.candidate_sha, task.target_sha)
            else:
                raise RuntimeError("Unknown persistent task phase")
            with transaction() as db:
                current = db.get(Task, task_id)
                project = db.get(Project, current.project_id)
                if current.lease_owner != owner or current.state != "running":
                    return
                current.state = "queued"
                if "cost_usd" in result and current.api_reserved_usd:
                    cost = max(0, min(current.api_reserved_usd, float(result["cost_usd"])))
                    current.api_spent_usd -= current.api_reserved_usd - cost
                    current.api_reserved_usd = 0
                status = result.get("status", "ok")
                if result.get("session"):
                    event(
                        db,
                        current,
                        "executor_session",
                        "Сессия исполнителя",
                        session=result["session"],
                        connection=current.connection_id,
                        phase=phase,
                    )
                if result.get("quota") and current.connection_id:
                    observed = db.get(Connection, current.connection_id)
                    observed.quota_observed = now()
                    observed.quota_source = result["quota"].get("source", "provider")
                    observed.quota_details = {
                        key: result["quota"][key]
                        for key in ("remaining_percent", "reset_at")
                        if key in result["quota"]
                    }
                    event(db, current, "quota_observed", "Получены данные квоты", **result["quota"])
                    if result["quota"].get("remaining_percent", 100) <= 0:
                        observed.status = "quota"
                        observed.retry_at = service.quota_retry(result["quota"].get("reset_at"))
                if (
                    phase == "merge"
                    and result.get("merged")
                    and current.dependency_commits
                    and service.dependencies_changed(db, current)
                ):
                    # The push ran outside the lock; an input revoked during it is surfaced, not hidden.
                    event(
                        db,
                        current,
                        "dependencies_changed_after_merge",
                        "Входы задачи изменились во время слияния; проверьте preprod",
                    )
                if current.cancel_requested:
                    current.state, current.reason = "cancelled", "Отменена пользователем"
                    if phase == "merge" and result.get("merged"):
                        accept_merge(db, current)
                        event(db, current, "merge", "Слияние завершилось до обработки отмены")
                elif phase != "merge" and service.dependencies_changed(db, current):
                    service.refresh_dependencies(db, current)
                    event(
                        db,
                        current,
                        "dependencies_interrupted",
                        "Входы изменились во время выполнения; результат требует повторной приёмки",
                    )
                elif phase == "monitor" and status in {"ok", "checks_failed"}:
                    self.record_health(db, current, project, result)
                elif status == "deferred":
                    current.role_config = {
                        **(current.role_config or {}),
                        "wait_files_until": now() + FILE_WAIT_SECONDS,
                    }
                    event(
                        db,
                        current,
                        "deferred",
                        "Ждёт: другая задача правит те же файлы — " + result.get("text", ""),
                    )
                elif status == "deadline":
                    finish_time_slice(db, current, "Исчерпано активное время дерева задач")
                elif status in {"quota", "auth"}:
                    conn = db.get(Connection, current.connection_id)
                    conn.status = status
                    conn.quota_observed, conn.quota_source = now(), "provider_error"
                    conn.quota_details = {
                        key: result["quota"][key]
                        for key in ("remaining_percent", "reset_at")
                        if key in result.get("quota", {})
                    }
                    pool_id = project.reviewer_pool if phase == "review" else project.writer_pool
                    pool = db.get(Pool, pool_id)
                    delay = (pool.policy or {}).get("retry_seconds", 300)
                    provider_reset = result.get("quota", {}).get("reset_at")
                    conn.retry_at = service.quota_retry(provider_reset, delay) if status == "quota" else None
                    if status == "quota" and provider_reset:
                        conn.quota_source = "provider_reset"
                    current.switches += 1
                    current.state = "waiting_pool"
                    write_handoff(current, result.get("text", ""))
                    event(
                        db,
                        current,
                        "switch",
                        "Квота исчерпана" if status == "quota" else "Требуется повторная авторизация",
                        connection=conn.id,
                        retry_at=conn.retry_at,
                        source="control_recheck" if status == "quota" else "auth",
                    )
                elif (
                    status == "checks_failed"
                    and phase in {"checks", "verify"}
                    and self.environment_failure(result.get("text", ""))
                ):
                    environment_retry(db, current, project, result["text"])
                elif status == "checks_failed":
                    correction(db, current, project, result.get("text", "Checks failed"))
                elif status != "ok":
                    current.state, current.reason = (
                        "needs_human",
                        result.get("text", "Executor failed")[:4000],
                    )
                    write_handoff(current, current.reason)
                    event(db, current, "execution_error", current.reason)
                elif phase == "prepare":
                    current.target_sha = result["target"]
                    current.phase = "plan" if needs_plan(db, current) else "implement"
                    toolchain = result.get("toolchain") or {}
                    if result.get("toolchain_changed") and toolchain.get("status") == "failed":
                        event(
                            db,
                            current,
                            "toolchain_failed",
                            "Не удалось собрать окружение проекта; агент работает в базовом: "
                            + (toolchain.get("error") or "")[-500:],
                            spec=toolchain.get("spec"),
                        )
                    elif result.get("toolchain_changed") and (
                        toolchain.get("spec") or toolchain.get("feeds")
                    ):
                        event(
                            db,
                            current,
                            "toolchain_ready",
                            "Окружение проекта: "
                            + (", ".join(f"{k} {v}" for k, v in toolchain["spec"].items()) or "базовое")
                            + (
                                " · реестры: " + ", ".join(toolchain["feeds"])
                                if toolchain.get("feeds")
                                else ""
                            ),
                            spec=toolchain.get("spec"),
                            feeds=toolchain.get("feeds"),
                        )
                elif phase == "plan":
                    if result["plan"]["questions"]:
                        current.state, current.reason = (
                            "needs_human",
                            "Нужно уточнение: " + "\n".join(result["plan"]["questions"]),
                        )
                        event(db, current, "clarification", current.reason)
                    else:
                        delegated = (
                            not initialization.active(current)
                            and not environment.active(current)
                            and delegate(db, current, result["plan"])
                        )
                        current.phase = "collect" if delegated else "implement"
                elif phase == "collect":
                    current.target_sha, current.candidate_sha = result["target"], result["sha"]
                    current.phase = "implement"
                elif phase == "implement" and (request := result.get("request")):
                    if request.get("questions"):
                        current.state, current.reason = (
                            "needs_human",
                            "Нужно уточнение: " + "\n".join(request["questions"]),
                        )
                        event(db, current, "clarification", current.reason)
                    elif delegate(db, current, request["split"]):
                        event(db, current, "split_requested", "Разработчик разбил работу на подзадачи")
                        current.phase = "collect"
                    else:
                        current.state, current.reason = (
                            "needs_human",
                            "Не удалось создать запрошенные подзадачи",
                        )
                elif phase == "implement":
                    if (current.role_config or {}).get("conflict_pending"):
                        current.role_config = {
                            key: value
                            for key, value in current.role_config.items()
                            if key != "conflict_pending"
                        }
                    current.candidate_sha, current.phase = result["sha"], "checks"
                    if (result.get("inner_checks") or {}).get("passed"):
                        # The writer's own session already passed the required checks on this tree.
                        current.verified_sha = current.candidate_sha
                        current.phase = "review"
                        event(
                            db,
                            current,
                            "checks_passed",
                            "Обязательные проверки пройдены в сессии разработчика",
                        )
                    event(
                        db,
                        current,
                        "implementation_summary",
                        completion_summary(project, result.get("text", "")) or "Изменения подготовлены",
                        commit=current.candidate_sha,
                    )
                    later_notes(db, current, deferred_lines(result.get("text")), "разработка")
                    inner = result.get("inner_checks") or {}
                    if inner and not inner.get("passed"):
                        # The writer's session already ran the same checks and used its fix attempts: a
                        # separate checks container would only repeat the failure.
                        correction(db, current, project, inner.get("text") or "Checks failed")
                elif phase == "checks":
                    # Review reads the frozen candidate before integration: it holds no integration lock,
                    # and an approval survives preprod moves in other files (untouched_by_preprod).
                    current.verified_sha = (
                        current.candidate_sha if project.checks or environment.active(current) else None
                    )
                    current.phase = "review"
                elif phase == "integrate":
                    checked = current.candidate_sha
                    reviewed = bool(checked) and current.reviewed_sha == checked
                    current.target_sha, current.candidate_sha = result["target"], result["sha"]
                    current.verified_sha = current.reviewed_sha = None
                    current.role_config = {**(current.role_config or {}), "integrated_sha": result["sha"]}
                    current.phase = "verify"
                    if (result["sha"] == checked or result.get("disjoint")) and not initialization.active(
                        current
                    ):
                        # Preprod brought nothing new, or only verified files this task never touched: the
                        # checks the task just passed still cover its code, and preprod's own monitor
                        # re-runs everything after the merge.
                        current.verified_sha = (
                            current.candidate_sha if project.checks or environment.active(current) else None
                        )
                        current.phase = "review"
                    if (
                        reviewed
                        and (result["sha"] == checked or result.get("untouched"))
                        and not initialization.active(current)
                    ):
                        # Re-integration after an approved review: preprod changed only other files, so the
                        # review of this task's own diff still holds.
                        current.reviewed_sha = current.candidate_sha
                        if current.phase == "review":
                            current.phase = publish_phase(current, project)
                elif phase == "verify":
                    current.verified_sha = (
                        current.candidate_sha if project.checks or environment.active(current) else None
                    )
                    current.phase = (
                        publish_phase(current, project)
                        if current.reviewed_sha == current.candidate_sha
                        else "review"
                    )
                elif phase == "review":
                    review_text = result.get("text", "Review rejected")
                    missing = result.get("review", {}).get("uncovered")
                    reruns = (current.role_config or {}).get("review_reruns") or {}
                    incomplete = bool(missing) and result.get("review", {}).get("approved")
                    if incomplete:
                        # An approval that never looked at some changed code is no approval. The review
                        # runs once more; a second incomplete one goes to a human.
                        if reruns.get(current.candidate_sha, 0) < 1:
                            current.role_config = {
                                **(current.role_config or {}),
                                "review_reruns": {**reruns, current.candidate_sha: 1},
                            }
                            event(
                                db,
                                current,
                                "review_incomplete",
                                "Ревью не проверило файлы кода; ревью повторяется: "
                                + ", ".join(missing[:20]),
                                files=missing[:50],
                            )
                        else:
                            current.state, current.reason = "needs_human", "review_incomplete"
                            event(
                                db,
                                current,
                                "review_incomplete",
                                "Ревью дважды не проверило файлы кода: " + ", ".join(missing[:20]),
                                files=missing[:50],
                            )
                    # A rejection that lists only [LATER] improvements is an approval.
                    elif not result.get("review", {}).get("approved") and (
                        blocking(review_text) or not review_findings(review_text)
                    ):
                        # The re-review reads what changed since this candidate first.
                        current.role_config = {
                            **(current.role_config or {}),
                            "rejected_sha": current.candidate_sha,
                        }
                        correction(db, current, project, without_later(review_text), source="review")
                    else:
                        current.reviewed_sha = current.candidate_sha
                        # First review of a fresh candidate: integrate it next. A review after
                        # integration (preprod changed this task's files) goes on to publish.
                        integrated = (current.role_config or {}).get("integrated_sha")
                        if integrated != current.candidate_sha:
                            current.phase = "integrate"
                        elif current.verified_sha != current.candidate_sha and (
                            project.checks or environment.active(current)
                        ):
                            current.phase = "verify"
                        else:
                            current.phase = publish_phase(current, project)
                elif phase == "publish":
                    current.change_url = result["url"]
                    if (project.checks and not environment.active(current)) or merge_allowed(
                        current, project
                    ):
                        current.phase = "merge"
                    else:
                        current.state, current.reason = (
                            "review_required",
                            "Нет обязательных проверок: слияние только человеком",
                        )
                        event(db, current, "review_required", current.reason, branch=current.branch)
                elif phase == "merge":
                    if result["merged"]:
                        accept_merge(db, current)
                    else:
                        # Integration decides whether the approved review still holds for the new preprod.
                        current.phase = "integrate"
                        current.verified_sha = None
                        event(
                            db,
                            current,
                            "target_changed",
                            "Вершина preprod изменилась; задача заново сольётся с ней",
                        )
                if current.phase == "review" and project.integration_task == current.id:
                    # Review reads only the frozen candidate: other tasks may integrate meanwhile.
                    project.integration_task = None
                initialization.publish(db, current, repo)
                environment.adopt(db, current, project, repo)
                if current.state == "merged":
                    if project.incident_id == current.id:
                        project.incident_id = None
                    project.health = {**(project.health or {}), "scheduled_at": 0}
                if "remote_failures" in (project.health or {}):
                    project.health = {
                        key: value
                        for key, value in project.health.items()
                        if key not in ("remote_failures", "remote_down_until", "remote_error")
                    }
                event(db, current, "phase_finished", phase, status=status)
                release(db, current)
        except Exception as exc:
            log.warning("Task %s stopped: %s", task_id, type(exc).__name__)
            try:
                self.executor.stop(task_id, owner)
                with transaction() as db:
                    task = db.get(Task, task_id)
                    if task.lease_owner == owner and task.state == "running":
                        project = db.get(Project, task.project_id)
                        if (
                            isinstance(exc, (Stopped, GitError, TimeoutError))
                            and not task.cancel_requested
                            and now() >= (task.execution_deadline or float("inf"))
                        ):
                            finish_time_slice(db, task, "Исчерпано активное время дерева задач")
                        elif isinstance(exc, Stopped) and service.dependencies_changed(db, task):
                            task.state, task.reason = "queued", None
                            service.refresh_dependencies(db, task)
                            event(
                                db,
                                task,
                                "dependencies_interrupted",
                                "Исполнитель остановлен из-за изменения зависимостей",
                            )
                        elif not task.cancel_requested and self.REMOTE_FAILURE.search(str(exc)):
                            self.remote_unreachable(db, task, project, str(exc))
                        elif isinstance(exc, GitError) and task.phase == "integrate":
                            correction(db, task, project, str(exc), source="conflict")
                        elif isinstance(exc, Stalled) and not task.cancel_requested:
                            stall_restart(db, task, str(exc))
                        else:
                            task.state = "cancelled" if task.cancel_requested else "needs_human"
                            task.reason = str(exc)[:4000]
                            event(db, task, "stopped", task.reason)
                        release(db, task)
            except Exception:
                log.exception("Could not confirm task stop; lease retained for recovery")
        finally:
            stopped.set()
            heart.join(timeout=3)


HEARTBEAT_FAILURES = 5
FILE_WAIT_SECONDS = 180
ACTIVE_STATES = (
    "running",
    "recovering",
    "queued",
    "waiting_pool",
    "waiting_integration",
    "waiting_dependency",
)
TOUCHING_PHASES = ("implement", "checks", "review", "integrate", "verify", "publish", "merge")


def task_files(task_id):
    """Files a task changes (its diff) or is about to change (its locate result)."""
    from .execution import LOCATE_FILE

    artifacts = ROOT / "artifacts" / task_id
    files = set()
    try:
        files |= {
            line.split(" b/", 1)[-1]
            for line in (artifacts / "diff.patch").read_text(errors="replace").splitlines()
            if line.startswith("diff --git ")
        }
    except OSError:
        pass
    try:
        files |= {
            str(item.get("path"))
            for item in json.loads((artifacts / LOCATE_FILE).read_text()).get("files") or []
            if isinstance(item, dict) and item.get("path")
        }
    except (OSError, ValueError, AttributeError):
        pass
    return files


# Files most tasks append to (translations, changelogs): a merge almost always combines them, and the
# integration step resolves the rare conflict. Serialising on them queued every UI task behind the others.
SHARED_FILE = re.compile(
    r"(?:^|/)(?:i18n|l10n|locales?|lang|translations?)/(?:[^/]+/)*[^/]+\.(?:json|ya?ml|po|properties|xlf|resx)$"
    r"|(?:^|/)(?:CHANGELOG|CHANGES|HISTORY)(?:\.[a-z]+)?$",
    re.IGNORECASE,
)


def overlapping_work(task):
    """Files this task is about to change that another active task of the project changes too.
    Only the earlier task keeps going; tasks of one tree (parent and children) are not compared."""
    mine = {path for path in task_files(task.id) if not SHARED_FILE.search(path)}
    if not mine:
        return set()
    with Session() as db:
        others = db.scalars(
            select(Task).where(
                Task.project_id == task.project_id,
                Task.id != task.id,
                Task.system_check.is_(False),
                Task.state.in_((*ACTIVE_STATES, "needs_human")),
                Task.phase.in_(TOUCHING_PHASES),
            )
        ).all()
        # A stop the supervisor has yet to decide usually resumes within seconds; its files stay taken.
        others = [other for other in others if other.state != "needs_human" or awaits_director(db, other)]
    shared = set()
    for other in others:
        # The earlier task goes first; a later one that already started writing (its locate finished
        # first) keeps going too, so two tasks never write the same files at once.
        if other.created < task.created or writer_started(other.id):
            shared |= mine & task_files(other.id)
    return shared


def writer_started(task_id):
    """A writer run of the task exists that began after its locate step."""
    from .execution import LOCATE_FILE

    artifacts = ROOT / "artifacts" / task_id
    try:
        located = (artifacts / LOCATE_FILE).stat().st_mtime
    except OSError:
        located = 0
    for path in (artifacts / "browser").glob("*/execution.json"):
        try:
            if json.loads(path.read_text()).get("role") == "implement" and path.stat().st_mtime >= located:
                return True
        except (OSError, ValueError):
            continue
    return False


# Phases that need no model and follow each other: integrate, verify, publish and merge.
CHAINED_PHASES = {"integrate", "verify", "publish", "merge"}
# Leases last 45 s; a beat every 5 s leaves room for several failed beats before one expires.
BEAT_SECONDS = 5
LOCATE_SECONDS = 600


def should_locate(task, connection):
    """Only a task's first writer run on a native agent, outside factory bookkeeping tasks."""
    from .execution import LOCATE_FILE

    return (
        os.getenv("DARKFACTORY_LOCATE", "1") == "1"
        and connection is not None
        and connection.provider != "api"
        and not task.corrections
        and not task.candidate_sha
        and not agents.light(task)
        and not initialization.active(task)
        and not environment.active(task)
        and not (ROOT / "artifacts" / task.id / LOCATE_FILE).exists()
    )


def periodic(future, pool, job):
    """Resubmit a background job once its last run finished. A failed run is logged once and replaced:
    re-raising it on every tick would keep the scheduler from ever reaching the claim."""
    if future is not None and not future.done():
        return future
    if future is not None and (error := future.exception()) is not None:
        log.error("Background job %s failed", job.__name__, exc_info=error)
    return pool.submit(job)


class SystemErrorHandler(RotatingFileHandler):
    """Bounded JSONL journal of failures of scheduler background jobs."""

    def __init__(self):
        path = ROOT / "system" / "errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        self.setLevel(logging.ERROR)
        self.addFilter(
            lambda record: (
                record.funcName == "periodic"
                or record.getMessage()
                in {
                    "Scheduler tick failed",
                    "Project schedules failed",
                }
            )
        )

    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
        return json.dumps({"at": record.created, "message": message}, ensure_ascii=False)


def test_stands():
    """Stand tests and stand builds share one browser slot: a build waits for a running test and back."""
    return standqa.run() or standbuild.run()


def heartbeat():
    """The host's self-update script reads this to tell a live scheduler from a crashed one."""
    try:
        (ROOT / "system").mkdir(parents=True, exist_ok=True)
        (ROOT / "system" / "worker-heartbeat").write_text(str(int(time.time())))
    except OSError:
        pass


def drain_seconds():
    return max(0, int(os.getenv("DARKFACTORY_DRAIN_SECONDS", "900")))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request URL, and Telegram bot URLs carry the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.addHandler(SystemErrorHandler())
    initialize()
    if upgraded := agents.upgrade_projects():
        log.info("Upgraded default skills and profiles of %d projects", len(upgraded))
    if seeded := schedules.seed_all():
        log.info("Turned project timers into %d schedules", seeded)
    # On SIGTERM stop taking work and let running steps finish; whatever outlives the grace
    # period is recovered by the next worker from the saved workspace and handoff.
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    worker = Worker()
    concurrency = max(1, int(os.getenv("GLOBAL_CONCURRENCY", "2")))
    futures = set()
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as notifications,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as logins,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as directors,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as supervisors,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as backlogs,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as telegrams,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as sweeps,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as syncs,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as testers,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as releases,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as promoting,
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as reporting,
    ):
        telegram = None
        grouping = None
        supervision = None
        delivery = None
        login = None
        dialogue = None
        sweeping = None
        syncing = None
        testing = None
        releasing = None
        promotion = None
        report = None
        while not stopping.is_set():
            try:
                futures = {f for f in futures if not f.done()}
                worker.recover()
                # Housekeeping failures must never keep the tick from claiming work.
                try:
                    worker.schedule_health()
                except Exception:
                    log.exception("Scheduling preprod health failed")
                try:
                    schedules.tick()
                except Exception:
                    log.exception("Project schedules failed")
                # Disk sweeps (rmtree, Docker prune) and main sync (fetch, push) take minutes at times;
                # in the tick they stalled every claim and recovery meanwhile.
                sweeping = periodic(sweeping, sweeps, cleanup.sweep)
                syncing = periodic(syncing, syncs, mainsync.sync)
                dialogue = periodic(dialogue, directors, advance_dialogues)
                supervision = periodic(supervision, supervisors, handle_stop)
                if backlog.enabled():
                    grouping = periodic(grouping, backlogs, backlog.plan)
                # A stand run takes up to half an hour; deploys only poll GitLab.
                if standqa.enabled():
                    testing = periodic(testing, testers, test_stands)
                releasing = periodic(releasing, releases, deploys.advance)
                promotion = periodic(promotion, promoting, promotions.auto_promote)
                report = periodic(report, reporting, selfreport.run)
                heartbeat()
                login = periodic(login, logins, advance_logins)
                telegram = periodic(telegram, telegrams, listen_telegram)
                delivery = periodic(delivery, notifications, exchange)
                if len(futures) < concurrency and not disk.check().get("paused"):
                    with transaction() as db:
                        claimed = service.claim(db, concurrency)
                    if claimed:
                        futures.add(pool.submit(worker.run, *claimed, concurrency))
            except Exception:
                log.exception("Scheduler tick failed")
            stopping.wait(1)
        running = {f for f in futures if not f.done()}
        log.info("Stopping: waiting up to %ss for %d running steps", drain_seconds(), len(running))
        concurrent.futures.wait(running, timeout=drain_seconds())
        log.info("Stopped")
        logging.shutdown()
        os._exit(0)  # Unfinished steps keep their leases until they expire; recovery picks them up.


if __name__ == "__main__":
    main()
