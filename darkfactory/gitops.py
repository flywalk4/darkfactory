"""Trusted Git operations. Repository scripts run only in task containers."""

import fcntl
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import httpx

from . import warm
from .db import ROOT, decrypt

log = logging.getLogger(__name__)

NETWORK_SECONDS = int(os.getenv("DARKFACTORY_GIT_NETWORK_SECONDS", "1800"))
NETWORK_COMMANDS = {"clone", "fetch", "push", "ls-remote"}
INSTALLED_DIRS = {"node_modules", ".venv", "venv"}
CACHE_PATTERNS = (
    ".npm-cache/",
    ".npm/",
    ".yarn-cache/",
    ".pnpm-store/",
    ".nuget/packages/",
    ".gradle/",
    ".m2/",
    "node_modules/",
    "__pycache__/",
)


# Scratch directories agents create for experiments: tmp/, mktemp-style `.name.XXXXXX/`.
SCRATCH = re.compile(r"(?:^|/)(?:tmp|temp|\.tmp|\.scratch)/|(?:^|/)\.[\w-]+\.[A-Za-z0-9]{6}/")

# Run output: belongs in the run's evidence directory, not in the project's history.
# Proof bundles agents invent live beside reports: scope the broad patterns to documentation directories.
_DOCS = r"(?:^|/)(?:docs|reviews?|reports?)/(?:.*/)?"
EVIDENCE = re.compile(
    r"\.(?:log|har|trace|webm|sha256)$"
    r"|(?:^|/)(?:test-results|playwright-report|coverage|screenshots?|evidence)/"
    + rf"|{_DOCS}(?:logs|correction-\d+)/"
    + rf"|{_DOCS}[^/]*(?:evidence|validation|recheck)[^/]*/(?:.*/)?[^/]+(?<!\.md)$"
    + rf"|{_DOCS}[^/]*evidence[^/]*\.\w+$"
    # Screenshots and tables beside review reports; documentation images elsewhere stay.
    + r"|(?:^|/)(?:reviews?|reports?)/(?:.*/)?[^/]+\.(?:png|jpe?g|webp|gif|tsv|csv|json|zip)$",
    re.IGNORECASE,
)


# The repository hygiene rules of nocode-x scripts/check-repo-hygiene.py, for any project: logs, npm caches,
# evidence, status files and images outside image directories are run output, not source.
IMAGE = re.compile(r"\.(?:png|jpe?g|gif|webp|bmp)$", re.IGNORECASE)
IMAGE_DIRS = {"assets", "shots", "design-refs", "public", "static", "images", "img", "icons", "res"}
CACHE_DIRS = {".npm-cache", ".npm", "_cacache", "npm-cache"}
# Code in a logs/ or evidence/ package (a log viewer, an evidence service) is source.
OUTPUT_DIRS = {"logs", "evidence"}
SOURCE = re.compile(r"\.(?:py|[cm]?[jt]sx?|cs|java|kt|go|rs|rb|php|vue|svelte|html|s?css|md)$", re.IGNORECASE)
STATUS = re.compile(r"(?:^|[-_.])status\.(?:md|json|txt)$|evidence[^/]*\.json$", re.IGNORECASE)


def run_output(path):
    """A new file that is run output by the hygiene rules."""
    *dirs, name = path.split("/")
    dirs = {part.lower() for part in dirs}
    return bool(
        name.lower().endswith(".log")
        or CACHE_DIRS & dirs
        or (OUTPUT_DIRS & dirs and not SOURCE.search(name))
        or STATUS.search(name)
        or (IMAGE.search(name) and not IMAGE_DIRS & dirs)
    )


def max_new_files():
    return max(1, int(os.getenv("DARKFACTORY_MAX_NEW_FILES", "300")))


STALE_LOCK_SECONDS = 120
COMMIT_TITLE_CHARS = 100


class GitError(RuntimeError):
    pass


