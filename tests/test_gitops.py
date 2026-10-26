import concurrent.futures
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from darkfactory import gitops


@pytest.fixture
def repository_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(gitops, "ROOT", tmp_path / "factory 'quoted'")
    monkeypatch.setattr(gitops, "decrypt", lambda value: value)

    def create(credentials=None, remote="https://example.invalid/repo.git", task_id="task1"):
        project = SimpleNamespace(
            id="project1",
            secret=json.dumps(credentials or {}),
            remote=remote,
            target="preprod",
            seed_branch="main",
        )
        task = SimpleNamespace(id=task_id, branch="darkfactory/" + task_id, title="Test fix")
        return gitops.Repository(project, task)

    return create


def test_ssh_credentials_are_private_and_strict(repository_factory, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/host/ssh-agent.sock")
    monkeypatch.setenv("SSH_AGENT_PID", "123")
    monkeypatch.setenv("SSH_ASKPASS", "/host/askpass")
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-key-material\n-----END OPENSSH PRIVATE KEY-----"
    hosts = "git.example.org ssh-ed25519 AAAAexample"
    repo = repository_factory({"ssh_private_key": key, "ssh_known_hosts": hosts})
    args = shlex.split(repo.env["GIT_SSH_COMMAND"])
    identity = Path(args[args.index("-i") + 1])
    option = next(arg.split("=", 1)[1] for arg in args if arg.startswith("-oUserKnownHostsFile="))
    known_hosts = Path(shlex.split(option)[0])
    assert identity.read_text() == key + "\n"
    assert known_hosts.read_text() == hosts + "\n"
    assert identity.stat().st_mode & 0o777 == 0o600
    assert known_hosts.stat().st_mode & 0o777 == 0o600
    assert identity.parent.stat().st_mode & 0o777 == 0o700
    assert identity.is_relative_to(gitops.ROOT / "system")
    assert args[:3] == ["ssh", "-F", "/dev/null"]
    assert "-oBatchMode=yes" in args
    assert "-oStrictHostKeyChecking=yes" in args
    assert "-oIdentitiesOnly=yes" in args
    assert "-oIdentityAgent=none" in args
    assert "-oGlobalKnownHostsFile=/dev/null" in args
    assert "SSH_AUTH_SOCK" not in repo.env
    assert "SSH_AGENT_PID" not in repo.env
    assert "SSH_ASKPASS" not in repo.env
    assert key not in repo.env["GIT_SSH_COMMAND"]


@pytest.mark.skipif(shutil.which("ssh") is None, reason="OpenSSH client is unavailable")
def test_openssh_preserves_quoted_credential_paths(repository_factory):
    repo = repository_factory(
        {"ssh_private_key": "key", "ssh_known_hosts": "git.example.org ssh-ed25519 AAAA"}
    )
    args = shlex.split(repo.env["GIT_SSH_COMMAND"])
    identity = Path(args[args.index("-i") + 1])
    result = subprocess.run(args + ["-G", "git.example.org"], check=True, capture_output=True, text=True)
    assert "identityfile " + str(identity) in result.stdout.splitlines()
    assert "userknownhostsfile " + str(identity.parent / "known_hosts") in result.stdout.splitlines()


def test_ssh_key_requires_known_hosts(repository_factory):
    with pytest.raises(gitops.GitError, match="ssh_known_hosts is required"):
        repository_factory({"ssh_private_key": "secret-key"})


def test_without_ssh_key_never_uses_host_identity(repository_factory):
    repo = repository_factory()
    args = shlex.split(repo.env["GIT_SSH_COMMAND"])
    assert "-oIdentityFile=none" in args
    assert "-oIdentityAgent=none" in args
    assert "-oUserKnownHostsFile=/dev/null" in args


def test_git_error_redacts_multiline_secrets_before_truncation(repository_factory, monkeypatch):
    secret_line = "private" * 1000
    key = "-----BEGIN PRIVATE KEY-----\n" + secret_line + "\n-----END PRIVATE KEY-----"
    token = "secret-http-token"
    repo = repository_factory(
        {"ssh_private_key": key, "ssh_known_hosts": "git.example.org ssh-ed25519 AAAA", "git_token": token}
    )
    monkeypatch.setattr(
        gitops.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="error: " + key + " " + token),
    )
    with pytest.raises(gitops.GitError) as raised:
        repo.git("fetch")
    assert token not in str(raised.value)
    assert "private" not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)


