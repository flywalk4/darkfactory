from conftest import connect, make_task
from sqlalchemy import select
from test_pipeline import DeterministicExecutor, drive, git, seed

from darkfactory import db, service
from darkfactory.gitops import Repository
from darkfactory.worker import Worker


def test_preprod_monitor_repairs_and_reuses_incident_counters(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    connect(client, pool)
    worker = Worker(DeterministicExecutor())
    normal = make_task(client, project)
    assert drive(worker, normal["id"]).state == "merged"

    broken = tmp_path / "external"
    git(tmp_path, "clone", "-b", "preprod", str(remote), str(broken))

    def break_preprod():
        git(broken, "pull", "--ff-only", "origin", "preprod")
        (broken / "value.py").write_text("VALUE = 0\n")
        git(broken, "add", ".")
        git(broken, "commit", "-m", "external regression")
        git(broken, "push", "origin", "preprod")

    def probe():
        worker.schedule_health()
        with db.transaction() as session:
            claimed = service.claim(session, 2)
        assert claimed
        worker.run(*claimed)
        with db.Session() as session:
            retry = session.get(db.Task, claimed[0])
            retried = retry.state == "queued" and retry.role_config.get("health_retried")
        if retried:  # A first failure is re-run before an incident opens.
            with db.transaction() as session:
                claimed = service.claim(session, 2)
            worker.run(*claimed)
        with db.Session() as session:
            return session.get(db.Project, project["id"]).incident_id

    # Simulate a crash after preparing an older monitor checkout, before checks.
    worker.schedule_health()
    with db.Session() as session:
        monitor = session.scalar(select(db.Task).where(db.Task.system_check.is_(True)))
        Repository(session.get(db.Project, project["id"]), monitor).prepare()
    break_preprod()
    incident = probe()
    assert incident, client.get(f"/api/projects/{project['id']}/tasks").json()
    with db.transaction() as session:
        repair = session.get(db.Task, incident)
        assert repair.phase == "plan" and repair.incident
        repair.corrections = 2
        repair.active_seconds = 110
    repaired = drive(worker, incident)
    assert repaired.state == "merged"
    with db.Session() as session:
        assert session.get(db.Project, project["id"]).incident_id is None
    break_preprod()
    assert probe() == incident
    with db.Session() as session:
        reopened = session.get(db.Task, incident)
        assert reopened.corrections == 2 and reopened.active_seconds >= 110
        assert reopened.phase == "prepare"


def test_limited_token_cannot_list_or_revoke_wider_own_token(client, setup_project):
    project, _ = setup_project
    owner_token = (
        client.get("/api/me").json()["token"]["id"] if "token" in client.get("/api/me").json() else None
    )
    tokens = client.get("/api/tokens").json()
    owner_token = owner_token or next(token["id"] for token in tokens if token["role"] == "owner")
    narrow = client.post("/api/tokens", json={"role": "observer", "project_ids": [project["id"]]}).json()
    headers = {"Authorization": "Bearer " + narrow["token"]}
    visible = client.get("/api/tokens", headers=headers).json()
    assert [token["id"] for token in visible] == [narrow["id"]]
    assert client.delete("/api/tokens/" + owner_token, headers=headers).status_code == 403
    assert client.get("/api/me").status_code == 200


def test_cancelled_incident_releases_project(setup_project):
    project, _ = setup_project
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        incident = db.Task(
            org_id=row.org_id,
            project_id=row.id,
            title="Исправить поломку preprod",
            request="x",
            branch="darkfactory/x",
            state="cancelled",
            phase="implement",
            incident=True,
        )
        session.add(incident)
        session.flush()
        row.incident_id, row.health = incident.id, {"state": "broken", "scheduled_at": 1e12}
    Worker(DeterministicExecutor()).schedule_health()
    with db.Session() as session:
        row = session.get(db.Project, project["id"])
        assert row.incident_id is None
        assert row.health["scheduled_at"] == 0 or row.health["scheduled_at"] > 1e9


def test_network_failure_in_preprod_check_does_not_open_incident(setup_project):
    project, _ = setup_project
    worker = Worker(DeterministicExecutor())
    log = "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.\nexit=1"
    for attempt in range(1, Worker.NETWORK_PATIENCE + 1):
        with db.transaction() as session:
            row = session.get(db.Project, project["id"])
            check = db.Task(
                org_id=row.org_id,
                project_id=row.id,
                title="Проверка preprod",
                request="x",
                branch="darkfactory/check",
                state="running",
                phase="monitor",
                system_check=True,
                role_config={"health_retried": True},
            )
            session.add(check)
            session.flush()
            worker.record_health(session, check, row, {"status": "failed", "text": log, "target": "abc"})
            assert check.state == "cancelled" and not check.incident
            assert row.incident_id is None
            assert row.health["state"] == "unknown" and row.health["network_failures"] == attempt
            kinds = [e.kind for e in session.scalars(select(db.Event).where(db.Event.task_id == check.id))]
            assert ("network_unstable" in kinds) == (attempt == Worker.NETWORK_PATIENCE)
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        worker.record_health(session, check, row, {"status": "ok", "target": "abc"})
        assert "network_failures" not in row.health


def test_unchanged_healthy_preprod_is_not_rechecked(client, setup_project, tmp_path, monkeypatch):
    project, pool = setup_project
    remote = seed(tmp_path)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    connect(client, pool)
    worker = Worker(DeterministicExecutor())
    assert drive(worker, make_task(client, project)["id"]).state == "merged"
    monkeypatch.setenv("PREPROD_CHECK_INTERVAL", "30")
    head = git(db.ROOT / "repositories" / (project["id"] + ".git"), "rev-parse", "refs/heads/preprod")

    def checks_scheduled():
        with db.transaction() as session:
            row = session.get(db.Project, project["id"])
            row.health = {**(row.health or {}), "scheduled_at": 0}
        worker.schedule_health()
        with db.transaction() as session:
            pending = session.scalars(
                select(db.Task).where(db.Task.system_check.is_(True), db.Task.state == "queued")
            ).all()
            for task in pending:
                task.state = "cancelled"
            return len(pending)

    with db.transaction() as session:
        session.get(db.Project, project["id"]).health = {
            "state": "healthy",
            "sha": head,
            "checked_at": db.now(),
        }
    assert checks_scheduled() == 0
    monkeypatch.setenv("PREPROD_RECHECK_SECONDS", "0")
    assert checks_scheduled() == 1
    monkeypatch.delenv("PREPROD_RECHECK_SECONDS")
    with db.transaction() as session:
        session.get(db.Project, project["id"]).health = {
            "state": "healthy",
            "sha": "0" * 40,
            "checked_at": db.now(),
        }
    assert checks_scheduled() == 1


def test_unreachable_remote_parks_project_instead_of_asking_human(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = "http://127.0.0.1:9/nocode.git"
    worker = Worker(DeterministicExecutor())
    task = make_task(client, project)
    with db.transaction() as session:
        claimed = service.claim(session, 2)
    worker.run(*claimed)
    with db.transaction() as session:
        row, current = session.get(db.Project, project["id"]), session.get(db.Task, task["id"])
        assert current.state == "queued" and current.reason is None
        assert row.health["remote_failures"] == 1 and service.remote_down(row)
        # Parked: nothing is claimed and no preprod check is scheduled while the forge is down.
        assert service.claim(session, 2) is None
        row.remote = str(remote)
        row.health = {**row.health, "remote_down_until": 0}
    assert drive(worker, task["id"]).state == "merged"
    with db.transaction() as session:
        assert "remote_failures" not in session.get(db.Project, project["id"]).health


def test_failed_install_or_missing_tool_retries_before_an_incident(setup_project):
    project, _ = setup_project
    worker = Worker(DeterministicExecutor())
    assert Worker.environment_failure("$ dotnet build x\nexit=127\nsh: 1: dotnet: not found")
    assert Worker.environment_failure("$ dotnet test a\nexit=0\n\n$ npm ci --prefix web\nexit=1\nnpm ERR! x")
    assert not Worker.environment_failure("$ npm ci\nexit=0\n\n$ npm test\nexit=1\nFAIL src/a.test.ts")
    log = "$ dotnet restore src/App --disable-parallel\nexit=1\nerror NU1301: whatever"
    for attempt in range(1, Worker.ENVIRONMENT_PATIENCE + 1):
        with db.transaction() as session:
            row = session.get(db.Project, project["id"])
            check = db.Task(
                org_id=row.org_id,
                project_id=row.id,
                title="Проверка preprod",
                request="x",
                branch="darkfactory/check",
                state="running",
                phase="monitor",
                system_check=True,
                role_config={"health_retried": True},
            )
            session.add(check)
            session.flush()
            worker.record_health(session, check, row, {"status": "failed", "text": log, "target": "abc"})
            # The failure keeps repeating: only then is it worth an agent's time.
            assert check.incident == (attempt == Worker.ENVIRONMENT_PATIENCE)
            assert (row.incident_id is not None) == (attempt == Worker.ENVIRONMENT_PATIENCE)


def test_removed_local_repository_stops_checks_and_syncs(client, setup_project, tmp_path, monkeypatch):
    from darkfactory import mainsync

    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    worker = Worker(DeterministicExecutor())
    assert drive(worker, make_task(client, project)["id"]).state == "merged"

    # The repository is deleted from /repos: no preprod check every five minutes, no main sync.
    gone = "/repos/removed-" + project["id"] + ".git"
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        row.remote, row.health = gone, {**row.health, "scheduled_at": 0, "main_sync_requested": 1}
    worker.schedule_health()
    with db.Session() as session:
        checks = session.query(db.Task).filter_by(project_id=project["id"], system_check=True).count()
    assert checks == 0
    assert mainsync.sync(force=True) == []
