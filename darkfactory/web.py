"""Shared request helpers: authentication and row serialization."""

import copy
import json
import os
import time

from fastapi import Header

from . import (
    backlog,
    service,
    standqa,
    toolchains,
)
from .db import (
    ROOT,
    Pool,
    Project,
    Session,
    Task,
    decrypt,
)
from .director_stops import default_limit as director_default


def access(authorization: str = Header(default="")):
    with Session() as db:
        return service.authenticate(db, authorization.removeprefix("Bearer "))


def public(row):
    hidden = {"secret", "digest", "lease_owner"}
    result = {c.name: getattr(row, c.name) for c in row.__table__.columns if c.name not in hidden}
    if isinstance(row, Pool):
        result.update(row.policy or {})
    if isinstance(row, Project):
        result.update(project_settings(row))
    if isinstance(row, Task) and row.state == "running":
        result.update(agent_activity(row.id))
    return result


# The project list is polled; decrypting secrets and parsing toolchain state each time is wasted work.
# The key covers both sources, so a saved setting or a rebuilt environment shows at once.
SETTINGS_TTL = 5.0
_settings_cache: dict[str, tuple[float, tuple, dict]] = {}


def project_settings(row):
    try:
        info = os.stat(toolchains.state_path(row.id))
        key = (row.secret, info.st_mtime_ns, info.st_ino, info.st_size)
    except OSError:
        key = (row.secret, None)
    cached = _settings_cache.get(row.id)
    if cached and cached[1] == key and time.monotonic() - cached[0] < SETTINGS_TTL:
        return copy.deepcopy(cached[2])
    config = json.loads(decrypt(row.secret) or "{}")
    value = dict(
        executor_image=config.get("executor_image"),
        allowed_hosts=config.get("allowed_hosts", []),
        allow_paid_fallback=config.get("allow_paid_fallback", False),
        package_registries=config.get("package_registries", True),
        merge_without_checks=config.get("merge_without_checks", True),
        director_autonomy=config.get("director_autonomy", config.get("supervisor", True)),
        director_limit=config.get("director_limit") or config.get("supervisor_limit") or director_default(),
        review_muted=config.get("review_muted", []),
        backlog_hours=config.get("backlog_hours", backlog.INTERVAL_HOURS),
        max_tree_seconds=config.get("max_tree_seconds") or 43200,
        test_env_keys=sorted(config.get("test_env", {})),
        stand_url=config.get("stand_url"),
        stand_hosts=config.get("stand_hosts", []),
        qa_hours=config.get("qa_hours", standqa.INTERVAL_HOURS),
        qa_max_tasks=config.get("qa_max_tasks", standqa.MAX_TASKS),
        qa_instructions=config.get("qa_instructions", ""),
        deploy_job=config.get("deploy_job"),
        environment={
            name: item
            for name, item in toolchains.state(row.id).items()
            if name in {"spec", "apt", "feeds", "status", "error", "declared"}
        },
    )
    if len(_settings_cache) > 512:
        _settings_cache.clear()
    _settings_cache[row.id] = (time.monotonic(), key, value)
    return copy.deepcopy(value)


def agent_activity(task_id):
    """Last executor pulse for the live run; the worker raises a stalled event past the same limit."""
    try:
        generation = (ROOT / "locks" / task_id / "generation").read_text().strip()
        run = ROOT / "artifacts" / task_id / "browser" / generation
        started = os.stat(run / "execution.json", follow_symlinks=False).st_mtime
    except (OSError, ValueError):
        return {}
    try:
        last = max(started, os.stat(run / "transcript.jsonl", follow_symlinks=False).st_mtime)
    except OSError:
        last = started
    return {
        "last_activity": last,
        "stall_seconds": max(60, int(os.getenv("DARKFACTORY_STALL_SECONDS", "600"))),
    }
