"""The Director's stop handling: tasks that stopped for a human are answered, extended, opened to a host,
cancelled or escalated. The Director's chat with the owner lives in director.py; both are one role to the owner."""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from . import service
from .db import ROOT, Event, Project, Task, decrypt, encrypt, event, transaction, uid
from .egress import _destination
from .execution import DockerExecutor, controller_lock

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "message", "extra_corrections", "extend_minutes", "hosts", "rework"],
    "properties": {
        "decision": {"type": "string", "enum": ["resume", "rereview", "allow_hosts", "cancel", "escalate"]},
        "message": {"type": "string"},
        "extra_corrections": {"type": "integer", "minimum": 0, "maximum": 2},
        "extend_minutes": {"type": "integer", "minimum": 0, "maximum": 60},
        "hosts": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "rework": {"type": "boolean"},
    },
}


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(resume|rereview|allow_hosts|cancel|escalate)$")
    message: str = Field(max_length=8000)
    extra_corrections: int = Field(ge=0, le=2)
    extend_minutes: int = Field(ge=0, le=60)
    hosts: list[str] = Field(max_length=10)
    rework: bool = True


PROMPT = """You are the Director of an autonomous software factory. The task below stopped and waits for a human.
The owner only creates tasks and promotes preprod to main; act in their place whenever a competent
engineer could decide without them. The repository is mounted read-only in /workspace: inspect it.

Decisions:
- resume: answer the question or give concrete next steps in `message` (the developer agent reads it as
  the owner's clarification). Add extra_corrections (≤2) when review findings change between rounds or a
  fix is close; add extend_minutes (≤60) when the time budget ran out but work progresses.
- rereview: the last review rejection does not match the current code: the finding is already fixed in
  the candidate, or it blames changes the task did not make (they came from preprod). Check the workspace
  first. The task goes back to review without another development run; `message` tells the reviewer what
  you verified (file and lines). Use resume instead when the developer still has something to change.
- allow_hosts: the agent needs network hosts (exact host:port, e.g. registry.example.com:443) that are
  clearly legitimate for this repository (package feeds, docs, the project's own services). Also resumes.
- cancel: the task is a duplicate, already done in preprod, or impossible by construction. Explain why.
  Also cancel a subtask that loops on review while other tasks wait for it: the waiting tasks receive
  its goal and do the needed part themselves.
- A developer question asking for data, logs, configuration or access to a stand or outside system is
  not an owner decision: answer it yourself from the repository, or tell the developer to work from what
  the repository defines and state the assumption. The owner cannot see more than the factory does.
  When the request itself demands facts from a stand, production or an outside system (the actual cause
  of an incident there, its real settings, a reproduction on it), read that part as "per what the
  repository defines": resume and tell the developer to finish against the repository and list what it
  lacks as [LATER] lines. Such a gap is never a reason to escalate.
- A question the owner already answered (owner clarifications in the request, resume events) is never escalated again: resume
  and point the developer to that answer.
- escalate: only for decisions that truly need the owner — credentials, paid services, product choices
  with no hint in the repository, destructive or security-sensitive actions, or repeated failure of the
  same approach. Write a short Russian summary for the owner in `message`: what happened, what you
  checked, what exactly they must decide.

A stop caused by the factory itself (a model the account cannot use, a phase that does not advance, a
stale or foreign review diff) is not the owner's decision either: if the history shows the factory has
changed since (a newer build looked at this stop), resume or rereview; escalate it only when the same
defect is clearly still there, and start `message` with "ФАБРИКА:" so the operator fixes the factory.
Set rework false when the stop came from the factory or infrastructure (time slice, deadline, restart,
network, quota) after the developer had finished, so the task continues from its current phase without
another development run; true when the developer must change code.
Never weaken tests or checks, never ask to skip review. Write `message` in Russian.
Respond with JSON only.

"""


def config(project):
    return json.loads(decrypt(project.secret) or "{}")


