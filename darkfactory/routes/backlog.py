"""Project backlog notes and their grouping into tasks."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from .. import (
    backlog,
    cleanup,
    schedules,
)
from ..db import (
    Session,
    Task,
    now,
    transaction,
)
from ..web import access

router = APIRouter()


class BacklogInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


def backlog_view(db, project):
    note = backlog.BacklogItem
    # Notes stay a day after their task finishes or the agent drops them; the board keeps the history.
    # Every branch is false, never NULL, for open notes without a task, so the negation keeps them.
    cutoff = now() - backlog.KEEP_SECONDS
    gone = or_(
        and_(note.state == "dismissed", note.planned < cutoff),
        and_(note.state == "planned", Task.id.is_(None), note.planned < cutoff),
        and_(Task.id.is_not(None), Task.state.in_(cleanup.FINISHED), Task.updated < cutoff),
    )
    pairs = db.execute(
        select(note, Task)
        .outerjoin(Task, Task.id == note.task_id)
        .where(note.project_id == project.id, ~gone)
        .order_by(note.created.desc())
        .limit(200)
    )
    rows = []
    for item, task in pairs:
        rows.append(
            {
                "id": item.id,
                "text": item.text,
                "state": item.state,
                "created": item.created,
                "planned": item.planned,
                "task": {"id": task.id, "title": task.title, "state": task.state} if task else None,
            }
        )
    current = backlog.state(project)
    schedule = db.scalars(
        select(schedules.Schedule)
        .where(
            schedules.Schedule.project_id == project.id,
            schedules.Schedule.kind == "backlog",
            schedules.Schedule.enabled.is_(True),
        )
        .order_by(schedules.Schedule.next_at)
    ).first()
    open_items = [row for row in rows if row["state"] == "open"]
    next_at = schedule.next_at if schedule and open_items and not current.get("requested") else None
    return {
        "items": rows,
        "cron": schedule.cron if schedule else None,
        "requested": current.get("requested"),
        "last_at": current.get("at"),
        "last_error": current.get("error"),
        "last_tasks": current.get("tasks"),
        "next_at": next_at,
    }


@router.get("/api/projects/{project_id}/backlog")
def backlog_list(project_id: str, a=Depends(access)):
    with Session() as db:
        return backlog_view(db, a.project(db, project_id))


@router.post("/api/projects/{project_id}/backlog", status_code=201)
def backlog_add(project_id: str, value: BacklogInput, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        project = a.project(db, project_id)
        if (
            db.scalar(
                select(func.count())
                .select_from(backlog.BacklogItem)
                .where(backlog.BacklogItem.project_id == project.id, backlog.BacklogItem.state == "open")
            )
            >= 500
        ):
            raise HTTPException(409, "В бэклоге уже 500 заметок")
        item = backlog.BacklogItem(
            org_id=project.org_id, project_id=project.id, text=value.text.strip(), author=a.principal.id
        )
        db.add(item)
        db.flush()
        return {"id": item.id}


@router.delete("/api/projects/{project_id}/backlog/{item_id}")
def backlog_remove(project_id: str, item_id: str, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        project = a.project(db, project_id)
        item = db.get(backlog.BacklogItem, item_id)
        if not item or item.project_id != project.id:
            raise HTTPException(404, "Заметка не найдена")
        if item.state != "open":
            raise HTTPException(409, "Заметка уже переведена в задачу")
        db.delete(item)
        return {"deleted": True}


@router.post("/api/projects/{project_id}/backlog/plan")
def backlog_plan(project_id: str, a=Depends(access)):
    """Turn the backlog into tasks now instead of waiting for the timer."""
    a.require("operator")
    with transaction() as db:
        project = a.project(db, project_id)
        if not db.scalar(
            select(backlog.BacklogItem.id).where(
                backlog.BacklogItem.project_id == project.id, backlog.BacklogItem.state == "open"
            )
        ):
            raise HTTPException(409, "В бэклоге нет новых заметок")
        backlog.request(db, project)
        return backlog_view(db, project)
