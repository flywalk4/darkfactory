"""Manual main-branch decisions recorded in the existing durable task event journal."""

import json
import subprocess

from fastapi import HTTPException
from sqlalchemy import select

from . import service
from .db import Event, Project, Session, Task, decrypt, event, now


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


def auto_hours(project):
    config = json.loads(decrypt(project.secret) or "{}")
    return max(0, int(config.get("auto_promote_hours") or 0))


def auto_request(db, project):
    """A schedule asks for an automatic promotion check; the git work runs outside the scheduler's lock."""
    project.health = {**(project.health or {}), "auto_promote": {**auto_state(project), "requested": now()}}


def auto_state(project):
    return dict((project.health or {}).get("auto_promote") or {})


def auto_candidate(db, project):
    """(task, proposal) when the newest preprod acceptance may go to main on its own, else (None, reason)."""
    hours = auto_hours(project)
    if not hours:
        return None, "Автоперенос выключен в настройках проекта"
    merged = list(db.scalars(select(Task.id).where(Task.project_id == project.id, Task.state == "merged")))
    newest = pending(db, merged)
    if not newest:
        return None, "Нечего переносить: main уже содержит принятый preprod"
    task = db.get(Task, next(iter(newest)))
    current = proposal(db, task)
    accepted = db.get(Event, current["id"])
    if now() - accepted.at < hours * 3600:
        return None, f"Выдержка: последнее принятие моложе {hours} ч"
    since = now() - hours * 3600
    incident = db.scalar(
        select(Event.id)
        .join(Task, Task.id == Event.task_id)
        .where(
            Task.project_id == project.id,
            Event.kind.in_(["incident_detected", "incident_reopened"]),
            Event.at > since,
        )
    )
    if incident:
        return None, f"За последние {hours} ч был инцидент preprod"
    if (project.health or {}).get("state") != "healthy":
        return None, "Preprod ещё не проверен или проверки не прошли"
    try:
        validate(db, task, current)
    except HTTPException as exc:
        return None, str(exc.detail)
    return task, current


_last = 0.0


def auto_promote(force=False):
    """Promote every project that asked and passed its soak; the change also passes the leak check."""
    import time

    global _last
    if not force and time.monotonic() - _last < 30:
        return []
    _last = time.monotonic()
    from .db import transaction
    from .execution import controller_lock
    from .gitops import GitError, Repository
    from .leaks import owner_patterns, scan_repository

    with Session() as db:
        projects = [row for row in db.scalars(select(Project)) if auto_state(row).get("requested")]
        db.expunge_all()
    done = []
    for project in projects:
        with transaction() as db:
            task, current = auto_candidate(db, db.get(Project, project.id))
            task_id = task.id if task else None
        result = current if task is None else None
        if task_id:
            try:
                with controller_lock(task_id), transaction() as db:
                    row = db.get(Project, project.id)
                    task = db.get(Task, task_id)
                    task, current = auto_candidate(db, row)
                    if task is None or task.id != task_id:
                        result = (
                            current
                            if task is None
                            else "Предложение изменилось; повтор при следующем запуске"
                        )
                    else:
                        repository = Repository(row, task)
                        main_sha = repository.main_preview(current["source_sha"])["main_sha"]
                        config = json.loads(decrypt(row.secret) or "{}")
                        patterns = owner_patterns(
                            (config.get("test_env") or {}).get("TEST_LEAK_PATTERNS", "")
                        )
                        findings = scan_repository(
                            main_sha, current["source_sha"], repository.mirror, patterns
                        )
                        if findings:
                            result = "Не перенесено: проверка утечек нашла " + ", ".join(
                                sorted({f"{rule} ({source})" for source, _, rule in findings})
                            )
                        else:
                            target = repository.promote_main(current["source_sha"], main_sha)
                            event(
                                db,
                                task,
                                "promotion_merged",
                                "Фабрика сама влила принятый коммит в main после выдержки",
                                proposal_id=current["id"],
                                commit=current["source_sha"],
                                main_sha=main_sha,
                                actor="factory",
                            )
                            from . import deploys

                            deploys.request(db, row, task, target, "factory")
                            result = f"Перенесено в main: {(target or '')[:12]}"
            except BlockingIOError:
                result = "Другая операция задачи ещё выполняется; повтор при следующем запуске"
            except (GitError, OSError, subprocess.CalledProcessError) as exc:
                result = f"Ошибка переноса: {str(exc)[-300:]}"
        with transaction() as db:
            row = db.get(Project, project.id)
            state = {k: v for k, v in auto_state(row).items() if k != "requested"}
            row.health = {**(row.health or {}), "auto_promote": {**state, "at": now(), "result": result}}
        done.append((project.id, result))
    return done
