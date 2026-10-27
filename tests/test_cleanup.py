"""Finished tasks free their workspace and leftover remote branch; live and resumable work stays."""

import os
import time

import pytest
from conftest import connect, make_task
from test_pipeline import git, seed

from darkfactory import cleanup, db, service


def finish(task_id, state, age_hours, branch=None):
    with db.transaction() as session:
        task = session.get(db.Task, task_id)
        task.state, task.updated = state, time.time() - age_hours * 3600
        if branch:
            task.branch = branch
    work = db.ROOT / "workspaces" / task_id
    (work / ".git").mkdir(parents=True, exist_ok=True)
    return work


def test_sweep_removes_old_finished_workspaces_and_merged_branches(
    client, setup_project, tmp_path, monkeypatch
):
    # Never prune the host's real Docker from a test.
    monkeypatch.setattr(cleanup.docker, "from_env", lambda: (_ for _ in ()).throw(RuntimeError("no docker")))
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    mirror = db.ROOT / "repositories" / (project["id"] + ".git")
    git(tmp_path, "clone", "-q", "--mirror", str(remote), str(mirror))
    git(remote, "branch", "preprod", "main")
    git(remote, "branch", "darkfactory/old", "main")
    git(remote, "branch", "darkfactory/dropped", "main")
    merged, cancelled, fresh, waiting = (make_task(client, project)["id"] for _ in range(4))
    finish(merged, "merged", 30, "darkfactory/old")
    finish(cancelled, "cancelled", 30, "darkfactory/dropped")
    finish(fresh, "merged", 1)
    finish(waiting, "needs_human", 300)
    stale = db.ROOT / "workspaces" / f".{fresh}.clone-x"
    stale.mkdir()
    os.utime(stale, (0, 0))

    assert set(cleanup.sweep(force=True)) == {merged, cancelled}
    left = {
        p.name for p in (db.ROOT / "workspaces").iterdir() if p.name in {merged, cancelled, fresh, waiting}
    }
    assert left == {fresh, waiting}
    assert not stale.exists()
    branches = git(remote, "branch", "--list", "--format=%(refname:short)").split()
    # Only merged work loses its branch; a cancelled branch may hold unmerged work.
    assert "darkfactory/old" not in branches and "darkfactory/dropped" in branches

    # Resuming a swept task starts from a fresh clone instead of a missing workspace.
    with db.transaction() as session:
        task = session.get(db.Task, cancelled)
        assert task.role_config["workspace_swept"]
        task.phase = "implement"
        service.resume(session, task)
        assert "workspace_swept" not in task.role_config
        assert task.phase == "prepare"


