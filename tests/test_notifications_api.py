import json

import pytest
from conftest import make_task

from darkfactory import db
from darkfactory.notifications import IMPORTANT_KINDS, NotificationChannel, NotificationDelivery

TOKEN = "123456:private-test-token"
CHAT = "-10099887766"
WEBHOOK = "https://chat.corp.internal/hooks/private-integration/private-secret"


def channel_payload(project_id=None, provider="telegram", **extra):
    return {
        "name": "Operators",
        "project_id": project_id,
        "provider": provider,
        "credentials": {"bot_token": TOKEN, "chat_id": CHAT}
        if provider == "telegram"
        else {"webhook_url": WEBHOOK},
        **extra,
    }


def create_channel(client, project_id=None, **extra):
    response = client.post("/api/notification-channels", json=channel_payload(project_id, **extra))
    assert response.status_code == 201, response.text
    return response.json()


def another_project(client):
    response = client.post(
        "/api/projects",
        json={"name": "Other project", "remote": "https://example.invalid/other.git"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def scoped_token(client, project, role="owner"):
    response = client.post("/api/tokens", json={"role": role, "project_ids": [project["id"]]})
    assert response.status_code == 201, response.text
    return "Bearer " + response.json()["token"]


def test_channel_secrets_encrypt_preserve_and_never_echo(client, setup_project):
    project, _ = setup_project
    assert client.get("/api/notification-events").json() == sorted(IMPORTANT_KINDS)
    created = create_channel(client, project["id"])
    assert created["events"] == sorted(IMPORTANT_KINDS)
    for secret in (TOKEN, CHAT, "credentials", "secret"):
        assert secret not in json.dumps(created)
    with db.Session() as session:
        stored = session.get(NotificationChannel, created["id"])
        encrypted = stored.secret
        assert TOKEN not in encrypted and CHAT not in encrypted
        assert json.loads(db.decrypt(encrypted)) == {"bot_token": TOKEN, "chat_id": CHAT}
    response = client.put(
        "/api/notification-channels/" + created["id"],
        json={
            "name": "Renamed",
            "project_id": project["id"],
            "provider": "telegram",
            "enabled": False,
            "events": ["human_message"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    with db.Session() as session:
        assert session.get(NotificationChannel, created["id"]).secret == encrypted
    listed = client.get("/api/notification-channels")
    assert listed.json()[0]["name"] == "Renamed"
    assert all(secret not in listed.text for secret in (TOKEN, CHAT, encrypted))


def test_provider_change_needs_new_credentials_and_validates_events(client, setup_project):
    project, _ = setup_project
    created = create_channel(client, project["id"])
    body = {"name": "Rocket", "provider": "rocketchat", "project_id": project["id"]}
    changed = client.put("/api/notification-channels/" + created["id"], json=body)
    assert changed.status_code == 400
    body["credentials"] = {"webhook_url": "http://insecure.internal/hook"}
    assert client.put("/api/notification-channels/" + created["id"], json=body).status_code == 400
    body["credentials"] = {"webhook_url": WEBHOOK}
    body["events"] = ["phase_finished"]
    assert client.put("/api/notification-channels/" + created["id"], json=body).status_code == 400
    body["events"] = ["accepted"]
    changed = client.put("/api/notification-channels/" + created["id"], json=body)
    assert changed.status_code == 200, changed.text
    assert WEBHOOK not in changed.text
    with db.Session() as session:
        row = session.get(NotificationChannel, created["id"])
        assert row.provider == "rocketchat"
        assert json.loads(db.decrypt(row.secret)) == {"webhook_url": WEBHOOK}


def test_project_owner_cannot_see_or_change_organization_channels(client, setup_project):
    project, _ = setup_project
    other = another_project(client)
    organization_channel = create_channel(client)
    own_channel = create_channel(client, project["id"])
    other_channel = create_channel(client, other["id"])
    client.headers["Authorization"] = scoped_token(client, project)
    listed = client.get("/api/notification-channels")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [own_channel["id"]]
    assert client.get("/api/notification-events").status_code == 200
    assert client.post("/api/notification-channels", json=channel_payload()).status_code == 403
    assert client.post("/api/notification-channels", json=channel_payload(other["id"])).status_code == 404
    assert (
        client.put(
            "/api/notification-channels/" + organization_channel["id"],
            json=channel_payload(project["id"]),
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/notification-channels/" + other_channel["id"],
            json=channel_payload(project["id"]),
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/api/notification-channels/" + own_channel["id"],
            json=channel_payload(),
        ).status_code
        == 403
    )
    assert create_channel(client, project["id"], name="Scoped destination")["project_id"] == project["id"]


@pytest.mark.parametrize("role", ["operator", "observer"])
def test_read_only_or_operator_tokens_cannot_manage_destinations(client, setup_project, role):
    project, _ = setup_project
    created = create_channel(client, project["id"])
    task = make_task(client, project)
    client.headers["Authorization"] = scoped_token(client, project, role)
    for path in ("/api/notification-events", "/api/notification-channels", "/api/notification-deliveries"):
        assert client.get(path).status_code == 403
    assert client.post("/api/notification-channels", json=channel_payload(project["id"])).status_code == 403
    assert (
        client.put(
            "/api/notification-channels/" + created["id"],
            json=channel_payload(project["id"]),
        ).status_code
        == 403
    )
    response = client.post(f"/api/tasks/{task['id']}/notify", json={"message": "Need an answer"})
    assert response.status_code == (200 if role == "operator" else 403)


def test_channel_and_task_boundaries_reject_other_organization(client, setup_project):
    project, _ = setup_project
    created = create_channel(client, project["id"])
    task = make_task(client, project)
    other = client.post("/api/organizations", json={"name": "Another company"}).json()
    client.headers["Authorization"] = "Bearer " + other["token"]
    assert client.get("/api/notification-channels").json() == []
    assert client.get("/api/notification-deliveries").json() == []
    assert (
        client.put(
            "/api/notification-channels/" + created["id"],
            json=channel_payload(),
        ).status_code
        == 404
    )
    assert client.post("/api/notification-channels", json=channel_payload(project["id"])).status_code == 404
    assert client.post(f"/api/tasks/{task['id']}/notify", json={"message": "Cross org"}).status_code == 404


def test_explicit_task_message_queues_only_opted_in_matching_channels(client, setup_project):
    project, _ = setup_project
    other = another_project(client)
    org_channel = create_channel(client, events=["human_message"])
    own_channel = create_channel(client, project["id"], events=["human_message"])
    create_channel(client, project["id"], events=["human_message"], enabled=False)
    create_channel(client, project["id"], events=["accepted"])
    create_channel(client, other["id"], events=["human_message"])
    task = make_task(client, project)
    client.headers["Authorization"] = scoped_token(client, project, "operator")
    response = client.post(f"/api/tasks/{task['id']}/notify", json={"message": "Please clarify acceptance"})
    assert response.status_code == 200, response.text
    with db.Session() as session:
        queued = session.query(NotificationDelivery).all()
        assert {item.channel_id for item in queued} == {org_channel["id"], own_channel["id"]}
        assert all(item.state == "pending" and item.attempts == 0 for item in queued)
        assert all("Please clarify acceptance" in item.message for item in queued)
        events = session.query(db.Event).filter_by(task_id=task["id"], kind="human_message").all()
        assert len(events) == 1
        assert all(item.event_id == events[0].id for item in queued)


def test_delivery_history_respects_project_scope_and_redacts_channel_secrets(client, setup_project):
    project, _ = setup_project
    other = another_project(client)
    create_channel(client)
    own_task, other_task = make_task(client, project), make_task(client, other)
    assert client.post(f"/api/tasks/{own_task['id']}/notify", json={"message": "Own task"}).status_code == 200
    assert (
        client.post(f"/api/tasks/{other_task['id']}/notify", json={"message": "Other task"}).status_code
        == 200
    )
    client.headers["Authorization"] = scoped_token(client, project)
    response = client.get("/api/notification-deliveries")
    assert response.status_code == 200
    assert {item["task_id"] for item in response.json()} == {own_task["id"]}
    assert all(secret not in response.text for secret in (TOKEN, CHAT, WEBHOOK))
    assert all("lease_owner" not in item for item in response.json())


def test_command_token_must_not_be_an_owner_and_senders_are_validated(client, setup_project):
    owner = (db.ROOT / "bootstrap-token").read_text()
    operator = client.post("/api/members", json={"name": "Chat", "role": "operator"}).json()["token"]
    telegram = {"bot_token": "123456:fake-bot-token", "chat_id": "-100123456"}

    def save(**credentials):
        return client.post(
            "/api/notification-channels",
            json={"name": "Ops", "provider": "telegram", "credentials": {**telegram, **credentials}},
        )

    assert save(director_token=owner).status_code == 400
    assert save(director_token=operator, allowed_senders=["bad id"]).status_code == 400
    saved = save(director_token=operator, allowed_senders=[42, "77"])
    assert saved.status_code == 201, saved.text
    with db.Session() as session:
        config = json.loads(db.decrypt(session.get(NotificationChannel, saved.json()["id"]).secret))
    assert config["allowed_senders"] == ["42", "77"]