def default_limit():
    # The old name stays readable: deployments set it before the supervisor became the Director.
    return int(os.getenv("DARKFACTORY_DIRECTOR_LIMIT") or os.getenv("DARKFACTORY_SUPERVISOR_LIMIT") or "6")


def autonomous(project):
    """The Director resolves stops for this project; projects saved earlier keep their `supervisor` key."""
    values = config(project)
    return values.get("director_autonomy", values.get("supervisor", True))


def limit(project):
    values = config(project)
    value = values.get("director_limit", values.get("supervisor_limit"))
    return int(value if value is not None else default_limit())


def used(db, task):
    try:
        members = [member.id for member in service.execution_limits(db, task)["members"]]
    except ValueError:
        members = [task.id]
    return (
        db.scalar(select(func.count(Event.id)).where(Event.task_id.in_(members), Event.kind == "supervisor"))
        or 0
    )


def last_event(db, task_id):
    # The supervisor's own run records denied hosts; that must not read as a new stop or an owner action.
    return (
        db.scalar(select(func.max(Event.id)).where(Event.task_id == task_id, Event.kind != "egress_denied"))
        or 0
    )


def eligible(db, task):
    """The Director will take this stop: notifications wait for its verdict instead of paging the owner."""
    if task.state != "needs_human" or task.system_check:
        return False
    project = db.get(Project, task.project_id)
    if not project or not autonomous(project):
        return False
    return used(db, task) < limit(project)


# A stop the supervisor cannot reach (no free subscription) stops holding files and reads as the owner's.
AWAIT_SECONDS = 900


def awaits_director(db, task):
    """A stop the Director will take and has not looked at yet."""
    return (
        task.state == "needs_human"
        and time.time() - task.updated < AWAIT_SECONDS
        and (task.role_config or {}).get("supervised", 0) < last_event(db, task.id)
        and eligible(db, task)
    )


RETRIES = 3
RETRY_SECONDS = 120


def pending(db):
    rows = db.scalars(select(Task).where(Task.state == "needs_human").order_by(Task.updated))
    for task in rows:
        seen = (task.role_config or {}).get("supervised", 0) >= last_event(db, task.id)
        # A stop the supervisor already handed to the owner is looked at again after a factory deploy:
        # many escalations are factory defects (an unsupported model, a broken phase transition) that
        # a fix removes, and those tasks should not keep waiting for a human.
        if seen and (
            (task.role_config or {}).get("supervised_build") in (None, BUILD) or not eligible(db, task)
        ):
            # Same build, or no budget left: nothing new to decide, and no repeated page to the owner.
            continue
        failed = (task.role_config or {}).get("supervisor_failure") or {}
        if (
            failed.get("marker") == last_event(db, task.id)
            and time.time() - failed.get("at", 0) < RETRY_SECONDS
        ):
            continue
        if eligible(db, task):
            return task
        if foldable(db, task):
            # Paging the owner would hold every waiting task for hours; they take over its goal instead.
            fold(db, task)
            continue
        # Out of budget or disabled: page the owner once for this stop.
        event(
            db,
            task,
            "supervisor_escalated",
            f"Директор не вмешивался (лимит {limit(db.get(Project, task.project_id))} на дерево исчерпан "
            "или выключен). Причина остановки: " + (task.reason or "—"),
        )
        mark(db, task)
    return None


LOOP_STOPS = {"review_repeated", "correction_limit"}


def foldable(db, task):
    return task.state == "needs_human" and task.reason in LOOP_STOPS and bool(service.waiting_on(db, task))


def fold(db, task):
    task.cancel_requested = True
    service.cancel_delegated(db, task)
    task.state, task.reason = "cancelled", "Зациклилась; цель передана ожидающим задачам"
    event(db, task, "folded", "Подзадача зациклилась на ревью; её цель передана ожидающим задачам")
    mark(db, task)


def mark(db, task):
    db.flush()
    task.role_config = {
        **(task.role_config or {}),
        "supervised": last_event(db, task.id),
        # The factory build that looked at this stop; a later build looks again (see pending()).
        "supervised_build": BUILD,
    }


