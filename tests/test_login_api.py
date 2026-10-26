import json

import pytest

from darkfactory import auth_sessions
from darkfactory.db import Session, decrypt, encrypt, now, transaction


def create_login(client, pool, **extra):
    return client.post(
        "/api/login-sessions",
        json={
            "provider": "claude",
            "name": "Personal Claude",
            "account_key": "owner@example.invalid",
            "pool_ids": [pool["id"]],
            **extra,
        },
    )


def change_login(identity, **values):
    with transaction() as db:
        row = db.get(auth_sessions.LoginSession, identity)
        for key, value in values.items():
            setattr(row, key, value)


@pytest.fixture(autouse=True)
def no_real_auth(monkeypatch):
    monkeypatch.setattr(auth_sessions.docker, "from_env", lambda: pytest.fail("API must not start Docker"))
    monkeypatch.setattr(
        auth_sessions.LoginFlow, "start", lambda self: pytest.fail("API must not start login")
    )


def test_owner_creates_durable_login_with_defaults_and_no_secrets(client, setup_project, monkeypatch):
    _, pool = setup_project
    response = create_login(client, pool, pool_ids=[pool["id"], pool["id"]])
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["status"] == "queued"
    assert value["priority"] == 100 and value["model"] is None
    assert value["pool_ids"] == [pool["id"]]
    assert value["expires"] - value["created"] <= 901
    assert value["expires"] > now()
    assert not {"command_secret", "credentials", "container_id", "controller_id", "snapshot"} & value.keys()
    with Session() as db:
        row = db.get(auth_sessions.LoginSession, value["id"])
        assert row.command_secret is None and row.container_id is None
    assert client.get("/api/login-sessions/" + value["id"]).json() == value
    assert [item["id"] for item in client.get("/api/login-sessions").json()] == [value["id"]]
    monkeypatch.delenv("DARKFACTORY_PERSONAL_MODE", raising=False)
    assert client.get("/api/me").json()["personal_mode"] is False
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    assert client.get("/api/me").json()["personal_mode"] is True


def test_create_validates_provider_identity_and_pool_ownership(client, setup_project):
    _, pool = setup_project
    assert create_login(client, pool, provider="api").status_code == 422
    assert create_login(client, pool, account_key="  ").status_code == 400
    assert create_login(client, pool, pool_ids=[]).status_code == 422
    assert create_login(client, pool, pool_ids=["missing-pool"]).status_code == 404
    first_auth = client.headers["Authorization"]
    other = client.post("/api/organizations", json={"name": "Other company"}).json()
    client.headers["Authorization"] = "Bearer " + other["token"]
    other_pool = client.post("/api/pools", json={"name": "Other subscriptions"}).json()
    client.headers["Authorization"] = first_auth
    assert create_login(client, pool, pool_ids=[other_pool["id"]]).status_code == 404


def test_codex_guided_login_requires_personal_deployment(client, setup_project, monkeypatch):
    _, pool = setup_project
    monkeypatch.delenv("DARKFACTORY_PERSONAL_MODE", raising=False)
    assert create_login(client, pool, provider="codex", account_key="").status_code == 400
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    assert create_login(client, pool, provider="codex", account_key="").status_code == 201


@pytest.mark.parametrize("role,project_scope", [("owner", True), ("operator", False), ("observer", False)])
def test_members_without_organization_owner_cannot_manage_logins(client, setup_project, role, project_scope):
    project, pool = setup_project
    identity = create_login(client, pool).json()["id"]
    member = client.post(
        "/api/members",
        json={
            "name": "Restricted member",
            "role": role,
            "project_ids": [project["id"]] if project_scope else [],
        },
    )
    assert member.status_code == 201, member.text
    client.headers["Authorization"] = "Bearer " + member.json()["token"]
    assert create_login(client, pool).status_code == 403
    assert client.get("/api/login-sessions").status_code == 403
    assert client.get("/api/login-sessions/" + identity).status_code == 403
    assert client.post(f"/api/login-sessions/{identity}/code", json={"code": "fake#state"}).status_code == 403
    assert client.post(f"/api/login-sessions/{identity}/cancel").status_code == 403


def test_organization_owner_project_token_cannot_manage_account_pool(client, setup_project):
    project, pool = setup_project
    identity = create_login(client, pool).json()["id"]
    token = client.post("/api/tokens", json={"role": "owner", "project_ids": [project["id"]]}).json()
    client.headers["Authorization"] = "Bearer " + token["token"]
    assert create_login(client, pool).status_code == 403
    assert client.get("/api/login-sessions").status_code == 403
    assert client.get("/api/login-sessions/" + identity).status_code == 403
    assert client.post(f"/api/login-sessions/{identity}/cancel").status_code == 403


