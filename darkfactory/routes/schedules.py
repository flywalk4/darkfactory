"""Project schedules: list, create, change, delete and run now."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import schedules
from ..db import Session, now, transaction
from ..web import access

router = APIRouter()


class ScheduleInput(BaseModel):
    kind: Literal["stand_qa", "stand_build", "backlog", "main_sync", "auto_promote", "lessons", "task"]
    title: str | None = Field(default=None, max_length=300)
    cron: str = Field(min_length=1, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    request: str | None = Field(default=None, max_length=20000)
    priority: Literal["critical", "high", "normal", "low"] = "normal"


def checked(value):
    timezone = value.timezone or schedules.default_timezone()
    try:
        schedules.next_time(value.cron, now(), timezone)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if value.kind == "task" and not (value.request or "").strip():
        raise HTTPException(400, "Для задачи по расписанию нужен текст задачи")
    if value.kind == "task" and not (value.title or "").strip():
        raise HTTPException(400, "Для задачи по расписанию нужно название")
    config = {"request": value.request.strip(), "priority": value.priority} if value.kind == "task" else {}
    return timezone, config


def owned(db, a, schedule_id):
    schedule = db.get(schedules.Schedule, schedule_id)
    if not schedule:
        raise HTTPException(404, "Расписание не найдено")
    a.project(db, schedule.project_id)
    return schedule


@router.get("/api/projects/{project_id}/schedules")
def schedule_list(project_id: str, a=Depends(access)):
    with Session() as db:
        project = a.project(db, project_id)
        rows = db.scalars(
            select(schedules.Schedule)
            .where(schedules.Schedule.project_id == project.id)
            .order_by(schedules.Schedule.created)
        )
        return {
            "items": [schedules.view(row) for row in rows],
            "jobs": schedules.jobs(db, project),
            "kinds": schedules.TITLES,
            "timezone": schedules.default_timezone(),
        }


@router.post("/api/projects/{project_id}/schedules", status_code=201)
def schedule_create(project_id: str, value: ScheduleInput, a=Depends(access)):
    a.require("owner")
    timezone, config = checked(value)
    with transaction() as db:
        project = a.project(db, project_id)
        schedule = schedules.create(
            db,
            project,
            value.kind,
            value.cron.strip(),
            title=(value.title or "").strip() or None,
            timezone=timezone,
            enabled=value.enabled,
            config=config,
        )
        return schedules.view(schedule)


@router.put("/api/schedules/{schedule_id}")
def schedule_update(schedule_id: str, value: ScheduleInput, a=Depends(access)):
    a.require("owner")
    timezone, config = checked(value)
    with transaction() as db:
        schedule = owned(db, a, schedule_id)
        schedule.kind = value.kind
        schedule.title = (value.title or "").strip() or schedules.TITLES[value.kind]
        schedule.cron = value.cron.strip()
        schedule.timezone = timezone
        schedule.enabled = value.enabled
        schedule.config = config
        schedule.next_at = schedules.next_time(schedule.cron, now(), timezone) if value.enabled else None
        return schedules.view(schedule)


@router.delete("/api/schedules/{schedule_id}")
def schedule_delete(schedule_id: str, a=Depends(access)):
    a.require("owner")
    with transaction() as db:
        db.delete(owned(db, a, schedule_id))
    return {"deleted": schedule_id}


@router.post("/api/schedules/{schedule_id}/run")
def schedule_run(schedule_id: str, a=Depends(access)):
    """Start the schedule's job now; its regular time stays as it was."""
    a.require("operator")
    with transaction() as db:
        schedule = owned(db, a, schedule_id)
        schedules.run(db, schedule)
        return schedules.view(schedule)
