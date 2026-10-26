"""Tasks: creation, board, detail, promotion, evidence, live run controls, links."""

import json
import os
import subprocess
import time
from pathlib import PurePosixPath
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from .. import (
    agents,
    attachments,
    director_stops,
    evidence,
    promotions,
    registry,
    service,
)
from ..db import (
    ROOT,
    Artifact,
    ArtifactVersion,
    Event,
    Link,
    Project,
    Session,
    Task,
    decrypt,
    encrypt,
    event,
    guard,
    transaction,
    uid,
)
from ..task_links import commit_url
from ..usage import run_usage, task_usage
from ..web import access, public
from .projects import valid_authority

router = APIRouter()

AttachmentInput, MAX_ATTACHMENTS = attachments.AttachmentInput, attachments.TASK_FILES


class TaskInput(BaseModel):
    artifact_version_ids: list[str] = Field(default_factory=list, max_length=registry.MAX_INPUTS)
    priority: Literal["critical", "high", "normal", "low"] = "normal"
    writer: str | None = Field(default=None, pattern="^(codex|claude|api)$")
    reviewer: str | None = Field(default=None, pattern="^(codex|claude|api)$")
    agent_profiles: dict[str, str] = Field(default_factory=dict)
    title: str = Field(min_length=1, max_length=300)
    request: str = Field(min_length=1, max_length=50000)
    explicit_api: bool = False
    # Small, fully described change: skip the planning run and write right away.
    quick: bool = False
    # Large work: a planner first, which may split it into subtasks. Default is one writer.
    plan: bool = False
    api_budget_usd: float = Field(default=0, ge=0, le=1000, allow_inf_nan=False)
    idempotency_key: str | None = Field(default=None, max_length=150)
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


def new_task(db, project, value, incident=False):
    config = json.loads(decrypt(project.secret) or "{}")
    if not set(value.agent_profiles) <= {"planner", "writer", "reviewer"}:
        raise HTTPException(400, "Неизвестная роль агента")
    try:
        profiles = [
            agents.resolve(config, role, value.agent_profiles) for role in ("plan", "implement", "review")
        ]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if any(profile and profile["provider"] == "api" for profile in profiles) and value.api_budget_usd <= 0:
        raise HTTPException(400, "API-профиль требует денежного бюджета задачи")
    if value.explicit_api and value.api_budget_usd <= 0:
        raise HTTPException(400, "Для API-задачи нужен денежный предел")
    if value.idempotency_key:
        existing = db.scalar(
            select(Task).where(Task.project_id == project.id, Task.idempotency_key == value.idempotency_key)
        )
        if existing:
            return existing
    ident = uid()
    if (value.writer == "api" or value.reviewer == "api") and value.api_budget_usd <= 0:
        raise HTTPException(400, "Назначение API роли требует денежного бюджета")
    task = Task(
        **value.model_dump(
            exclude={
                "writer",
                "reviewer",
                "agent_profiles",
                "artifact_version_ids",
                "attachments",
                "quick",
                "plan",
            }
        ),
        role_config={
            **({"profiles": value.agent_profiles} if value.agent_profiles else {}),
            **({"quick": True} if value.quick else {}),
            **({"plan": True} if value.plan and not value.quick else {}),
            **{
                key: provider
                for key, provider in {"writer": value.writer, "reviewer": value.reviewer}.items()
                if provider
            },
        },
        id=ident,
        org_id=project.org_id,
        project_id=project.id,
        branch=f"darkfactory/{ident}",
        incident=incident,
    )
    db.add(task)
    db.flush()
    event(db, task, "created", task.title)
    for version_id in value.artifact_version_ids:
        version = db.get(ArtifactVersion, version_id)
        if not version:
            raise HTTPException(404, "Версия артефакта не найдена")
        registry.bind(db, task, db.get(Artifact, version.artifact_id), version)
    if getattr(value, "attachments", None):
        stored = attachments.save(task.id, value.attachments)
        event(db, task, "attachments", "Приложены файлы: " + ", ".join(stored))
    return task


