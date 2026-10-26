"""Project schedules: recurring factory jobs the owner sets up on the project page with a cron expression.

A schedule starts one of the factory's own jobs (stand testing, backlog grouping, syncing main into preprod) or
creates an ordinary task from a saved request. The jobs themselves stay where they are; a schedule only asks
for them at its time, the same way the "run now" buttons do.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlalchemy import JSON, Boolean, Float, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, Project, Task, decrypt, event, now, transaction, uid

log = logging.getLogger(__name__)

KINDS = ("stand_qa", "stand_build", "backlog", "main_sync", "auto_promote", "task")
TITLES = {
    "stand_qa": "Тестирование стенда",
    "stand_build": "Сборка приложения на стенде",
    "backlog": "Бэклог в задачи",
    "main_sync": "Влить main в preprod",
    "auto_promote": "Автоперенос preprod в main",
    "task": "Задача",
}
CLOSED = {"merged", "cancelled", "duplicate", "checked"}
# A job handed over to its own worker loop: its progress shows in `jobs`, not in this text.
QUEUED = {
    "stand_qa": "Тестирование стенда поставлено в очередь",
    "stand_build": "Сборка приложения на стенде поставлена в очередь",
    "backlog": "Бэклог поставлен в очередь",
    "main_sync": "Синхронизация main поставлена в очередь",
    "auto_promote": "Проверка автопереноса в main поставлена в очередь",
}
CHECK_EVERY = 30
# Field ranges of a five-field cron line: minute, hour, day of month, month, day of week (0 and 7 are Sunday).
FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}

_last = 0.0


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(300))
    cron: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # kind "task": {"request": ..., "priority": ...}
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[float] = mapped_column(Float, default=now)
    next_at: Mapped[float | None] = mapped_column(Float, index=True)
    last_at: Mapped[float | None] = mapped_column(Float)
    last_result: Mapped[str | None] = mapped_column(Text)
    last_task_id: Mapped[str | None] = mapped_column(String(32))


def default_timezone():
    return os.getenv("DARKFACTORY_TIMEZONE", "UTC")


def parse(expression):
    """Five sets of allowed values; ValueError names what is wrong."""
    expression = ALIASES.get(expression.strip(), expression.strip())
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("Cron: нужно пять полей — минута, час, день, месяц, день недели")
    result = []
    for part, (low, high) in zip(parts, FIELDS, strict=True):
        values = set()
        for item in part.split(","):
            match = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", item)
            if not match:
                raise ValueError(f"Cron: не понимаю «{item}»")
            span, step = match.group(1), int(match.group(2) or 1)
            if span == "*":
                start, end = low, high
            elif "-" in span:
                start, end = map(int, span.split("-"))
            else:
                start = int(span)
                end = high if match.group(2) else start
            if not low <= start <= end <= high or step < 1:
                raise ValueError(f"Cron: «{item}» вне диапазона {low}–{high}")
            values.update(range(start, end + 1, step))
        result.append(values)
    if 7 in result[4]:
        result[4] = (result[4] - {7}) | {0}
    result.append((parts[2] != "*", parts[4] != "*"))
    return result


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Неизвестный часовой пояс: {name}") from None


def next_time(expression, after, timezone="UTC"):
    """The first matching minute strictly after `after` (epoch seconds), as epoch seconds."""
    minutes, hours, days, months, weekdays, (day_set, weekday_set) = parse(expression)
    tz = zone(timezone)
    moment = datetime.fromtimestamp(after, tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = moment.replace(hour=0, minute=0)
    for _ in range(366 * 5):
        weekday = (day.weekday() + 1) % 7  # cron counts from Sunday
        # Both restricted: either matches (classic cron); one restricted: that one decides.
        if day_set and weekday_set:
            day_ok = day.day in days or weekday in weekdays
        else:
            day_ok = day.day in days and weekday in weekdays
        if day.month in months and day_ok:
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    candidate = day.replace(hour=hour, minute=minute)
                    # Wall-clock time on a DST jump: let zoneinfo normalize it, then keep only later minutes.
                    stamp = candidate.timestamp()
                    if stamp >= moment.timestamp():
                        return stamp
        day = (day + timedelta(days=1)).replace(hour=0, minute=0)
    raise ValueError("Cron: такое время не наступит")


def hours_to_cron(hours):
    """The old "every N hours" settings, as cron: divisors of a day keep their rhythm, longer ones run daily."""
    hours = int(hours or 0)
    if hours <= 0:
        return None
    if hours < 24:
        return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
    return "0 3 * * *"


def create(db, project, kind, cron, *, title=None, timezone=None, enabled=True, config=None):
    schedule = Schedule(
        org_id=project.org_id,
        project_id=project.id,
        kind=kind,
        title=title or TITLES[kind],
        cron=cron,
        timezone=timezone or default_timezone(),
        enabled=enabled,
        config=config or {},
    )
    schedule.next_at = next_time(cron, now(), schedule.timezone) if enabled else None
    db.add(schedule)
    db.flush()
    return schedule


def seed(db, project):
    """Turn a project's old timers into schedules once; the owner edits them on the project page afterwards."""
    if (project.health or {}).get("schedules_seeded"):
        return []
    config = json.loads(decrypt(project.secret) or "{}")
    made = [create(db, project, "main_sync", "0 * * * *")]
    if cron := hours_to_cron(config.get("backlog_hours", 4)):
        made.append(create(db, project, "backlog", cron))
    if config.get("stand_url") and (cron := hours_to_cron(config.get("qa_hours", 4))):
        made.append(create(db, project, "stand_qa", cron))
    project.health = {**(project.health or {}), "schedules_seeded": True}
    return made


