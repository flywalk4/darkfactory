import json

import httpx
import pytest
from conftest import connect, make_task
from test_director import Model, action

from darkfactory import chat_commands, db, director, telegram_bot
from darkfactory.notifications import (
    NotificationChannel,
    NotificationDelivery,
    dispatch_notifications,
    validate_channel_config,
)

BOT = "123456:fake-bot-token"
CHAT = "-100123456"
WEBHOOK = "https://chat.corp.internal/hooks/integration/secret-token"
OUTGOING = "outgoing-webhook-token"


@pytest.fixture(autouse=True)
def quiet_supervisor(monkeypatch):
    monkeypatch.setenv("DARKFACTORY_DIRECTOR_LIMIT", "0")


def owner_token():
    return (db.ROOT / "bootstrap-token").read_text()


def command_token(client):
    """An operator token: chat channels refuse owner tokens."""
    if not hasattr(client, "chat_token"):
        member = client.post("/api/members", json={"name": "Chat", "role": "operator"})
        assert member.status_code == 201, member.text
        client.chat_token = member.json()["token"]
    return client.chat_token


def save_channel(client, provider, **credentials):
    base = {"bot_token": BOT, "chat_id": CHAT} if provider == "telegram" else {"webhook_url": WEBHOOK}
    response = client.post(
        "/api/notification-channels",
        json={
            "name": "Ops",
            "provider": provider,
            "recipient": "#ops" if provider == "rocketchat" else None,
            "credentials": {**base, **credentials},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def telegram_updates(client, *messages, start=1):
    """One poll; returns every request made to Telegram, getUpdates first."""
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/getUpdates"):
            updates = [
                {"update_id": start + index, **(item if "callback_query" in item else {"message": item})}
                for index, item in enumerate(messages)
            ]
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 500}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        telegram_bot.poll(http)
    return seen


def sent(requests, method="sendMessage"):
    return [json.loads(request.content) for request in requests if request.url.path.endswith("/" + method)]


def press(client, data, message_id=40, text="Darkfactory menu"):
    query = {
        "id": "q1",
        "from": {"id": 5, "is_bot": False},
        "data": data,
        "message": {"message_id": message_id, "chat": {"id": int(CHAT)}, "text": text},
    }
    return telegram_updates(client, {"callback_query": query})


def deliver():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        if "telegram" in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        return httpx.Response(200, json={"success": True})

    chat_commands.queue_director_replies()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        dispatch_notifications(client=http)
    return sent


def message(text, message_id=7, **extra):
    return {
        "message_id": message_id,
        "from": {"id": 5, "is_bot": False},
        "chat": {"id": int(CHAT), "type": "supergroup"},
        "text": text,
        **extra,
    }


def stopped_task(client, project):
    task = make_task(client, project)
    with db.transaction() as session:
        session.get(db.Task, task["id"]).state = "needs_human"
    return task["id"]


def test_channel_command_token_is_checked_and_never_shown(client, setup_project):
    response = client.post(
        "/api/notification-channels",
        json={
            "name": "Ops",
            "provider": "telegram",
            "credentials": {"bot_token": BOT, "chat_id": CHAT, "director_token": "not-a-valid-token"},
        },
    )
    assert response.status_code == 400
    channel = save_channel(client, "telegram", director_token=command_token(client))
    assert channel["commands"] is True
    assert command_token(client) not in json.dumps(client.get("/api/notification-channels").json())
    # Renaming keeps the command token; changing only the bot keeps it too.
    client.put(
        "/api/notification-channels/" + channel["id"], json={"name": "Renamed", "provider": "telegram"}
    )
    response = client.put(
        "/api/notification-channels/" + channel["id"],
        json={
            "name": "Renamed",
            "provider": "telegram",
            "credentials": {"bot_token": BOT, "chat_id": "-100777"},
        },
    )
    assert response.json()["commands"] is True
    response = client.put(
        "/api/notification-channels/" + channel["id"],
        json={"name": "Renamed", "provider": "telegram", "credentials": {"director_token": ""}},
    )
    assert response.json()["commands"] is False


def test_telegram_director_command_replies_to_the_message(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    save_channel(client, "telegram", director_token=command_token(client))
    requests = telegram_updates(client, message("/director@factory_bot покажи проекты"))
    assert "offset" not in requests[0].url.params
    with db.Session() as session:
        dialogue = session.query(director.Dialogue).one()
        assert dialogue.request == "покажи проекты" and dialogue.origin["message_id"] == 7
        assert dialogue.origin["sender"] == "5"
    # Telegram returns the same update again until the offset confirms it: no second dialogue.
    assert telegram_updates(client, message("/director покажи проекты"))[0].url.params["offset"] == "2"
    with db.Session() as session:
        assert session.query(director.Dialogue).count() == 1
    assert deliver() == []
    director.advance_dialogues(
        Model(
            {"reply": "Смотрю", "actions": [action("GET", "/api/projects")]},
            {"reply": "Проект Pilot.", "actions": []},
        )
    )
    sent = deliver()
    assert sent[0]["text"] == "Директор: Проект Pilot."
    assert sent[0]["reply_parameters"]["message_id"] == 7
    assert command_token(client) not in sent[0]["text"]
    assert deliver() == []


def test_telegram_ignores_other_chats_bots_and_busy_director(client, setup_project):
    save_channel(client, "telegram", director_token=command_token(client))
    telegram_updates(
        client,
        message("/director чужой чат", chat={"id": -1}),
        message("/director от бота", **{"from": {"id": 1, "is_bot": True}}),
    )
    with db.Session() as session:
        assert session.query(director.Dialogue).count() == 0
    telegram_updates(client, message("/director первая", 1), message("/director вторая", 2), start=10)
    sent = deliver()
    assert [item["text"] for item in sent] == ["Директор занят предыдущей командой. Повтори позже."]
    assert sent[0]["reply_parameters"]["message_id"] == 2


def test_telegram_reply_to_notification_answers_stopped_task(client, setup_project):
    project, _ = setup_project
    task_id = stopped_task(client, project)
    save_channel(client, "telegram", director_token=command_token(client))
    notification = {
        "message_id": 3,
        "from": {"id": 9, "is_bot": True},
        "chat": {"id": int(CHAT)},
        "text": f"Darkfactory · Fix\nTask {task_id} · needs_human\nКакой формат?",
    }
    telegram_updates(client, message("Используй JSON", 8, reply_to_message=notification))
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["state"] != "needs_human"
    sent = deliver()
    assert sent[0]["text"] == "Ответ передан, задача продолжена."
    assert sent[0]["reply_parameters"]["message_id"] == 8
    # A repeated update answers nothing twice.
    telegram_updates(client, message("Используй JSON", 8, reply_to_message=notification))
    assert deliver() == []


def private(text, message_id, **extra):
    return message(text, message_id, chat={"id": int(CHAT), "type": "private"}, **extra)


def test_telegram_forward_with_comment_answers_task(client, setup_project):
    project, _ = setup_project
    task_id = stopped_task(client, project)
    save_channel(client, "telegram", director_token=command_token(client))
    forwarded = private(f"Darkfactory · Fix\nTask {task_id} · needs_human\nВопрос", 12, forward_date=1)
    telegram_updates(client, private("Да, удаляй", 11), forwarded)
    assert client.get(f"/api/tasks/{task_id}").json()["state"] != "needs_human"
    assert deliver()[0]["reply_parameters"]["message_id"] == 12
    with db.Session() as session:
        assert session.query(director.Dialogue).count() == 0


def test_forward_alone_asks_for_answer_and_plain_private_text_goes_to_director(client, setup_project):
    project, _ = setup_project
    task_id = stopped_task(client, project)
    save_channel(client, "telegram", director_token=command_token(client))
    requests = telegram_updates(client, private(f"Darkfactory · Fix\nTask {task_id}", 12, forward_date=1))
    assert sent(requests)[0]["text"].endswith(f"<code>Task {task_id}</code>")
    assert sent(requests)[0]["reply_markup"]["force_reply"] is True
    telegram_updates(client, private("Сколько задач в очереди?", 13), start=5)
    with db.Session() as session:
        assert session.query(director.Dialogue).one().request == "Сколько задач в очереди?"


def buttons(payload):
    return [
        button.get("callback_data") for row in payload["reply_markup"]["inline_keyboard"] for button in row
    ]


def test_menu_board_column_card_priority_and_move(client, setup_project):
    project, _ = setup_project
    first = make_task(client, project, title="Первая")
    second = make_task(client, project, title="Вторая")
    save_channel(client, "telegram", director_token=command_token(client))
    home = sent(telegram_updates(client, message("/start", 1)))[0]
    assert "p:" + project["id"] in buttons(home) and "s" not in buttons(home)
    board = sent(press(client, "p:" + project["id"]), "editMessageText")[0]
    assert "К запуску: <b>2</b>" in board["text"] and board["message_id"] == 40
    queue = sent(press(client, f"c:{project['id']}:0:0"), "editMessageText")[0]
    assert buttons(queue)[:2] == ["t:" + first["id"], "t:" + second["id"]]
    card = sent(press(client, "t:" + second["id"]), "editMessageText")[0]
    assert "u:" + second["id"] in buttons(card) and f"Task {second['id']}" in card["text"]
    requests = press(client, "u:" + second["id"])
    assert sent(requests, "answerCallbackQuery")[0]["text"] == "Приоритет: ⏫ высокий"
    assert client.get(f"/api/tasks/{second['id']}").json()["priority"] == "high"
    queue = sent(press(client, f"c:{project['id']}:0:0"), "editMessageText")[0]
    assert buttons(queue)[0] == "t:" + second["id"]
    moves = sent(press(client, "mv:" + first["id"]), "editMessageText")[0]
    assert f"pr:{first['id']}:critical" in buttons(moves) and "x:" + first["id"] in buttons(moves)
    press(client, f"pr:{first['id']}:critical")
    assert client.get(f"/api/tasks/{first['id']}").json()["priority"] == "critical"
    assert "xx:" + first["id"] in buttons(sent(press(client, "x:" + first["id"]), "editMessageText")[0])
    press(client, "xx:" + first["id"])
    assert client.get(f"/api/tasks/{first['id']}").json()["state"] == "cancelled"
    # From a notification, a screen opens as a new message so the question stays visible.
    requests = press(client, "t:" + second["id"], text="Darkfactory · Вторая")
    assert sent(requests) and not sent(requests, "editMessageText")


def bot_prompt(requests):
    text = sent(requests)[0]["text"]
    return {
        "message_id": 501,
        "from": {"id": 9, "is_bot": True},
        "text": text.replace("<code>", "").replace("</code>", ""),
    }


def test_buttons_create_task_answer_agent_and_message_director(client, setup_project):
    project, _ = setup_project
    save_channel(client, "telegram", director_token=command_token(client))
    asked = bot_prompt(press(client, "n:" + project["id"]))
    requests = telegram_updates(
        client, message("Починить вход\nКнопка не работает", 2, reply_to_message=asked)
    )
    created = [
        task
        for task in client.get(f"/api/projects/{project['id']}/tasks").json()
        if task["title"] == "Починить вход"
    ]
    assert created and created[0]["request"] == "Починить вход\nКнопка не работает"
    assert "Починить вход" in sent(requests)[0]["text"]

    task_id = stopped_task(client, project)
    asked = bot_prompt(press(client, "a:" + task_id))
    telegram_updates(client, message("Да", 3, reply_to_message=asked), start=20)
    assert client.get(f"/api/tasks/{task_id}").json()["state"] != "needs_human"

    asked = bot_prompt(press(client, "d:" + project["id"]))
    telegram_updates(client, message("Что в очереди?", 4, reply_to_message=asked), start=30)
    with db.Session() as session:
        dialogue = session.query(director.Dialogue).one()
        assert dialogue.project_id == project["id"] and dialogue.request == "Что в очереди?"


def test_chat_never_starts_subscription_logins(client, setup_project):
    _, pool = setup_project
    save_channel(client, "telegram", director_token=command_token(client))
    for data in ("s", "pl:" + pool["id"], f"ac:{pool['id']}:codex", "lc:abc"):
        requests = press(client, data)
        assert not sent(requests) and not sent(requests, "editMessageText")
    assert client.get("/api/login-sessions").json() == []


def test_owner_token_refused_for_commands(client, setup_project):
    project, _ = setup_project
    response = client.post(
        "/api/notification-channels",
        json={
            "name": "Ops",
            "provider": "telegram",
            "credentials": {"bot_token": BOT, "chat_id": CHAT, "director_token": owner_token()},
        },
    )
    assert response.status_code == 400
    # A channel saved before the rule existed serves nobody.
    channel = save_channel(client, "telegram", director_token=command_token(client))
    with db.transaction() as session:
        row = session.get(NotificationChannel, channel["id"])
        row.secret = db.encrypt(
            json.dumps({"bot_token": BOT, "chat_id": CHAT, "director_token": owner_token()})
        )
    task_id = stopped_task(client, project)
    assert telegram_updates(client, message("/start", 1)) == []
    chat_commands.answer_task(channel["id"], task_id, "Да", "r1", {"message_id": 1})
    assert client.get(f"/api/tasks/{task_id}").json()["state"] == "needs_human"


def test_sender_allowlist_and_sender_recorded_on_task(client, setup_project):
    project, _ = setup_project
    task_id = stopped_task(client, project)
    save_channel(client, "telegram", director_token=command_token(client), allowed_senders=[42])
    notification = {
        "message_id": 3,
        "from": {"id": 9, "is_bot": True},
        "chat": {"id": int(CHAT)},
        "text": f"Darkfactory · Fix\nTask {task_id} · needs_human\nКакой формат?",
    }
    telegram_updates(client, message("Используй JSON", 8, reply_to_message=notification))
    assert client.get(f"/api/tasks/{task_id}").json()["state"] == "needs_human"
    allowed = message(
        "Используй JSON", 9, reply_to_message=notification, **{"from": {"id": 42, "is_bot": False}}
    )
    telegram_updates(client, allowed, start=5)
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["state"] != "needs_human"
    recorded = [event for event in detail["events"] if event["kind"] == "chat_command"]
    assert recorded and "42" in recorded[-1]["message"]

    channel = save_channel(
        client,
        "rocketchat",
        director_token=command_token(client),
        outgoing_token=OUTGOING,
        allowed_senders=["U1"],
    )
    rocket(client, channel, "director что в очереди?", "m1", user_id="U2")
    rocket(client, channel, "director что в очереди?", "m2")
    with db.Session() as session:
        assert session.query(director.Dialogue).count() == 0
    rocket(client, channel, "director что в очереди?", "m3", user_id="U1")
    with db.Session() as session:
        assert session.query(director.Dialogue).one().origin["sender"] == "U1"
    with pytest.raises(ValueError):
        validate_channel_config("telegram", {"bot_token": BOT, "chat_id": CHAT, "allowed_senders": "5"})


def test_task_notifications_carry_buttons_only_with_command_token(client, setup_project):
    project, _ = setup_project
    task_id = stopped_task(client, project)
    save_channel(client, "telegram", director_token=command_token(client))
    client.post(f"/api/tasks/{task_id}/notify", json={"message": "Нужен ответ"})
    payload = deliver()[0]
    assert buttons(payload) == ["a:" + task_id, "t:" + task_id]


def rocket(client, channel, text, message_id, **extra):
    return client.post(
        f"/api/chat-commands/rocketchat/{channel['id']}",
        headers={"Authorization": ""},
        json={"token": OUTGOING, "channel_id": "ROOM", "message_id": message_id, "text": text, **extra},
    )


def test_rocket_command_and_thread_answer(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    channel = save_channel(
        client, "rocketchat", director_token=command_token(client), outgoing_token=OUTGOING
    )
    assert rocket(client, channel, "/director что в очереди?", "m1", token="wrong").status_code == 403
    assert rocket(client, channel, "Director: что в очереди?", "m1").status_code == 200
    director.advance_dialogues(Model({"reply": "Очередь пуста.", "actions": []}))
    sent = deliver()
    assert sent[0] == {
        "text": "🧠 *Директор:* Очередь пуста.",
        "alias": "Darkfactory",
        "roomId": "ROOM",
        "tmid": "m1",
    }

    task_id = stopped_task(client, project)
    with db.transaction() as session:
        session.add(
            NotificationDelivery(
                channel_id=channel["id"], task_id=task_id, dedupe_key="q", message="q", state="sent"
            )
        )
    # The webhook echoes our own notification; its ID is how a thread reply finds the task.
    rocket(client, channel, f"Darkfactory · Fix\nTask {task_id} · needs_human", "n1", bot={"i": "hook"})
    assert rocket(client, channel, "Бери PostgreSQL", "r1", tmid="n1").status_code == 200
    assert client.get(f"/api/tasks/{task_id}").json()["state"] != "needs_human"
    sent = deliver()
    assert sent[0] == {
        "text": "Ответ передан, задача продолжена.",
        "alias": "Darkfactory",
        "roomId": "ROOM",
        "tmid": "n1",
    }
    # Unrelated thread replies and plain messages do nothing.
    rocket(client, channel, "спасибо", "r2", tmid="other")
    rocket(client, channel, "привет", "r3")
    rocket(client, channel, "directory пуст", "r4")
    assert deliver() == []
    with db.Session() as session:
        assert session.get(NotificationChannel, channel["id"]).command_offset is None


def test_rocket_thread_reply_without_destination_override(client, setup_project):
    """A channel without a recipient answers in the thread with tmid alone: Rocket.Chat refuses roomId
    when the webhook's "Override Destination Channel" is off."""
    project, pool = setup_project
    connect(client, pool)
    response = client.post(
        "/api/notification-channels",
        json={
            "name": "Ops",
            "provider": "rocketchat",
            "credentials": {
                "webhook_url": WEBHOOK,
                "director_token": command_token(client),
                "outgoing_token": OUTGOING,
            },
        },
    )
    assert response.status_code == 201, response.text
    channel = response.json()
    assert rocket(client, channel, "!director что в очереди?", "m1").status_code == 200
    director.advance_dialogues(Model({"reply": "Очередь пуста.", "actions": []}))

    def refuse(request):
        body = json.loads(request.content)
        if "roomId" in body or "channel" in body:
            error = "overriding destination channel is disabled for this integration"
            return httpx.Response(400, json={"success": False, "error": error})
        return httpx.Response(200, json={"success": True})

    with httpx.Client(transport=httpx.MockTransport(refuse)) as http:
        chat_commands.exchange(client=http)
    with db.Session() as session:
        delivery = session.query(NotificationDelivery).filter_by(channel_id=channel["id"]).one()
        assert delivery.state == "sent", delivery.last_error
        assert delivery.reply_to == {"room_id": "ROOM", "tmid": "m1"}
        assert session.query(director.Dialogue).one().origin_replied is True


def test_director_answer_is_replied_only_once_delivered(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    channel = save_channel(
        client, "rocketchat", director_token=command_token(client), outgoing_token=OUTGOING
    )
    rocket(client, channel, "!director что в очереди?", "m1")
    director.advance_dialogues(Model({"reply": "Очередь пуста.", "actions": []}))
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(400))) as http:
        chat_commands.exchange(client=http)
    with db.Session() as session:
        assert session.query(NotificationDelivery).filter_by(channel_id=channel["id"]).one().state == "failed"
        assert session.query(director.Dialogue).one().origin_replied is False
    # A failed answer is not queued again.
    chat_commands.queue_director_replies()
    with db.Session() as session:
        assert session.query(NotificationDelivery).filter_by(channel_id=channel["id"]).count() == 1


def test_rocket_bang_commands(client, setup_project, monkeypatch):
    project, pool = setup_project
    connect(client, pool)
    channel = save_channel(
        client, "rocketchat", director_token=command_token(client), outgoing_token=OUTGOING
    )
    rocket(client, channel, "!director покажи задачи", "m1")
    with db.Session() as session:
        assert session.query(director.Dialogue).one().request == "покажи задачи"

    rocket(client, channel, "!help", "h1")
    rocket(client, channel, "!непонятно", "h2")
    help_text, unknown = (item["text"] for item in deliver())
    assert "`!director текст`" in help_text and "`!waiting`" in help_text
    assert "`!непонятно`" in unknown and "`!help`" in unknown

    task_id = stopped_task(client, project)
    rocket(client, channel, "!projects", "p1")
    rocket(client, channel, "!waiting", "w1")
    rocket(client, channel, f"!task {task_id}", "t1")
    projects, waiting, card = (item["text"] for item in deliver())
    assert "📁 *" in projects and "🙋 1" in projects
    assert f"`!task {task_id}`" in waiting
    assert f"`Task {task_id}`" in card and "Нужно решение" in card

    # In a notification's thread "!" commands act on that task.
    with db.transaction() as session:
        session.add(
            NotificationDelivery(
                channel_id=channel["id"], task_id=task_id, dedupe_key="q", message="q", state="sent"
            )
        )
    rocket(client, channel, f"Darkfactory · Fix\nTask {task_id} · needs_human", "n1", bot={"i": "hook"})
    rocket(client, channel, "!task", "t2", tmid="n1")
    assert f"`Task {task_id}`" in deliver()[0]["text"]
    rocket(client, channel, "!cancel", "c1", tmid="n1")
    assert deliver()[0] == {
        "text": "🚫 Задача отменена.",
        "alias": "Darkfactory",
        "roomId": "ROOM",
        "tmid": "n1",
    }
    assert client.get(f"/api/tasks/{task_id}").json()["state"] == "cancelled"
    rocket(client, channel, "!resume", "c2")
    assert "Укажи задачу" in deliver()[0]["text"]
