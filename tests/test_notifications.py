import json

import httpx
import pytest

from darkfactory import db
from darkfactory.notifications import (
    IMPORTANT_KINDS,
    MAX_ATTEMPTS,
    NotificationChannel,
    NotificationDelivery,
    dispatch_notifications,
    public_channel,
    queue_notification,
    validate_channel_config,
)

TOKEN = "123456:fake-bot-token"
WEBHOOK = "https://chat.corp.internal/hooks/integration/secret-token"


@pytest.fixture(autouse=True)
def without_supervisor(monkeypatch):
    # Owner-facing stops are paged directly when the supervisor has no budget left.
    monkeypatch.setenv("DARKFACTORY_DIRECTOR_LIMIT", "0")


@pytest.fixture
def task(setup_project):
    project, _ = setup_project
    with db.transaction() as session:
        task = db.Task(
            org_id=project["org_id"],
            project_id=project["id"],
            title="Fix calculation",
            request="Make value 42",
            branch="factory/test",
            state="needs_human",
        )
        session.add(task)
        session.flush()
        return task


def channel(task, provider="telegram", **extra):
    config = (
        {"bot_token": TOKEN, "chat_id": "-100123456"} if provider == "telegram" else {"webhook_url": WEBHOOK}
    )
    with db.transaction() as session:
        value = NotificationChannel(
            org_id=task.org_id,
            name="Operators",
            provider=provider,
            secret=db.encrypt(json.dumps(config)),
            **extra,
        )
        session.add(value)
        session.flush()
        return value


def enqueue(task, kind="clarification", message="Need an answer", event_id=None):
    with db.transaction() as session:
        return queue_notification(session, task, kind, message, event_id=event_id)


def delivery():
    with db.Session() as session:
        return session.query(NotificationDelivery).one()


def test_channel_validation_and_public_secrets(task):
    value = channel(task)
    assert "secret" not in public_channel(value)
    assert TOKEN not in json.dumps(public_channel(value))
    assert TOKEN not in value.secret
    assert validate_channel_config("rocketchat", {"webhook_url": WEBHOOK}) == {"webhook_url": WEBHOOK}
    for url in ["http://chat.example/hooks/x", "https://u:p@chat.example/hooks/x", "https://chat/#secret"]:
        with pytest.raises(ValueError, match="HTTPS"):
            validate_channel_config("rocketchat", {"webhook_url": url})
    with pytest.raises(ValueError, match="bot token"):
        validate_channel_config("telegram", {"bot_token": "../../attacker", "chat_id": "1"})
    with pytest.raises(ValueError, match="chat ID"):
        validate_channel_config("telegram", {"bot_token": TOKEN, "chat_id": "https://attacker"})


def test_only_important_events_queue_once(task):
    channel(task)
    assert enqueue(task, kind="phase_finished") == 0
    assert enqueue(task, kind="switch") == 0
    assert enqueue(task) == 1
    assert enqueue(task) == 0
    assert delivery().state == "pending"
    assert "Fix calculation" in delivery().message
    assert "Make value 42" not in delivery().message


def test_event_id_dedupes_but_new_event_notifies(task):
    channel(task)
    with db.transaction() as session:
        first = db.Event(task_id=task.id, kind="limit", message="Time exhausted")
        second = db.Event(task_id=task.id, kind="limit", message="Time exhausted")
        session.add_all([first, second])
        session.flush()
        assert queue_notification(session, task, "limit", first.message, first.id) == 1
        assert queue_notification(session, task, "limit", first.message, first.id) == 0
        assert queue_notification(session, task, "limit", second.message, second.id) == 1


