import concurrent.futures
import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import select

from darkfactory import backlog, db, schedules, selfreport, worker


@pytest.fixture
def factory(client, setup_project, monkeypatch, tmp_path):
    monkeypatch.setattr(selfreport, "ROOT", tmp_path)
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    project, _ = setup_project
    response = client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": project["name"],
            "remote": project["remote"],
            "factory_self": True,
            "test_env": {"TEST_LEAK_PATTERNS": "private-fixture-value"},
        },
    )
    assert response.status_code == 200
    assert response.json()["factory_self"] is True
    return project["id"]


def source(project_id, message, kind="execution_error", at=None):
    with db.transaction() as session:
        project = session.get(db.Project, project_id)
        task = db.Task(
            id=db.uid(),
            org_id=project.org_id,
            project_id=project.id,
            title="Source",
            request="Source",
            branch="source",
        )
        session.add(task)
        session.flush()
        session.add(db.Event(task_id=task.id, kind=kind, message=message, at=at or db.now()))
        return task.id


def report(project_id):
    with db.transaction() as session:
        selfreport.request(session, session.get(db.Project, project_id))
    return selfreport.run()


def notes(project_id):
    with db.Session() as session:
        return list(
            session.scalars(select(backlog.BacklogItem).where(backlog.BacklogItem.project_id == project_id))
        )


def other_project(client, name="Other"):
    response = client.post(
        "/api/projects",
        json={
            "name": name,
            "remote": "https://example.invalid/other.git",
            "initialize": False,
            "prepare_environment": False,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_groups_two_projects_scrubs_text_and_replays(client, factory):
    first = other_project(client, "CustomerOne")
    second = other_project(client, "CustomerTwo")
    for project_id, name, number in [(first, "CustomerOne", 123), (second, "CustomerTwo", 456)]:
        source(
            project_id,
            f"{name}: request {number} private-fixture-value at https://example.invalid/api 192.0.2.1 failed",
        )
    assert report(factory) == [(factory, 1)]
    item = notes(factory)[0]
    assert "Occurrences: 2" in item.text
    for hidden in ("CustomerOne", "CustomerTwo", "private-fixture-value", "example.invalid", "192.0.2.1"):
        assert hidden not in item.text
    assert report(factory) == [(factory, 0)]
    # New instances of the fingerprint also stay suppressed while the note is open.
    source(
        first,
        "CustomerOne: request 789 private-fixture-value at https://example.invalid/api 192.0.2.1 failed",
    )
    report(factory)
    assert len(notes(factory)) == 1
    # Planning a note must not permit duplicates until its linked task closes.
    task_id = source(first, "unrelated", kind="progress")
    with db.transaction() as session:
        row = session.get(backlog.BacklogItem, item.id)
        row.state, row.task_id = "planned", task_id
    source(
        second,
        "CustomerTwo: request 999 private-fixture-value at https://example.invalid/api 192.0.2.1 failed",
    )
    report(factory)
    assert len(notes(factory)) == 1
    with db.transaction() as session:
        session.get(db.Task, task_id).state = "merged"
    source(
        first,
        "CustomerOne: request 222 private-fixture-value at https://example.invalid/api 192.0.2.1 failed",
    )
    report(factory)
    assert len(notes(factory)) == 2


def test_no_opt_in_and_organization_isolation(client, setup_project):
    project_id = setup_project[0]["id"]
    source(project_id, "failure 1")
    source(project_id, "failure 2")
    assert report(project_id) == []
    assert notes(project_id) == []
    with db.transaction() as session:
        row = session.get(db.Project, project_id)
        row.secret = db.encrypt(json.dumps({"factory_self": True}))
        org = db.Organization(name="Isolated")
        session.add(org)
        session.flush()
        foreign = db.Project(id=db.uid(), org_id=org.id, name="Foreign", remote="unused")
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id
    source(foreign_id, "foreign failure 1")
    source(foreign_id, "foreign failure 2")
    report(project_id)
    assert len(notes(project_id)) == 1
    assert "foreign" not in notes(project_id)[0].text


@pytest.mark.parametrize("kind", sorted(selfreport.KINDS))
def test_all_event_kinds_and_counts_across_runs(client, factory, kind):
    source(factory, "failure 1", kind)
    report(factory)
    assert not notes(factory)
    source(factory, "failure 2", kind)
    report(factory)
    assert len(notes(factory)) == 1


def test_snapshots_system_errors_rotation_and_cursor(client, factory):
    other = other_project(client)
    stamp = db.now()
    with db.transaction() as session:
        for project_id in (factory, other):
            session.get(db.Project, project_id).health = {
                "qa": {"error": "qa failure", "at": stamp},
                "deploy": {"error": "deploy failure", "updated": stamp},
            }
    system = selfreport.ROOT / "system"
    system.mkdir()
    (system / "self-update.json").write_text(
        json.dumps({"state": "rolled_back", "message": "update failure", "at": stamp})
    )
    (system / "errors.jsonl.1").write_text(
        json.dumps({"at": stamp, "message": "worker failure 1"}) + "\ninvalid\n"
    )
    (system / "errors.jsonl").write_text(json.dumps({"at": stamp, "message": "worker failure 2"}) + "\n")
    report(factory)
    assert len(notes(factory)) == 4
    assert report(factory) == [(factory, 0)]
    (system / "self-update.json").write_text(
        json.dumps({"state": "failed", "message": "update failure", "at": db.now()})
    )
    report(factory)
    assert len(notes(factory)) == 5
    source(factory, "old failure", at=1)
    source(factory, "old failure", at=2)
    report(factory)
    assert len(notes(factory)) == 5


def test_concurrent_consumers_create_one_note(client, factory):
    source(factory, "failure 1")
    source(factory, "failure 2")
    with db.transaction() as session:
        selfreport.request(session, session.get(db.Project, factory))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: selfreport.run(), range(2)))
    assert len(notes(factory)) == 1