def test_cross_organization_owner_cannot_read_submit_or_cancel_login(client, setup_project):
    _, pool = setup_project
    identity = create_login(client, pool).json()["id"]
    other = client.post("/api/organizations", json={"name": "Isolated company"}).json()
    client.headers["Authorization"] = "Bearer " + other["token"]
    assert client.get("/api/login-sessions").json() == []
    assert client.get("/api/login-sessions/" + identity).status_code == 404
    assert client.post(f"/api/login-sessions/{identity}/code", json={"code": "fake#state"}).status_code == 404
    assert client.post(f"/api/login-sessions/{identity}/cancel").status_code == 404


def test_public_login_strips_untrusted_snapshot_error_and_encrypted_command(client, setup_project):
    _, pool = setup_project
    identity = create_login(client, pool).json()["id"]
    secret = "super-private-callback-token"
    change_login(
        identity,
        status="waiting",
        error=secret,
        command_secret=encrypt(secret),
        snapshot={
            "verification_url": "https://claude.com/cai/oauth/authorize?access_token=" + secret,
            "user_code": secret,
            "prompt": secret,
            "credentials": {"oauth_token": secret},
        },
    )
    for path in ("/api/login-sessions", "/api/login-sessions/" + identity):
        response = client.get(path)
        assert response.status_code == 200
        assert secret not in response.text
        value = response.json()
        value = value[0] if isinstance(value, list) else value
        assert value["verification_url"] is None
        assert value["user_code"] is None
        assert (
            not {"command_secret", "credentials", "container_id", "controller_id", "snapshot"} & value.keys()
        )


def test_browser_code_encrypted_pending_code_rejected_and_cancel_wipes_it(client, setup_project):
    _, pool = setup_project
    identity = create_login(client, pool).json()["id"]
    change_login(identity, status="waiting")
    code = "fake-browser-code#fake-state"
    response = client.post(f"/api/login-sessions/{identity}/code", json={"code": " " + code + " "})
    assert response.status_code == 200, response.text
    assert code not in response.text
    with Session() as db:
        row = db.get(auth_sessions.LoginSession, identity)
        assert code not in row.command_secret
        assert json.loads(decrypt(row.command_secret)) == {"action": "code", "code": code}
    assert client.post(f"/api/login-sessions/{identity}/code", json={"code": "other#code"}).status_code == 409
    cancelled = client.post(f"/api/login-sessions/{identity}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert code not in cancelled.text
    with Session() as db:
        row = db.get(auth_sessions.LoginSession, identity)
        assert row.command_secret is None and row.snapshot == {}


@pytest.mark.parametrize("status,expired", [("starting", False), ("done", False), ("waiting", True)])
def test_browser_code_requires_waiting_unexpired_session(client, setup_project, status, expired):
    _, pool = setup_project
    identity = create_login(client, pool).json()["id"]
    change_login(identity, status=status, expires=now() - 1 if expired else now() + 60)
    assert client.post(f"/api/login-sessions/{identity}/code", json={"code": "fake#state"}).status_code == 409
    if status == "done":
        assert client.post(f"/api/login-sessions/{identity}/cancel").status_code == 409


def test_codex_code_submission_and_multiline_claude_code_rejected(client, setup_project, monkeypatch):
    _, pool = setup_project
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    codex = create_login(client, pool, provider="codex").json()["id"]
    change_login(codex, status="waiting")
    assert client.post(f"/api/login-sessions/{codex}/code", json={"code": "fake#state"}).status_code == 409
    claude = create_login(client, pool).json()["id"]
    change_login(claude, status="waiting")
    assert (
        client.post(f"/api/login-sessions/{claude}/code", json={"code": "fake#state\nother"}).status_code
        == 400
    )


def test_malformed_browser_code_keeps_flow_waiting(client, setup_project):
    _, pool = setup_project
    identity = create_login(client, pool).json()["id"]
    change_login(identity, status="waiting")
    response = client.post(f"/api/login-sessions/{identity}/code", json={"code": "garbage"})
    assert response.status_code == 400
    with Session() as db:
        row = db.get(auth_sessions.LoginSession, identity)
        assert row.status == "waiting" and row.command_secret is None


def test_three_active_logins_limit_is_per_organization_and_terminal_slot_reopens(client, setup_project):
    _, pool = setup_project
    identities = [create_login(client, pool).json()["id"] for _ in range(3)]
    assert create_login(client, pool).status_code == 409
    change_login(identities[0], status="failed")
    assert create_login(client, pool).status_code == 201
    other = client.post("/api/organizations", json={"name": "Independent quota"}).json()
    client.headers["Authorization"] = "Bearer " + other["token"]
    other_pool = client.post("/api/pools", json={"name": "Other pool"}).json()
    assert create_login(client, other_pool).status_code == 201
