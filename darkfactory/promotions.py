"""Manual main-branch decisions recorded in the existing durable task event journal."""

import json

from fastapi import HTTPException
from sqlalchemy import select

from . import service
from .db import Event, Project, Task


def proposal(db, task):
    accepted = db.scalar(
        select(Event).where(Event.task_id == task.id, Event.kind == "accepted").order_by(Event.id.desc())
    )
    if not accepted or not accepted.data.get("promotion"):
        return None
    result = {
        "id": accepted.id,
        "source_sha": accepted.data["commit"],
        "target": "main",
        "state": "pending",
        "checks": accepted.data.get("checks"),
    }
    decisions = db.scalars(
        select(Event)
        .where(
            Event.task_id == task.id,
            Event.kind.in_(["promotion_merged", "promotion_rejected"]),
            Event.id > accepted.id,
        )
        .order_by(Event.id.desc())
    )
    for decision in decisions:
        if decision.data.get("proposal_id") == accepted.id:
            result.update(
                state="merged" if decision.kind == "promotion_merged" else "rejected",
                decided_at=decision.at,
                decided_by=decision.data.get("actor"),
            )
            break
    if result["state"] == "pending" and superseded(db, task.project_id, accepted.id):
        result["state"] = "superseded"
    return result


def superseded(db, project_id, accepted_id):
    """A later acceptance of the same project already promoted to main carried this commit with it."""
    rows = db.scalars(
        select(Event)
        .join(Task, Task.id == Event.task_id)
        .where(Task.project_id == project_id, Event.kind == "promotion_merged")
    )
    return any((row.data or {}).get("proposal_id", 0) > accepted_id for row in rows)


def pending(db, task_ids):
    """Tasks whose latest preprod acceptance still waits for the owner's main decision."""
    if not task_ids:
        return set()
    rows = db.scalars(
        select(Event)
        .where(
            Event.task_id.in_(task_ids),
            Event.kind.in_(["accepted", "promotion_merged", "promotion_rejected"]),
        )
        .order_by(Event.id)
    )
    latest, decided, promoted = {}, set(), {}
    for row in rows:
        if row.kind == "accepted":
            latest[row.task_id] = row if row.data.get("promotion") else None
        else:
            decided.add((row.task_id, row.data.get("proposal_id")))
            if row.kind == "promotion_merged" and row.data.get("proposal_id"):
                project_id = db.get(Task, row.task_id).project_id
                promoted[project_id] = max(promoted.get(project_id, 0), row.data["proposal_id"])
    # A stale acceptance cannot be promoted; the next accepted change carries its commits to main anyway.
    open_ = [
        (accepted.id, task_id)
        for task_id, accepted in latest.items()
        if accepted
        and (task_id, accepted.id) not in decided
        and not stale(db, db.get(Task, task_id), accepted.data["commit"], accepted.data.get("checks"))
    ]
    # Promotion carries all of preprod up to the accepted commit: per project only the newest one is worth
    # offering, older ones would bring an earlier preprod (with whatever later tasks cleaned up).
    newest = {}
    for accepted_id, task_id in sorted(open_):
        project_id = db.get(Task, task_id).project_id
        # Preprod is linear: a later acceptance already promoted to main carried this one too.
        if accepted_id > promoted.get(project_id, 0):
            newest[project_id] = task_id
    return set(newest.values())


def stale(db, task, sha, checks):
    project = db.get(Project, task.project_id)
    return (
        task.state != "merged"
        or project.target != "preprod"
        or checks != project.checks
        or not (task.result_sha == task.reviewed_sha == task.candidate_sha == sha)
        # Without required checks the owner's review of the main diff is the only gate.
        or bool(project.checks and task.verified_sha != sha)
        or service.dependencies_changed(db, task)
    )


def validate(db, task, value):
    project = db.get(Project, task.project_id)
    if stale(db, task, value["source_sha"], value["checks"]):
        raise HTTPException(409, "Принятый результат или его проверки устарели; нужна повторная приёмка")
    if project.incident_id:
        raise HTTPException(409, "Сначала нужно исправить инцидент preprod")
    if (project.health or {}).get("state") == "broken":
        check = db.get(Task, (project.health or {}).get("task_id") or "")
        if check and check.state in {"queued", "running", "recovering"}:
            raise HTTPException(
                409, "Идёт повторная проверка preprod после исправления; повтори через пару минут"
            )
        raise HTTPException(409, "Preprod сломан; дождись проверки или исправления")
    return project


INCLUDED_LIMIT = 200


def included(db, task, proposal_id):
    """Everything this promotion carries to main: the tasks accepted into preprod since the last promotion,
    each with the reviewer's open notes and whether it touched risky paths. Preprod is linear, so event
    order is commit order."""
    from .db import ROOT
    from .logs import RISKY_PATH, diff_files

    promoted = db.scalars(
        select(Event)
        .join(Task, Task.id == Event.task_id)
        .where(Task.project_id == task.project_id, Event.kind == "promotion_merged")
    )
    watermark = max([(row.data or {}).get("proposal_id", 0) for row in promoted] or [0])
    rows = db.execute(
        select(Event, Task)
        .join(Task, Task.id == Event.task_id)
        .where(
            Task.project_id == task.project_id,
            Event.kind == "accepted",
            Event.id > watermark,
            Event.id <= proposal_id,
        )
        .order_by(Event.id)
    ).all()
    latest = {}
    for accepted, owner in rows:
        latest[owner.id] = (accepted, owner)
    result = []
    for accepted, owner in list(latest.values())[-INCLUDED_LIMIT:]:
        artifacts = ROOT / "artifacts" / owner.id
        try:
            files = diff_files((artifacts / "diff.patch").read_text(errors="replace"))
        except OSError:
            files = []
        try:
            review = json.loads((artifacts / "review.json").read_text())
            notes = [
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in review.get("findings") or []
            ][:10]
        except (OSError, ValueError, AttributeError):
            notes = []
        result.append(
            {
                "task_id": owner.id,
                "title": owner.title,
                "commit": (accepted.data or {}).get("commit"),
                "files": len(files),
                "risky": sorted({path for path in files if RISKY_PATH.search(path)})[:10],
                "notes": notes,
            }
        )
    return result