def test_channels_scope_and_event_preferences(task):
    expected = channel(task, project_id=task.project_id)
    channel(task, enabled=False)
    channel(task, events=["accepted"])
    with db.transaction() as session:
        other = db.Organization(name="Other company")
        session.add(other)
        session.flush()
        project = db.Project(org_id=task.org_id, name="Other project", remote="/tmp/git")
        session.add(project)
        session.flush()
        session.add(
            NotificationChannel(
                org_id=other.id,
                name="Other org",
                provider="telegram",
                secret="invalid",
            )
        )
        session.add(
            NotificationChannel(
                org_id=task.org_id,
                project_id=project.id,
                name="Other project",
                provider="telegram",
                secret="invalid",
            )
        )
    assert enqueue(task) == 1
    assert delivery().channel_id == expected.id


@pytest.mark.parametrize("kind", ["clarification", "limit", "stopped", "execution_error", "needs_human"])
def test_human_states_required(task, kind):
    channel(task)
    task.state = "running"
    assert enqueue(task, kind) == 0
    task.state = "needs_human"
    assert enqueue(task, kind) == 1


def test_merged_and_incident_notifications(task):
    channel(task)
    assert enqueue(task, "accepted") == 0
    task.state = "merged"
    assert enqueue(task, "accepted") == 1
    task.state = "queued"
    assert enqueue(task, "incident_detected") == 1
    assert enqueue(task, "incident_reopened") == 1
    assert "human_message" in IMPORTANT_KINDS


@pytest.mark.parametrize("provider", ["telegram", "rocketchat"])
def test_offline_delivery_payload_and_no_open_db_transaction(task, provider):
    channel(task, provider=provider)
    enqueue(task, message=f"Need answer {TOKEN} {WEBHOOK}")
    calls = []

    def handler(request):
        # Re-enter dispatcher from the transport: the active row must be leased.
        assert dispatch_notifications(client=client) == 0
        calls.append(request)
        payload = json.loads(request.content)
        assert TOKEN not in payload["text"] if provider == "telegram" else WEBHOOK not in payload["text"]
        if provider == "telegram":
            assert str(request.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
            assert payload["chat_id"] == "-100123456"
            assert "parse_mode" not in payload
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        assert str(request.url) == WEBHOOK
        assert set(payload) == {"text", "alias", "emoji"}
        return httpx.Response(200, json={"success": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dispatch_notifications(client=client) == 1
        assert dispatch_notifications(client=client) == 0
    assert len(calls) == 1
    assert delivery().state == "sent"
    assert delivery().attempts == 1


def test_429_waits_then_retries_and_error_never_retains_token(task):
    channel(task)
    enqueue(task)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                429,
                json={"ok": False, "description": TOKEN, "parameters": {"retry_after": 300}},
            )
        )
    ) as client:
        before = db.now()
        assert dispatch_notifications(client=client) == 1
        assert dispatch_notifications(client=client) == 0
    value = delivery()
    assert value.state == "pending"
    assert value.retry_at >= before + 300
    assert TOKEN not in value.last_error
    assert value.lease_owner is None


def test_redirect_is_not_followed(task):
    channel(task, provider="rocketchat")
    enqueue(task)
    seen = []

    def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://attacker.invalid/capture"})

    with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        assert dispatch_notifications(client=client) == 1
    assert seen == [WEBHOOK]
    assert delivery().state == "failed"
    assert WEBHOOK not in delivery().last_error


def test_backoff_attempt_bound_and_expired_lease_recovery(task):
    channel(task)
    enqueue(task)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(
                httpx.ConnectError(f"secret {TOKEN} {WEBHOOK}", request=request),
            )
        )
    ) as client:
        for attempt in range(MAX_ATTEMPTS):
            assert dispatch_notifications(client=client) == 1
            with db.transaction() as session:
                value = session.query(NotificationDelivery).one()
                assert value.attempts == attempt + 1
                assert TOKEN not in value.last_error
                if attempt < MAX_ATTEMPTS - 1:
                    assert value.retry_at > db.now()
                    # Simulate a worker dying while owning its next attempt.
                    value.state = "sending"
                    value.lease_owner = "dead-worker"
                    value.lease_until = db.now() - 1
        assert dispatch_notifications(client=client) == 0
    assert delivery().state == "failed"