class Repository:
    def __init__(self, project, task):
        self.project, self.task = project, task
        self.mirror = ROOT / "repositories" / (project.id + ".git")
        self.work = ROOT / "workspaces" / task.id
        self.artifacts = ROOT / "artifacts" / task.id
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.credentials = json.loads(decrypt(project.secret) or "{}")
        self.env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        system = ROOT / "system"
        system.mkdir(parents=True, exist_ok=True)
        askpass = system / "askpass"
        if not askpass.exists():
            askpass.write_text(
                '#!/bin/sh\ncase "$1" in *sername*) echo oauth2;; *) echo "$DF_GIT_TOKEN";; esac\n'
            )
            askpass.chmod(0o700)
        self.env.update(GIT_ASKPASS=str(askpass), DF_GIT_TOKEN=self.credentials.get("git_token", ""))
        self._configure_ssh(system)

    def _configure_ssh(self, system):
        key = self.credentials.get("ssh_private_key", "")
        known_hosts = self.credentials.get("ssh_known_hosts", "")
        if not isinstance(key, str) or not isinstance(known_hosts, str):
            raise GitError("SSH credentials must contain text values")
        if key and not known_hosts.strip():
            raise GitError("ssh_known_hosts is required with ssh_private_key")
        args = [
            "ssh",
            "-F",
            "/dev/null",
            "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes",
            "-oIdentitiesOnly=yes",
            "-oIdentityAgent=none",
            "-oGlobalKnownHostsFile=/dev/null",
            "-oPasswordAuthentication=no",
            "-oKbdInteractiveAuthentication=no",
            "-oPermitLocalCommand=no",
            "-oClearAllForwardings=yes",
            "-oConnectTimeout=10",
        ]
        if key:
            directory = system / (self.project.id + ".ssh")
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
            for name, content in (("identity", key), ("known_hosts", known_hosts)):
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as handle:
                        temporary = Path(handle.name)
                        os.fchmod(handle.fileno(), 0o600)
                        handle.write(content if content.endswith("\n") else content + "\n")
                    temporary.replace(directory / name)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            args.extend(["-i", str(directory / "identity")])
            known_hosts_path = str(directory / "known_hosts").replace("\\", "\\\\").replace('"', '\\"')
            args.append(f'-oUserKnownHostsFile="{known_hosts_path}"')
        else:
            args.extend(["-oIdentityFile=none", "-oUserKnownHostsFile=/dev/null"])
        for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"):
            self.env.pop(name, None)
        self.env.update(GIT_SSH_COMMAND=shlex.join(args), GIT_SSH_VARIANT="ssh")

    def _timeout(self, limit=90):
        deadline = getattr(self.task, "execution_deadline", None)
        remaining = deadline - time.time() if deadline is not None else limit
        if remaining <= 0:
            raise GitError("Task execution deadline exceeded")
        return min(limit, remaining)

    @contextmanager
    def _mirror_lock(self):
        lock_path = ROOT / "system" / f"{self.project.id}.mirror.lock"
        # The holder may be fetching a large repository; waiting only 90 s failed phases spuriously.
        wait_deadline = time.monotonic() + self._timeout(NETWORK_SECONDS)
        with lock_path.open("a+b") as lock:
            while True:
                self._timeout()
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= wait_deadline:
                        raise GitError("Timed out waiting for repository mirror lock") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def git(self, *args, cwd=None, check=True, _retried=False):
        if args and args[0] == "diff":
            args = ("diff", "--no-ext-diff", "--no-textconv", *args[1:])
        proc = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"safe.directory={self.work}",
                "-c",
                "diff.external=",
                "-c",
                "remote.origin.mirror=false",
                "-c",
                "user.name=Darkfactory",
                "-c",
                "user.email=factory@localhost",
                *args,
            ],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            # Network operations of a large repository (first mirror clone, fetch, push) take minutes.
            timeout=self._timeout(NETWORK_SECONDS if args and args[0] in NETWORK_COMMANDS else 90),
            check=False,
        )
        if proc.returncode and not _retried and "index.lock': File exists" in proc.stderr:
            # A git process killed mid-write (a worker restart during a deploy) leaves index.lock behind and
            # every later command of the task fails. The controller lock means nobody else writes this
            # repository now; a lock older than a running command could be is a leftover.
            lock = Path(re.search(r"'([^']*index\.lock)'", proc.stderr).group(1))
            if lock.is_file() and time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                lock.unlink(missing_ok=True)
                return self.git(*args, cwd=cwd, check=check, _retried=True)
        if check and proc.returncode:
            message = proc.stderr
            for value in self.credentials.values():
                if isinstance(value, str) and value:
                    for secret in sorted({value, *value.splitlines()}, key=len, reverse=True):
                        if secret:
                            message = message.replace(secret, "[REDACTED]")
            raise GitError(message[-4000:])
        return proc.stdout.strip() if check else proc

    def refresh(self):
        with self._mirror_lock():
            return self._refresh_locked()

    def _refresh_locked(self):
        if not self.mirror.exists():
            self.mirror.parent.mkdir(parents=True, exist_ok=True)
            # Cloned aside and renamed: an interrupted clone must not leave a half mirror that later
            # refreshes take for a complete one.
            partial = self.mirror.with_name(self.mirror.name + ".partial")
            shutil.rmtree(partial, ignore_errors=True)
            self.git("clone", "--mirror", "--", self.project.remote, str(partial))
            partial.rename(self.mirror)
        # Branches only: forges keep thousands of refs/pull/* and refs/merge-requests/* the factory never reads.
        self.git("fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*", cwd=self.mirror)
        target = self.git(
            "rev-parse", "--verify", f"refs/heads/{self.project.target}", cwd=self.mirror, check=False
        )
        if target.returncode:
            seed = self.git(
                "rev-parse", "--verify", f"refs/heads/{self.project.seed_branch}", cwd=self.mirror
            )
            self.git("push", "origin", f"{seed}:refs/heads/{self.project.target}", cwd=self.mirror)
            self.git("fetch", "origin", "+refs/heads/*:refs/heads/*", cwd=self.mirror)
        return self.git("rev-parse", f"refs/heads/{self.project.target}", cwd=self.mirror)

    def prepare(self):
        target = self.refresh()
        fresh = not (self.work / ".git").exists()
        if fresh:
            self.work.parent.mkdir(parents=True, exist_ok=True)
            if self.work.exists():
                raise GitError("Incomplete workspace exists; manual inspection required")
            with tempfile.TemporaryDirectory(
                dir=self.work.parent, prefix=f".{self.task.id}.clone-"
            ) as staging:
                staging = Path(staging)
                self.git("clone", "--no-hardlinks", "--", str(self.mirror), str(staging))
                self.git("checkout", "-b", self.task.branch, target, cwd=staging)
                self.git("remote", "remove", "origin", cwd=staging)
                if self.work.exists():
                    raise GitError("Workspace appeared during preparation; manual inspection required")
                staging.rename(self.work)
        self.own_files()
        if fresh:
            # After own_files: linked dependency files already belong to the agent user.
            try:
                warm.seed(self.project.id, self.task.id, self.work)
            except Exception:
                log.warning("Could not reuse installed dependencies", exc_info=True)
        return target

    def own_files(self):
        if os.geteuid() != 0:
            return
        for root, dirs, files in os.walk(self.work, followlinks=False):
            # Installed trees are written by the agent's own uid; walking them cost a syscall per file.
            dirs[:] = [d for d in dirs if d != ".git" and d not in INSTALLED_DIRS]
            os.chown(root, 1000, 1000)
            for name in dirs + files:
                os.lchown(Path(root) / name, 1000, 1000)

    def head(self):
        return self.git("rev-parse", "HEAD", cwd=self.work)

    def _pending_merge(self):
        return self.git("rev-parse", "--verify", "MERGE_HEAD", cwd=self.work, check=False).returncode == 0

    def _conflict_guidance(self):
        result = self.git("diff", "--name-only", "--diff-filter=U", "-z", cwd=self.work, check=False)
        if result.returncode:
            raise GitError("Cannot inspect integration conflicts; working files are preserved")
        paths = result.stdout.split("\0")
        paths = [path for path in paths if path]
        if paths:
            return (
                "Integration conflict with preprod in " + json.dumps(paths, ensure_ascii=False) + ". "
                "Conflict markers and the pending merge are preserved. Edit the working files to resolve "
                "all conflict markers; do not run Git commands or change .git. The controller will stage "
                "the resolved files and commit the merge after implementation."
            )
        return None

    def commit(self, title=None):
        """Commit the work under the writer's own title for it; the task title only names the task.

        A pending merge is committed first, as a merge of the resolved files only: task changes made in the
        same run go into their own commit, so the merge does not carry them under its title."""
        self.exclude_caches()
        if self._pending_merge():
            conflicted = self.git("diff", "--name-only", "--diff-filter=U", "-z", cwd=self.work, check=False)
            paths = [path for path in conflicted.stdout.split("\0") if path]
            if paths:
                self.git("add", "--", *paths, cwd=self.work)
            message = f"Merge {self.project.target} into {self.task.branch}"
            self.git("commit", "-m", message, cwd=self.work)
        self.git("add", "-A", cwd=self.work)
        if self.git("diff", "--cached", "--quiet", cwd=self.work, check=False).returncode:
            title = " ".join(str(title or "").split())[:COMMIT_TITLE_CHARS].strip()
            message = f"{title}\n\nfactory: {self.task.title}" if title else f"factory: {self.task.title}"
            self.git("commit", "-m", message, cwd=self.work)
        return self.head()

    def _added(self):
        self.exclude_caches()
        self.git("add", "-A", cwd=self.work)
        base = getattr(self.task, "target_sha", None) or "HEAD"
        added = self.git(
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "--diff-filter=A",
            base,
            cwd=self.work,
            check=False,
        )
        return added.stdout.splitlines() if added.returncode == 0 else []

    def tidy(self):
        """Move new scratch and run-output files out of the candidate into the task's artifacts.

        Agents leave logs, screenshots and experiment directories behind; sending them back to clean
        up costs a whole correction cycle, so the factory removes them itself and keeps a copy.
        """
        names = [
            name
            for name in self._added()
            if SCRATCH.search(name) or EVIDENCE.search(name) or run_output(name)
        ]
        keep = self.artifacts / "run-output"
        for name in names:
            source = self.work / name
            target = keep / name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(source, target)
            except OSError:
                source.unlink(missing_ok=True)
        for name in names:
            parent = (self.work / name).parent
            while parent != self.work and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        if names:
            self.git("add", "-A", cwd=self.work)
        return names

    def junk(self):
        """Too many new files is a sign of generated output: say what to remove instead of committing it."""
        names = self._added()
        if len(names) > max_new_files():
            tops = {}
            for name in names:
                tops[name.split("/")[0]] = tops.get(name.split("/")[0], 0) + 1
            biggest = ", ".join(
                f"{key} ({count})" for key, count in sorted(tops.items(), key=lambda x: -x[1])[:5]
            )
            return (
                f"Изменение добавляет {len(names)} новых файлов (лимит {max_new_files()}): {biggest}. "
                "Убери сгенерированные, временные и скачанные файлы; оставь только нужное задаче."
            )
        return None

    def exclude_caches(self):
        """Tool caches agents create in the workspace never belong in a factory commit."""
        path = self.work / ".git" / "info" / "exclude"
        current = path.read_text() if path.is_file() else ""
        missing = [item for item in CACHE_PATTERNS if item not in current.splitlines()]
        if missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                current + ("" if current.endswith("\n") or not current else "\n") + "\n".join(missing) + "\n"
            )

    def integrate(self):
        if self._pending_merge():
            self.own_files()
            raise GitError(
                self._conflict_guidance()
                or (
                    "Pending integration merge is preserved. Finish editing the working files; do not run "
                    "Git commands or change .git. The controller must commit the merge after implementation."
                )
            )
        target = self.refresh()
        self.git("fetch", str(self.mirror), f"refs/heads/{self.project.target}", cwd=self.work)
        result = self.git("merge", "--no-edit", target, cwd=self.work, check=False)
        if result.returncode:
            conflicts = self._conflict_guidance()
            if conflicts:
                self.own_files()
                raise GitError(conflicts)
            if self._pending_merge():
                self.git("merge", "--abort", cwd=self.work)
            raise GitError(
                "Integration with preprod failed without file conflicts; working drafts are preserved"
            )
        self.own_files()
        sha = self.head()
        self.write_diff(target, sha)
        return target, sha

    def write_diff(self, target, sha):
        """The change the reviewer reads: candidate against the preprod it is based on. A candidate that
        merged a newer preprod (a resolved conflict, maybe with commits on top) would otherwise show
        another task's merged work as its own, and review rejected it for that. A three-dot diff against
        the current preprod tip leaves out everything the candidate took from preprod."""
        base = target
        if self.mirror.is_dir():
            fetched = self.git(
                "fetch",
                "--no-tags",
                str(self.mirror),
                f"refs/heads/{self.project.target}",
                cwd=self.work,
                check=False,
            )
            if fetched.returncode == 0:
                fork = self.git("merge-base", "FETCH_HEAD", sha, cwd=self.work, check=False)
                newer = (
                    fork.returncode == 0
                    and self.git(
                        "merge-base", "--is-ancestor", target, fork.stdout.strip(), cwd=self.work, check=False
                    ).returncode
                    == 0
                )
                if newer:
                    base = fork.stdout.strip()
        if base == target:
            merged = self.git("rev-parse", "--verify", "-q", f"{sha}^2", cwd=self.work, check=False)
            if (
                merged.returncode == 0
                and self.git(
                    "merge-base", "--is-ancestor", target, merged.stdout.strip(), cwd=self.work, check=False
                ).returncode
                == 0
            ):
                base = merged.stdout.strip()
        # Unquoted paths: non-ASCII names stay readable to the reviewer and to the claims check.
        diff = self.git(
            "-c", "core.quotepath=off", "diff", "--no-ext-diff", "--no-textconv", base, sha, cwd=self.work
        )
        (self.artifacts / "diff.patch").write_text(diff + "\n")

    def publish(self, sha):
        self.git("push", self.project.remote, f"{sha}:refs/heads/{self.task.branch}", cwd=self.work)
        if self.project.forge == "git":
            return f"/api/tasks/{self.task.id}"
        target = self.refresh()
        if sha == target:
            return f"/api/tasks/{self.task.id}"
        self.git("fetch", str(self.mirror), f"refs/heads/{self.project.target}", cwd=self.work)
        difference = self.git("diff", "--quiet", target, sha, cwd=self.work, check=False)
        if difference.returncode == 0:
            return f"/api/tasks/{self.task.id}"
        if difference.returncode != 1:
            raise GitError("Cannot compare candidate with current preprod")
        base = self.project.forge_url.rstrip("/")
        repo = self.project.forge_repo
        token = self.credentials.get("forge_token", "")
        with httpx.Client(timeout=30) as client:
            if self.project.forge == "github":
                headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
                endpoint = base + "/repos/" + repo + "/pulls"
                response = client.get(
                    endpoint,
                    headers=headers,
                    params={
                        "head": repo.split("/")[0] + ":" + self.task.branch,
                        "base": self.project.target,
                        "state": "all",
                    },
                )
                response.raise_for_status()
                existing = response.json()
                if existing:
                    return existing[0]["html_url"]
                response = client.post(
                    endpoint,
                    headers=headers,
                    json={
                        "title": self.task.title,
                        "head": self.task.branch,
                        "base": self.project.target,
                        "body": "Created by Darkfactory. Acceptance evidence is stored in the factory task.",
                    },
                )
                response.raise_for_status()
                return response.json()["html_url"]
            headers = {"PRIVATE-TOKEN": token}
            endpoint = base + "/projects/" + quote(repo, safe="") + "/merge_requests"
            response = client.get(
                endpoint,
                headers=headers,
                params={
                    "source_branch": self.task.branch,
                    "target_branch": self.project.target,
                    "state": "all",
                },
            )
            response.raise_for_status()
            if response.json():
                return response.json()[0]["web_url"]
            response = client.post(
                endpoint,
                headers=headers,
                json={
                    "title": self.task.title,
                    "source_branch": self.task.branch,
                    "target_branch": self.project.target,
                },
            )
            response.raise_for_status()
            return response.json()["web_url"]

    def merge(self, sha, expected):
        if self.project.target != "preprod":
            raise GitError("Automatic merge target must be preprod")
        current = self.refresh()
        if current == sha:
            self.drop_branch()
            return True  # Restart after successful push, before acceptance transaction.
        if current != expected:
            return False
        if self.git("merge-base", "--is-ancestor", current, sha, cwd=self.work, check=False).returncode:
            raise GitError("Candidate does not include current preprod")
        result = self.git(
            "push",
            f"--force-with-lease=refs/heads/preprod:{expected}",
            self.project.remote,
            f"{sha}:refs/heads/preprod",
            cwd=self.work,
            check=False,
        )
        if result.returncode:
            if self.refresh() != expected:
                return False
            raise GitError("Push to preprod failed; check repository permissions")
        self.drop_branch()
        return True

    def drop_branch(self):
        """Merged work lives in preprod; the task branch is only noise in the remote. Best effort."""
        branch = self.task.branch or ""
        if not branch.startswith("darkfactory/") or branch in {"preprod", "main", self.project.seed_branch}:
            return False
        cwd = self.mirror if self.mirror.is_dir() else self.work
        result = self.git("push", self.project.remote, f":refs/heads/{branch}", cwd=cwd, check=False)
        if self.mirror.is_dir():
            self.git("update-ref", "-d", f"refs/heads/{branch}", cwd=self.mirror, check=False)
        return result.returncode == 0

    def main_preview(self, sha):
        """Inspect a frozen, already accepted commit; never move a branch on preview."""
        with self._mirror_lock():
            self._refresh_locked()
            return self._main_preview_locked(sha)

    def _main_preview_locked(self, sha):
        main = self.git("rev-parse", "--verify", "refs/heads/main", cwd=self.mirror)
        self.git("cat-file", "-e", sha + "^{commit}", cwd=self.mirror)
        fast_forward = not self.git(
            "merge-base", "--is-ancestor", main, sha, cwd=self.mirror, check=False
        ).returncode
        already = (
            main == sha
            or not self.git("merge-base", "--is-ancestor", sha, main, cwd=self.mirror, check=False).returncode
        )
        tree, conflicts = sha, []
        if not fast_forward and not already:
            # main has its own commits: preview the merge commit the owner would create.
            merged = self.git(
                "merge-tree", "--write-tree", "--name-only", main, sha, cwd=self.mirror, check=False
            )
            lines = merged.stdout.splitlines()
            if merged.returncode == 0 and lines:
                tree = lines[0]
            else:
                # Conflicted paths come first; a blank line separates git's informational messages.
                names = lines[1 : lines.index("")] if "" in lines else lines[1:]
                tree, conflicts = None, sorted(set(names))[:50]
        diff = self.git("diff", "--stat", main, tree, cwd=self.mirror) if tree else ""
        patch = self.git("diff", main, tree, cwd=self.mirror) if tree else ""
        return {
            "main_sha": main,
            "can_merge": tree is not None,
            "mode": "fast_forward" if fast_forward else "merge",
            "tree": tree,
            "conflicts": conflicts,
            "already_merged": already,
            "diff_stat": diff[:20000],
            "diff": patch[:100000],
            "truncated": len(patch) > 100000,
            "includes_preprod": True,
        }

    def sync_main(self):
        """Bring commits that reached main outside the factory into preprod; never rewrites either branch."""
        with self._mirror_lock():
            self._refresh_locked()
            source, target = self.project.seed_branch, self.project.target
            main = self.git("rev-parse", "--verify", f"refs/heads/{source}", cwd=self.mirror)
            found = self.git("rev-parse", "--verify", f"refs/heads/{target}", cwd=self.mirror, check=False)
            preprod = found.stdout.strip()
            result = {"main": main, "preprod": preprod, "conflicts": []}
            if found.returncode:
                return {**result, "state": "no_target"}
            if not self.git(
                "merge-base", "--is-ancestor", main, preprod, cwd=self.mirror, check=False
            ).returncode:
                return {**result, "state": "up_to_date"}
            if not self.git(
                "merge-base", "--is-ancestor", preprod, main, cwd=self.mirror, check=False
            ).returncode:
                head = main
            else:
                merged = self.git(
                    "merge-tree", "--write-tree", "--name-only", preprod, main, cwd=self.mirror, check=False
                )
                lines = merged.stdout.splitlines()
                if merged.returncode or not lines:
                    names = lines[1 : lines.index("")] if "" in lines else lines[1:]
                    return {**result, "state": "conflict", "conflicts": sorted(set(names))[:50]}
                head = self.git(
                    "commit-tree",
                    lines[0],
                    "-p",
                    preprod,
                    "-p",
                    main,
                    "-m",
                    f"Merge {source} {main[:12]} into {target}",
                    cwd=self.mirror,
                )
            pushed = self.git(
                "push",
                f"--force-with-lease=refs/heads/{target}:{preprod}",
                "origin",
                f"{head}:refs/heads/{target}",
                cwd=self.mirror,
                check=False,
            )
            if pushed.returncode:
                # preprod moved meanwhile (a task merged): the next round merges main into the new head.
                return {**result, "state": "raced"}
            self.git("update-ref", f"refs/heads/{target}", head, cwd=self.mirror)
            return {**result, "state": "merged", "preprod": head}

    def merge_main(self, sha):
        """Start the main -> preprod merge in a fresh workspace; conflicts stay as markers for the writer."""
        self.git("fetch", "--no-tags", str(self.mirror), sha, cwd=self.work)
        result = self.git("merge", "--no-edit", "--no-ff", sha, cwd=self.work, check=False)
        self.own_files()
        if result.returncode and not self._pending_merge():
            raise GitError("Слияние main в preprod не началось: " + (result.stderr or result.stdout)[-500:])
        return self._conflict_guidance()

    def promote_main(self, sha, expected_main):
        """Called only by the authenticated human decision endpoint. Fail closed on races."""
        with self._mirror_lock():
            self._refresh_locked()
            preview = self._main_preview_locked(sha)
            if preview["already_merged"]:
                return
            if preview["main_sha"] != expected_main:
                raise GitError("Ветка main изменилась; заново открой предложение слияния")
            if not preview["can_merge"]:
                raise GitError("Конфликт main и preprod: " + ", ".join(preview["conflicts"][:10]))
            target = sha
            if preview["mode"] == "merge":
                target = self.git(
                    "commit-tree",
                    preview["tree"],
                    "-p",
                    expected_main,
                    "-p",
                    sha,
                    "-m",
                    f"Merge preprod {sha[:12]} into main",
                    cwd=self.mirror,
                )
            self.git(
                "push",
                f"--force-with-lease=refs/heads/main:{expected_main}",
                "origin",
                f"{target}:refs/heads/main",
                cwd=self.mirror,
            )