@router.post("/api/projects/{project_id}/tasks", status_code=201)
def create_task(project_id: str, value: TaskInput, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        return public(new_task(db, a.project(db, project_id), value))


@router.post("/api/projects/{project_id}/incidents", status_code=201)
def incident(project_id: str, value: TaskInput, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        project = a.project(db, project_id)
        if project.incident_id:
            return public(db.get(Task, project.incident_id))
        task = new_task(db, project, value, incident=True)
        project.incident_id = task.id
        return public(task)


# Finished work older than this leaves the board unless the caller asks for everything.
FINISHED_STATES, FINISHED_DAYS = ("merged", "cancelled", "duplicate", "checked"), 7
PREVIEW_CHARS, EVENT_PAGE = 300, 50


WAITING_STATES = {
    "waiting_dependency": "зависимость",
    "waiting_pool": "свободную подписку",
    "waiting_integration": "объединение с preprod",
}


def held_files(db, task_ids):
    """For queued tasks held by another task's files: the files named in their latest deferral."""
    if not task_ids:
        return {}
    files = {}
    for row in db.scalars(
        select(Event).where(Event.task_id.in_(task_ids), Event.kind == "deferred").order_by(Event.id.desc())
    ):
        if row.task_id not in files:
            names = (row.message or "").partition(" — ")[2].split(", ")
            files[row.task_id] = ", ".join(PurePosixPath(name).name for name in names[:2] if name)
    return files


def waiting_for(task, files=None):
    """What a waiting task waits for, in a few words for its card."""
    if task.state in WAITING_STATES:
        return WAITING_STATES[task.state]
    if task.state == "queued" and (task.role_config or {}).get("wait_files_until", 0) > time.time():
        return "свободные файлы" + (f" ({files})" if files else "")
    return None


def board_row(task, parent_id, pending, awaiting=(), files=None):
    """The board card: everything but the full request, which only the task page needs."""
    row = public(task)
    request = row.pop("request") or ""
    row["request"] = request[:PREVIEW_CHARS]
    row["request_truncated"] = len(request) > PREVIEW_CHARS
    return {
        **row,
        "parent_id": parent_id,
        "promotion_pending": task.id in pending,
        "awaits_director": task.id in awaiting,
        "waiting_for": waiting_for(task, (files or {}).get(task.id)),
    }


# Event kinds the Director writes when it resolves or escalates a stop.
DIRECTOR_KINDS = ("supervisor", "supervisor_escalated", "folded")


@router.get("/api/projects/{project_id}/director-journal")
def director_journal(project_id: str, limit: int = Query(default=30, ge=1, le=200), a=Depends(access)):
    """The Director's decisions on stopped tasks, newest first; `open` marks the ones waiting for the owner."""
    with Session() as db:
        project = a.project(db, project_id)
        rows = db.execute(
            select(Event, Task)
            .join(Task, Task.id == Event.task_id)
            .where(Task.project_id == project.id, Event.kind.in_(DIRECTOR_KINDS))
            .order_by(Event.id.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": row.id,
                "at": row.at,
                "kind": row.kind,
                "decision": (row.data or {}).get("decision"),
                "message": row.message,
                "task_id": task.id,
                "task_title": task.title,
                "task_state": task.state,
                "open": row.kind == "supervisor_escalated" and task.state == "needs_human",
            }
            for row, task in rows
        ]


@router.get("/api/projects/{project_id}/tasks")
def tasks(
    project_id: str,
    response: Response,
    limit: int = Query(default=500, ge=1, le=1000),
    before: float | None = None,
    include_all: bool = Query(default=False, alias="all"),
    a=Depends(access),
):
    """Newest first. `before` is the `created` of the last row seen; X-Next-Before carries the next one."""
    query = select(Task).where(Task.project_id == project_id, Task.system_check.is_(False))
    if not include_all:
        query = query.where(
            or_(Task.state.not_in(FINISHED_STATES), Task.updated >= time.time() - FINISHED_DAYS * 86400)
        )
    if before is not None:
        query = query.where(Task.created < before)
    with Session() as db:
        a.project(db, project_id)
        rows = list(db.scalars(query.order_by(Task.created.desc(), Task.id.desc()).limit(limit + 1)))
        if len(rows) > limit:
            rows = rows[:limit]
            response.headers["X-Next-Before"] = repr(rows[-1].created)
        parents = {
            link.other_id: link.task_id
            for link in db.scalars(
                select(Link).where(Link.kind == "parent", Link.other_id.in_([t.id for t in rows]))
            )
        }
        pending = promotions.pending(db, [t.id for t in rows if t.state == "merged"])
        awaiting = {t.id for t in rows if t.state == "needs_human" and director_stops.awaits_director(db, t)}
        files = held_files(
            db, [t.id for t in rows if t.state == "queued" and (t.role_config or {}).get("wait_files_until")]
        )
    return [board_row(t, parents.get(t.id), pending, awaiting, files) for t in rows]


def relations(db, task_id):
    """Links in both directions with the other task's title and state, for the task page."""
    links = list(db.scalars(select(Link).where(or_(Link.task_id == task_id, Link.other_id == task_id))))
    others = {link.other_id if link.task_id == task_id else link.task_id for link in links}
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(others)))} if others else {}
    result = []
    for link in links:
        outgoing = link.task_id == task_id
        other = tasks.get(link.other_id if outgoing else link.task_id)
        if other:
            result.append(
                {
                    "id": link.id,
                    "kind": link.kind,
                    "outgoing": outgoing,
                    "owner_id": link.task_id,
                    "task_id": other.id,
                    "title": other.title,
                    "state": other.state,
                    "phase": other.phase,
                    "created": other.created,
                }
            )
    result.sort(key=lambda item: item["created"])
    return result