def test_deadline_and_exact_safe_directory(repository_factory, monkeypatch):
    repo = repository_factory()
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(gitops.subprocess, "run", run)
    assert repo.git("status") == "ok"
    assert "safe.directory=" + str(repo.work) in calls[0][0]
    assert calls[0][1]["timeout"] == 90
    repo.task.execution_deadline = time.time() + 2
    repo.git("diff", "HEAD")
    assert 0 < calls[-1][1]["timeout"] <= 2
    assert "--no-ext-diff" in calls[-1][0]
    assert "--no-textconv" in calls[-1][0]
    repo.task.execution_deadline = time.time() - 1
    with pytest.raises(gitops.GitError, match="deadline exceeded"):
        repo.git("status")
    assert len(calls) == 2


def test_real_git_concurrent_refresh_integrate_and_idempotent_merge(repository_factory, tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args, cwd=source):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args],
            cwd=cwd,
            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-b", "main")
    (source / "value.txt").write_text("before\n")
    git("add", ".")
    git("commit", "-m", "Initial")
    remote = tmp_path / "remote.git"
    git("clone", "--bare", str(source), str(remote))
    repos = [repository_factory(remote=str(remote), task_id="task" + str(index)) for index in range(2)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        targets = list(pool.map(lambda repo: repo.refresh(), repos))
    assert targets[0] == targets[1]
    repo = repos[0]
    target = repo.prepare()
    assert target == targets[0]
    (repo.work / "value.txt").write_text("after\n")
    candidate = repo.commit()
    assert candidate != target
    assert repo.integrate() == (target, candidate)
    assert (repo.artifacts / "diff.patch").read_text().endswith("+after\n")
    assert repo.merge(candidate, target)
    assert repo.merge(candidate, target)
    assert git("rev-parse", "refs/heads/preprod", cwd=remote) == candidate


@pytest.mark.parametrize("resolve_with_ours", [False, True])
def test_conflict_workfiles_survive_until_controller_commits_merge(
    publish_repository, repository_factory, monkeypatch, resolve_with_ours
):
    first = publish_repository
    main = first.head()
    (first.work / "value.txt").write_text("base\n")
    base = first.commit()
    target, _ = first.integrate()
    assert first.merge(base, target)
    second = repository_factory(remote=first.project.remote, task_id="parallel-task")
    assert second.prepare() == base
    (first.work / "value.txt").write_text("task one\n")
    first_change = first.commit()
    target, _ = first.integrate()
    assert first.merge(first_change, target)
    (second.work / "value.txt").write_text("task two\n")
    second_change = second.commit()
    if not resolve_with_ours:
        (second.work / "draft.txt").write_text("uncommitted implementation\n")
    owned = []
    own_files = second.own_files

    def record_ownership():
        owned.append(True)
        own_files()

    monkeypatch.setattr(second, "own_files", record_ownership)
    with pytest.raises(gitops.GitError) as conflict:
        second.integrate()
    assert owned == [True]
    assert "value.txt" in str(conflict.value)
    assert "Edit the working files" in str(conflict.value)
    assert "controller" in str(conflict.value)
    markers = (second.work / "value.txt").read_text()
    assert "<<<<<<<" in markers and "=======" in markers and ">>>>>>>" in markers
    assert "task one" in markers and "task two" in markers
    assert second.git("rev-parse", "MERGE_HEAD", cwd=second.work) == first_change
    assert second.git("ls-files", "-u", cwd=second.work)
    # A repeated integration attempt must preserve the same index and workfiles.
    with pytest.raises(gitops.GitError, match="value.txt"):
        second.integrate()
    assert (second.work / "value.txt").read_text() == markers
    assert second.git("rev-parse", "MERGE_HEAD", cwd=second.work) == first_change

    resolved = "task two\n" if resolve_with_ours else "task one and task two\n"
    (second.work / "value.txt").write_text(resolved)
    if resolve_with_ours:
        # The trusted controller stages files; the agent only edits their content.
        second.git("add", "-A", cwd=second.work)
        assert second.git("diff", "--cached", "--quiet", cwd=second.work, check=False).returncode == 0
    merged = second.commit("Keep both values")
    assert merged != second_change
    # The merge carries only the resolved files; the draft is task work and gets its own commit.
    merge = merged if resolve_with_ours else second.git("rev-parse", "HEAD~1", cwd=second.work)
    assert second.git("rev-list", "--parents", "-n", "1", merge, cwd=second.work).split() == [
        merge,
        second_change,
        first_change,
    ]
    assert (
        second.git("log", "-1", "--format=%s", merge, cwd=second.work)
        == "Merge preprod into darkfactory/parallel-task"
    )
    if not resolve_with_ours:
        assert second.git("diff", "--name-only", merge, merged, cwd=second.work) == "draft.txt"
        assert second.git("log", "-1", "--format=%B", merged, cwd=second.work) == (
            "Keep both values\n\nfactory: Test fix"
        )
    assert not second._pending_merge()
    assert second.integrate() == (first_change, merged)
    assert second.git("diff", "--check", first_change, merged, cwd=second.work) == ""
    assert (second.work / "value.txt").read_text() == resolved
    if not resolve_with_ours:
        assert (second.work / "draft.txt").read_text() == "uncommitted implementation\n"
        assert second.git("show", "HEAD:draft.txt", cwd=second.work) == "uncommitted implementation"
    assert second.merge(merged, first_change)
    remote = Path(first.project.remote)
    assert second.git("rev-parse", "refs/heads/preprod", cwd=remote) == merged
    assert second.git("rev-parse", "refs/heads/main", cwd=remote) == main


def test_unrelated_merge_failure_preserves_drafts_without_pending_merge(
    publish_repository, repository_factory
):
    repo = publish_repository
    before = repo.head()
    (repo.work / "draft.txt").write_text("uncommitted draft\n")
    peer = repository_factory(remote=repo.project.remote, task_id="parallel-task")
    target = peer.prepare()
    (peer.work / "draft.txt").write_text("tracked preprod version\n")
    candidate = peer.commit()
    assert peer.merge(candidate, target)
    # Git refuses to overwrite an existing untracked draft, without starting a merge.
    with pytest.raises(gitops.GitError, match="without file conflicts"):
        repo.integrate()
    assert not repo._pending_merge()
    assert repo.head() == before
    assert (repo.work / "draft.txt").read_text() == "uncommitted draft\n"


def test_nonconflict_merge_commit_failure_cleans_only_new_merge_state(
    publish_repository, repository_factory, monkeypatch
):
    repo = publish_repository
    target = repo.head()
    peer = repository_factory(remote=repo.project.remote, task_id="parallel-task")
    peer.prepare()
    (peer.work / "peer.txt").write_text("preprod change\n")
    candidate = peer.commit()
    assert peer.merge(candidate, target)
    (repo.work / "own.txt").write_text("task change\n")
    own = repo.commit()
    (repo.work / "draft.txt").write_text("uncommitted draft\n")
    repo.git("config", "commit.gpgsign", "true", cwd=repo.work)
    repo.git("config", "gpg.program", "/usr/bin/false", cwd=repo.work)
    aborted = []
    original = repo.git

    def inspect_abort(*args, **kwargs):
        if args == ("merge", "--abort"):
            aborted.append(repo._pending_merge())
        return original(*args, **kwargs)

    monkeypatch.setattr(repo, "git", inspect_abort)
    with pytest.raises(gitops.GitError, match="without file conflicts"):
        repo.integrate()
    assert aborted == [True]
    assert not repo._pending_merge()
    assert repo.head() == own
    assert (repo.work / "draft.txt").read_text() == "uncommitted draft\n"
    assert (repo.work / "own.txt").read_text() == "task change\n"


@pytest.fixture
def publish_repository(repository_factory, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (
        ["init", "-b", "main"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "--allow-empty",
            "-m",
            "Initial",
        ],
    ):
        subprocess.run(["git", *args], cwd=source, env=env, capture_output=True, text=True, check=True)
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)], env=env, capture_output=True, check=True
    )
    repo = repository_factory(remote=str(remote))
    repo.project.forge_url = "https://forge.invalid/api"
    repo.project.forge_repo = "company/project"
    repo.prepare()
    return repo


