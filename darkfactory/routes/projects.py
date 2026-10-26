"""Organization, projects, environment and agent profiles."""

import json
import re
import shutil
import socket
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

from .. import (
    agents,
    backlog,
    environment,
    initialization,
    mainsync,
    service,
    toolchains,
)
from ..db import (
    ROOT,
    Artifact,
    ArtifactInput,
    ArtifactVersion,
    Connection,
    Event,
    Link,
    Organization,
    Pool,
    Principal,
    Project,
    Session,
    Task,
    decrypt,
    encrypt,
    event,
    transaction,
)
from ..egress import ip_literal, refused_address, single_label
from ..web import access, public

router = APIRouter()


class OrganizationInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    max_active: int = Field(default=2, ge=1, le=100)


@router.post("/api/organizations", status_code=201)
def create_organization(value: OrganizationInput, a=Depends(access)):
    a.organization_owner()
    with transaction() as db:
        organization = Organization(**value.model_dump())
        db.add(organization)
        db.flush()
        owner = Principal(name=a.principal.name, org_id=organization.id, role="owner")
        db.add(owner)
        db.flush()
        token = service.issue_token(db, owner, "owner", [])
        return {
            "organization": public(organization),
            "owner_id": owner.id,
            "token": token["token"],
            "token_id": token["id"],
        }


@router.put("/api/organization")
def update_organization(value: OrganizationInput, a=Depends(access)):
    a.organization_owner()
    with transaction() as db:
        organization = db.get(Organization, a.org_id)
        organization.name, organization.max_active = value.name, value.max_active
        return public(organization)


class ProjectInput(BaseModel):
    initialize: bool = True
    prepare_environment: bool = True
    name: str = Field(min_length=1, max_length=150)
    remote: str = Field(min_length=1, max_length=2000)
    seed_branch: str = Field(default="main", pattern=r"^[a-zA-Z0-9_/-]+$", max_length=150)
    checks: list[str] = Field(default_factory=list, max_length=20)
    writer_pool: str | None = None
    reviewer_pool: str | None = None
    writer: str = Field(default="codex", pattern="^(codex|claude|api)$")
    reviewer: str = Field(default="claude", pattern="^(codex|claude|api)$")
    max_seconds: int = Field(default=3600, ge=60, le=86400)
    max_corrections: int = Field(default=3, ge=0, le=20)
    forge: str = Field(default="git", pattern="^(git|github|gitlab)$")
    forge_url: str | None = None
    forge_repo: str | None = None
    git_token: str = ""
    forge_token: str = ""
    ssh_private_key: str = ""
    ssh_known_hosts: str = ""
    executor_image: str | None = Field(default=None, max_length=250)
    allowed_hosts: list[str] | None = Field(default=None, max_length=50)
    allow_paid_fallback: bool = False
    package_registries: bool = True
    merge_without_checks: bool = True
    director_autonomy: bool = True
    director_limit: int | None = Field(default=None, ge=1, le=20)
    review_muted: list[str] | None = Field(default=None, max_length=30)
    backlog_hours: int = Field(default=backlog.INTERVAL_HOURS, ge=0, le=168)
    max_tree_seconds: int = Field(default=43200, ge=600, le=7 * 86400)
    test_env: dict[str, str] | None = None


PROJECT_SECRETS = {
    "allow_paid_fallback",
    "package_registries",
    "merge_without_checks",
    "director_autonomy",
    "director_limit",
    "review_muted",
    "backlog_hours",
    "max_tree_seconds",
    "git_token",
    "forge_token",
    "ssh_private_key",
    "ssh_known_hosts",
    "executor_image",
    "allowed_hosts",
    "test_env",
}


def project_secrets(value, previous=None, *, initialize_agents=False):
    result = json.loads(decrypt(previous) or "{}")
    if initialize_agents:
        result["agents"] = agents.default_settings()
    for key in PROJECT_SECRETS & value.model_fields_set:
        item = getattr(value, key)
        if item is not None and (item != "" or key in {"executor_image"}):
            result[key] = item
    return encrypt(json.dumps(result))