def test_sweep_prunes_exited_containers_and_unused_toolchain_images(client, monkeypatch):
    from types import SimpleNamespace

    from darkfactory import toolchains

    state = db.ROOT / "system" / "toolchains"
    state.mkdir(parents=True, exist_ok=True)
    (state / "p1.json").write_text('{"image": "darkfactory-toolchain:current"}')
    images = [
        SimpleNamespace(id="a", tags=["darkfactory-toolchain:current", "darkfactory-toolchain:alias"]),
        SimpleNamespace(id="b", tags=["darkfactory-toolchain:old"]),
        SimpleNamespace(id="c", tags=["darkfactory-toolchain:busy"]),
    ]
    removed = []

    def remove(tag):
        if tag == "darkfactory-toolchain:busy":
            raise RuntimeError("image is being used by running container")
        removed.append(tag)

    gone = []
    exited = [
        SimpleNamespace(name="old", attrs={"State": {"FinishedAt": "2020-01-01T00:00:00.1Z"}}),
        SimpleNamespace(
            name="fresh", attrs={"State": {"FinishedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}}
        ),
    ]
    for item in exited:
        item.remove = lambda force, name=item.name: gone.append(name)
    pruned = []
    client_stub = SimpleNamespace(
        images=SimpleNamespace(
            list=lambda name: images, remove=remove, prune=lambda filters: pruned.append(filters) or {}
        ),
        containers=SimpleNamespace(list=lambda all, filters: exited),
        api=SimpleNamespace(prune_builds=lambda filters: pruned.append(filters) or {"SpaceReclaimed": 1}),
    )
    assert cleanup.orphans(client_stub) == ["old"]
    monkeypatch.setattr(cleanup.docker, "from_env", lambda: client_stub)
    assert toolchains.prune(client_stub) == ["darkfactory-toolchain:alias", "darkfactory-toolchain:old"]
    assert removed == ["darkfactory-toolchain:alias", "darkfactory-toolchain:old"]
    # A pruned or lost image falls back to the base executor instead of failing the run.
    missing = SimpleNamespace(images=SimpleNamespace(get=lambda tag: (_ for _ in ()).throw(LookupError(tag))))
    (state / "p1.json").write_text('{"image": "darkfactory-toolchain:current", "status": "ready"}')
    assert toolchains.image("p1") == "darkfactory-toolchain:current"
    assert toolchains.image("p1", missing) is None
    cleanup.sweep(force=True)
    # Every deploy leaves dangling images and build cache; the sweep drops them too.
    assert pruned == [{"dangling": True}, {"until": "24h"}]


def test_rebuilt_executor_image_rebuilds_the_toolchain_image(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from darkfactory import toolchains

    state = tmp_path / "system" / "toolchains"
    state.mkdir(parents=True)
    monkeypatch.setattr(toolchains, "ROOT", tmp_path)
    (state / "p1.json").write_text(
        '{"image": "darkfactory-toolchain:old", "status": "ready", "base_id": "sha:old"}'
    )
    images = SimpleNamespace(get=lambda tag: SimpleNamespace(id="sha:new" if tag == "base" else tag))
    client = SimpleNamespace(images=images)
    rebuilt = []
    monkeypatch.setattr(
        toolchains,
        "provision",
        lambda client, project, root, base, heartbeat: rebuilt.append(base) or {"image": "t:new"},
    )
    assert toolchains.fresh(client, "p1", tmp_path, "base") == "t:new" and rebuilt == ["base"]
    (state / "p1.json").write_text(
        '{"image": "darkfactory-toolchain:cur", "status": "ready", "base_id": "sha:new"}'
    )
    assert toolchains.fresh(client, "p1", tmp_path, "base") == "darkfactory-toolchain:cur" and rebuilt == [
        "base"
    ]


def test_fresh_workspace_reuses_installed_dependencies(client, setup_project, tmp_path):
    from darkfactory.gitops import Repository

    project, _ = setup_project
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / ".gitignore").write_text("node_modules/\n.state/\n")
    (source / "web").mkdir()
    (source / "web" / "package.json").write_text("{}\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "seed")
    git(tmp_path, "clone", "--bare", str(source), str(tmp_path / "remote.git"))
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(tmp_path / "remote.git")
    first, second = make_task(client, project), make_task(client, project)
    with db.Session() as session:
        row = session.get(db.Project, project["id"])
        Repository(row, session.get(db.Task, first["id"])).prepare()
        installed = db.ROOT / "workspaces" / first["id"] / "web" / "node_modules" / "left-pad"
        installed.mkdir(parents=True)
        (installed / "index.js").write_text("module.exports = 1\n")
        # A project-specific setup output (a virtualenv under .state/) must come along with the stamp.
        venv = db.ROOT / "workspaces" / first["id"] / ".state" / "mcp" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("#!/bin/sh\n")
        markers = db.ROOT / "caches" / project["id"] / "setup"
        markers.mkdir(parents=True)
        (markers / first["id"]).write_text("stamp")
        Repository(row, session.get(db.Task, second["id"])).prepare()

    work = db.ROOT / "workspaces" / second["id"]
    assert (work / "web" / "node_modules" / "left-pad" / "index.js").read_text() == "module.exports = 1\n"
    assert (work / ".state" / "mcp" / "bin" / "python").exists()
    assert (markers / second["id"]).read_text() == "stamp"


def test_failed_toolchain_build_is_retried_and_never_pruned_mid_build(monkeypatch, tmp_path):
    import fcntl
    from types import SimpleNamespace

    from darkfactory import cleanup, toolchains

    state = tmp_path / "system" / "toolchains"
    state.mkdir(parents=True)
    monkeypatch.setattr(toolchains, "ROOT", tmp_path)
    (state / "p1.json").write_text(
        '{"status": "failed", "spec": {"dotnet": "10.0"}, "error": "containerd blob gone"}'
    )
    images = SimpleNamespace(get=lambda tag: SimpleNamespace(id="sha:base" if tag == "base" else tag))
    retried = []

    def provision(client, project, root, base, heartbeat):
        retried.append(project)
        toolchains._save(project, {"status": "ready", "image": "t:new", "base_id": "sha:base"})
        return toolchains.state(project)

    monkeypatch.setattr(toolchains, "provision", provision)
    assert toolchains.fresh(SimpleNamespace(images=images), "p1", tmp_path, "base") == "t:new"
    assert retried == ["p1"]

    pruned = []
    monkeypatch.setattr(
        cleanup.docker,
        "from_env",
        lambda: SimpleNamespace(
            images=SimpleNamespace(prune=lambda filters: pruned.append("images") or {}),
            api=SimpleNamespace(prune_builds=lambda filters: pruned.append("cache") or {}),
        ),
    )
    with (state / "abc.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert cleanup.reclaim() == 0 and pruned == []
    cleanup.reclaim()
    assert pruned == ["images", "cache"]


def test_dotnet_channels_come_from_sdk_images_and_failed_builds_back_off(monkeypatch, tmp_path):
    import time as clock

    from darkfactory import toolchains

    text = toolchains.dockerfile("tools", {"dotnet": "8.0 10.0", "go": "1.22.0"})
    assert "COPY --from=mcr.microsoft.com/dotnet/sdk:8.0 /usr/share/dotnet /usr/share/dotnet" in text
    assert "COPY --from=mcr.microsoft.com/dotnet/sdk:10.0 /usr/share/dotnet /usr/share/dotnet" in text
    assert "dotnet-install.sh" not in text and "go1.22.0" in text
    assert "dotnet-install.sh" in toolchains.dockerfile("tools", {"dotnet": "LTS"})

    monkeypatch.setattr(toolchains, "ROOT", tmp_path)
    (tmp_path / "go.mod").write_text("module x\ngo 1.22.0\n")
    attempts = []

    def failing(client, base, spec, current, heartbeat, apt=()):
        attempts.append(spec)
        raise RuntimeError("failed to export layer")

    monkeypatch.setattr(toolchains, "_build", failing)
    client = object()
    assert toolchains.provision(client, "p1", tmp_path, "base")["status"] == "failed"
    # Within the backoff window the same spec is not rebuilt on every prepare.
    again = toolchains.provision(client, "p1", tmp_path, "base")
    assert again["status"] == "failed" and len(attempts) == 1 and again["attempts"] == 1
    toolchains._save("p1", {**toolchains.state("p1"), "failed_at": clock.time() - 3600})
    assert toolchains.provision(client, "p1", tmp_path, "base")["attempts"] == 2
    assert len(attempts) == 2
    # An environment retry asks for an immediate rebuild.
    toolchains.invalidate("p1")
    assert not toolchains.backing_off(toolchains.state("p1"))


def test_factory_deploy_rebuilds_only_the_runner_layer(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from darkfactory import toolchains

    monkeypatch.setattr(toolchains, "ROOT", tmp_path)
    toolchains._save(
        "p1",
        {
            "status": "ready",
            "image": "darkfactory-toolchain:old",
            "heavy": "darkfactory-toolchain:sdk",
            "base_id": "sha:old",
        },
    )
    ids = {"base": "sha:new", "darkfactory-toolchain:sdk": "sha:sdk"}
    built = []

    def get(tag):
        if tag in ids or tag in built:
            return SimpleNamespace(id=ids.get(tag, tag))
        raise LookupError(tag)

    def build(**options):
        built.append(options["tag"])
        assert (
            options["fileobj"].read().decode().startswith("FROM darkfactory-toolchain:sdk\nCOPY --from=base")
        )
        return iter([{"stream": "ok"}])

    client = SimpleNamespace(images=SimpleNamespace(get=get), api=SimpleNamespace(build=build))
    monkeypatch.setattr(toolchains, "_build_client", lambda client: client.api)
    monkeypatch.setattr(toolchains.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        toolchains, "provision", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full rebuild"))
    )
    tag = toolchains.fresh(client, "p1", tmp_path, "base")
    assert tag == built[0] and toolchains.state("p1")["base_id"] == "sha:new"


def test_package_caches_of_least_recently_used_projects_are_trimmed(monkeypatch, tmp_path):
    import os
    import time as clock

    from darkfactory import cleanup

    monkeypatch.setattr(cleanup, "ROOT", tmp_path)
    for name, age in (("old", 3000), ("new", 10)):
        project = tmp_path / "caches" / name
        (project / "npm").mkdir(parents=True)
        (project / "npm" / "blob").write_bytes(b"x" * 200_000)
        (project / "notes.md").write_text("facts")
        (project / "setup").mkdir()
        stamp = clock.time() - age
        for path in (project, project / "setup"):
            os.utime(path, (stamp, stamp))
    assert cleanup.trim_caches(limit=300_000) == ["old"]
    assert not (tmp_path / "caches" / "old" / "npm").exists()
    assert (tmp_path / "caches" / "old" / "notes.md").exists() and (
        tmp_path / "caches" / "new" / "npm"
    ).exists()


def test_toolchain_spec_is_read_from_preprod_not_the_task_branch(tmp_path):
    import json
    import subprocess

    from darkfactory import toolchains

    work = tmp_path / "work"
    work.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args],
            cwd=work,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    git("init", "-q", "-b", "preprod")
    (work / ".darkfactory").mkdir()
    (work / ".darkfactory" / "environment.json").write_text(json.dumps({"toolchains": {"go": "1.22.0"}}))
    (work / "src").mkdir()
    (work / "src" / "App.csproj").write_text("<Project><TargetFramework>net8.0</TargetFramework></Project>")
    (work / "src" / "main.cs").write_text("class A {}")
    git("add", ".")
    git("commit", "-qm", "seed")
    git("checkout", "-qb", "task")
    (work / ".darkfactory" / "environment.json").write_text(json.dumps({"toolchains": {"rust": "stable"}}))
    git("commit", "-qam", "agent edit")
    with toolchains.snapshot(work / ".git", "preprod") as tree:
        assert json.loads((tree / ".darkfactory" / "environment.json").read_text())["toolchains"] == {
            "go": "1.22.0"
        }
        assert (tree / "src" / "App.csproj").exists() and not (tree / "src" / "main.cs").exists()


def test_toolchain_images_build_with_buildkit_when_the_cli_is_present(monkeypatch, tmp_path):
    from darkfactory import toolchains

    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text('#!/bin/sh\ncat > "$DOCKERFILE_SEEN"\necho "$@" > "$ARGS_SEEN"\n')
    (fake / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    monkeypatch.setenv("DOCKERFILE_SEEN", str(tmp_path / "Dockerfile"))
    monkeypatch.setenv("ARGS_SEEN", str(tmp_path / "args"))
    toolchains._buildkit("darkfactory-toolchain:x", "FROM tools\n", {"go": "1"}, lambda: True)
    assert (tmp_path / "Dockerfile").read_text() == "FROM tools\n"
    assert (tmp_path / "args").read_text().startswith("buildx build --load")
    # Reads the Dockerfile like the real CLI does, so the failure never races the stdin write.
    (fake / "docker").write_text("#!/bin/sh\ncat > /dev/null\necho 'ERROR: failed to solve'\nexit 1\n")
    with pytest.raises(RuntimeError, match="failed to solve"):
        toolchains._buildkit("darkfactory-toolchain:x", "FROM tools\n", {}, lambda: True)