@pytest.mark.parametrize("forge", ["github", "gitlab"])
@pytest.mark.parametrize("empty_commit", [False, True])
def test_publish_no_diff_uses_internal_record_without_forge(
    publish_repository, monkeypatch, forge, empty_commit
):
    repo = publish_repository
    repo.project.forge = forge
    target = repo.head()
    if empty_commit:
        repo.git("commit", "--allow-empty", "-m", "Collect accepted children", cwd=repo.work)
    sha = repo.head()
    assert (sha != target) is empty_commit
    repo.task.candidate_sha = repo.task.verified_sha = repo.task.reviewed_sha = sha

    def unexpected_client(**kwargs):
        raise AssertionError("Empty changes must not contact the forge HTTP API")

    monkeypatch.setattr(gitops.httpx, "Client", unexpected_client)
    assert repo.publish(sha) == f"/api/tasks/{repo.task.id}"
    assert repo.task.candidate_sha == repo.task.verified_sha == repo.task.reviewed_sha == sha
    assert repo.git("rev-parse", f"refs/heads/{repo.task.branch}", cwd=Path(repo.project.remote)) == sha


@pytest.mark.parametrize("forge", ["github", "gitlab"])
def test_publish_changed_tree_still_creates_forge_record(publish_repository, monkeypatch, forge):
    repo = publish_repository
    repo.project.forge = forge
    (repo.work / "changed.txt").write_text("new implementation\n")
    sha = repo.commit()
    calls = []
    expected_url = "https://forge.invalid/change/123"

    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return gitops.httpx.Response(200, json=[])
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload["head" if forge == "github" else "source_branch"] == repo.task.branch
        assert payload["base" if forge == "github" else "target_branch"] == "preprod"
        return gitops.httpx.Response(201, json={"html_url" if forge == "github" else "web_url": expected_url})

    real_client = gitops.httpx.Client
    monkeypatch.setattr(
        gitops.httpx,
        "Client",
        lambda **kwargs: real_client(transport=gitops.httpx.MockTransport(respond), **kwargs),
    )
    assert repo.publish(sha) == expected_url
    assert [request.method for request in calls] == ["GET", "POST"]
    assert all(request.url.host == "forge.invalid" for request in calls)