def test_disabled_channel_cancels_pending_send(task):
    value = channel(task)
    enqueue(task)
    with db.transaction() as session:
        session.get(NotificationChannel, value.id).enabled = False
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected network call"))
    ) as client:
        assert dispatch_notifications(client=client) == 1
    assert delivery().state == "skipped"


def test_invalid_credentials_are_redacted_permanent_failure(task):
    value = channel(task)
    enqueue(task)
    with db.transaction() as session:
        session.get(NotificationChannel, value.id).secret = "not-valid-encryption"
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected network call"))
    ) as client:
        assert dispatch_notifications(client=client) == 1
    assert delivery().state == "failed"
    assert delivery().last_error == "Invalid notification credentials"


@pytest.mark.parametrize("provider", ["telegram", "rocketchat"])
def test_successful_http_requires_provider_acknowledgement(task, provider):
    channel(task, provider=provider)
    enqueue(task)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as client:
        assert dispatch_notifications(client=client) == 1
    assert delivery().state == "pending"
    assert "acknowledge" in delivery().last_error


def test_expired_worker_cannot_overwrite_new_lease(task):
    channel(task)
    enqueue(task)

    def handler(_request):
        with db.transaction() as session:
            value = session.query(NotificationDelivery).one()
            value.lease_owner = "replacement-worker"
            value.last_error = "Replacement owns this delivery"
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dispatch_notifications(client=client) == 1
    assert delivery().lease_owner == "replacement-worker"
    assert delivery().state == "sending"
    assert delivery().last_error == "Replacement owns this delivery"


