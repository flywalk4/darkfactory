"""Project backlog notes and their grouping into tasks."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import (
    backlog,
)
from ..db import (
    Session,
    Task,
    transaction,
)
from ..web import access

router = APIRouter()


class BacklogInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


def backlog_view(db, project):
    items = db.scalars(
        select(backlog.BacklogItem)
        .where(backlog.BacklogItem.project_id == project.id)
        .order_by(backlog.BacklogItem.created.desc())
        .limit(200)
    )
    rows = []
    for item in items:
        task = db.get(Task, item.task_id) if item.task_id else None
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
    hours = backlog.interval_hours(project)
    open_items = [row for row in rows if row["state"] == "open"]
    next_at = None
    if hours and open_items and not current.get("requested"):
        next_at = max(min(row["created"] for row in open_items), current.get("at", 0)) + hours * 3600
    return {
        "items": rows,
        "hours": hours,
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