@pytest.mark.parametrize("interrupt_phase", ["clone", "checkout"])
def test_interrupted_initial_workspace_is_atomic_and_can_retry(
    publish_repository, repository_factory, monkeypatch, interrupt_phase
):
    existing = publish_repository
    (existing.work / "kept.txt").write_text("existing uncommitted implementation\n")
    repo = repository_factory(remote=existing.project.remote, task_id="recoverytask")
    orphan = repo.work.parent / ".recoverytask.clone-orphan"
    orphan.mkdir()
    (orphan / "kept.txt").write_text("another attempt\n")
    original_git = repo.git
    stages = []

    def interrupted_git(*args, **kwargs):
        if args[0] == "clone" and "--no-hardlinks" in args:
            stages.append(Path(args[-1]))
            assert stages[-1] != repo.work
            if interrupt_phase == "clone":
                (stages[-1] / ".git").mkdir()
                (stages[-1] / ".git" / "HEAD").write_text("incomplete clone\n")
                raise subprocess.TimeoutExpired("git clone", 0.01)
        if args[0] == "checkout" and interrupt_phase == "checkout":
            assert Path(kwargs["cwd"]) == stages[-1]
            raise subprocess.TimeoutExpired("git checkout", 0.01)
        return original_git(*args, **kwargs)

    monkeypatch.setattr(repo, "git", interrupted_git)
    with pytest.raises(subprocess.TimeoutExpired):
        repo.prepare()
    assert not repo.work.exists()
    assert len(stages) == 1 and not stages[0].exists()
    assert (orphan / "kept.txt").read_text() == "another attempt\n"
    assert (existing.work / "kept.txt").read_text() == "existing uncommitted implementation\n"
    monkeypatch.setattr(repo, "git", original_git)
    target = repo.prepare()
    assert repo.head() == target
    assert repo.git("symbolic-ref", "--short", "HEAD", cwd=repo.work) == repo.task.branch
    assert (
        repo.git("config", "--local", "--get", "remote.origin.url", cwd=repo.work, check=False).returncode
        == 1
    )
    (repo.work / "draft.txt").write_text("resumed uncommitted work\n")
    assert repo.prepare() == target
    assert (repo.work / "draft.txt").read_text() == "resumed uncommitted work\n"
    assert repo.head() == target