def event_page(db, task_id, before=None, limit=EVENT_PAGE):
    """Oldest-first page of the newest `limit` events, optionally before an event id."""
    query = select(Event).where(Event.task_id == task_id)
    if before is not None:
        query = query.where(Event.id < before)
    rows = list(db.scalars(query.order_by(Event.id.desc()).limit(limit + 1)))
    more = len(rows) > limit
    return [public(e) for e in reversed(rows[:limit])], more


@router.get("/api/tasks/{task_id}")
def task_detail(task_id: str, a=Depends(access)):
    with Session() as db:
        task = a.task(db, task_id)
        events, more = event_page(db, task_id)
        return {
            **public(task),
            "commit_url": commit_url(db.get(Project, task.project_id), task),
            "promotion": promotions.proposal(db, task),
            "awaits_director": director_stops.awaits_director(db, task),
            "waiting_for": waiting_for(task, held_files(db, [task.id]).get(task.id)),
            "usage": task_usage(task_id),
            "attachments": attachments.listing(task_id),
            "events": events,
            "events_more": more,
            "event_count": db.scalar(select(func.count()).select_from(Event).where(Event.task_id == task_id)),
            "links": [public(link) for link in db.scalars(select(Link).where(Link.task_id == task_id))],
            "relations": relations(db, task_id),
        }


@router.get("/api/tasks/{task_id}/events")
def task_events(
    task_id: str, before: int | None = None, limit: int = Query(default=200, ge=1, le=1000), a=Depends(access)
):
    """Older history for the task page: events before an id, oldest first, with `more` for the next page."""
    with Session() as db:
        a.task(db, task_id)
        events, more = event_page(db, task_id, before, limit)
        return {"events": events, "more": more}