def valid_authority(authority):
    try:
        endpoint = urlsplit("https://" + authority)
        if (
            not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.path
            or endpoint.query
            or endpoint.fragment
            or not endpoint.port
            or single_label(endpoint.hostname)
        ):
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "Разрешённые адреса задаются точно как host:port") from None


TEST_ENV_NAME = re.compile(r"^(TEST|APP|E2E)_[A-Z0-9_]+$")


def _endpoint(url):
    """(host, port) a Git remote or forge URL dials, including scp-like git@host:path."""
    if re.match(r"^[^/@:]+@[^/:]+:", url) and "://" not in url:
        return url.split("@", 1)[1].split(":", 1)[0], 22
    parsed = urlsplit(url)
    return parsed.hostname, parsed.port or {"https": 443, "http": 80, "ssh": 22}.get(parsed.scheme)


def check_destination(url, allowed_hosts, label):
    """Refuse loopback/private/link-local/CGNAT/reserved targets and single-label names.

    A private address passes only when its exact `ip:port` is an operator-listed allowed host.
    """
    try:
        host, port = _endpoint(url)
    except ValueError:
        host = port = None
    if not host or not port:
        raise HTTPException(400, label + ": нужен адрес сервера")
    host = host.strip("[]").rstrip(".").lower()
    if single_label(host):
        raise HTTPException(400, label + ": нужно полное имя сервера, а не однословное")
    listed = set()
    for authority in allowed_hosts or []:
        endpoint = urlsplit("https://" + authority)
        listed.add((endpoint.hostname, endpoint.port))
    if ip_literal(host):
        addresses = [host]
    else:
        try:
            addresses = [item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        except (OSError, UnicodeError):
            # The worker re-checks at connect time; an unresolvable name here is not a private one.
            addresses = []
    for address in addresses:
        if refused_address(address) and (address, port) not in listed:
            raise HTTPException(
                400,
                label
                + f": внутренний адрес {address}:{port}; добавь его как IP в разрешённые адреса проекта",
            )


def validate_project(db, a, value, previous=None):
    remote = value.remote
    allowed_hosts = (
        value.allowed_hosts if value.allowed_hosts is not None else (previous or {}).get("allowed_hosts")
    )
    if remote.startswith("/repos/"):
        resolved = Path(remote).resolve()
        if not resolved.is_relative_to(Path("/repos")):
            raise HTTPException(400, "Локальный репозиторий должен находиться внутри /repos")
        for other in db.scalars(select(Project).where(Project.org_id != a.org_id)):
            if other.remote.startswith("/repos/"):
                claimed = Path(other.remote).resolve()
                if resolved.is_relative_to(claimed) or claimed.is_relative_to(resolved):
                    raise HTTPException(403, "Локальный репозиторий недоступен этой организации")
    elif not remote.startswith(("https://", "http://", "ssh://", "git@")) or any(
        c in remote for c in "\n\r\0"
    ):
        raise HTTPException(400, "Нужен Git URL или разрешённый путь /repos/…")
    if remote.startswith(("https://", "http://")) and urlsplit(remote).username:
        raise HTTPException(400, "Передавай Git-токен отдельным полем")
    if not remote.startswith("/repos/"):
        check_destination(remote, allowed_hosts, "Git URL")
    if value.seed_branch.startswith("/") or ".." in value.seed_branch or "//" in value.seed_branch:
        raise HTTPException(400, "Недопустимая исходная ветка")
    for pool_id in (value.writer_pool, value.reviewer_pool):
        pool = db.get(Pool, pool_id) if pool_id else None
        if pool_id and (not pool or pool.org_id != a.org_id):
            raise HTTPException(400, "Пул другой организации или не существует")
    if value.forge != "git":
        parsed = urlsplit(value.forge_url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not value.forge_repo:
            raise HTTPException(400, "Укажи URL сервера и путь репозитория для PR/MR")
        check_destination(value.forge_url, allowed_hosts, "URL сервера PR/MR")
    if bool(value.ssh_private_key) != bool(value.ssh_known_hosts):
        raise HTTPException(400, "SSH-ключ и проверенные known_hosts нужны вместе")
    for authority in value.allowed_hosts or []:
        valid_authority(authority)
    # Only names a test suite would read: an agent must not reach PATH, LD_PRELOAD, proxies or credentials.
    for key in value.test_env or {}:
        if not TEST_ENV_NAME.fullmatch(key):
            raise HTTPException(400, "Тестовая переменная должна называться TEST_*, APP_* или E2E_*: " + key)


@router.get("/api/projects")
def projects(a=Depends(access)):
    with Session() as db:
        return [
            public(p) for p in db.scalars(select(Project).where(Project.org_id == a.org_id)) if a.permits(p)
        ]


@router.post("/api/projects", status_code=201)
def create_project(value: ProjectInput, a=Depends(access)):
    a.organization_owner()
    with transaction() as db:
        validate_project(db, a, value)
        values = value.model_dump(exclude=PROJECT_SECRETS | {"initialize", "prepare_environment"})
        if {"writer_pool", "reviewer_pool"} - value.model_fields_set:
            available = list(db.scalars(select(Pool).where(Pool.org_id == a.org_id).order_by(Pool.id)))
            pool = next((item for item in available if (item.policy or {}).get("default_for_projects")), None)
            pool = pool or next((item for item in available if item.name == "Общий пул"), None)
            pool = pool or (available[0] if available else None)
            if pool is None:
                pool = Pool(name="Общий пул", org_id=a.org_id)
                db.add(pool)
                db.flush()
            pool.policy = {**(pool.policy or {}), "default_for_projects": True}
            for key in ("writer_pool", "reviewer_pool"):
                if key not in value.model_fields_set:
                    values[key] = pool.id
        project = Project(
            **values,
            org_id=a.org_id,
            secret=project_secrets(value, initialize_agents=True),
        )
        db.add(project)
        db.flush()
        if value.initialize:
            initialization.queue(db, project, a.principal.id)
        if value.prepare_environment:
            environment.queue(db, project, a.principal.id)
        return public(project)


@router.put("/api/projects/{project_id}")
def update_project(project_id: str, value: ProjectInput, a=Depends(access)):
    a.require("owner")
    with transaction() as db:
        project = a.project(db, project_id)
        if db.scalar(
            select(Task).where(Task.project_id == project_id, Task.state.in_(["running", "recovering"]))
        ):
            raise HTTPException(409, "Останови активные задачи перед сменой настроек")
        validate_project(db, a, value, json.loads(decrypt(project.secret) or "{}"))
        existing = list(db.scalars(select(Task).where(Task.project_id == project_id)))
        if existing and (
            value.remote != project.remote
            or ("seed_branch" in value.model_fields_set and value.seed_branch != project.seed_branch)
        ):
            raise HTTPException(409, "Для другого репозитория или исходной ветки создай новый проект")
        runtime_changed = bool(
            {"executor_image", "allowed_hosts", "package_registries", "test_env"} & value.model_fields_set
        )
        review_changed = any(
            key in value.model_fields_set and getattr(value, key) != getattr(project, key)
            for key in ("reviewer", "reviewer_pool")
        )
        if (
            ("checks" in value.model_fields_set and value.checks != project.checks)
            or runtime_changed
            or review_changed
        ):
            for task in existing:
                if task.state not in {"merged", "duplicate", "cancelled", "checked"}:
                    task.verified_sha = task.reviewed_sha = None
                    if task.phase not in {"prepare", "implement"}:
                        task.phase = "integrate"
                    event(db, task, "checks_changed", "Обязательные проверки изменены; допуск сброшен")
            project.integration_task = None
        for key, item in value.model_dump(
            exclude=PROJECT_SECRETS | {"initialize", "prepare_environment"}, exclude_unset=True
        ).items():
            setattr(project, key, item)
        project.secret = project_secrets(value, project.secret)
        return public(project)


@router.delete("/api/projects/{project_id}")
def delete_project(project_id: str, a=Depends(access)):
    """Remove the project with its tasks, history, artifacts, backlog, chats and channels; the repository stays."""
    from ..backlog import BacklogItem
    from ..director import ChatThread, Dialogue
    from ..notifications import NotificationChannel, NotificationDelivery

    a.organization_owner()
    with transaction() as db:
        project = a.project(db, project_id)
        if db.scalar(
            select(Task).where(Task.project_id == project_id, Task.state.in_(["running", "recovering"]))
        ):
            raise HTTPException(409, "Останови активные задачи перед удалением проекта")
        tasks = select(Task.id).where(Task.project_id == project_id)
        task_ids = list(db.scalars(tasks))
        artifacts = select(Artifact.id).where(Artifact.project_id == project_id)
        channels = select(NotificationChannel.id).where(NotificationChannel.project_id == project_id)
        threads = select(ChatThread.id).where(ChatThread.project_id == project_id)
        events = select(Event.id).where(Event.task_id.in_(tasks))
        db.execute(
            delete(NotificationDelivery).where(
                NotificationDelivery.task_id.in_(tasks)
                | NotificationDelivery.event_id.in_(events)
                | NotificationDelivery.channel_id.in_(channels)
            )
        )
        db.execute(delete(NotificationChannel).where(NotificationChannel.id.in_(channels)))
        db.execute(
            delete(Dialogue).where((Dialogue.project_id == project_id) | Dialogue.thread_id.in_(threads))
        )
        db.execute(delete(ChatThread).where(ChatThread.project_id == project_id))
        db.execute(delete(BacklogItem).where(BacklogItem.project_id == project_id))
        db.execute(
            delete(ArtifactInput).where(
                ArtifactInput.task_id.in_(tasks) | ArtifactInput.artifact_id.in_(artifacts)
            )
        )
        # Another project's artifact keeps its version; it only forgets the task that wrote it.
        db.execute(
            update(ArtifactVersion)
            .where(ArtifactVersion.source_task_id.in_(tasks), ArtifactVersion.artifact_id.not_in(artifacts))
            .values(source_task_id=None)
        )
        db.execute(delete(ArtifactVersion).where(ArtifactVersion.artifact_id.in_(artifacts)))
        db.execute(delete(Artifact).where(Artifact.project_id == project_id))
        db.execute(delete(Link).where(Link.task_id.in_(tasks) | Link.other_id.in_(tasks)))
        db.execute(delete(Event).where(Event.task_id.in_(tasks)))
        db.execute(delete(Task).where(Task.project_id == project_id))
        db.delete(project)
    # Workspaces can hold gigabytes; the answer does not wait for them.
    paths = [ROOT / "caches" / project_id, toolchains.state_path(project_id)]
    for task_id in task_ids:
        paths += [ROOT / "workspaces" / task_id, ROOT / "sessions" / task_id, ROOT / "artifacts" / task_id]
    threading.Thread(target=_remove, args=(paths,), daemon=True).start()
    return {"deleted": project_id}


def _remove(paths):
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


@router.post("/api/projects/{project_id}/environment")
def prepare_environment(project_id: str, a=Depends(access)):
    a.require("owner")
    with transaction() as db:
        project = a.project(db, project_id)
        return public(environment.queue(db, project, a.principal.id))


@router.post("/api/projects/{project_id}/main-sync")
def sync_main(project_id: str, a=Depends(access)):
    """Pull main into preprod now instead of waiting for the hourly round; main itself never changes."""
    a.require("operator")
    with Session() as db:
        project = a.project(db, project_id)
        if project.target != "preprod":
            raise HTTPException(409, "Синхронизация main работает только для проекта с веткой preprod")
        if project.incident_id:
            raise HTTPException(409, "Сначала нужно исправить инцидент preprod")
        db.expunge(project)
    result = mainsync.sync_project(project)
    return {key: result.get(key) for key in ("state", "main", "preprod", "conflicts", "task_id", "error")}


@router.get("/api/projects/{project_id}/environment")
def environment_state(project_id: str, a=Depends(access)):
    with Session() as db:
        a.project(db, project_id)
    return toolchains.state(project_id)


@router.post("/api/projects/{project_id}/initialize")
def initialize_project(project_id: str, a=Depends(access)):
    a.require("owner")
    with transaction() as db:
        project = a.project(db, project_id)
        return public(initialization.queue(db, project, a.principal.id))


@router.get("/api/projects/{project_id}/agents")
def agent_settings(project_id: str, a=Depends(access)):
    with Session() as db:
        project = a.project(db, project_id)
        config = json.loads(decrypt(project.secret) or "{}")
        return agents.public_settings(config.get("agents"))


PREVIEW_STAGES = {"planner": "plan", "writer": "implement", "reviewer": "review", "director": "director"}


@router.get("/api/projects/{project_id}/agents/preview")
def agent_prompt_preview(
    project_id: str, stage: str = Query(pattern="^(planner|writer|reviewer|director)$"), a=Depends(access)
):
    """What an agent of this stage is told, in parts: the factory's fixed rules, then the role's own part.
    Task text, history and the diff come on top at run time."""
    from .. import director_stops
    from ..execution import PLAN_RULES, PLAN_SPLIT_RULES, REVIEW_RULES, writer_rules

    with Session() as db:
        project = a.project(db, project_id)
        config = json.loads(decrypt(project.secret) or "{}")
        guide = initialization.project_guide(project.id)
    role = PREVIEW_STAGES[stage]
    try:
        profile = agents.resolve(config, role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if profile is not None and guide and role != "director":
        profile = {**profile, "skill_definitions": [*profile["skill_definitions"], guide]}
    rules = {
        "plan": "Plan task before implementation. Inspect the read-only repository and acceptance criteria.\n"
        + PLAN_RULES
        + PLAN_SPLIT_RULES,
        "implement": writer_rules([], True),
        "review": "Independently review commit <кандидат> against <preprod>.\n" + REVIEW_RULES,
        "director": director_stops.PROMPT,
    }[role]
    return {
        "stage": stage,
        "profile": profile and profile["name"],
        "sections": [
            {"title": "Правила фабрики (в коде, одинаковы для всех проектов)", "text": rules.strip()},
            {"title": "Роль и скиллы (экран «Роли»)", "text": agents.skill_prompt(profile, role).strip()},
        ],
    }


@router.get("/api/projects/{project_id}/agent-catalog")
def agent_catalog(project_id: str, a=Depends(access)):
    from ..catalog import catalog

    with Session() as db:
        a.project(db, project_id)
        models = []
        for connection in db.scalars(select(Connection).where(Connection.org_id == a.org_id)):
            if connection.model and not any(
                m["provider"] == connection.provider and m["value"] == connection.model for m in models
            ):
                models.append(
                    {
                        "provider": connection.provider,
                        "value": connection.model,
                        "label": connection.provider + " · " + connection.model,
                    }
                )
        return {**catalog(), "models": models}


@router.put("/api/projects/{project_id}/agents")
def save_agent_settings(project_id: str, value: agents.AgentSettings, a=Depends(access)):
    a.require("owner")
    with transaction() as db:
        project = a.project(db, project_id)
        tasks = list(db.scalars(select(Task).where(Task.project_id == project_id)))
        if any(task.state in {"running", "recovering"} for task in tasks):
            raise HTTPException(409, "Останови активные задачи перед сменой ролей")
        config = json.loads(decrypt(project.secret) or "{}")
        config["agents"] = agents.merge_settings(value, config.get("agents"))
        project.secret = encrypt(json.dumps(config))
        for task in tasks:
            if task.state not in {"merged", "duplicate", "cancelled", "checked"}:
                task.verified_sha = task.reviewed_sha = None
                task.phase = "plan" if task.phase != "prepare" else "prepare"
                event(db, task, "agent_settings_changed", "Роли, скиллы или MCP изменены; допуск сброшен")
        project.integration_task = None
        return agents.public_settings(config["agents"])


# Notes the owner marked "not useful" steer the project's reviewer; the newest ones win.
MUTED_NOTES = 30


class MutedNote(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/api/projects/{project_id}/review-muted")
def mute_review_note(project_id: str, value: MutedNote, a=Depends(access)):
    a.require("operator")
    text = value.text.strip()
    with transaction() as db:
        project = a.project(db, project_id)
        config = json.loads(decrypt(project.secret) or "{}")
        muted = [note for note in config.get("review_muted", []) if note != text] + [text]
        config["review_muted"] = muted[-MUTED_NOTES:]
        project.secret = encrypt(json.dumps(config))
        return {"muted": len(config["review_muted"])}


# The factory page re-reads run files of every recent task; a minute-old answer is good enough.
METRICS_TTL = 60.0
_metrics_cache: dict[tuple, tuple[float, dict]] = {}
# One computation at a time: concurrent cache misses wait for it instead of reading every run file again.
_metrics_lock = threading.Lock()


@router.get("/api/projects/{project_id}/metrics")
def project_metrics(project_id: str, days: int = Query(default=7, ge=1, le=90), a=Depends(access)):
    """The factory page: throughput, review returns, human involvement, Director decisions, tokens."""
    import time

    from .. import metrics
    from ..db import Event, Task

    since = time.time() - days * 86400
    with Session() as db:
        project = a.project(db, project_id)
        cached = _metrics_cache.get((project.id, days))
        if cached and time.monotonic() - cached[0] < METRICS_TTL:
            return cached[1]
        decisions = {}
        for row in db.scalars(
            select(Event)
            .join(Task, Task.id == Event.task_id)
            .where(
                Task.project_id == project.id,
                Event.at >= since,
                Event.kind.in_(["supervisor", "supervisor_escalated", "folded"]),
            )
        ):
            key = {"supervisor_escalated": "escalate", "folded": "fold"}.get(row.kind) or (
                row.data or {}
            ).get("decision", "resume")
            decisions[key] = decisions.get(key, 0) + 1
    with _metrics_lock:
        cached = _metrics_cache.get((project.id, days))
        if cached and time.monotonic() - cached[0] < METRICS_TTL:
            return cached[1]
        report = metrics.report(metrics.load_tasks(1000, since, project.id, transcripts=False))
    merged, everything = report["merged"], report["all"]
    phases = sorted(
        ({"phase": phase, **row} for phase, row in everything["phases"].items() if phase != "monitor"),
        key=lambda row: -(row["sum"] or 0),
    )
    value = {
        "days": days,
        "tasks": everything["tasks"],
        "merged": merged["tasks"],
        "task_minutes_median": merged["task_minutes"]["median"],
        "corrections_per_task": merged["corrections_per_task"],
        "no_human_share": everything["no_human_share"],
        "input_per_task": merged["input_per_task"],
        "roles": {
            role: {"runs": row["runs"], "input": row["input"]["sum"], "median": row["input"]["median"]}
            for role, row in everything["roles"].items()
        },
        "profiles": everything["profiles"],
        "phases": phases[:8],
        "director": decisions,
    }
    if len(_metrics_cache) > 64:
        _metrics_cache.clear()
    _metrics_cache[(project.id, days)] = (time.monotonic(), value)
    return value
