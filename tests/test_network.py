import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from conftest import make_task
from test_egress import connect_proxy, response

from darkfactory import execution, gitops, worker
from darkfactory.db import Event, Project, Session, Task, decrypt, event, transaction
from darkfactory.egress import egress_proxy, local_proxy


def test_proxy_records_refused_destinations():
    denied = set()
    with tempfile.TemporaryDirectory(prefix="df-egress-", dir="/tmp") as directory:
        path = Path(directory) / "egress.sock"
        with egress_proxy(path, {("api.example.com", 443)}, denied), local_proxy(str(path)) as url:
            with connect_proxy(url) as client:
                client.sendall(b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n")
                assert b"403 Forbidden" in response(client)
    assert denied == {"registry.npmjs.org:443"}


def test_denied_hosts_skip_agent_telemetry(client, setup_project, tmp_path):
    project, _ = setup_project
    task = make_task(client, project)
    execution.record_denied(task["id"], "plan", "g1", tmp_path, {"chatgpt.com:443", "api.openai.com:443"})
    assert not (tmp_path / "egress-denied.json").exists()
    execution.record_denied(task["id"], "implement", "g2", tmp_path, {"api.openai.com:443", "npm.corp:8443"})
    assert json.loads((tmp_path / "egress-denied.json").read_text()) == ["npm.corp:8443"]
    with Session() as db:
        rows = db.query(Event).filter(Event.task_id == task["id"], Event.kind == "egress_denied").all()
        assert [row.data["hosts"] for row in rows] == [["npm.corp:8443"]]


def test_network_block_escalates_instead_of_burning_corrections(client, setup_project, monkeypatch):
    project, _ = setup_project
    task = make_task(client, project)
    monkeypatch.setattr(worker, "write_handoff", lambda task, text: None)
    with transaction() as db:
        row = db.get(Task, task["id"])
        event(db, row, "egress_denied", "old", hosts=["stale.example:443"])
        event(db, row, "phase_started", "implement")
        event(db, row, "egress_denied", "now", hosts=["registry.npmjs.org:443"])
        worker.correction(
            db, row, db.get(Project, row.project_id), "[P1] Studio not built: npm error 403 Forbidden"
        )
    with Session() as db:
        row = db.get(Task, task["id"])
        assert (row.state, row.reason, row.corrections) == ("needs_human", "network_blocked", 0)
        blocked = db.query(Event).filter(Event.task_id == row.id, Event.kind == "network_blocked").one()
        assert blocked.data["hosts"] == ["registry.npmjs.org:443"]
    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["reason"] == "network_blocked"


def test_environment_only_rejection_fixes_environment_then_escalates(client, setup_project, monkeypatch):
    project, _ = setup_project
    first, second = make_task(client, project), make_task(client, project)
    monkeypatch.setattr(worker, "write_handoff", lambda task, text: None)
    gaps = json.dumps({"approved": False, "findings": ["[ENV] dotnet отсутствует", " [ENV] нет Docker"]})
    mixed = json.dumps(
        {"approved": False, "findings": ["[ENV] dotnet отсутствует", "Нет регрессионного теста"]}
    )
    with transaction() as db:
        worker.correction(db, db.get(Task, first["id"]), db.get(Project, project["id"]), gaps)
        worker.correction(db, db.get(Task, second["id"]), db.get(Project, project["id"]), mixed)
    with Session() as db:
        row = db.get(Task, first["id"])
        fixer = db.get(Task, row.role_config["environment_requested"])
        assert fixer.role_config["environment"] and "- dotnet отсутствует\n- нет Docker" in fixer.request
        assert (row.state, row.phase, row.corrections) == ("waiting_dependency", "collect", 0)
        assert row.reason == f"Ожидает {fixer.title}: queued"
        assert db.query(Event).filter(Event.task_id == row.id, Event.kind == "environment_requested").one()
        assert db.get(Task, second["id"]).reason != "environment_blocked"
    with transaction() as db:  # The fixed environment still lacks it: now the owner decides.
        worker.correction(db, db.get(Task, first["id"]), db.get(Project, project["id"]), gaps)
    with Session() as db:
        row = db.get(Task, first["id"])
        assert (row.state, row.reason, row.corrections) == ("needs_human", "environment_blocked", 0)
        blocked = db.query(Event).filter(Event.task_id == row.id, Event.kind == "environment_blocked").one()
        assert blocked.message == "Среде агента не хватает: dotnet отсутствует; нет Docker"


def test_owner_allows_refused_hosts_once(client, setup_project):
    project, _ = setup_project
    task = make_task(client, project)
    url = f"/api/tasks/{task['id']}/allow-egress"
    assert client.post(url, json={"hosts": ["https://x"]}).status_code == 400
    assert client.post(url, json={"hosts": ["npm.corp:8443"]}).json()["allowed_hosts"] == ["npm.corp:8443"]
    assert client.post(url, json={"hosts": ["npm.corp:8443"]}).json()["allowed_hosts"] == ["npm.corp:8443"]
    with Session() as db:
        config = json.loads(decrypt(db.get(Project, project["id"]).secret))
        assert config["allowed_hosts"] == ["npm.corp:8443"]
    assert client.get("/api/projects").json()[0]["package_registries"] is True


def test_factory_commits_never_include_tool_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(gitops, "ROOT", tmp_path)
    repo = gitops.Repository(
        SimpleNamespace(id="p", secret=None, remote="", target="preprod", seed_branch="main"),
        SimpleNamespace(id="t", branch="darkfactory/t", title="x"),
    )
    repo.work.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo.work)], check=True)
    (repo.work / ".git" / "info" / "exclude").write_text("# local\n.npm/")
    repo.exclude_caches()
    repo.exclude_caches()
    lines = (repo.work / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines[:2] == ["# local", ".npm/"] and lines.count(".npm/") == 1
    assert set(gitops.CACHE_PATTERNS) <= set(lines)
    (repo.work / ".npm-cache" / "_logs").mkdir(parents=True)
    (repo.work / ".npm-cache" / "_logs" / "debug.log").write_text("x")
    (repo.work / "app.ts").write_text("x")
    status = subprocess.run(
        ["git", "status", "--porcelain", "-uall"], cwd=repo.work, capture_output=True, text=True, check=True
    ).stdout
    assert status.split() == ["??", "app.ts"]


def test_package_caches_live_outside_the_workspace():
    assert ("registry.npmjs.org", 443) in execution.PACKAGE_REGISTRIES
    assert all(
        value.startswith(execution.CACHE) or value.endswith(execution.CACHE + "/m2")
        for value in execution.CACHE_ENV.values()
    )


def test_task_list_marks_children_with_parent(client, setup_project):
    project, _ = setup_project
    parent, child = make_task(client, project), make_task(client, project)
    response = client.post(
        f"/api/tasks/{parent['id']}/links", json={"other_id": child["id"], "kind": "parent"}
    )
    assert response.status_code == 201
    listed = {t["id"]: t["parent_id"] for t in client.get(f"/api/projects/{project['id']}/tasks").json()}
    assert listed == {parent["id"]: None, child["id"]: parent["id"]}


def test_unrelated_rejection_is_a_normal_correction_despite_refused_hosts(client, setup_project, monkeypatch):
    project, _ = setup_project
    task = make_task(client, project)
    monkeypatch.setattr(worker, "write_handoff", lambda task, text: None)
    with transaction() as db:
        row = db.get(Task, task["id"])
        event(db, row, "phase_started", "implement")
        event(db, row, "egress_denied", "now", hosts=["github.com:443"])
        worker.correction(
            db, row, db.get(Project, row.project_id), "[P2] Report still says dependencies are unavailable"
        )
    with Session() as db:
        row = db.get(Task, task["id"])
        assert (row.state, row.corrections) == ("queued", 1)


def test_browser_background_hosts_are_not_reported(client, setup_project, tmp_path):
    project, _ = setup_project
    task = make_task(client, project)
    execution.record_denied(
        task["id"],
        "implement",
        "g3",
        tmp_path,
        {"update.googleapis.com:443", "redirector.gvt1.com:443", "www.google.com:443"},
    )
    assert not (tmp_path / "egress-denied.json").exists()


def test_only_operator_listed_hosts_may_reach_private_addresses(client, setup_project, monkeypatch):
    from contextlib import nullcontext

    import pytest

    from darkfactory import db, toolchains
    from darkfactory.db import encrypt

    project, _ = setup_project
    task = make_task(client, project)
    work = db.ROOT / "workspaces" / task["id"]
    work.mkdir(parents=True, exist_ok=True)
    # A feed the repository names (an agent can edit this file) must stay public-only.
    (work / "nuget.config").write_text(
        '<configuration><add key="f" value="http://10.0.0.5:8081/v3"/></configuration>'
    )
    toolchains.provision(None, project["id"], work, "base")
    with transaction() as session:
        row = session.get(Project, project["id"])
        config = json.loads(decrypt(row.secret) or "{}")
        row.secret = encrypt(
            json.dumps({**config, "allowed_hosts": ["10.1.2.3:5432", "nuget.corp.example:80"]})
        )
    calls = []

    class Captured(Exception):
        pass

    def proxy(path, allowed, denied=None, private=None):
        calls.append((set(allowed), private))
        return nullcontext()

    def create(_image, **kwargs):
        raise Captured()

    monkeypatch.setattr(execution, "egress_proxy", proxy)
    monkeypatch.setattr(
        execution.docker, "from_env", lambda: SimpleNamespace(containers=SimpleNamespace(create=create))
    )
    with Session() as session, pytest.raises(Captured):
        execution.DockerExecutor().run(
            session.get(Task, task["id"]),
            session.get(Project, project["id"]),
            None,
            "checks",
            lambda: True,
            60,
        )
    allowed, private = calls[0]
    assert ("10.0.0.5", 8081) in allowed and ("10.1.2.3", 5432) in allowed
    # Only what the operator listed may be private; the repository-named feed stays public-only.
    assert private == {("10.1.2.3", 5432), ("nuget.corp.example", 80)}
    assert ("10.0.0.5", 8081) not in private


def test_owner_configured_model_endpoint_may_be_private(client, setup_project, monkeypatch):
    from contextlib import nullcontext

    import pytest

    from darkfactory import db
    from darkfactory.db import encrypt

    project, _ = setup_project
    task = make_task(client, project)
    (db.ROOT / "workspaces" / task["id"]).mkdir(parents=True, exist_ok=True)
    calls = []

    class Captured(Exception):
        pass

    def proxy(path, allowed, denied=None, private=None):
        calls.append(private)
        return nullcontext()

    def create(_image, **kwargs):
        raise Captured()

    connection = SimpleNamespace(
        provider="api",
        model="m",
        secret=encrypt(json.dumps({"base_url": "https://worker:8443/v1", "api_key": "k"})),
    )
    monkeypatch.setattr(execution, "egress_proxy", proxy)
    monkeypatch.setattr(
        execution.docker, "from_env", lambda: SimpleNamespace(containers=SimpleNamespace(create=create))
    )
    with Session() as session, pytest.raises(Captured):
        execution.DockerExecutor().run(
            session.get(Task, task["id"]),
            session.get(Project, project["id"]),
            connection,
            "implement",
            lambda: True,
            60,
        )
    assert ("worker", 8443) in calls[0]


def test_conflict_resolution_run_gets_a_short_leash(client, setup_project, monkeypatch):
    from contextlib import nullcontext

    import pytest

    from darkfactory import db

    project, _ = setup_project
    task = make_task(client, project)
    (db.ROOT / "workspaces" / task["id"]).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(worker, "write_handoff", lambda *args: None)
    with transaction() as session:
        row = session.get(Task, task["id"])
        worker.correction(
            session, row, session.get(Project, row.project_id), "conflict in a.ts", source="conflict"
        )
        assert row.role_config["conflict_pending"] and row.corrections == 0
    jobs = []

    class Captured(Exception):
        pass

    def create(_image, **kwargs):
        jobs.append(
            json.loads(
                (db.ROOT / "jobs" / kwargs["labels"]["darkfactory.generation"] / "input.json").read_text()
            )
        )
        raise Captured()

    monkeypatch.setattr(execution, "egress_proxy", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(
        execution.docker, "from_env", lambda: SimpleNamespace(containers=SimpleNamespace(create=create))
    )
    with Session() as session, pytest.raises(Captured):
        execution.DockerExecutor().run(
            session.get(Task, task["id"]),
            session.get(Project, project["id"]),
            None,
            "implement",
            lambda: True,
            60,
        )
    assert jobs[0]["max_steps"] == execution.CONFLICT_STEPS
    assert jobs[0]["prompt"].startswith("This run only resolves a merge conflict")