def build_id():
    """Digest of the factory's own code: changes with every deploy of the worker."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


BUILD = build_id()


def tail(path, size):
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-size:].decode(errors="replace")


def latest_transcript(task_id, entries=60):
    root = ROOT / "artifacts" / task_id / "browser"
    try:
        runs = sorted(
            (p for p in root.iterdir() if (p / "transcript.jsonl").is_file()), key=lambda p: p.stat().st_mtime
        )
    except OSError:
        return ""
    lines = []
    for run in runs[-2:]:
        for line in tail(run / "transcript.jsonl", 200_000).splitlines()[-entries:]:
            try:
                item = json.loads(line)
                lines.append(f"[{item['kind']}] {str(item['text'])[:800]}")
            except (ValueError, KeyError, TypeError):
                continue
    return "\n".join(lines[-entries:])


def context(db, task):
    project = db.get(Project, task.project_id)
    limits = service.execution_limits(db, task)
    events = list(
        db.scalars(select(Event).where(Event.task_id == task.id).order_by(Event.id.desc()).limit(40))
    )[::-1]
    artifacts = ROOT / "artifacts" / task.id
    parts = {
        "task": {
            "title": task.title,
            "phase": task.phase,
            "stop_reason": task.reason,
            "request": task.request[-8000:],
        },
        "budgets": {
            "active_minutes": round(limits["active_seconds"] / 60),
            "time_budget_minutes": round(limits["time_budget"] / 60),
            "corrections": limits["corrections"],
            "correction_limit": limits["correction_limit"],
        },
        "project": {
            "name": project.name,
            "checks": project.checks,
            "allowed_hosts": config(project).get("allowed_hosts", []),
        },
        "events": [f"{e.kind}: {e.message[:600]}" for e in events],
    }
    text = PROMPT + json.dumps(parts, ensure_ascii=False, indent=1)
    for name, size in (
        ("handoff.md", 6000),
        ("review.txt", 6000),
        ("checks.txt", 4000),
        ("egress-denied.json", 1000),
    ):
        if content := tail(artifacts / name, size):
            text += f"\n\n--- {name} (tail) ---\n{content}"
    if transcript := latest_transcript(task.id):
        text += "\n\n--- last agent transcript ---\n" + transcript
    return text


def allow_hosts(db, task, hosts):
    """Validated host:port entries join the project allowlist (≤50); returns the added ones."""
    for authority in hosts:
        _destination(authority)
    project = db.get(Project, task.project_id)
    values = config(project)
    current = list(values.get("allowed_hosts", []))
    added = [item for item in dict.fromkeys(hosts) if item not in current]
    if len(current) + len(added) > 50:
        raise ValueError("Не больше 50 разрешённых адресов")
    values["allowed_hosts"] = current + added
    project.secret = encrypt(json.dumps(values))
    return added


def apply(db, task, decision):
    kind = decision.decision
    if kind == "allow_hosts":
        try:
            added = allow_hosts(db, task, decision.hosts)
        except ValueError as exc:
            return escalate(db, task, f"Директор хотел разрешить адреса {decision.hosts}, но: {exc}")
        event(
            db,
            task,
            "egress_allowed",
            "Директор разрешил адреса: " + ", ".join(added or decision.hosts),
            hosts=added,
        )
    if kind in {"resume", "allow_hosts"}:
        reason = task.reason
        service.resume(
            db,
            task,
            message=decision.message.strip(),
            extend_seconds=decision.extend_minutes * 60,
            extra_corrections=decision.extra_corrections,
            author="Решение директора",
            keep_phase=not decision.rework,
        )
        event(
            db,
            task,
            "supervisor",
            f"Директор продолжил задачу (причина: {reason or '—'}): {decision.message[:1500]}",
            decision=kind,
            extra_corrections=decision.extra_corrections,
            extend_minutes=decision.extend_minutes,
        )
    elif kind == "rereview":
        reason = task.reason
        service.resume(
            db,
            task,
            message=decision.message.strip(),
            extra_corrections=max(1, decision.extra_corrections),
            author="Решение директора (повторное ревью)",
            keep_phase=True,
        )
        # The writer's work stands; only the verdict was wrong. A fresh review reads a fresh diff.
        task.phase, task.reviewed_sha = "review", None
        event(
            db,
            task,
            "supervisor",
            f"Директор вернул задачу на повторное ревью (причина: {reason or '—'}): {decision.message[:1500]}",
            decision=kind,
        )
    elif kind == "cancel":
        task.cancel_requested = True
        service.cancel_delegated(db, task)
        task.state, task.reason = "cancelled", "Отменена директором: " + decision.message[:500]
        event(db, task, "supervisor", "Директор отменил задачу: " + decision.message[:1500], decision=kind)
    else:
        return escalate(db, task, decision.message)
    mark(db, task)


def escalate(db, task, message):
    event(db, task, "supervisor_escalated", "Нужно решение владельца. " + message[:3000])
    mark(db, task)


def handle_stop(executor=None):
    """Handle one stopped task per call; the decision is applied only if the stop is still the same."""
    with transaction() as db:
        task = pending(db)
        if task is None:
            return False
        project = db.get(Project, task.project_id)
        marker = last_event(db, task.id)
        prompt = context(db, task)
        probe = SimpleNamespace(
            id=task.id,
            org_id=task.org_id,
            project_id=task.project_id,
            phase="director",  # Routes by the Director profile, on the writer pool.
            plan_depth=0,
            role_config={},
            explicit_api=False,
            last_connection=None,
            api_reserved_usd=0,
            title=task.title,
            request="",
            lease_owner=uid(),
            candidate_sha=task.candidate_sha,
            target_sha=task.target_sha,
            verified_sha=None,
        )
        connection = service.choose_connection(
            db, probe, project, {"api_budget": 0}, allow_quota_probe=True, shared=True
        )
        if connection is None:
            # No subscription right now: retry on a later tick without marking the stop as handled.
            return False
        task_id = task.id
        db.expunge(project)
        db.expunge(connection)

    def unchanged():
        with transaction() as db:
            row = db.get(Task, task_id)
            return bool(row and row.state == "needs_human" and last_event(db, task_id) == marker)

    executor = executor or DockerExecutor()
    started = time.monotonic()
    try:
        with controller_lock(task_id):
            result = executor.run(
                probe,
                project,
                connection,
                "director",
                unchanged,
                600,  # A slow model thinks minutes before answering about a large repository.
                prompt_override=prompt,
                output_schema=SCHEMA,
            )
        if result.get("status") != "ok":
            raise ValueError(f"исполнитель вернул {result.get('status')}")
        decision = Decision.model_validate_json(result["text"])
    except BlockingIOError:
        return False
    except Exception as exc:
        log.warning("Supervisor failed for %s: %s", task_id, exc)
        with transaction() as db:
            row = db.get(Task, task_id)
            if not row or row.state != "needs_human" or last_event(db, task_id) != marker:
                return True
            failed = (row.role_config or {}).get("supervisor_failure") or {}
            attempts = failed.get("attempts", 0) + 1 if failed.get("marker") == marker else 1
            if attempts < RETRIES:
                # A restart or a busy lock fails one run; only a repeated failure needs the owner.
                row.role_config = {
                    **(row.role_config or {}),
                    "supervisor_failure": {"marker": marker, "attempts": attempts, "at": time.time()},
                }
            else:
                escalate(
                    db,
                    row,
                    f"Директор не смог разобрать остановку ({type(exc).__name__}). Причина: {row.reason or '—'}",
                )
        return True
    with transaction() as db:
        row = db.get(Task, task_id)
        if not row or row.state != "needs_human" or last_event(db, task_id) != marker:
            return True  # The owner acted meanwhile.
        apply(db, row, decision)
        log.info("Supervisor %s → %s in %.0fs", task_id, decision.decision, time.monotonic() - started)
    return True