def test_review_diff_after_a_resolved_merge_shows_only_this_tasks_change(repository_factory):
    repo = repository_factory()
    work = repo.work
    work.mkdir(parents=True)

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args],
            cwd=work,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "preprod")
    (work / "shared.ts").write_text("a\n")
    git("add", ".")
    git("commit", "-qm", "base")
    target = git("rev-parse", "HEAD")
    git("checkout", "-qb", "task")
    (work / "mine.ts").write_text("mine\n")
    git("add", ".")
    git("commit", "-qm", "task work")
    git("checkout", "-q", "preprod")
    (work / "other.ts").write_text("another task's merged feature\n")
    git("add", ".")
    git("commit", "-qm", "other task")
    git("checkout", "-q", "task")
    git("merge", "-q", "--no-edit", "preprod")
    repo.artifacts.mkdir(parents=True, exist_ok=True)
    repo.write_diff(target, git("rev-parse", "HEAD"))
    diff = (repo.artifacts / "diff.patch").read_text()
    assert "mine.ts" in diff and "other.ts" not in diff


def test_review_diff_with_commits_on_top_of_a_merge_uses_the_preprod_fork(repository_factory):
    repo = repository_factory()
    work = repo.work
    work.mkdir(parents=True)

    def git(cwd, *args):
        return subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git(work, "init", "-q", "-b", "preprod")
    (work / "shared.ts").write_text("a\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "base")
    target = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-qb", "task")
    (work / "mine.ts").write_text("mine\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "task work")
    git(work, "checkout", "-q", "preprod")
    (work / "other.ts").write_text("other\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "other task")
    git(work, "checkout", "-q", "task")
    git(work, "merge", "-q", "--no-edit", "preprod")
    (work / "mine.ts").write_text("mine, fixed after review\n")
    git(work, "commit", "-qam", "correction on top of the merge")
    repo.mirror.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--mirror", str(work), str(repo.mirror)], check=True)
    repo.artifacts.mkdir(parents=True, exist_ok=True)
    repo.write_diff(target, git(work, "rev-parse", "HEAD"))
    diff = (repo.artifacts / "diff.patch").read_text()
    assert "mine.ts" in diff and "other.ts" not in diff


def test_stale_index_lock_from_a_killed_git_is_removed(repository_factory, tmp_path):
    repo = repository_factory()
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "a.txt").write_text("x")
    lock = work / ".git" / "index.lock"
    lock.write_text("")
    # A fresh lock belongs to a live git: the command fails as before.
    with pytest.raises(gitops.GitError):
        repo.git("add", "a.txt", cwd=work)
    old = time.time() - gitops.STALE_LOCK_SECONDS - 5
    os.utime(lock, (old, old))
    repo.git("add", "a.txt", cwd=work)
    assert not lock.exists()
    assert "a.txt" in repo.git("diff", "--cached", "--name-only", cwd=work)


def test_commit_title_comes_from_the_writer_and_the_task_title_from_the_body(publish_repository):
    repo = publish_repository
    (repo.work / "value.txt").write_text("changed\n")
    sha = repo.commit("  Show the compact panel\nby default  ")
    assert repo.git("log", "-1", "--format=%B", sha, cwd=repo.work) == (
        "Show the compact panel by default\n\nfactory: Test fix"
    )
    (repo.work / "value.txt").write_text("again\n")
    sha = repo.commit()
    assert repo.git("log", "-1", "--format=%B", sha, cwd=repo.work) == "factory: Test fix"
    # Nothing staged: no empty commit.
    assert repo.commit("Nothing") == sha