def seed_all():
    with transaction() as db:
        return sum(len(seed(db, project)) for project in db.scalars(select(Project)))


def stand_added(db, project):
    """A stand configured later gets the default tester schedule, unless the owner already made one."""
    if db.scalar(select(Schedule.id).where(Schedule.project_id == project.id, Schedule.kind == "stand_qa")):
        return None
    return create(db, project, "stand_qa", "0 */4 * * *")


def fire(db, schedule):
    """Ask for the schedule's job now; returns a short result for the page."""
    from . import backlog, standqa

    project = db.get(Project, schedule.project_id)
    if schedule.kind == "stand_qa":
        if not standqa.stand(project):
            return "Адрес стенда не задан"
        if len(standqa.open_bugs(db, project)) >= standqa.MAX_OPEN:
            return f"Пропущено: открыто {standqa.MAX_OPEN}+ дефектов стенда"
        standqa.request(db, project, "schedule")
        return QUEUED["stand_qa"]
    if schedule.kind == "stand_build":
        from . import standbuild

        if not standbuild.request(db, project, "schedule"):
            return "Адрес стенда не задан"
        return QUEUED["stand_build"]
    if schedule.kind == "backlog":
        if not db.scalar(
            select(backlog.BacklogItem.id).where(
                backlog.BacklogItem.project_id == project.id, backlog.BacklogItem.state == "open"
            )
        ):
            return "Новых заметок нет"
        backlog.request(db, project)
        return QUEUED["backlog"]
    if schedule.kind == "auto_promote":
        from . import promotions

        if not promotions.auto_hours(project):
            return "Автоперенос выключен в настройках проекта"
        promotions.auto_request(db, project)
        return QUEUED["auto_promote"]
    if schedule.kind == "main_sync":
        project.health = {**(project.health or {}), "main_sync_requested": now()}
        return QUEUED["main_sync"]
    previous = db.get(Task, schedule.last_task_id or "")
    if previous and previous.state not in CLOSED:
        return f"Пропущено: прошлая задача {previous.id} ещё не закончена"
    ident = uid()
    task = Task(
        id=ident,
        org_id=project.org_id,
        project_id=project.id,
        title=schedule.title,
        request=schedule.config.get("request") or schedule.title,
        branch=f"darkfactory/{ident}",
        priority=schedule.config.get("priority") or "normal",
        role_config={"schedule": schedule.id},
    )
    db.add(task)
    db.flush()
    event(db, task, "created", task.title)
    event(db, task, "scheduled", f"Создана по расписанию «{schedule.title}» ({schedule.cron})")
    schedule.last_task_id = ident
    return f"Создана задача {ident}"


