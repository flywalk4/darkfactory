import os
import re
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import event

TEST_ROOT = Path(tempfile.mkdtemp(prefix="darkfactory-test-"))
os.environ["DATA_ROOT"] = str(TEST_ROOT)
os.environ["DATABASE_URL"] = "sqlite:///" + str(TEST_ROOT / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from darkfactory import db  # noqa: E402
from darkfactory.api import app  # noqa: E402

# The factory develops itself: its tests also run inside an agent session, whose own variables (Claude Code's
# CLAUDE_*, AI_AGENT, the task's proxies and evidence folder) must not change what the code under test sees.
AGENT_ENV = re.compile(
    r"^(CLAUDE|CLAUDECODE|ANTHROPIC|CODEX|OPENAI|AI_AGENT|DARKFACTORY_(?!TEST)|HTTPS?_PROXY|ALL_PROXY)", re.I
)


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch):
    for name in list(os.environ):
        if AGENT_ENV.match(name):
            monkeypatch.delenv(name, raising=False)


@event.listens_for(db.engine, "connect")
def enable_foreign_keys(connection, _record):
    connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture
def client():
    db.Base.metadata.drop_all(db.engine)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + (db.ROOT / "bootstrap-token").read_text()
        yield client


@pytest.fixture
def setup_project(client):
    pool = client.post("/api/pools", json={"name": "Subscriptions"}).json()
    project = client.post(
        "/api/projects",
        json={
            "name": "Pilot",
            "initialize": False,
            "prepare_environment": False,
            "remote": "https://example.invalid/pilot.git",
            "checks": ["python -m unittest"],
            "writer_pool": pool["id"],
            "reviewer_pool": pool["id"],
        },
    ).json()
    return project, pool


def make_task(client, project, **extra):
    response = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={
            "title": "Fix calculation",
            "request": "Make value equal 42. Preserve tests.",
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def connect(client, pool, provider="claude", account="account-1"):
    credentials = (
        {"oauth_token": "not-a-real-token-" + account}
        if provider == "claude"
        else {
            "accessToken": "not-a-real-token-" + account,
            "chatgptAccountId": account,
            "auth_method": "siwc",
        }
        if provider == "codex"
        else {"api_key": "not-a-real-key-" + account, "base_url": "https://example.invalid/v1"}
    )
    response = client.post(
        "/api/connections",
        json={
            "name": account,
            "account_key": account,
            "provider": provider,
            "pool_ids": [pool["id"]],
            "credentials": credentials,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
