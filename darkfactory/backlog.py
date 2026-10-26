"""Backlog: loose owner notes that an agent groups by feature or area into ordinary tasks."""

import json
import logging
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Float, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from . import service
from .db import ROOT, Base, Project, Task, decrypt, event, now, transaction, uid

log = logging.getLogger(__name__)

MAX_ITEMS = 60
MAX_GROUPS = 12
INTERVAL_HOURS = 4
KEEP_SECONDS = 86400


class BacklogItem(Base):
    __tablename__ = "backlog_items"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(20), default="open")
    task_id: Mapped[str | None] = mapped_column(String(32))
    created: Mapped[float] = mapped_column(Float, default=now)
    planned: Mapped[float | None] = mapped_column(Float)


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "maxItems": MAX_GROUPS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "request", "items"],
                "properties": {
                    "title": {"type": "string"},
                    "request": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
            },
        }
    },
}


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    request: str = Field(min_length=1, max_length=20000)
    items: list[str] = Field(min_length=1, max_length=MAX_ITEMS)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: list[Group] = Field(max_length=MAX_GROUPS)


PROMPT = """You turn an owner's backlog into tasks for an autonomous software factory.
The current preprod code is mounted read-only in /workspace: look where each item lives before grouping.
Group items that belong together: the same feature, screen or module, or bugs close to each other in the
code, so one developer fixes them in one change. Unrelated items become separate groups. Every item id
must appear in exactly one group. Do not invent work beyond the items.
For each group write a Russian `title` (short, imperative) and a Russian `request`: the goal, every item
restated concretely (keep the owner's details), and verifiable acceptance criteria.
Respond with JSON only.

"""


def interval_hours(project):
    config = json.loads(decrypt(project.secret) or "{}")
    value = config.get("backlog_hours", INTERVAL_HOURS)
    return max(0, min(int(value), 168))


def state(project):
    return dict((project.health or {}).get("backlog") or {})


def request(db, project):
    """Ask the worker to plan now; the click and the timer share one path."""
    project.health = {**(project.health or {}), "backlog": {**state(project), "requested": now()}}


def due(db, project):
    if not db.scalar(
        select(BacklogItem.id).where(BacklogItem.project_id == project.id, BacklogItem.state == "open")
    ):
        return False
    current = state(project)
    if current.get("requested"):
        return True
    hours = interval_hours(project)
    # The timer counts from the oldest unplanned note, so a fresh backlog is not planned on its first minute.
    oldest = db.scalar(
        select(BacklogItem.created)
        .where(BacklogItem.project_id == project.id, BacklogItem.state == "open")
        .order_by(BacklogItem.created)
    )
    return bool(hours) and now() - max(oldest, current.get("at", 0)) >= hours * 3600


