import json

import pytest
from conftest import make_task
from sqlalchemy import create_engine, select, text

from darkfactory import coordinator, db, service
from darkfactory.director import Action, validate_action


def test_priority_queue_keeps_incidents_first_and_fifo_within_priority(client, setup_project):
    project, _ = setup_project
    items = [
        make_task(client, project, priority=priority)
        for priority in ("low", "normal", "high", "critical", "high", "low")
    ]
    with db.transaction() as session:
        for index, item in enumerate(items):
            row = session.get(db.Task, item["id"])
            row.created = index + 1
        session.get(db.Task, items[-1]["id"]).incident = True
        order = []
        for _ in items:
            identifier, _ = service.claim(session, 2)
            order.append(identifier)
            session.get(db.Task, identifier).state = "merged"
        assert order == [items[i]["id"] for i in (5, 3, 2, 4, 1, 0)]


def test_critical_priority_still_waits_for_dependencies_and_capacity(client, setup_project):
    project, _ = setup_project
    predecessor = make_task(client, project)
    critical = make_task(client, project, priority="critical")
    available = make_task(client, project, priority="low")
    with db.transaction() as session:
        session.get(db.Task, predecessor["id"]).state = "needs_human"
        session.add(db.Link(task_id=critical["id"], other_id=predecessor["id"], kind="depends_on"))
        session.flush()
        assert service.claim(session, 1)[0] == available["id"]
        assert session.get(db.Task, critical["id"]).state == "waiting_dependency"
        assert service.claim(session, 1) is None


def test_priority_changes_preserve_execution_and_acceptance(client, setup_project):
    project, _ = setup_project
    item = make_task(client, project)
    assert item["priority"] == "normal"
    with db.transaction() as session:
        task = session.get(db.Task, item["id"])
        task.state, task.phase, task.lease_owner = "running", "review", "live-worker"
        task.verified_sha = task.reviewed_sha = "a" * 40
        task.active_seconds, task.corrections = 10, 2
    for _ in range(2):
        response = client.put(f"/api/tasks/{item['id']}/priority", json={"priority": "critical"})
        assert response.status_code == 200
        assert response.json()["priority"] == "critical"
        with db.Session() as session:
            assert session.get(db.Task, item["id"]).lease_owner == "live-worker"
        assert response.json()["verified_sha"] == "a" * 40
        assert response.json()["reviewed_sha"] == "a" * 40
        assert response.json()["active_seconds"] == 10
        assert response.json()["corrections"] == 2
    events = client.get(f"/api/tasks/{item['id']}").json()["events"]
    assert len([event for event in events if event["kind"] == "priority_changed"]) == 1


@pytest.mark.parametrize("priority", ["urgent", "", 0, None])
def test_priority_validation(client, setup_project, priority):
    project, _ = setup_project
    item = make_task(client, project)
    assert client.put(f"/api/tasks/{item['id']}/priority", json={"priority": priority}).status_code == 422
    assert (
        client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Invalid", "request": "Check", "priority": priority},
        ).status_code
        == 422
    )


def test_priority_requires_operator_and_project_access(client, setup_project):
    project, _ = setup_project
    item = make_task(client, project)
    other = client.post(
        "/api/projects", json={"name": "Other", "remote": "https://example.invalid/other.git"}
    ).json()
    inaccessible = make_task(client, other)
    observer = client.post("/api/tokens", json={"role": "observer"}).json()["token"]
    operator = client.post("/api/tokens", json={"role": "operator", "project_ids": [project["id"]]}).json()[
        "token"
    ]
    client.headers["Authorization"] = "Bearer " + observer
    assert client.put(f"/api/tasks/{item['id']}/priority", json={"priority": "high"}).status_code == 403
    client.headers["Authorization"] = "Bearer " + operator
    assert (
        client.put(f"/api/tasks/{inaccessible['id']}/priority", json={"priority": "high"}).status_code == 404
    )
    assert client.put(f"/api/tasks/{item['id']}/priority", json={"priority": "high"}).status_code == 200


def test_delegated_tasks_inherit_priority(client, setup_project):
    project, _ = setup_project
    item = make_task(client, project, priority="high")
    with db.transaction() as session:
        root = session.get(db.Task, item["id"])
        assert coordinator.delegate(
            session,
            root,
            {
                "plan": "Delegate bounded work",
                "questions": [],
                "subtasks": [
                    {
                        "key": "analysis",
                        "title": "Analysis",
                        "request": "Write analysis.md",
                        "acceptance": "Document exists",
                        "depends_on": [],
                        "agent_profile": None,
                    }
                ],
            },
        )
        child_id = session.scalar(select(db.Link.other_id).where(db.Link.task_id == root.id))
        assert session.get(db.Task, child_id).priority == "high"


def test_legacy_tasks_receive_normal_priority():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for table in db.Base.metadata.sorted_tables:
            conn.execute(text(f'CREATE TABLE "{table.name}" (id TEXT PRIMARY KEY)'))
        conn.execute(text("INSERT INTO tasks (id) VALUES ('existing')"))
        db.expand_schema(conn)
        db.expand_schema(conn)
        assert conn.execute(text("SELECT id, priority FROM tasks")).one() == ("existing", "normal")


def test_director_can_set_task_priority():
    assert validate_action(
        Action(method="PUT", path="/api/tasks/abc123/priority", body_json=json.dumps({"priority": "high"}))
    ) == {"priority": "high"}
