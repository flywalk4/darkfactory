"""A fresh workspace borrows installed dependencies from the project's latest workspace.

Setup (npm ci, dotnet restore, pip install) takes minutes on a clean clone. The dependency folders of an
earlier workspace of the same project are hard-linked into the new clone together with that task's setup
marker, so the runner skips setup while the dependency manifests are unchanged and reinstalls otherwise.
Installers replace files rather than editing them in place, so linked package folders stay independent.
Build output is different: MSBuild and Roslyn rewrite obj/ files in place, so obj/ is copied, never linked,
and tool caches inside node_modules (.cache) are dropped instead of shared.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

from sqlalchemy import select

from .db import ROOT, Session, Task

log = logging.getLogger(__name__)
# Written in place by their tools: a hard link would let one task's build corrupt another's workspace.
COPIED_DIRS = {"obj", "bin"}


def donor(project_id, task_id):
    """The most recently updated other task of the project whose workspace still holds a setup marker."""
    setup = ROOT / "caches" / project_id / "setup"
    if not setup.is_dir():
        return None
    marked = {path.name for path in setup.iterdir()} - {task_id}
    if not marked:
        return None
    with Session() as db:
        ids = db.scalars(
            select(Task.id)
            .where(Task.project_id == project_id, Task.id.in_(marked))
            .order_by(Task.updated.desc())
        ).all()
    return next((other for other in ids if (ROOT / "workspaces" / other / ".git").exists()), None)


def dependency_dirs(work):
    """Every directory Git ignores in a workspace, relative to it: whatever setup created (node_modules,
    obj, a venv under .state/, tool caches). A copied setup stamp is only true when all of it comes along;
    a fixed list missed project-specific ones and skipped setup with a missing virtualenv."""
    proc = subprocess.run(
        ["git", "-c", f"safe.directory={work}", "ls-files", "-o", "-i", "--exclude-standard", "--directory"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        return []
    return [line.rstrip("/") for line in proc.stdout.splitlines() if line.endswith("/") and line != ".git/"]


def seed(project_id, task_id, work):
    """Link the donor's dependency folders and setup marker into a fresh workspace; return the donor id."""
    source = donor(project_id, task_id)
    if not source:
        return None
    origin = ROOT / "workspaces" / source
    linked, complete = [], True
    for relative in dependency_dirs(origin):
        target = work / relative
        if target.exists() or not target.parent.is_dir():
            continue
        # Only folders Git ignores: a tracked folder of that name is source code. The trailing slash
        # matches "node_modules/" patterns while the folder does not exist yet.
        ignored = subprocess.run(
            ["git", "-c", f"safe.directory={work}", "check-ignore", "-q", relative + "/"], cwd=work
        )
        if ignored.returncode:
            continue
        mode = "-a" if COPIED_DIRS & set(Path(relative).parts) else "-al"
        copied = subprocess.run(["cp", mode, str(origin / relative), str(target)], capture_output=True)
        if not copied.returncode and os.path.basename(relative) == "node_modules":
            shutil.rmtree(target / ".cache", ignore_errors=True)
        if copied.returncode:
            shutil.rmtree(target, ignore_errors=True)
            log.warning("Could not link %s from %s", relative, source)
            complete = False
            continue
        linked.append(relative)
    # The stamp says "everything setup made is here": only true when nothing failed to come along.
    if linked and complete:
        setup = ROOT / "caches" / project_id / "setup"
        shutil.copyfile(setup / source, setup / task_id)
    return source if linked else None
