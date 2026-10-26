"""Keep preprod on top of main: hotfixes and owner commits that land in main reach preprod on their own."""

import logging
import os
import time
from types import SimpleNamespace

from sqlalchemy import select

from . import service
from .db import Project, Session, Task, event, transaction, uid
from .gitops import Repository

log = logging.getLogger(__name__)

_last = 0.0
CHECK_EVERY = 60


def interval():
    """Seconds between syncs; 0 turns syncing off."""
    return max(0, int(os.getenv("DARKFACTORY_MAIN_SYNC_SECONDS", "3600")))


def due(project, period):
    health = project.health or {}
    return (
        not project.incident_id
        and not service.remote_down(project)
        and time.time() - health.get("main_sync_at", 0) >= period
    )


def sync(force=False):
    """Merge main into preprod for every due project; a conflict becomes a task for the writer."""
    global _last
    period = interval()
    if not period or (not force and time.monotonic() - _last < CHECK_EVERY):
        return []
    _last = time.monotonic()
    with Session() as db:
        projects = [
            project for project in db.scalars(select(Project)) if due(project, 0 if force else period)
        ]
        db.expunge_all()
    return [(project.id, sync_project(project)) for project in projects]


def sync_project(project):
    """Merge main into this project's preprod now and record the outcome in its health."""
    try:
        result = Repository(project, SimpleNamespace(id=f"main-sync-{project.id}", branch=None)).sync_main()
    except Exception as exc:
        log.warning("main -> preprod sync for %s failed: %s", project.id, exc)
        result = {"state": "failed", "error": str(exc)[-500:]}
    with transaction() as db:
        current = db.get(Project, project.id)
        current.health = {
            **(current.health or {}),
            "main_sync_at": time.time(),
            "main_sync": {key: result.get(key) for key in ("state", "main", "error")},
        }
        if result["state"] == "conflict":
            task = conflict_task(db, current, result)
            if task:
                result["task_id"] = task.id
        elif result["state"] == "merged":
            log.info("Merged %s %s into %s", current.seed_branch, result["main"][:12], current.target)
    return result


def conflict_task(db, project, result):
    """One open resolution task per project; it starts from preprod with main merged in and markers left."""
    for task in db.scalars(
        select(Task).where(
            Task.project_id == project.id, Task.state.notin_(["merged", "cancelled", "duplicate", "checked"])
        )
    ):
        if (task.role_config or {}).get("merge_from"):
            return None
    ident = uid()
    files = ", ".join(result["conflicts"]) or "см. рабочую папку"
    task = Task(
        id=ident,
        org_id=project.org_id,
        project_id=project.id,
        title=f"Влить {project.seed_branch} в {project.target}: конфликт",
        request=(
            f"В {project.seed_branch} есть коммиты, которых нет в {project.target} (до {result['main'][:12]}). "
            f"Автоматическое слияние упало на конфликтах: {files}. В рабочей папке уже начато слияние "
            f"{project.seed_branch} в {project.target} с маркерами конфликтов. Разреши конфликты, сохранив "
            "смысл изменений обеих веток, и убедись, что проверки проходят. Других изменений не делай."
        ),
        branch=f"darkfactory/{ident}",
        role_config={"merge_from": result["main"], "quick": True},
    )
    db.add(task)
    db.flush()
    event(db, task, "created", task.title)
    return task
