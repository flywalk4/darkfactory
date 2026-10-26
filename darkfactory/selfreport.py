"""Turn recurring organization failures into scrubbed notes in the factory project's backlog."""

import hashlib
import json
import re
import threading

from sqlalchemy import select, update

from . import backlog, deploys, sanitize, standqa
from .db import ROOT, Event, Project, Task, decrypt, now, transaction, uid
from .leaks import owner_patterns

KINDS = {
    "supervisor_escalated",
    "review_repeated",
    "environment_blocked",
    "network_blocked",
    "stall_restart",
    "execution_error",
}
CLOSED = {"merged", "cancelled", "duplicate", "checked"}
_lock = threading.Lock()


def config(project):
    return json.loads(decrypt(project.secret) or "{}")


def state(project):
    return dict((project.health or {}).get("self_report") or {})


def request(db, project):
    # A caller may have loaded the row before a collector committed its cursor. Acquire the
    # write lock and reload before merging the request, including on SQLite.
    db.execute(update(Project).where(Project.id == project.id).values(health=Project.health))
    db.refresh(project)
    if not config(project).get("factory_self", False):
        return False
    project.health = {**(project.health or {}), "self_report": {**state(project), "requested": now()}}
    return True


def _json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _sources(db, project, since, until, snapshots):
    projects = list(db.scalars(select(Project).where(Project.org_id == project.org_id)))
    events = db.scalars(
        select(Event)
        .join(Task, Task.id == Event.task_id)
        .where(
            Task.project_id.in_([p.id for p in projects]),
            Event.kind.in_(KINDS),
            Event.at > since,
            Event.at <= until,
        )
        .order_by(Event.id)
    )
    sources = [(row.kind, row.message) for row in events]

    def snapshot(key, kind, value, message, stamp):
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        if stamp and not since < stamp <= until:
            return
        if snapshots.get(key) != digest:
            sources.append((kind, str(message)))
            snapshots[key] = digest

    for row in projects:
        for kind, status in (("stand_qa", standqa.state(row)), ("deploy", deploys.state(row))):
            if status.get("error"):
                snapshot(
                    f"{row.id}:{kind}",
                    kind,
                    status,
                    status["error"],
                    status.get("updated") or status.get("at"),
                )
    system = ROOT / "system"
    for path in [*(system / f"errors.jsonl.{n}" for n in (3, 2, 1)), system / "errors.jsonl"]:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
                if since < item["at"] <= until:
                    sources.append(("worker_error", item["message"]))
            except (ValueError, KeyError, TypeError):
                continue
    update = _json(system / "self-update.json")
    if update.get("state") in {"rolled_back", "failed"}:
        snapshot("self_update", update["state"], update, update.get("message", ""), update.get("at"))
    return projects, sources


def _collect(db, project):
    current = state(project)
    until = now()
    snapshots = dict(current.get("snapshots") or {})
    projects, sources = _sources(db, project, current.get("at", 0), until, snapshots)
    patterns = owner_patterns((config(project).get("test_env") or {}).get("TEST_LEAK_PATTERNS", ""))
    names = [row.name for row in projects if row.id != project.id]
    fingerprints = {key: dict(value) for key, value in (current.get("fingerprints") or {}).items()}
    changed = set()
    for kind, reason in sources:
        reason = sanitize.scrub(reason, patterns=patterns, names=names)
        normalized = re.sub(r"\b[0-9a-f]{8,}\b|\b[\w-]*\d[\w-]*\b", "#", reason.lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        key = hashlib.sha256(f"{kind}:{normalized}".encode()).hexdigest()
        entry = fingerprints.setdefault(key, {"count": 0})
        entry["count"] += 1
        changed.add(key)
        entry.update(kind=kind, reason=reason[:4000])
    created = 0
    for key in changed:
        entry = fingerprints[key]
        if entry["count"] < 2 and entry["kind"] not in {"rolled_back", "failed"}:
            continue
        previous = db.get(backlog.BacklogItem, entry.get("item_id") or "")
        if previous and previous.project_id == project.id:
            task = db.get(Task, previous.task_id or "")
            if previous.state == "open" or (task and task.state not in CLOSED):
                continue
        item = backlog.BacklogItem(
            id=uid(),
            org_id=project.org_id,
            project_id=project.id,
            text=sanitize.scrub(
                f"Factory failure: {entry['kind']}\nOccurrences: {entry['count']}\n{entry['reason']}",
                patterns=patterns,
                names=names,
            ),
        )
        db.add(item)
        entry["item_id"] = item.id
        created += 1
    project.health = {
        **(project.health or {}),
        "self_report": {
            "at": until,
            "found": created,
            "fingerprints": fingerprints,
            "snapshots": snapshots,
        },
    }
    return created


def run():
    """Consume requests atomically with notes and the cursor, including concurrent worker processes."""
    with _lock, transaction() as db:
        if db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        done = []
        for project in db.scalars(select(Project).order_by(Project.id)):
            if state(project).get("requested"):
                if config(project).get("factory_self", False):
                    done.append((project.id, _collect(db, project)))
                else:
                    project.health = {
                        **(project.health or {}),
                        "self_report": {k: v for k, v in state(project).items() if k != "requested"},
                    }
        return done
