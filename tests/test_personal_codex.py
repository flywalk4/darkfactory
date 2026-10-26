import json

import pytest
from fastapi import HTTPException

from darkfactory import runner
from darkfactory.api import check_credentials
from darkfactory.db import Connection, Session, decrypt
from tests.test_adapters import Process


def test_local_account_requires_explicit_personal_deployment(client, setup_project, monkeypatch):
    _, pool = setup_project
    monkeypatch.delenv("DARKFACTORY_PERSONAL_MODE", raising=False)
    credentials = {"auth_method": "codex_local", "accessToken": "personal-short-lived-access-token"}
    payload = {
        "name": "Personal Codex",
        "provider": "codex",
        "account_key": "personal-account",
        "pool_ids": [pool["id"]],
        "credentials": credentials,
    }
    assert client.post("/api/connections", json=payload).status_code == 400
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    assert client.post("/api/connections", json=payload).status_code == 400
    credentials["chatgptAccountId"] = "personal-account"
    credentials["base_url"] = "http://host.docker.internal:8787/v1"
    response = client.post("/api/connections", json=payload)
    assert response.status_code == 201, response.text
    assert "secret" not in response.json()
    assert credentials["accessToken"] not in response.text
    with Session() as db:
        connection = db.get(Connection, response.json()["id"])
        assert credentials["accessToken"] not in connection.secret
        assert json.loads(decrypt(connection.secret)) == credentials


def test_personal_runner_cannot_be_enabled_by_credentials_alone(monkeypatch):
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: pytest.fail("Must not start Codex"))
    assert runner.codex({"credentials": {"auth_method": "codex_local"}})["status"] == "auth"


@pytest.mark.parametrize("gateway", [None, "http://127.0.0.1:12345/v1"])
def test_personal_runner_uses_native_account_login_without_api_credentials(monkeypatch, gateway):
    process = Process(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"type": "chatgptAuthTokens"}},
            {"id": 4, "result": {"thread": {"id": "independent-session"}}},
            {"id": 5, "result": {}},
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "status": "completed",
                        "items": [
                            {"type": "agentMessage", "id": "answer", "phase": "final_answer", "text": "done"}
                        ],
                    }
                },
            },
        ]
    )
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "paid-key-must-never-be-used")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)) or process)
    result = runner.codex(
        {
            "credentials": {
                "auth_method": "codex_local",
                "accessToken": "header.claims.real-signature",
                "chatgptAccountId": "personal-account",
            },
            "personal_mode": True,
            "role": "implement",
            "prompt": "Fix bug",
        },
        gateway_url=gateway,
    )
    assert result == {"status": "ok", "text": "done", "session": "independent-session"}
    frames = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert frames[0]["params"]["capabilities"]["experimentalApi"] is True
    assert frames[2]["method"] == "account/login/start"
    # The CLI logs in with the claims only; relays add the real token to the backend and the gateway.
    assert frames[2]["params"] == {
        "type": "chatgptAuthTokens",
        "accessToken": "header.claims.ZGFya2ZhY3RvcnktcmVsYXk",
        "chatgptAccountId": "personal-account",
    }
    assert "real-signature" not in process.stdin.getvalue() and "real-signature" not in json.dumps(calls)
    assert any(arg.startswith('chatgpt_base_url="http://127.0.0.1:') for arg in calls[0][0])
    args, options = calls[0]
    assert not any("openai_chatgpt_plan" in arg for arg in args)
    if gateway:
        assert 'model_provider="personal_gateway"' in args
        relayed = next(arg for arg in args if arg.startswith("model_providers.personal_gateway.base_url="))
        assert relayed.startswith('model_providers.personal_gateway.base_url="http://127.0.0.1:')
        # A relay in front of the gateway keeps the gateway's path.
        assert relayed.endswith(
            '/v1"'
        ) and relayed != "model_providers.personal_gateway.base_url=" + json.dumps(gateway)
        assert "model_providers.personal_gateway.requires_openai_auth=true" in args
    else:
        assert 'model_provider="openai"' in args
    assert not any(arg.startswith("model_providers.openai.") for arg in args)
    assert "ACCESS_TOKEN" not in options["env"]
    assert "OPENAI_API_KEY" not in options["env"]


@pytest.mark.parametrize(
    "url",
    [
        "https://host.docker.internal:8787/v1",
        "http://example.com/v1",
        "http://localhost:0/v1",
        "http://localhost:99999/v1",
        "http://user:secret@localhost/v1",
        "http://localhost/v1?token=secret",
        "http://localhost/v1#fragment",
        "http://localhost/v1\ninjection",
        "http://localhost/evil\\path",
    ],
)
def test_personal_gateway_rejects_remote_or_ambiguous_urls(monkeypatch, url):
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    with pytest.raises(HTTPException) as error:
        check_credentials(
            "codex",
            {
                "auth_method": "codex_local",
                "accessToken": "test-access",
                "chatgptAccountId": "test-account",
                "base_url": url,
            },
        )
    assert error.value.status_code == 400
