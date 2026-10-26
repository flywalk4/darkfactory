import pytest
from conftest import connect, make_task

from darkfactory import db
from darkfactory.auth_sessions import LoginSession


def test_delete_connection_forgets_account_and_keeps_task_history(client, setup_project):
    project, pool = setup_project
    connection = connect(client, pool)
    other = connect(client, pool, account="account-2")
    task = make_task(client, project)
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        row.state, row.connection_id, row.last_connection = "merged", connection["id"], connection["id"]
        login = LoginSession(
            org_id=pool["org_id"],
            provider="claude",
            name="Done",
            status="done",
            connection_id=connection["id"],
        )
        session.add(login)
        session.flush()
        login_id = login.id

    response = client.delete(f"/api/connections/{connection['id']}")

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": connection["id"]}
    assert [item["id"] for item in client.get("/api/connections").json()] == [other["id"]]
    with db.Session() as session:
        row = session.get(db.Task, task["id"])
        assert row.connection_id is None and row.last_connection == connection["id"]
        assert session.get(LoginSession, login_id).connection_id is None
    assert client.delete(f"/api/connections/{connection['id']}").status_code == 404
    # The same account can be connected again.
    connect(client, pool)


@pytest.mark.parametrize("busy", ["lease", "task"])
def test_busy_connection_cannot_be_deleted(client, setup_project, busy):
    project, pool = setup_project
    connection = connect(client, pool)
    task = make_task(client, project)
    with db.transaction() as session:
        if busy == "lease":
            session.get(db.Connection, connection["id"]).lease_task = task["id"]
        else:
            row = session.get(db.Task, task["id"])
            row.state, row.connection_id = "running", connection["id"]
    assert client.delete(f"/api/connections/{connection['id']}").status_code == 409
    with db.Session() as session:
        assert session.get(db.Connection, connection["id"]) is not None


def test_active_login_blocks_connection_deletion(client, setup_project):
    _, pool = setup_project
    connection = connect(client, pool)
    with db.transaction() as session:
        session.add(
            LoginSession(
                org_id=pool["org_id"],
                provider="claude",
                name="Relogin",
                status="waiting",
                connection_id=connection["id"],
            )
        )
    assert client.delete(f"/api/connections/{connection['id']}").status_code == 409


@pytest.mark.parametrize("role,scoped", [("observer", False), ("operator", False), ("owner", True)])
def test_only_organization_owner_can_delete_connection(client, setup_project, role, scoped):
    project, pool = setup_project
    connection = connect(client, pool)
    token = client.post(
        "/api/tokens", json={"role": role, "project_ids": [project["id"]] if scoped else []}
    ).json()["token"]
    client.headers["Authorization"] = "Bearer " + token
    assert client.delete(f"/api/connections/{connection['id']}").status_code == 403
    with db.Session() as session:
        assert session.get(db.Connection, connection["id"]) is not None


def test_foreign_connection_cannot_be_deleted(client, setup_project):
    _, pool = setup_project
    connection = connect(client, pool)
    with db.transaction() as session:
        foreign = db.Organization(name="Foreign")
        session.add(foreign)
        session.flush()
        session.get(db.Connection, connection["id"]).org_id = foreign.id
    assert client.delete(f"/api/connections/{connection['id']}").status_code == 404
