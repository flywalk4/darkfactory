import json

import pytest
from conftest import connect

from darkfactory import db, director
from darkfactory.auth_sessions import LoginSession


def test_delete_pool_preserves_connections_credentials_and_other_memberships(client):
    pool = client.post("/api/pools", json={"name": "Disposable"}).json()
    other = client.post("/api/pools", json={"name": "Keep"}).json()
    connection = connect(client, pool)
    unassigned = connect(client, pool, account="unassigned")
    with db.transaction() as session:
        row = session.get(db.Connection, connection["id"])
        row.pool_ids = [pool["id"], other["id"]]
        row.lease_task = "running-director"
        original_secret = row.secret
    response = client.delete(f"/api/pools/{pool['id']}")
    assert response.status_code == 200
    assert response.json() == {"deleted": pool["id"]}
    assert [item["id"] for item in client.get("/api/pools").json()] == [other["id"]]
    with db.Session() as session:
        row = session.get(db.Connection, connection["id"])
        assert row.pool_ids == [other["id"]]
        assert row.secret == original_secret
        assert row.lease_task == "running-director"
        assert session.get(db.Connection, unassigned["id"]).pool_ids == []
    assert client.delete(f"/api/pools/{pool['id']}").status_code == 404


@pytest.mark.parametrize("field", ["writer_pool", "reviewer_pool"])
def test_project_reference_blocks_deletion_without_changing_memberships(client, setup_project, field):
    project, pool = setup_project
    connection = connect(client, pool)
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        row.writer_pool = row.reviewer_pool = None
        setattr(row, field, pool["id"])
    response = client.delete(f"/api/pools/{pool['id']}")
    assert response.status_code == 409
    assert project["name"] in response.json()["detail"]
    with db.Session() as session:
        assert session.get(db.Pool, pool["id"]) is not None
        assert session.get(db.Connection, connection["id"]).pool_ids == [pool["id"]]


@pytest.mark.parametrize("status", ["queued", "starting", "waiting"])
def test_active_login_blocks_deletion_until_finished(client, status):
    pool = client.post("/api/pools", json={"name": "Login target"}).json()
    with db.transaction() as session:
        login = LoginSession(
            org_id=pool["org_id"], provider="claude", name="Login", pool_ids=[pool["id"]], status=status
        )
        session.add(login)
        session.flush()
        login_id = login.id
    assert client.delete(f"/api/pools/{pool['id']}").status_code == 409
    with db.transaction() as session:
        session.get(LoginSession, login_id).status = "cancelled"
    assert client.delete(f"/api/pools/{pool['id']}").status_code == 200


@pytest.mark.parametrize("role,scoped", [("observer", False), ("operator", False), ("owner", True)])
def test_only_organization_owner_can_delete_pool(client, setup_project, role, scoped):
    project, _ = setup_project
    pool = client.post("/api/pools", json={"name": "Unused"}).json()
    token = client.post(
        "/api/tokens", json={"role": role, "project_ids": [project["id"]] if scoped else []}
    ).json()["token"]
    client.headers["Authorization"] = "Bearer " + token
    assert client.delete(f"/api/pools/{pool['id']}").status_code == 403
    with db.Session() as session:
        assert session.get(db.Pool, pool["id"]) is not None


def test_foreign_pool_not_visible_or_mutable(client):
    pool = client.post("/api/pools", json={"name": "Other organization"}).json()
    with db.transaction() as session:
        foreign = db.Organization(name="Foreign")
        session.add(foreign)
        session.flush()
        session.get(db.Pool, pool["id"]).org_id = foreign.id
    assert client.delete(f"/api/pools/{pool['id']}").status_code == 404
    with db.Session() as session:
        assert session.get(db.Pool, pool["id"]) is not None


def test_director_can_request_pool_deletion():
    action = director.Action(method="DELETE", path="/api/pools/abc123", body_json=json.dumps({}))
    assert director.validate_action(action) == {}
