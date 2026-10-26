import pytest
from fastapi.testclient import TestClient

from darkfactory import db
from darkfactory.api import app


def bearer(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def fresh(monkeypatch):
    db.Base.metadata.drop_all(db.engine)
    (db.ROOT / "bootstrap-token").unlink(missing_ok=True)
    yield
    db.Base.metadata.drop_all(db.engine)
    (db.ROOT / "bootstrap-token").unlink(missing_ok=True)


def test_fresh_install_takes_the_owner_token_from_env(fresh, monkeypatch):
    monkeypatch.setenv("DARKFACTORY_ADMIN_TOKEN", "first-admin-token")
    with TestClient(app) as client:
        me = client.get("/api/me", headers=bearer("first-admin-token")).json()
        assert me["role"] == "owner" and me["token_role"] == "owner"
    assert not (db.ROOT / "bootstrap-token").exists()


def test_new_env_value_replaces_the_previous_env_token(fresh, monkeypatch):
    monkeypatch.setenv("DARKFACTORY_ADMIN_TOKEN", "old-admin-token")
    db.initialize()
    monkeypatch.setenv("DARKFACTORY_ADMIN_TOKEN", "new-admin-token")
    with TestClient(app) as client:
        assert client.get("/api/me", headers=bearer("old-admin-token")).status_code == 401
        assert client.get("/api/me", headers=bearer("new-admin-token")).json()["role"] == "owner"
    db.initialize()
    with db.Session() as session:
        assert session.query(db.Token).filter(db.Token.from_env, ~db.Token.revoked).count() == 1


def test_env_token_joins_an_existing_bootstrap_install(fresh, monkeypatch):
    monkeypatch.delenv("DARKFACTORY_ADMIN_TOKEN", raising=False)
    db.initialize()
    bootstrap = (db.ROOT / "bootstrap-token").read_text()
    monkeypatch.setenv("DARKFACTORY_ADMIN_TOKEN", "added-admin-token")
    with TestClient(app) as client:
        first = client.get("/api/me", headers=bearer(bootstrap)).json()
        second = client.get("/api/me", headers=bearer("added-admin-token")).json()
        assert first["id"] == second["id"]


def test_revoked_env_token_is_not_restored_on_restart(fresh, monkeypatch):
    monkeypatch.setenv("DARKFACTORY_ADMIN_TOKEN", "admin-token")
    with TestClient(app) as client:
        client.headers.update(bearer("admin-token"))
        token_id = next(t["id"] for t in client.get("/api/tokens").json() if t["from_env"])
        assert client.delete(f"/api/tokens/{token_id}").status_code == 200
    with TestClient(app) as client:
        assert client.get("/api/me", headers=bearer("admin-token")).status_code == 401


def test_owner_adds_second_admin_and_reissues_member_tokens(client):
    admin = client.post("/api/members", json={"name": "Second admin", "role": "owner"}).json()
    second = TestClient(app, headers=bearer(admin["token"]))
    assert second.get("/api/members").status_code == 200
    observer = second.post("/api/members", json={"name": "Reader", "role": "observer"}).json()
    reader = bearer(observer["token"])
    assert client.get("/api/members", headers=reader).status_code == 403

    reissued = second.post(f"/api/members/{observer['id']}/token")
    assert reissued.status_code == 201
    assert client.get("/api/me", headers=reader).status_code == 401
    assert client.get("/api/me", headers=bearer(reissued.json()["token"])).json()["role"] == "observer"

    assert second.post(f"/api/members/{admin['id']}/token").status_code == 400
    reader = bearer(reissued.json()["token"])
    assert client.post(f"/api/members/{admin['id']}/token", headers=reader).status_code == 403