@router.get("/api/tasks/{task_id}/promotion")
def promotion_preview(task_id: str, a=Depends(access)):
    from ..gitops import GitError, Repository

    with Session() as db:
        task = a.task(db, task_id)
        value = promotions.proposal(db, task)
        if not value:
            raise HTTPException(404, "Предложение слияния не найдено")
        if value["state"] != "pending":
            return value
        project = promotions.validate(db, task, value)
        try:
            return {
                **value,
                **Repository(project, task).main_preview(value["source_sha"]),
                "tasks": promotions.included(db, task, value["id"]),
            }
        except GitError as exc:
            raise HTTPException(409, str(exc)) from None


class PromotionInput(BaseModel):
    action: Literal["merge", "reject"]
    proposal_id: int = Field(ge=1)
    source_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    main_sha: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@router.post("/api/tasks/{task_id}/promotion")
def promotion_decision(task_id: str, value: PromotionInput, a=Depends(access)):
    from ..execution import controller_lock
    from ..gitops import GitError, Repository

    a.require("owner")
    with Session() as db:
        a.task(db, task_id)
    try:
        with controller_lock(task_id), transaction() as db:
            guard(db)
            task = a.task(db, task_id)
            current = promotions.proposal(db, task)
            if not current:
                raise HTTPException(404, "Предложение слияния не найдено")
            if current["id"] != value.proposal_id or current["source_sha"] != value.source_sha:
                raise HTTPException(409, "Предложение изменилось; открой актуальную версию")
            wanted = "merged" if value.action == "merge" else "rejected"
            if current["state"] == wanted:
                return current
            if current["state"] != "pending":
                raise HTTPException(409, "По этому предложению уже принято решение")
            if value.action == "merge":
                if not value.main_sha:
                    raise HTTPException(400, "Сначала открой сравнение с текущей main")
                project = promotions.validate(db, task, current)
                Repository(project, task).promote_main(value.source_sha, value.main_sha)
            event(
                db,
                task,
                "promotion_" + wanted,
                "Владелец влил принятый коммит в main"
                if wanted == "merged"
                else "Владелец отклонил перенос в main; preprod сохранена",
                proposal_id=current["id"],
                commit=value.source_sha,
                main_sha=value.main_sha,
                actor=a.principal.id,
            )
            db.flush()
            return promotions.proposal(db, task)
    except BlockingIOError:
        raise HTTPException(409, "Другая операция задачи ещё выполняется") from None
    except GitError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/api/tasks/{task_id}/artifacts/{name}")
def artifact(task_id: str, name: str, a=Depends(access)):
    with Session() as db:
        a.task(db, task_id)
    if name not in {
        "diff.patch",
        "review.txt",
        "review.json",
        "checks.txt",
        "handoff.md",
        "plan.md",
        "repository-index.md",
    }:
        raise HTTPException(404)
    path = ROOT / "artifacts" / task_id / name
    if not path.is_file():
        raise HTTPException(404, "Артефакт ещё не создан")
    return FileResponse(path, media_type="text/plain", filename=name)


@router.get("/api/tasks/{task_id}/evidence")
def browser_evidence(task_id: str, a=Depends(access)):
    with Session() as db:
        a.task(db, task_id)
    return evidence.listing(ROOT / "artifacts" / task_id / "browser")


@router.get("/api/tasks/{task_id}/evidence/{name:path}")
def browser_evidence_file(task_id: str, name: str, a=Depends(access)):
    from urllib.parse import quote

    with Session() as db:
        a.task(db, task_id)
    try:
        data, media = evidence.read(ROOT / "artifacts" / task_id / "browser", name)
    except (OSError, ValueError):
        raise HTTPException(404, "Файл проверки недоступен")
    # Reports may contain executable HTML. Always download; UI previews only raster images.
    return Response(
        data,
        media_type=media,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name.split("/")[-1], safe=""),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


DIFF_LIMIT = 200_000


