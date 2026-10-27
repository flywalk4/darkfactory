"""Persist bounded planner delegation as ordinary tasks and dependency edges."""

import json
import re
from copy import deepcopy

from sqlalchemy import select

from . import registry
from .agents import AgentSettings, delegation_allowed, resolve
from .db import ROOT, Artifact, ArtifactVersion, Link, Project, Task, decrypt, event, uid
from .runner import plan_object
from .service import execution_limits

TRACKER_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,30}-[0-9]{1,9}\b")


def needs_plan(db, task):
    """One writer goes straight to the code; planning and subtasks are for work the owner marked as large."""
    config = task.role_config or {}
    # A preprod incident arrives with its diagnosis: the writer reproduces and fixes it in one run.
    if config.get("quick") or task.incident:
        return False
    # A separate planner plus a subtask tree spent more wall time waiting than writing; a subtask's
    # writer can still ask to split its own part.
    return task.plan_depth == 0 and bool(config.get("plan"))


def delegate(db, task, plan):
    existing = list(db.scalars(select(Link).where(Link.task_id == task.id, Link.kind == "parent")))
    if existing:
        return True
    project = db.get(Project, task.project_id)
    if project is None or project.org_id != task.org_id:
        raise ValueError("Missing or unauthorized task project")
    config = json.loads(decrypt(project.secret) or "{}")
    settings = AgentSettings.model_validate(config.get("agents") or {})
    plan = plan_object(json.dumps(plan), allow_subtasks=delegation_allowed(config, task))
    if plan["questions"]:
        return False
    if not plan["subtasks"]:
        return False
    limits = execution_limits(db, task)
    if len(limits["members"]) - 1 + len(plan["subtasks"]) > settings.max_delegated_tasks:
        raise ValueError("Planner subtasks exceed the shared task tree limit")
    profiles = {profile.name for profile in settings.profiles}
    writes = {server.name for server in settings.mcp_servers if not server.read_only}
    # A profile with a write MCP can only write; its subtask is planned by the project's planner.
    writers_only = {profile.name for profile in settings.profiles if writes & set(profile.mcp_servers)}
    child_configs = {}
    # Validate the entire batch before adding any child or relationship. A later
    # invalid profile must not leave earlier siblings queued for execution.
    for item in plan["subtasks"]:
        overrides = deepcopy(task.role_config or {})
        assignments = dict(overrides.get("profiles") or {})
        if set(assignments) - {"planner", "writer", "reviewer"} or any(
            name not in profiles for name in assignments.values()
        ):
            raise ValueError("Task references an unknown agent profile")
        selected = item.get("agent_profile")
        if selected is not None:
            if selected not in profiles:
                raise ValueError("Planner referenced an unknown agent profile")
            if selected in writers_only:
                assignments.pop("planner", None)
                assignments["writer"] = selected
            else:
                assignments.update(planner=selected, writer=selected)
        if assignments:
            overrides["profiles"] = assignments
        if item.get("tier") in ("light", "heavy"):
            overrides["tier"] = item["tier"]
        else:
            overrides.pop("tier", None)
        for role in ("plan", "implement", "review"):
            resolve(config, role, assignments)
        child_configs[item["key"]] = overrides
    children = {}
    for item in plan["subtasks"]:
        ident = uid()
        child = Task(
            id=ident,
            project_id=task.project_id,
            org_id=task.org_id,
            # The parent's tracker key (ABC-1948 …) keeps subtask commits traceable to the issue.
            title=(
                key.group(0) + " " + item["title"]
                if (key := TRACKER_KEY.match(task.title)) and key.group(0) not in item["title"]
                else item["title"]
            )[:300],
            priority=task.priority,
            request=f"Parent context only: {task.title}\n{task.request}\n\nImplement ONLY your bounded goal; do not duplicate sibling work:\n{item['request']}\n\nAcceptance criteria:\n{item['acceptance']}",
            branch="darkfactory/" + ident,
            explicit_api=task.explicit_api,
            created_by=task.created_by,
            role_config=child_configs[item["key"]],
            plan_depth=task.plan_depth + 1,
            api_budget_usd=0,
            # Every subtask brings its own allowance: delegation must not shrink the time per task.
            time_credit=project.max_seconds,
        )
        db.add(child)
        db.flush()
        for item_input in registry.inputs(db, task):
            registry.bind(
                db,
                child,
                db.get(Artifact, item_input.artifact_id),
                db.get(ArtifactVersion, item_input.version_id),
            )
        db.add(Link(task_id=task.id, other_id=ident, kind="parent", artifact=False))
        if item.get("files"):
            # The planner's file list stands in for the child's own search: overlapping siblings wait for
            # each other from the start instead of colliding at integration.
            from .execution import LOCATE_FILE

            artifacts = ROOT / "artifacts" / ident
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / LOCATE_FILE).write_text(
                json.dumps(
                    {
                        "files": [
                            {"path": path, "lines": "", "why": "назначено планировщиком"}
                            for path in item["files"]
                        ]
                    }
                )
            )
        children[item["key"]] = child
        event(db, child, "delegated", "Создана планировщиком", parent=task.id, acceptance=item["acceptance"])
    # The parent's own pre-plan search was a guess for the whole request; left in place it would hold
    # files against unrelated tasks while the subtasks work.
    from .execution import LOCATE_FILE

    guessed = ROOT / "artifacts" / task.id / LOCATE_FILE
    if plan["subtasks"] and guessed.exists():
        guessed.rename(guessed.with_name("locate-plan.json"))
    for item in plan["subtasks"]:
        for dependency in item["depends_on"]:
            db.add(
                Link(
                    task_id=children[item["key"]].id,
                    other_id=children[dependency].id,
                    kind="depends_on",
                    artifact=False,
                )
            )
    event(
        db,
        task,
        "decomposed",
        "Подзадачи и зависимости сохранены",
        children=[child.id for child in children.values()],
    )
    return True