def overview(project):
    """Architecture notes from preprod help the grouper see module boundaries."""
    mirror = ROOT / "repositories" / (project.id + ".git")
    for name in ("ARCHITECTURE.md", "INDEX.md"):
        try:
            text = subprocess.run(
                ["git", "--git-dir", str(mirror), "show", f"{project.target}:docs/darkfactory/{name}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if text.strip():
            return f"\n{name}:\n{text[:12000]}\n"
    return ""


def checkout(project, ident):
    """Fresh read-only snapshot of preprod so the grouper can look at the code it groups by."""
    workspace = ROOT / "workspaces" / ident
    shutil.rmtree(workspace, ignore_errors=True)
    mirror = ROOT / "repositories" / (project.id + ".git")
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--depth",
                "1",
                "-b",
                project.target,
                "file://" + str(mirror),
                str(workspace),
            ],
            capture_output=True,
            timeout=300,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(workspace, ignore_errors=True)
    (workspace / ".git").mkdir(parents=True, exist_ok=True)


def prompt(project, items):
    lines = [f"- {item.id}: {item.text}" for item in items]
    return PROMPT + f"Project: {project.name}\n{overview(project)}\nBacklog items:\n" + "\n".join(lines)


def create(db, project, plan, items):
    by_id = {item.id: item for item in items}
    seen = set()
    created = []
    for group in plan.groups:
        members = [by_id[key] for key in dict.fromkeys(group.items) if key in by_id and key not in seen]
        if not members:
            continue
        seen.update(item.id for item in members)
        ident = uid()
        task = Task(
            id=ident,
            org_id=project.org_id,
            project_id=project.id,
            title=group.title,
            request=group.request,
            branch=f"darkfactory/{ident}",
            # Owner tasks take free slots first; backlog work fills the gaps.
            priority="low",
            # Its own [LATER] notes do not return to the backlog: that loop would never end.
            role_config={"backlog": True},
        )
        db.add(task)
        db.flush()
        event(db, task, "created", task.title)
        event(db, task, "backlog", f"Из бэклога: {len(members)} заметок")
        for item in members:
            item.state, item.task_id, item.planned = "planned", ident, now()
        created.append(ident)
    # Notes the agent left out of every group are the ones it judged not worth a task (fragments, file
    # paths, checklist echoes); as tasks of their own they were noise that cost a full run each.
    for item in items:
        if item.id not in seen:
            item.state, item.planned = "dismissed", now()
    return created


def finish(db, project, **fields):
    project.health = {
        **(project.health or {}),
        "backlog": {**{k: v for k, v in state(project).items() if k != "requested"}, **fields},
    }


def plan(executor=None):
    """Plan one project's backlog per call; True when something was attempted."""
    from .execution import DockerExecutor

    with transaction() as db:
        project = next((row for row in db.scalars(select(Project)) if due(db, row)), None)
        if project is None:
            return False
        items = list(
            db.scalars(
                select(BacklogItem)
                .where(BacklogItem.project_id == project.id, BacklogItem.state == "open")
                .order_by(BacklogItem.created)
                .limit(MAX_ITEMS)
            )
        )
        ids = [item.id for item in items]
        text = prompt(project, items)
        probe = SimpleNamespace(
            id="backlog-" + project.id,
            org_id=project.org_id,
            project_id=project.id,
            phase="director",  # Routes by the Director profile, on the writer pool.
            plan_depth=0,
            role_config={},
            explicit_api=False,
            last_connection=None,
            api_reserved_usd=0,
            title="Бэклог",
            request="",
            lease_owner=uid(),
            candidate_sha=None,
            target_sha=None,
            verified_sha=None,
        )
        connection = service.choose_connection(
            db, probe, project, {"api_budget": 0}, allow_quota_probe=True, shared=True
        )
        if connection is None:
            return False  # Retry on a later tick once a subscription is free.
        project_id = project.id
        db.expunge(project)
        db.expunge(connection)
    started = time.monotonic()
    try:
        checkout(project, probe.id)
        result = (executor or DockerExecutor()).run(
            probe,
            project,
            connection,
            "director",
            lambda: True,
            600,
            prompt_override=text,
            output_schema=SCHEMA,
        )
        if result.get("status") != "ok":
            raise ValueError(f"исполнитель вернул {result.get('status')}")
        decision = Plan.model_validate_json(result["text"])
    except Exception as exc:
        log.warning("Backlog planning failed for %s: %s", project_id, exc)
        with transaction() as db:
            finish(db, db.get(Project, project_id), at=now(), error=f"{type(exc).__name__}: {exc}"[:300])
        return True
    with transaction() as db:
        row = db.get(Project, project_id)
        # Notes deleted or planned meanwhile drop out; new notes wait for the next run.
        items = list(
            db.scalars(select(BacklogItem).where(BacklogItem.id.in_(ids), BacklogItem.state == "open"))
        )
        created = create(db, row, decision, items)
        finish(db, row, at=now(), error=None, tasks=len(created))
        log.info("Backlog %s → %d tasks in %.0fs", project_id, len(created), time.monotonic() - started)
    return True


def enabled():
    return os.getenv("DARKFACTORY_BACKLOG", "1") != "0"
