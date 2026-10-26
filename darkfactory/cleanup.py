"""Finished tasks give back their disk: the workspace clone and the remote task branch."""

import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import docker
from sqlalchemy import select

from . import toolchains
from .db import ROOT, Project, Session, Task, transaction
from .gitops import Repository

log = logging.getLogger(__name__)
NOTES_FILE = "notes.md"
FINISHED = ("merged", "cancelled", "duplicate", "checked")
INTERVAL = 600
# The running worker reads logs and removes its container right after exit.
ORPHAN_GRACE = 1800
_last = 0.0


def retention_seconds():
    return max(0.0, float(os.getenv("DARKFACTORY_WORKSPACE_RETENTION_HOURS", "24"))) * 3600


def artifact_seconds():
    """Run evidence (transcripts, screenshots, traces) of finished tasks is kept this long."""
    return max(0.0, float(os.getenv("DARKFACTORY_ARTIFACT_RETENTION_DAYS", "30"))) * 86400


def cache_limit_bytes():
    return max(0.0, float(os.getenv("DARKFACTORY_CACHE_LIMIT_GB", "60"))) * 1024**3


def _size(path):
    """Disk usage of a directory tree in bytes; 0 when it cannot be measured."""
    proc = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, timeout=600)
    try:
        return int(proc.stdout.split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


# Project facts and install stamps are tiny and expensive to lose; package caches are refetched.
CACHE_KEEP = {NOTES_FILE, "setup"}


def trim_caches(limit=None):
    """Package caches of every project share one disk budget; the projects used least recently give theirs
    back first. Returns the project ids whose caches were emptied."""
    base = ROOT / "caches"
    limit = cache_limit_bytes() if limit is None else limit
    if not limit or not base.is_dir():
        return []
    projects = [path for path in base.iterdir() if path.is_dir()]
    sizes = {path: _size(path) for path in projects}
    total = sum(sizes.values())
    trimmed = []

    def used(path):
        # Setup stamps are rewritten by every install; the directory's own mtime covers the rest.
        stamps = path / "setup"
        return max(path.stat().st_mtime, stamps.stat().st_mtime if stamps.is_dir() else 0)

    for path in sorted(projects, key=used):
        if total <= limit:
            break
        for item in path.iterdir():
            if item.name not in CACHE_KEEP:
                shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
        total -= sizes[path] - _size(path)
        trimmed.append(path.name)
    if trimmed:
        log.info("Emptied package caches of %d projects to stay under the cache limit", len(trimmed))
    return trimmed


def expire_artifacts():
    """Drop run directories of finished tasks older than the artifact retention; the task's summary files
    (handoff, diff, review, checks) stay for its page."""
    browser_dirs = list((ROOT / "artifacts").glob("*/browser"))
    if not browser_dirs:
        return
    cutoff = time.time() - artifact_seconds()
    with Session() as db:
        finished = set(
            db.scalars(
                select(Task.id).where(
                    Task.id.in_([path.parent.name for path in browser_dirs]),
                    Task.state.in_(FINISHED),
                    Task.updated < cutoff,
                )
            )
        )
    for path in browser_dirs:
        if path.parent.name in finished:
            shutil.rmtree(path, ignore_errors=True)


def orphans(client, grace=ORPHAN_GRACE):
    """Remove agent containers that exited long ago; a worker restart mid-run skips its own cleanup."""
    removed = []
    cutoff = time.time() - grace
    for container in client.containers.list(
        all=True, filters={"label": "darkfactory.task", "status": "exited"}
    ):
        finished = (container.attrs.get("State") or {}).get("FinishedAt", "")
        try:
            at = datetime.fromisoformat(finished[:19]).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if at < cutoff:
            try:
                container.remove(force=True)
                removed.append(container.name)
            except Exception:
                continue
    return removed


def sweep(force=False, retention=None):
    """Remove workspaces of tasks finished longer than the retention ago; return removed task ids."""
    global _last
    if not force and time.monotonic() - _last < INTERVAL:
        return []
    _last = time.monotonic()
    base = ROOT / "workspaces"
    if not base.is_dir():
        return []
    cutoff = time.time() - (retention_seconds() if retention is None else retention)
    present = {path.name for path in base.iterdir() if path.is_dir() and not path.name.startswith(".")}
    removed = []
    with Session() as db:
        rows = db.execute(
            select(Task.id, Task.project_id, Task.branch, Task.state).where(
                Task.id.in_(present), Task.state.in_(FINISHED), Task.updated < cutoff
            )
        ).all()
        projects = {row.project_id: db.get(Project, row.project_id) for row in rows}
    for task_id, project_id, branch, state in rows:
        project = projects[project_id]
        if project and state == "merged":
            # Branches of tasks merged before they were dropped on merge; preprod holds the work.
            try:
                Repository(project, SimpleNamespace(id=task_id, branch=branch)).drop_branch()
            except Exception:
                log.exception("Could not drop branch %s", branch)
        trash = []
        with transaction() as db:
            task = db.get(Task, task_id)
            if task.state not in FINISHED:
                continue
            task.role_config = {**(task.role_config or {}), "workspace_swept": True}
            # Renamed inside the transaction, deleted after it: removing gigabytes of node_modules while
            # holding the global lock stalled every heartbeat and claim.
            for path in (base / task_id, ROOT / "sessions" / task_id):
                if path.exists():
                    target = path.with_name(f".trash-{path.name}-{time.monotonic_ns()}")
                    path.rename(target)
                    trash.append(target)
        for path in trash:
            shutil.rmtree(path, ignore_errors=True)
        (ROOT / "caches" / project_id / "setup" / task_id).unlink(missing_ok=True)
        removed.append(task_id)
    # Trash left by an interrupted sweep.
    for path in [*base.glob(".trash-*"), *(ROOT / "sessions").glob(".trash-*")]:
        shutil.rmtree(path, ignore_errors=True)
    expire_artifacts()
    try:
        trim_caches()
    except (OSError, subprocess.SubprocessError):
        log.warning("Could not trim package caches", exc_info=True)
    # Interrupted clones never become workspaces.
    for path in base.glob(".*.clone-*"):
        if path.stat().st_mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)
    if removed:
        log.info("Removed %d finished workspaces", len(removed))
    try:
        client = docker.from_env()
        if containers := orphans(client):
            log.info("Removed %d exited agent containers", len(containers))
        with toolchains.gc_lock(exclusive=True, wait=False) as free:
            if free and (images := toolchains.prune(client)):
                log.info("Removed toolchain images %s", ", ".join(images))
    except Exception:
        log.warning("Could not prune agent containers or toolchain images", exc_info=True)
    reclaim()
    return removed


def reclaim():
    """Drop Docker garbage of any origin: dangling images and day-old build cache, which every deploy leaves."""
    if toolchains.building():
        return 0
    # Exclusive against every build: pruning snapshots under a running build is what breaks it with
    # containerd's "failed to export layer ... no such file or directory".
    with toolchains.gc_lock(exclusive=True, wait=False) as free:
        if not free:
            return 0
        try:
            client = docker.from_env()
            images = client.images.prune(filters={"dangling": True}).get("SpaceReclaimed") or 0
            cache = client.api.prune_builds(filters={"until": "24h"}).get("SpaceReclaimed") or 0
        except Exception:
            log.warning("Could not prune Docker images or build cache", exc_info=True)
            return 0
    if images or cache:
        log.info("Reclaimed %.1f GB of dangling images and build cache", (images + cache) / 1024**3)
    return images + cache