def workspace_git(work, *args):
    proc = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(work), *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
    )
    # `diff --no-index` exits 1 when the files differ.
    if proc.returncode not in (0, 1):
        raise HTTPException(409, "Не удалось прочитать изменения: " + proc.stderr.strip()[-300:])
    return proc.stdout


@router.get("/api/tasks/{task_id}/changes")
def task_changes(task_id: str, path: str | None = None, a=Depends(access)):
    """What the agent changed so far: the live workspace against the base the task started from."""
    with Session() as db:
        target = a.task(db, task_id).target_sha
    work = ROOT / "workspaces" / task_id
    if not target or not (work / ".git").exists():
        raise HTTPException(404, "Рабочей папки задачи нет: она ещё не создана или уже очищена")
    if path is None:
        files = [
            {"status": line[:1], "path": line.split("\t")[-1]}
            for line in workspace_git(work, "diff", "--name-status", "--no-renames", target).splitlines()
            if "\t" in line
        ]
        files += [
            {"status": "A", "path": name}
            for name in workspace_git(work, "ls-files", "--others", "--exclude-standard").splitlines()
        ]
        return {"files": files[:2000]}
    path = path.removeprefix("/workspace/").removeprefix("./")
    parts = PurePosixPath(path).parts
    if not path or path.startswith("/") or ".." in parts or ".git" in parts:
        raise HTTPException(400, "Недопустимый путь")
    diff = workspace_git(work, "diff", target, "--", ":(literal)" + path)
    candidate = work / path
    # An untracked file is read directly: it must not lead outside the workspace through a symlink.
    inside = candidate.resolve().is_relative_to(work.resolve())
    if not diff and inside and candidate.is_file() and not candidate.is_symlink():
        if not workspace_git(work, "ls-files", "--", ":(literal)" + path):
            diff = workspace_git(work, "diff", "--no-index", "--", "/dev/null", path)
    return {"path": path, "diff": diff[:DIFF_LIMIT], "truncated": len(diff) > DIFF_LIMIT}


TRANSCRIPT_ENTRIES, TRANSCRIPT_BYTES, RUNS_TTL = 500, 1024 * 1024, 5.0
_runs_cache: dict[str, tuple[float, int, list]] = {}