@pytest.mark.parametrize("recipient", ["@vadim", "#engineering"])
def test_rocket_recipient_is_sent_in_webhook(task, recipient):
    channel(task, provider="rocketchat", recipient=recipient)
    enqueue(task)
    sent = []

    def receive(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    with httpx.Client(transport=httpx.MockTransport(receive)) as http:
        assert dispatch_notifications(client=http) == 1
    assert sent[0]["channel"] == recipient
    assert delivery().state == "sent"


def test_recipient_edit_preserves_webhook_and_is_public(client, task):
    value = channel(task, provider="rocketchat", recipient="@old")
    response = client.put(
        "/api/notification-channels/" + value.id,
        json={
            "name": "Rocket team",
            "provider": "rocketchat",
            "recipient": "#new-team",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["recipient"] == "#new-team"
    assert WEBHOOK not in response.text
    with db.Session() as session:
        updated = session.get(NotificationChannel, value.id)
        assert json.loads(db.decrypt(updated.secret))["webhook_url"] == WEBHOOK
    assert client.get("/api/notification-channels").json()[0]["recipient"] == "#new-team"
    for invalid in ["vadim", "@name\nother", "@user,#room", "@", "#room with space"]:
        assert (
            client.put(
                "/api/notification-channels/" + value.id,
                json={
                    "name": "Rocket team",
                    "provider": "rocketchat",
                    "recipient": invalid,
                },
            ).status_code
            == 422
        )
    # API clients from the previous version may omit recipient without erasing it.
    assert (
        client.put(
            "/api/notification-channels/" + value.id,
            json={
                "name": "Renamed",
                "provider": "rocketchat",
            },
        ).json()["recipient"]
        == "#new-team"
    )


def test_channels_saved_before_new_kinds_still_receive_them():
    from darkfactory.notifications import NotificationChannel, subscribed

    legacy = NotificationChannel(events=["needs_human"], known_events=[])
    assert subscribed(legacy, "network_blocked") and subscribed(legacy, "needs_human")
    assert not subscribed(legacy, "merged")
    current = NotificationChannel(events=["needs_human"], known_events=["needs_human", "network_blocked"])
    assert not subscribed(current, "network_blocked")


JIRA = "https://jira.example.com"


def jira_channel(task, **config):
    value = {
        "base_url": JIRA,
        "reviewer": "owner",
        "review_status": "Ожидает проверку",
        "project_keys": ["ABC"],
    }
    with db.transaction() as session:
        row = NotificationChannel(
            org_id=task.org_id,
            project_id=task.project_id,
            name="Jira",
            provider="jira",
            secret=db.encrypt(json.dumps(value | config)),
        )
        session.add(row)
        session.flush()
        return row


def test_jira_validation_and_issue_matching(task):
    config = validate_channel_config("jira", {"base_url": JIRA + "/", "project_keys": ["ABC"]})
    assert config == {
        "base_url": JIRA,
        "reviewer": "",
        "review_status": "",
        "work_status": "",
        "assignee_fields": [],
        "project_keys": ["ABC"],
    }
    for bad in (
        {"base_url": "http://jira.example.com"},
        {"base_url": JIRA, "reviewer": "a b"},
        {"base_url": JIRA, "project_keys": ["npm"]},
        {"base_url": JIRA, "token": "two\nlines"},
    ):
        with pytest.raises(ValueError):
            validate_channel_config("jira", bad)
    from types import SimpleNamespace

    from darkfactory.notifications import jira_issue

    def issue(title, request=""):
        return jira_issue(SimpleNamespace(title=title, request=request), config)

    assert issue("Fix", f"See {JIRA}/browse/ABC-12 now") == "ABC-12"
    assert issue("ABC-7: fix") == "ABC-7"
    assert issue("UTF-8 output", "https://other.example/browse/XYZ-1") is None


def test_jira_needs_an_issue_and_review_moves_assigns_then_comments(task, monkeypatch):
    jira_channel(task, token="jira-secret-token")
    assert enqueue(task, message="Need answer") == 0
    with db.transaction() as session:
        row = session.get(db.Task, task.id)
        row.title, row.state = "Реализовать ABC-1974", "review_required"
    task.title, task.state = "Реализовать ABC-1974", "review_required"
    assert enqueue(task, kind="review_required", message="Ready jira-secret-token") == 1
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer jira-secret-token"
        if request.method == "GET" and request.url.path.endswith("/transitions"):
            return httpx.Response(
                200,
                json={"transitions": [{"id": "191", "name": "Review", "to": {"name": "Ожидает проверку"}}]},
            )
        if request.method == "GET":
            return httpx.Response(200, json={"fields": {"status": {"name": "В работе"}, "assignee": None}})
        if request.url.path.endswith("/comment"):
            assert "jira-secret-token" not in json.loads(request.content)["body"]
            return httpx.Response(201, json={"id": "1"})
        if request.url.path.endswith("/transitions"):
            assert json.loads(request.content) == {"transition": {"id": "191"}}
        if request.method == "PUT":
            assert json.loads(request.content) == {"fields": {"assignee": {"name": "owner"}}}
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dispatch_notifications(client=client) == 1
    issue = "/rest/api/2/issue/ABC-1974"
    assert calls == [
        ("GET", issue),
        ("PUT", issue),
        ("GET", issue + "/transitions"),
        ("POST", issue + "/transitions"),
        ("POST", issue + "/comment"),
    ]
    assert delivery().state == "sent"


def test_jira_uses_project_mcp_token_and_fails_closed_without_one(task):
    from darkfactory.notifications import jira_token

    config = {"base_url": JIRA}
    assert jira_token(config, task.project_id) is None
    with db.transaction() as session:
        project = session.get(db.Project, task.project_id)
        settings = json.loads(db.decrypt(project.secret) or "{}")
        settings["agents"] = {
            "mcp_servers": [
                {"name": "jira", "command": "mcp-atlassian", "env": {"JIRA_PERSONAL_TOKEN": "mcp-pat"}}
            ]
        }
        project.secret = db.encrypt(json.dumps(settings))
    assert jira_token(config, task.project_id) == "mcp-pat"
    assert jira_token({**config, "token": "own"}, task.project_id) == "own"


def test_jira_alone_gets_start_and_progress_comments(task):
    jira_channel(task)
    channel(task, provider="rocketchat")
    with db.transaction() as session:
        session.get(db.Task, task.id).title = "ABC-5 Fix"
    task.title = "ABC-5 Fix"
    assert enqueue(task, kind="created", message="ABC-5 Fix") == 1
    assert enqueue(task, kind="implementation_summary", message="Added the parser and its tests") == 1
    assert enqueue(task, kind="phase_started", message="implement") == 0
    with db.transaction() as session:
        db.event(session, session.get(db.Task, task.id), "implementation_summary", "Second run")
    with db.Session() as session:
        rows = session.query(NotificationDelivery).order_by(NotificationDelivery.created).all()
        assert [row.reply_to for row in rows] == [
            {"issue": "ABC-5", "kind": "created"},
            {"issue": "ABC-5", "kind": "implementation_summary"},
            {"issue": "ABC-5", "kind": "implementation_summary"},
        ]
        assert "взяла задачу в работу" in rows[0].message and "Added the parser" in rows[1].message


def test_jira_preprod_merge_also_goes_to_review(task):
    jira_channel(task, token="jira-secret-token")
    with db.transaction() as session:
        row = session.get(db.Task, task.id)
        row.title, row.state = "ABC-9 Fix", "merged"
    task.title, task.state = "ABC-9 Fix", "merged"
    assert enqueue(task, kind="accepted", message="Слито в preprod") == 1
    assert delivery().reply_to == {"issue": "ABC-9", "kind": "accepted"}
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path.rsplit("/", 1)[-1]))
        if request.method == "GET" and request.url.path.endswith("/transitions"):
            return httpx.Response(
                200, json={"transitions": [{"id": "7", "name": "x", "to": {"name": "Ожидает проверку"}}]}
            )
        if request.method == "GET":
            return httpx.Response(
                200, json={"fields": {"status": {"name": "В работе"}, "assignee": {"name": "owner"}}}
            )
        return httpx.Response(201 if request.url.path.endswith("/comment") else 204)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dispatch_notifications(client=client) == 1
    # Already assigned to the reviewer: no assignee call.
    assert calls == [("GET", "ABC-9"), ("GET", "transitions"), ("POST", "transitions"), ("POST", "comment")]


def test_jira_new_issue_starts_work_and_reaches_review_through_it(task):
    jira_channel(task, token="jira-secret-token", work_status="В работе", assignee_fields=["customfield_1"])
    with db.transaction() as session:
        row = session.get(db.Task, task.id)
        row.title, row.state = "ABC-3 Fix", "merged"
    task.title, task.state = "ABC-3 Fix", "merged"
    status = {"name": "Новая", "statusCategory": {"key": "new"}}
    flows = {
        "Новая": [{"id": "341", "name": "В работу", "to": {"name": "В работе"}}],
        "В работе": [{"id": "191", "name": "Ожидает проверку", "to": {"name": "Ожидает проверку"}}],
    }
    moves = []

    def handler(request):
        path = request.url.path
        if request.method == "GET" and path.endswith("/transitions"):
            return httpx.Response(200, json={"transitions": flows.get(status["name"], [])})
        if request.method == "GET":
            return httpx.Response(
                200, json={"fields": {"status": dict(status), "assignee": {"name": "owner"}}}
            )
        if request.method == "PUT":
            assert json.loads(request.content) == {"fields": {"customfield_1": {"name": "owner"}}}
            moves.append("assign")
            return httpx.Response(204)
        if path.endswith("/transitions"):
            step = json.loads(request.content)["transition"]["id"]
            moves.append(step)
            status.update(
                name={"341": "В работе", "191": "Ожидает проверку"}[step], statusCategory={"key": "x"}
            )
        return httpx.Response(201 if path.endswith("/comment") else 204)

    assert enqueue(task, kind="created", message="ABC-3 Fix") == 1
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dispatch_notifications(client=client) == 1
        assert moves == ["assign", "341"]
        status.update(name="Новая", statusCategory={"key": "new"})
        moves.clear()
        assert enqueue(task, kind="accepted", message="Слито") == 1
        assert dispatch_notifications(client=client) == 1
    assert moves == ["assign", "341", "191"] and status["name"] == "Ожидает проверку"


def test_jira_subtask_comments_but_never_moves_the_issue(task):
    jira_channel(task, token="jira-secret-token", work_status="В работе")
    with db.transaction() as session:
        row = session.get(db.Task, task.id)
        row.title, row.state, row.plan_depth = "Backend part", "merged", 1
        row.request = "https://jira.example.com/browse/ABC-4 backend part"
    task.title, task.state, task.plan_depth = "Backend part", "merged", 1
    task.request = "https://jira.example.com/browse/ABC-4 backend part"
    assert enqueue(task, kind="created", message="Backend part") == 1
    assert enqueue(task, kind="accepted", message="Слито") == 1
    calls = []

    def handler(request):
        calls.append(request.method + " " + request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(201)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dispatch_notifications(client=client) == 2
    assert calls == ["POST comment", "POST comment"]


def test_jira_behind_a_down_gateway_waits_without_using_attempts(task):
    jira_channel(task, token="jira-secret-token")
    with db.transaction() as session:
        row = session.get(db.Task, task.id)
        row.title = "ABC-6 Fix"
    task.title = "ABC-6 Fix"
    assert enqueue(task, kind="created", message="ABC-6 Fix") == 1
    page = httpx.Response(403, headers={"Content-Type": "text/html"}, text="<html>403 Forbidden</html>")
    with httpx.Client(transport=httpx.MockTransport(lambda request: page)) as client:
        assert dispatch_notifications(client=client) == 1
    row = delivery()
    assert row.state == "pending" and row.attempts == 0 and row.last_error.startswith("Unreachable:")
    rejected = httpx.Response(403, json={"errorMessages": ["no permission"]})
    with db.transaction() as session:
        session.get(NotificationDelivery, row.id).retry_at = 0
    with httpx.Client(transport=httpx.MockTransport(lambda request: rejected)) as client:
        assert dispatch_notifications(client=client) == 1
    assert delivery().state == "failed"


def test_rocket_notifications_have_emoji_avatar_without_public_url(task, monkeypatch):
    monkeypatch.delenv("DARKFACTORY_PUBLIC_URL", raising=False)
    channel(task, provider="rocketchat", recipient="#ops")
    enqueue(task)
    sent = []

    def receive(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    with httpx.Client(transport=httpx.MockTransport(receive)) as http:
        assert dispatch_notifications(client=http) == 1
    assert "avatar" not in sent[0]
    assert sent[0]["emoji"] == ":factory:"


def test_rocket_notifications_are_markdown_with_logo(task, monkeypatch):
    from darkfactory.notifications import rocketchat_text

    monkeypatch.setenv("DARKFACTORY_PUBLIC_URL", "https://factory.example")
    channel(task, provider="rocketchat", recipient="#ops")
    enqueue(task)
    sent = []

    def receive(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True})

    with httpx.Client(transport=httpx.MockTransport(receive)) as http:
        assert dispatch_notifications(client=http) == 1
    assert sent[0]["alias"] == "Darkfactory"
    assert sent[0]["avatar"] == "https://factory.example/static/logo.png"

    task_id = "a" * 32
    body = f"Darkfactory · Fix login\nTask {task_id} · needs_human\nКакую базу брать?"
    text = rocketchat_text(body, commands=True)
    assert text.startswith("🔔 *Fix login*")
    assert f"`Task {task_id}` · 🙋 нужно твоё решение" in text
    assert "Ответь в треде" in text and "`!help`" in text
    assert "Ответь в треде" not in rocketchat_text(body)
    done = rocketchat_text(
        "Darkfactory · Выполнено: Fix\nПроект: Demo\nГотово\nЗадача: https://f.example/?task=" + task_id
    )
    assert done.startswith("✅ *Выполнено:* Fix")
    assert "*Проект:* Demo" in done
    assert f"[📋 Открыть карточку](https://f.example/?task={task_id})" in done
