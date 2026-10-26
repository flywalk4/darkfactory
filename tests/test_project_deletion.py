import json

import pytest
from conftest import make_task
from sqlalchemy import select

from darkfactory import db, director, toolchains
from darkfactory.backlog import BacklogItem
from darkfactory.director import ChatThread, Dialogue
from darkfactory.notifications import NotificationChannel, NotificationDelivery


def second_project(client, pool, name="Keep"):
    return client.post(
        "/api/projects",
        json={
            "name": name,
            "initialize": False,
            "prepare_environment": False,
            "remote": f"https://example.invalid/{name.lower()}.git",
            "writer_pool": pool["id"],
            "reviewer_pool": pool["id"],
        },
    ).json()


def test_delete_project_removes_its_records_and_keeps_other_projects(client, setup_project):
    project, pool = setup_project
    other = second_project(client, pool)
    task = make_task(client, project)
    kept = make_task(client, other)
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        org_id, token_id = row.org_id, session.scalar(select(db.Token.id))
        db.event(session, row, "note", "history")
        session.add(db.Link(task_id=kept["id"], other_id=task["id"], kind="related_to"))
        artifact = db.Artifact(project_id=project["id"], key="spec", title="Spec", kind="doc")
        foreign = db.Artifact(project_id=other["id"], key="spec", title="Spec", kind="doc")
        session.add_all([artifact, foreign])
        session.flush()
        principal = session.scalar(select(db.Principal.id))
        version = db.ArtifactVersion(artifact_id=artifact.id, number=1, content="x", created_by=principal)
        written = db.ArtifactVersion(
            artifact_id=foreign.id, number=1, content="y", source_task_id=task["id"], created_by=principal
        )
        session.add_all([version, written])
        session.flush()
        session.add(db.ArtifactInput(task_id=kept["id"], artifact_id=artifact.id, version_id=version.id))
        session.add(BacklogItem(org_id=org_id, project_id=project["id"], text="later"))
        thread = ChatThread(token_id=token_id, org_id=org_id, project_id=project["id"])
        session.add(thread)
        session.flush()
        session.add(Dialogue(token_id=token_id, org_id=org_id, thread_id=thread.id, request="hi"))
        channel = NotificationChannel(
            org_id=org_id, project_id=project["id"], name="Chat", provider="telegram", secret=""
        )
        session.add(channel)
        session.flush()
        session.add(
            NotificationDelivery(channel_id=channel.id, task_id=task["id"], dedupe_key="k", message="m")
        )
        written_id = written.id
    state = toolchains.state_path(project["id"])
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}")

    response = client.delete(f"/api/projects/{project['id']}")

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": project["id"]}
    assert [item["id"] for item in client.get("/api/projects").json()] == [other["id"]]
    with db.Session() as session:
        assert session.get(db.Task, task["id"]) is None
        assert session.get(db.Task, kept["id"]) is not None
        assert session.scalar(select(db.Link)) is None
        assert session.scalar(select(db.ArtifactInput)) is None
        assert [a.project_id for a in session.scalars(select(db.Artifact))] == [other["id"]]
        assert session.get(db.ArtifactVersion, written_id).source_task_id is None
        for model in (BacklogItem, ChatThread, Dialogue, NotificationChannel, NotificationDelivery):
            assert session.scalar(select(model)) is None
    assert client.delete(f"/api/projects/{project['id']}").status_code == 404


@pytest.mark.parametrize("state", ["running", "recovering"])
def test_active_task_blocks_project_deletion(client, setup_project, state):
    project, _ = setup_project
    task = make_task(client, project)
    with db.transaction() as session:
        session.get(db.Task, task["id"]).state = state
    assert client.delete(f"/api/projects/{project['id']}").status_code == 409
    with db.Session() as session:
        assert session.get(db.Project, project["id"]) is not None


@pytest.mark.parametrize("role,scoped", [("observer", False), ("operator", False), ("owner", True)])
def test_only_organization_owner_can_delete_project(client, setup_project, role, scoped):
    project, _ = setup_project
    token = client.post(
        "/api/tokens", json={"role": role, "project_ids": [project["id"]] if scoped else []}
    ).json()["token"]
    client.headers["Authorization"] = "Bearer " + token
    assert client.delete(f"/api/projects/{project['id']}").status_code == 403
    with db.Session() as session:
        assert session.get(db.Project, project["id"]) is not None


def test_director_cannot_delete_projects():
    action = director.Action(method="DELETE", path="/api/projects/abc123", body_json=json.dumps({}))
    with pytest.raises(ValueError):
        director.validate_action(action)