def test_schedule_queues_then_worker_reports(client, factory):
    response = client.post(
        f"/api/projects/{factory}/schedules", json={"kind": "self_report", "cron": "@daily"}
    )
    assert response.status_code == 201
    schedule_id = response.json()["id"]
    source(factory, "failure 1")
    source(factory, "failure 2")
    response = client.post(f"/api/schedules/{schedule_id}/run")
    assert response.status_code == 200
    assert not notes(factory)
    status = client.get(f"/api/projects/{factory}/schedules").json()["jobs"]["self_report"]
    assert status["requested"]
    selfreport.run()
    assert len(notes(factory)) == 1
    with db.Session() as session:
        assert schedules.jobs(session, session.get(db.Project, factory))["self_report"]["found"] == 1


def test_worker_error_handler_collects_only_background_errors_and_rotates(client, factory):
    handler = worker.SystemErrorHandler()
    handler.maxBytes = 250
    logger = logging.getLogger("selfreport-test")
    logger.addHandler(handler)
    try:
        logger.error("unrelated error")
        logger.warning("Scheduler tick failed")
        assert not Path(handler.baseFilename).read_text()
        for message, function in [
            ("Background job demo failed", "periodic"),
            ("Scheduler tick failed", "main"),
            ("Project schedules failed", "main"),
        ]:
            record = logging.LogRecord(
                logger.name,
                logging.ERROR,
                __file__,
                1,
                message,
                (),
                (ValueError, ValueError("fixture failure"), None),
                function,
            )
            logger.handle(record)
        paths = list(Path(handler.baseFilename).parent.glob("errors.jsonl*"))
        assert len(paths) > 1
        records = [json.loads(line) for path in paths for line in path.read_text().splitlines()]
        assert len(records) == 3
        assert all("fixture failure" in record["message"] for record in records)
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_stale_request_preserves_completed_report(client, factory):
    source(factory, "failure 1")
    source(factory, "failure 2")
    with db.Session() as stale:
        project = stale.get(db.Project, factory)
        stale.commit()
        # Retain a stale ORM row as a concurrent schedule request could do.
        stale.expire_on_commit = False
        stale.refresh(project)
        stale.commit()
        report(factory)
        selfreport.request(stale, project)
        stale.commit()
    selfreport.run()
    assert len(notes(factory)) == 1
    with db.Session() as session:
        fingerprints = selfreport.state(session.get(db.Project, factory))["fingerprints"]
        assert sum(item["count"] for item in fingerprints.values()) == 2


def test_schedule_opt_out_and_observer_permissions(client, setup_project):
    project_id = setup_project[0]["id"]
    created = client.post(
        f"/api/projects/{project_id}/schedules", json={"kind": "self_report", "cron": "@daily"}
    )
    assert created.status_code == 201
    schedule_id = created.json()["id"]
    result = client.post(f"/api/schedules/{schedule_id}/run")
    assert "не отмечен" in result.json()["last_result"]
    assert not selfreport.run()
    token = client.post("/api/tokens", json={"role": "observer"}).json()["token"]
    headers = {"Authorization": "Bearer " + token}
    assert client.post(f"/api/schedules/{schedule_id}/run", headers=headers).status_code == 403
    assert (
        client.post(
            f"/api/projects/{project_id}/schedules",
            json={"kind": "self_report", "cron": "@daily"},
            headers=headers,
        ).status_code
        == 403
    )


def test_create_project_with_factory_setting(client):
    response = client.post(
        "/api/projects",
        json={
            "name": "Factory",
            "remote": "https://example.invalid/factory.git",
            "factory_self": True,
            "initialize": False,
            "prepare_environment": False,
        },
    )
    assert response.status_code == 201
    assert response.json()["factory_self"] is True
    project_id = response.json()["id"]
    response = client.put(
        f"/api/projects/{project_id}", json={"name": "Factory", "remote": response.json()["remote"]}
    )
    assert response.json()["factory_self"] is True