def run(db, schedule):
    schedule.last_result = fire(db, schedule)[:500]
    schedule.last_at = now()


def tick(force=False):
    """Fire every due schedule once and move it to its next time; returns the ids fired."""
    global _last
    if not force and time.monotonic() - _last < CHECK_EVERY:
        return []
    _last = time.monotonic()
    fired = []
    with transaction() as db:
        due = db.scalars(
            select(Schedule).where(
                Schedule.enabled.is_(True), Schedule.next_at.is_not(None), Schedule.next_at <= now()
            )
        )
        for schedule in due:
            try:
                run(db, schedule)
            except Exception as exc:  # One broken schedule must not stop the others.
                log.exception("Schedule %s failed", schedule.id)
                schedule.last_result, schedule.last_at = f"Ошибка: {exc}"[:500], now()
            # After a long outage the missed runs collapse into this one.
            schedule.next_at = next_time(schedule.cron, now(), schedule.timezone)
            fired.append(schedule.id)
    return fired


def jobs(db, project):
    """What each kind of job is doing now and what its last run gave, for the schedules page: a stand run or
    a backlog grouping is not a task on the board, so the page is where the owner sees it."""
    from . import backlog, standqa

    health = project.health or {}
    qa = standqa.state(project)
    tasks = [db.get(Task, ident) for ident in qa.get("task_ids") or []]
    grouping = backlog.state(project)
    return {
        "stand_qa": {
            "running": qa.get("running"),
            "requested": qa.get("requested"),
            "at": qa.get("at"),
            "error": qa.get("error"),
            "summary": qa.get("summary"),
            "found": qa.get("tasks"),
            "tasks": [{"id": task.id, "title": task.title, "state": task.state} for task in tasks if task],
        },
        "stand_build": build_job(project),
        "backlog": {
            "requested": grouping.get("requested"),
            "at": grouping.get("at"),
            "error": grouping.get("error"),
            "found": grouping.get("tasks"),
        },
        "auto_promote": {
            "requested": (health.get("auto_promote") or {}).get("requested"),
            "at": (health.get("auto_promote") or {}).get("at"),
            "result": (health.get("auto_promote") or {}).get("result"),
        },
        "main_sync": {
            "requested": health.get("main_sync_requested"),
            "at": health.get("main_sync_at"),
            "state": (health.get("main_sync") or {}).get("state"),
            "error": (health.get("main_sync") or {}).get("error"),
        },
    }


def build_job(project):
    from . import standbuild

    current = standbuild.state(project)
    history = current.get("history") or []
    last = history[-1] if history else None
    return {
        "running": current.get("running"),
        "requested": current.get("requested"),
        "at": current.get("at"),
        "error": current.get("error"),
        "task": current.get("task") if current.get("running") else (last or {}).get("task"),
        "last": last,
        "average": standbuild.summary(history),
        "found": current.get("tasks"),
    }


def view(schedule):
    return {
        "id": schedule.id,
        "project_id": schedule.project_id,
        "kind": schedule.kind,
        "title": schedule.title,
        "cron": schedule.cron,
        "timezone": schedule.timezone,
        "enabled": schedule.enabled,
        "request": (schedule.config or {}).get("request", ""),
        "priority": (schedule.config or {}).get("priority", "normal"),
        "next_at": schedule.next_at,
        "last_at": schedule.last_at,
        "last_result": schedule.last_result,
        "queued": schedule.last_result == QUEUED.get(schedule.kind),
        "last_task_id": schedule.last_task_id,
    }


def timezones():
    return sorted(available_timezones())
