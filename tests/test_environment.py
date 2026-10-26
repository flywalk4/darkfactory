"""The factory prepares a project environment through an agent task verified in the image it describes."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from conftest import connect
from test_pipeline import drive, seed

from darkfactory import db, environment, execution, toolchains
from darkfactory.worker import Worker

SPEC = {
    "toolchains": {"dotnet": "10.0"},
    "apt": ["libicu-dev"],
    "feeds": ["nuget.corp.example:80"],
    "setup": ["dotnet restore"],
    "checks": ["dotnet test"],
}


def start(client, project):
    response = client.post(f"/api/projects/{project['id']}/environment")
    assert response.status_code == 200, response.text
    return response.json()


class EnvironmentExecutor:
    def __init__(self, broken_once=False):
        self.broken_once = broken_once
        self.checks = []

    def stop(self, *_):
        pass

    def run(self, task, project, connection, role, heartbeat, timeout):
        work = db.ROOT / "workspaces" / task.id
        if role == "plan":
            return {"status": "ok", "plan": {"plan": "Inspect build files", "questions": [], "subtasks": []}}
        if role == "implement":
            (work / ".darkfactory").mkdir(exist_ok=True)
            spec = dict(SPEC, checks=["dotnet test --broken"]) if self.broken_once else SPEC
            (work / environment.SPEC).write_text(json.dumps(spec))
            return {"status": "ok", "text": "Environment described"}
        if role == "review":
            return {"status": "ok", "review": {"approved": True, "findings": []}}
        commands = environment.commands(work)
        self.checks.append(commands)
        if "dotnet test --broken" in commands:
            self.broken_once = False
            return {"status": "checks_failed", "text": "$ dotnet test --broken\nexit=1"}
        return {"status": "ok", "text": "ok"}


def test_parse_rejects_unknown_toolchains_and_multiline_commands():
    assert environment.parse(json.dumps(SPEC))["toolchains"] == {"dotnet": "10.0"}
    for broken in (
        {"toolchains": {"cobol": "1"}},
        {"setup": ["a\nb"]},
        {"apt": ["Bad Package"]},
        {"feeds": ["https://x"]},
        {"surprise": 1},
    ):
        with pytest.raises(ValueError):
            environment.parse(json.dumps(broken))


def test_declared_spec_overrides_detection(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.22\n")
    assert toolchains.provision(None, "p1", tmp_path, "base")["spec"] == {"go": "1.22.0"}
    (tmp_path / ".darkfactory").mkdir()
    (tmp_path / environment.SPEC).write_text(json.dumps(SPEC))
    state = toolchains.provision(None, "p1", tmp_path, "base")
    assert state["spec"] == {"dotnet": "10.0"} and state["apt"] == ["libicu-dev"]
    assert state["feeds"] == ["nuget.corp.example:80"] and state["setup"] == ["dotnet restore"]
    assert "libicu-dev" in toolchains.dockerfile("base", state["spec"], state["apt"])


def test_environment_task_retries_failed_checks_merges_and_adopts_checks(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool, "codex", "writer")
    connect(client, pool, "claude", "reviewer")
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        row.remote, row.checks = str(remote), []
    task = start(client, project)
    assert start(client, project)["id"] == task["id"]
    executor = EnvironmentExecutor(broken_once=True)
    result = drive(Worker(executor), task["id"])
    assert result.state == "merged", result.reason
    assert executor.checks[0] == ["dotnet restore", "dotnet test --broken"]
    assert executor.checks[-1] == ["dotnet restore", "dotnet test"]
    with db.Session() as session:
        # Setup stays in the toolchain state; adopted checks hold only the checks.
        assert session.get(db.Project, project["id"]).checks == ["dotnet test"]
        kinds = [e.kind for e in session.query(db.Event).filter_by(task_id=task["id"])]
    assert "checks_adopted" in kinds and "correction" in kinds


def test_environment_task_may_only_touch_its_spec(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool, "codex", "writer")
    connect(client, pool, "claude", "reviewer")
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)

    class Tamper(EnvironmentExecutor):
        def run(self, task, project, connection, role, heartbeat, timeout):
            if role == "implement":
                (db.ROOT / "workspaces" / task.id / "value.py").write_text("VALUE = 1\n")
            return super().run(task, project, connection, role, heartbeat, timeout)

    result = drive(Worker(Tamper()), start(client, project)["id"])
    assert result.state != "merged"


def test_checks_job_runs_spec_commands_and_implement_runs_setup(client, setup_project, monkeypatch):
    project, _ = setup_project
    task = start(client, project)
    work = db.ROOT / "workspaces" / task["id"]
    (work / ".darkfactory").mkdir(parents=True)
    (work / environment.SPEC).write_text(json.dumps(SPEC))
    toolchains.provision(None, project["id"], work, "base")
    jobs = {}

    class Captured(Exception):
        pass

    def create(_image, **kwargs):
        job = json.loads(
            (db.ROOT / "jobs" / kwargs["labels"]["darkfactory.generation"] / "input.json").read_text()
        )
        jobs[job["role"]] = job
        raise Captured()

    monkeypatch.setattr(
        execution.docker, "from_env", lambda: SimpleNamespace(containers=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(execution, "egress_proxy", lambda *args, **kwargs: nullcontext())
    with db.transaction() as session:
        # Adopted checks carry the setup commands first.
        session.get(db.Project, project["id"]).checks = ["dotnet restore", "dotnet test"]
    # A note the owner marked "not useful" steers the reviewer away from similar ones.
    assert client.post(
        f"/api/projects/{project['id']}/review-muted", json={"text": "Rename variables"}
    ).json() == {"muted": 1}
    for role in ("checks", "implement", "review"):
        with db.Session() as session, pytest.raises(Captured):
            execution.DockerExecutor().run(
                session.get(db.Task, task["id"]),
                session.get(db.Project, project["id"]),
                None,
                role,
                lambda: True,
                60,
            )
    # The environment task verifies its spec by running setup and checks itself.
    assert jobs["checks"]["checks"] == ["dotnet restore", "dotnet test"]
    assert jobs["checks"]["setup"] == []
    assert jobs["implement"]["setup"] == ["dotnet restore"]
    # The writer leaves review to the factory and finishes its own findings instead of listing them.
    assert "do not spend this run on one" in jobs["implement"]["prompt"]
    assert "close every one of them" in jobs["implement"]["prompt"]
    assert jobs["review"]["setup"] == []
    # The writer sees setup as done, not as a required check to re-run.
    assert 'Required checks: ["dotnet test"]' in jobs["implement"]["prompt"]
    assert "Never re-run these" in jobs["implement"]["prompt"]
    assert "Never re-run these" not in jobs["review"]["prompt"]
    assert "not useful in this project" in jobs["review"]["prompt"]
    assert "- Rename variables" in jobs["review"]["prompt"]


DB = {"name": "db", "image": "postgres:17", "env": {"POSTGRES_PASSWORD": "dev"}, "command": []}


def test_parse_validates_services():
    assert environment.parse(json.dumps({**SPEC, "services": [DB]}))["services"] == [DB]
    assert environment.parse(json.dumps(SPEC))["services"] == []
    for broken in (
        [DB] * 2,
        [{**DB, "name": "Bad_Name"}],
        [{**DB, "image": "postgres 17"}],
        [{**DB, "env": {"BAD-KEY": "x"}}],
        [{**DB, "command": "postgres -c fsync=off"}],
        [{**DB, "name": f"s{i}"} for i in range(6)],
    ):
        with pytest.raises(ValueError):
            environment.parse(json.dumps({**SPEC, "services": broken}))
    assert environment.parse(json.dumps({**SPEC, "env": {"PG": "Host=db"}}))["env"] == {"PG": "Host=db"}
    for broken in ({"BAD-KEY": "x"}, {"PG": 1}, ["PG=x"]):
        with pytest.raises(ValueError):
            environment.parse(json.dumps({**SPEC, "env": broken}))


def test_services_run_on_private_network_beside_agent(client, setup_project, monkeypatch):
    project, _ = setup_project
    task = start(client, project)
    work = db.ROOT / "workspaces" / task["id"]
    (work / ".darkfactory").mkdir(parents=True)
    (work / environment.SPEC).write_text(
        json.dumps({**SPEC, "services": [DB], "env": {"PG": "Host=db", "HOME": "/elsewhere"}})
    )
    toolchains.provision(None, project["id"], work, "base")
    created, removed = {}, []

    class Captured(Exception):
        pass

    class Network:
        name = None

        def __init__(self, name, **kwargs):
            self.name, self.kwargs, self.aliases = name, kwargs, {}

        def disconnect(self, container):
            pass

        def connect(self, container, aliases=()):
            self.aliases[container.name] = list(aliases)

        def remove(self):
            removed.append(self.name)

    class Container:
        def __init__(self, name, labels):
            self.name, self.labels = name, labels

        def start(self):
            pass

        def remove(self, force=False, v=False):
            removed.append(self.name)

    networks = []

    def create_network(name, **kwargs):
        networks.append(Network(name, **kwargs))
        return networks[-1]

    def create(image, **kwargs):
        service = "darkfactory.service" in kwargs["labels"]
        created[kwargs["name"] if service else "agent"] = (image, kwargs)
        if service:
            return Container(kwargs["name"], kwargs["labels"])
        raise Captured()

    fake = SimpleNamespace(
        containers=SimpleNamespace(
            create=create,
            list=lambda **_: [Container(n, k["labels"]) for n, (_, k) in created.items() if n != "agent"],
        ),
        networks=SimpleNamespace(create=create_network, list=lambda **_: networks),
        images=SimpleNamespace(get=lambda image: None, pull=lambda image: None),
    )
    monkeypatch.setattr(execution.docker, "from_env", lambda: fake)
    monkeypatch.setattr(execution, "egress_proxy", lambda *args, **kwargs: nullcontext())
    for role in ("director", "implement"):
        created.clear()
        with db.Session() as session, pytest.raises(Captured):
            execution.DockerExecutor().run(
                session.get(db.Task, task["id"]),
                session.get(db.Project, project["id"]),
                None,
                role,
                lambda: True,
                60,
            )
        if role == "director":
            # Read-only roles without services stay fully offline.
            assert list(created) == ["agent"] and created["agent"][1]["network_mode"] == "none"
            assert "PG" not in created["agent"][1]["environment"]
    network = networks[-1]
    assert network.kwargs["internal"] is True
    service = next(name for name in created if name.startswith("df-svc-"))
    assert created[service][0] == "postgres:17" and created[service][1]["environment"] == DB["env"]
    assert network.aliases[service] == ["db"]
    agent = created["agent"][1]
    assert agent["network"] == network.name and "network_mode" not in agent
    assert agent["environment"]["PG"] == "Host=db" and agent["environment"]["HOME"] == "/tmp/home"
    assert service in removed and network.name in removed


def test_ordinary_checks_run_setup_under_the_stamp_not_as_checks(client, setup_project, monkeypatch):
    from conftest import make_task

    project, _ = setup_project
    task = make_task(client, project)
    work = db.ROOT / "workspaces" / task["id"]
    (work / ".darkfactory").mkdir(parents=True)
    (work / environment.SPEC).write_text(json.dumps(SPEC))
    toolchains.provision(None, project["id"], work, "base")
    jobs = {}

    class Captured(Exception):
        pass

    def create(_image, **kwargs):
        job = json.loads(
            (db.ROOT / "jobs" / kwargs["labels"]["darkfactory.generation"] / "input.json").read_text()
        )
        jobs[job["role"]] = job
        raise Captured()

    monkeypatch.setattr(
        execution.docker, "from_env", lambda: SimpleNamespace(containers=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(execution, "egress_proxy", lambda *args, **kwargs: nullcontext())
    with db.transaction() as session:
        # Older projects adopted setup as checks.
        session.get(db.Project, project["id"]).checks = ["dotnet restore", "dotnet test"]
    with db.Session() as session, pytest.raises(Captured):
        execution.DockerExecutor().run(
            session.get(db.Task, task["id"]),
            session.get(db.Project, project["id"]),
            None,
            "checks",
            lambda: True,
            60,
        )
    assert jobs["checks"]["checks"] == ["dotnet test"]
    assert jobs["checks"]["setup"] == ["dotnet restore"]
    assert jobs["checks"]["setup_marker"].endswith("/setup/" + task["id"])


def test_toolchain_paths_survive_login_shells():
    # Setup and checks run in `sh -lc`; /etc/profile resets PATH, so the image re-exports it there.
    text = toolchains.dockerfile("base", {"go": "1.24.0", "java": "21"})
    profile = next(line for line in text.splitlines() if "/etc/profile.d/" in line)
    assert 'export PATH="/opt/jdk/bin:/usr/local/go/bin:$PATH"' in profile
    assert 'export JAVA_HOME="/opt/jdk"' in profile


def test_changed_install_recipe_marks_images_stale():
    spec = {"rust": "stable"}
    current = {"spec": spec, "apt": [], "recipe": toolchains.recipe(spec), "status": "ready"}
    assert not toolchains.stale(current)
    assert toolchains.stale({**current, "recipe": "older"})
    assert toolchains.stale({**current, "recipe": None})
    assert not toolchains.stale({"spec": {}, "apt": [], "status": "ready"})


def test_rebuilt_tools_image_makes_project_images_stale():
    from types import SimpleNamespace

    spec = {"dotnet": "10.0"}
    current = {
        "spec": spec,
        "apt": [],
        "recipe": toolchains.recipe(spec),
        "heavy": "h",
        "tools_id": "sha:old",
    }

    def client(tools_id):
        return SimpleNamespace(images=SimpleNamespace(get=lambda tag: SimpleNamespace(id=tools_id)))

    assert not toolchains.stale(current, client("sha:old"))
    assert toolchains.stale(current, client("sha:new"))
    assert toolchains.stale({**current, "tools_id": None}, client("sha:old"))