def transcript_runs(task_id, root):
    """Executor runs of a task, oldest first; cached briefly because the transcript view polls."""
    try:
        version = os.stat(root, follow_symlinks=False).st_mtime_ns
    except OSError:
        return []
    cached = _runs_cache.get(task_id)
    if cached and cached[1] == version and time.monotonic() - cached[0] < RUNS_TTL:
        return cached[2]
    runs = []
    try:
        directories = [entry.name for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        directories = []
    for name in directories:
        try:
            data, _ = evidence.read(root, name + "/execution.json")
            meta = json.loads(data)
            at = os.stat(root / name / "execution.json", follow_symlinks=False).st_mtime
        except (OSError, ValueError):
            continue
        runs.append(
            {"generation": name, "role": str(meta.get("role", "")), "at": at, "usage": run_usage(root, name)}
        )
    runs.sort(key=lambda run: run["at"])
    if len(_runs_cache) > 256:
        _runs_cache.clear()
    _runs_cache[task_id] = (time.monotonic(), version, runs)
    return runs


@router.get("/api/tasks/{task_id}/transcript")
def agent_transcript(task_id: str, generation: str | None = None, after: int = 0, a=Depends(access)):
    """Transcript entries from byte offset `after`; `next` is the offset to pass back, `more` asks to."""
    with Session() as db:
        state = a.task(db, task_id).state
    root = ROOT / "artifacts" / task_id / "browser"
    try:
        current = (ROOT / "locks" / task_id / "generation").read_text().strip()
    except OSError:
        current = None
    runs = transcript_runs(task_id, root)
    if generation is None:
        generation = runs[-1]["generation"] if runs else None
    elif generation not in {run["generation"] for run in runs}:
        raise HTTPException(404, "Запуск не найден")
    after, entries, more = max(0, after), [], False
    position = after
    if generation:
        try:
            data, position, more = evidence.read_lines(
                root, generation + "/transcript.jsonl", after, TRANSCRIPT_BYTES
            )
        except (OSError, ValueError):
            data = b""
        offset = after
        for line in data.splitlines(keepends=True):
            offset += len(line)
            try:
                entry = json.loads(line)
                entries.append(
                    {"at": float(entry["at"]), "kind": str(entry["kind"]), "text": str(entry["text"])}
                )
            except (ValueError, KeyError, TypeError):
                continue
            if len(entries) == TRANSCRIPT_ENTRIES:
                position, more = offset, True
                break
    return {
        "generation": generation,
        "live": state == "running" and generation is not None and generation == current,
        "runs": runs[-30:],
        "entries": entries,
        "next": position,
        "more": more,
    }


class EgressInput(BaseModel):
    hosts: list[str] = Field(min_length=1, max_length=20)


@router.post("/api/tasks/{task_id}/allow-egress")
def allow_egress(task_id: str, value: EgressInput, a=Depends(access)):
    """Add hosts a task was refused to the project allowlist; affects only containers started later."""
    a.require("owner")
    for authority in value.hosts:
        valid_authority(authority)
    with transaction() as db:
        task = a.task(db, task_id)
        project = db.get(Project, task.project_id)
        config = json.loads(decrypt(project.secret) or "{}")
        hosts = list(config.get("allowed_hosts", []))
        added = [item for item in dict.fromkeys(value.hosts) if item not in hosts]
        if len(hosts) + len(added) > 50:
            raise HTTPException(400, "Не больше 50 разрешённых адресов")
        config["allowed_hosts"] = hosts + added
        project.secret = encrypt(json.dumps(config))
        event(db, task, "egress_allowed", "Разрешены адреса: " + ", ".join(added or value.hosts), hosts=added)
        return {"allowed_hosts": config["allowed_hosts"]}


class AgentHint(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/api/tasks/{task_id}/hint")
def agent_hint(task_id: str, value: AgentHint, a=Depends(access)):
    """Queue an operator note for the live run; the executor reads it over its read-only lock mount."""
    a.require("operator")
    with transaction() as db:
        task = a.task(db, task_id)
        lockdir = ROOT / "locks" / task_id
        try:
            generation = (lockdir / "generation").read_text().strip()
        except OSError:
            generation = None
        if task.state != "running" or not generation:
            raise HTTPException(409, "Агент сейчас не работает; используйте «Продолжить»")
        line = json.dumps({"at": time.time(), "text": value.text}, ensure_ascii=False) + "\n"
        fd = os.open(
            lockdir / f"hints-{generation}.jsonl",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o644,
        )
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
        event(db, task, "agent_hint", value.text, generation=generation)
        return {"queued": True, "generation": generation}


class TaskPriorityInput(BaseModel):
    priority: Literal["critical", "high", "normal", "low"]


@router.put("/api/tasks/{task_id}/priority")
def update_task_priority(task_id: str, value: TaskPriorityInput, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        task = a.task(db, task_id)
        if task.priority != value.priority:
            previous = task.priority
            task.priority = value.priority
            event(
                db,
                task,
                "priority_changed",
                f"Приоритет: {previous} → {value.priority}",
                previous=previous,
                priority=value.priority,
            )
        return public(task)


class ActionInput(BaseModel):
    action: str = Field(pattern="^(cancel|resume|merge)$")
    extend_seconds: int = Field(default=0, ge=0, le=86400)
    extra_corrections: int = Field(default=0, ge=0, le=20)
    message: str = Field(default="", max_length=20000)
    extra_api_budget_usd: float = Field(default=0, ge=0, le=1000, allow_inf_nan=False)
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


@router.post("/api/tasks/{task_id}/actions")
def action(task_id: str, value: ActionInput, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        task = a.task(db, task_id)
        if task.state in {"merged", "duplicate"}:
            raise HTTPException(409, "Завершённую задачу нельзя перезапустить")
        if value.action == "merge":
            project = db.get(Project, task.project_id)
            if task.state != "review_required" or task.reviewed_sha != task.candidate_sha:
                raise HTTPException(409, "Слить можно только задачу, прошедшую ревью и ждущую человека")
            if project.target != "preprod":
                raise HTTPException(409, "Фабрика сливает только в preprod; слейте ветку вручную")
            task.role_config = {**(task.role_config or {}), "merge_approved": a.principal.id}
            task.state, task.reason, task.phase = "queued", None, "merge"
        elif value.action == "cancel":
            task.cancel_requested = True
            service.cancel_delegated(db, task)
            if task.state not in {"running", "recovering"}:
                task.state, task.reason = "cancelled", "Отменена пользователем"
                project = db.get(Project, task.project_id)
                if project.integration_task == task.id:
                    project.integration_task = None
        else:
            if task.state in {"running", "recovering"}:
                raise HTTPException(409, "Задача ещё выполняется")
            if value.attachments:
                stored = attachments.save(task_id, value.attachments)
                event(db, task, "attachments", "Приложены файлы: " + ", ".join(stored))
            service.resume(
                db,
                task,
                message=value.message,
                extend_seconds=value.extend_seconds,
                extra_corrections=value.extra_corrections,
                extra_api_budget_usd=value.extra_api_budget_usd,
                rework=bool(value.attachments),
            )
        event(db, task, value.action, value.message or value.action)
        return public(task)


class AttachmentsInput(BaseModel):
    files: list[AttachmentInput] = Field(min_length=1, max_length=MAX_ATTACHMENTS)


@router.post("/api/tasks/{task_id}/attachments", status_code=201)
def add_attachments(task_id: str, value: AttachmentsInput, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        task = a.task(db, task_id)
        stored = attachments.save(task_id, value.files)
        event(db, task, "attachments", "Приложены файлы: " + ", ".join(stored))
        return {"files": attachments.listing(task_id)}


@router.get("/api/tasks/{task_id}/attachments/{name}")
def attachment(task_id: str, name: str, a=Depends(access)):
    with Session() as db:
        a.task(db, task_id)
    if name not in {item["name"] for item in attachments.listing(task_id)}:
        raise HTTPException(404, "Файл не найден")
    data = (attachments.directory(task_id) / name).read_bytes()
    kind = attachments.image_type(data)
    return Response(
        data,
        media_type=kind or "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            **({} if kind else {"Content-Disposition": f'attachment; filename="{name}"'}),
        },
    )


class LinkInput(BaseModel):
    other_id: str
    kind: str
    artifact: bool = False


@router.post("/api/tasks/{task_id}/links", status_code=201)
def links(task_id: str, value: LinkInput, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        service.add_link(db, a, task_id, value.other_id, value.kind, value.artifact)
    return {"ok": True}


@router.delete("/api/tasks/{task_id}/links/{link_id}")
def remove_link(task_id: str, link_id: str, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        task = a.task(db, task_id)
        link = db.get(Link, link_id)
        if not link or link.task_id != task.id:
            raise HTTPException(404)
        if task.state not in service.READY | {"needs_human"}:
            raise HTTPException(409, "Сначала останови задачу")
        if link.kind == "parent":
            members = service.execution_limits(db, task)["members"]
            if any(member.state in {"running", "recovering"} for member in members):
                raise HTTPException(409, "Дождись остановки дерева задач перед изменением общего бюджета")
        db.delete(link)
        task.verified_sha = task.reviewed_sha = None
        if task.phase not in {"prepare", "implement"}:
            task.phase = "integrate"
        event(db, task, "relationship_removed", f"{link.kind}: {link.other_id}")
    return {"ok": True}
