"""Inbound chat: /director commands and answers to task notifications from Telegram and Rocket.Chat.

Commands act through the normal API with the channel's command token, so its role and project
scope bound what a chat member can do. That token is never an owner's, and a channel may list the
sender IDs allowed to use it. Answers go back through the delivery outbox: Telegram
replies to the user's message, Rocket.Chat answers in the message's thread.
"""

import hashlib
import hmac
import json
import re
from types import SimpleNamespace

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from . import service
from .db import Session, Task, decrypt, encrypt, event, transaction
from .director import ACTIVE, Dialogue, call
from .notifications import NotificationChannel, NotificationDelivery, dispatch_notifications
from .task_links import task_url

# Rocket.Chat rejects unknown slash commands, so chat commands there start with "!";
# a plain "director" prefix still works for channels set up before.
COMMAND = re.compile(r"^[/!]?director(?:@\w+)?[:,]?(?:\s+(.*))?$", re.S | re.I)
BANG = re.compile(r"^!([\wа-яё]+)(?:\s+(.*))?$", re.S | re.I)
ALIASES = {
    "help": "help",
    "помощь": "help",
    "команды": "help",
    "waiting": "waiting",
    "ждут": "waiting",
    "projects": "projects",
    "проекты": "projects",
    "task": "task",
    "задача": "task",
    "resume": "resume",
    "продолжить": "resume",
    "cancel": "cancel",
    "отменить": "cancel",
    "merge": "merge",
    "слить": "merge",
}
HELP = """🏭 *Darkfactory — команды*
`!director текст` — поручение директору, например `!director покажи задачи`
`!projects` — проекты и сколько задач в каждой колонке
`!waiting` — задачи, которые ждут ответа или слияния
`!task <id>` — карточка задачи
`!help` — эта подсказка

💬 *В треде уведомления о задаче*
Обычный текст — ответ агенту, задача продолжится
`!task` — карточка, `!resume` — продолжить, `!merge` — слить, `!cancel` — отменить"""
TASK_ID = re.compile(r"(?:\bTask |[?&]task=)([0-9a-f]{32})\b")
FINISHED = ("succeeded", "failed", "cancelled")


def _config(channel):
    return json.loads(decrypt(channel.secret) or "{}")


def _key(channel, ref):
    return hashlib.sha256(f"{channel.id}\0{ref}".encode()).hexdigest()[:32]


def _reply(db, channel, key, text, reply_to):
    if db.query(NotificationDelivery.id).filter_by(channel_id=channel.id, dedupe_key=key).first():
        return
    db.add(
        NotificationDelivery(
            channel_id=channel.id, task_id=None, dedupe_key=key, message=text[:3500], reply_to=reply_to
        )
    )


def sender_allowed(config, sender):
    """A channel with `allowed_senders` accepts only those chat user IDs; without it, the whole chat."""
    allowed = config.get("allowed_senders")
    return not allowed or (sender is not None and str(sender) in allowed)


def record_sender(task_id, provider, sender):
    """Leave who in the chat caused a change on the task, since the API sees only the channel token."""
    if sender is None:
        return
    with transaction() as db:
        task = db.get(Task, task_id)
        if task:
            event(
                db,
                task,
                "chat_command",
                f"Из {provider}: отправитель {sender}",
                provider=provider,
                sender=str(sender),
            )


def _access(db, channel, token):
    access = service.authenticate(db, token)
    access.require("operator")
    if access.token.role == "owner":
        # Anyone in the chat would hold owner rights: settings, members and secrets stay in the UI.
        raise HTTPException(403)
    if access.org_id != channel.org_id:
        raise HTTPException(403)
    if channel.project_id:
        access.project(db, channel.project_id)
    return access


def submit_command(channel_id, text, ref, reply_to, project_id=None, sender=None):
    """Queue a director dialogue for a chat command; the answer is posted when it finishes."""
    with transaction() as db:
        channel = db.get(NotificationChannel, channel_id)
        token = _config(channel).get("director_token")
        identifier = _key(channel, ref)
        if not token or db.get(Dialogue, identifier):
            return
        request = (text or "").strip()
        if not request:
            _reply(db, channel, "command:" + identifier, "Напиши задание после director.", reply_to)
            return
        try:
            access = _access(db, channel, token)
            project_id = channel.project_id or project_id
            if project_id:
                access.project(db, project_id)
        except HTTPException:
            _reply(
                db,
                channel,
                "command:" + identifier,
                "Токен команд канала недействителен или без прав.",
                reply_to,
            )
            return
        if db.scalar(
            select(Dialogue.id).where(Dialogue.token_id == access.token.id, Dialogue.status.in_(ACTIVE))
        ):
            _reply(
                db,
                channel,
                "command:" + identifier,
                "Директор занят предыдущей командой. Повтори позже.",
                reply_to,
            )
            return
        db.add(
            Dialogue(
                id=identifier,
                org_id=channel.org_id,
                token_id=access.token.id,
                project_id=project_id,
                request=request[:12000],
                credential_secret=encrypt(token),
                origin={
                    "channel_id": channel.id,
                    **reply_to,
                    "sender": None if sender is None else str(sender),
                },
            )
        )


def answer_task(channel_id, task_id, text, ref, reply_to, sender=None):
    """Resume a stopped task with the chat answer, exactly as the task card does."""
    with Session() as db:
        channel = db.get(NotificationChannel, channel_id)
        token = _config(channel).get("director_token")
        key = "answer:" + _key(channel, ref)
        provider = channel.provider
        try:
            if token:
                _access(db, channel, token)
        except HTTPException:
            token = None
        if (
            not token
            or db.query(NotificationDelivery.id).filter_by(channel_id=channel_id, dedupe_key=key).first()
        ):
            return
    message = (text or "").strip()[:20000]
    if not message:
        return
    code, value = call(
        token, "POST", f"/api/tasks/{task_id}/actions", {"action": "resume", "message": message}
    )
    if code == 200:
        record_sender(task_id, provider, sender)
        result = "Ответ передан, задача продолжена."
    else:
        detail = value.get("detail") if isinstance(value, dict) else None
        result = "Ответ не принят: " + (detail if isinstance(detail, str) else f"HTTP {code}")
    with transaction() as db:
        _reply(db, db.get(NotificationChannel, channel_id), key, result, reply_to)


def handle_rocketchat(channel_id, payload):
    """Rocket.Chat outgoing webhook. Returns False when the request is not from this channel's webhook."""
    with Session() as db:
        channel = db.get(NotificationChannel, channel_id)
        if not channel or channel.provider != "rocketchat" or not channel.enabled:
            return False
        config = _config(channel)
        expected = config.get("outgoing_token")
    supplied = payload.get("token")
    if not expected or not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
        return False
    text, message_id = payload.get("text"), payload.get("message_id")
    room, thread = payload.get("channel_id"), payload.get("tmid")
    if not isinstance(text, str) or not isinstance(message_id, str) or not isinstance(room, str):
        return True
    if payload.get("bot"):
        # The webhook also sees our own notifications: remember which task each posted message is about.
        task = TASK_ID.search(text)
        if task:
            with transaction() as db:
                delivery = (
                    db.query(NotificationDelivery)
                    .filter(
                        NotificationDelivery.channel_id == channel_id,
                        NotificationDelivery.task_id == task.group(1),
                        NotificationDelivery.state.in_(["sending", "sent"]),
                        NotificationDelivery.message_ref.is_(None),
                    )
                    .order_by(NotificationDelivery.created.desc())
                    .first()
                )
                if delivery:
                    delivery.message_ref = message_id[:64]
        return True
    sender = payload.get("user_id")
    if not sender_allowed(config, sender if isinstance(sender, str) else None):
        return True
    thread = thread if isinstance(thread, str) else None
    reply_to = {"room_id": room, "tmid": thread or message_id}
    task_id = None
    if thread:
        with Session() as db:
            task_id = db.scalar(
                select(NotificationDelivery.task_id).where(
                    NotificationDelivery.channel_id == channel_id,
                    NotificationDelivery.message_ref == thread,
                    NotificationDelivery.task_id.is_not(None),
                )
            )
    if command := COMMAND.match(text.strip()):
        submit_command(channel_id, command.group(1), message_id, reply_to, sender=sender)
    elif bang := BANG.match(text.strip()):
        run_bang(
            channel_id,
            bang.group(1).lower(),
            (bang.group(2) or "").strip(),
            message_id,
            reply_to,
            task_id,
            sender,
        )
    elif task_id:
        answer_task(channel_id, task_id, text, message_id, reply_to, sender=sender)
    return True


def _detail(code, value):
    found = value.get("detail") if isinstance(value, dict) else None
    return found if isinstance(found, str) else f"HTTP {code}"


def _card(token, task_id):
    from .telegram_bot import COLUMNS, PRIORITY_LABELS, QUESTION_KINDS, STATES, column_of

    code, task = call(token, "GET", f"/api/tasks/{task_id}")
    if code != 200:
        return "Задача недоступна: " + _detail(code, task)
    state = task["state"]
    lines = [
        f"{COLUMNS[column_of(state)][0]} *{task['title']}*",
        f"*Статус:* {STATES.get(state, state)} · *приоритет:* "
        + PRIORITY_LABELS.get(task.get("priority"), task.get("priority") or "обычный"),
    ]
    if task.get("reason"):
        lines.append(f"*Причина:* {task['reason']}")
    if state in {"needs_human", "review_required"}:
        question = next(
            (item for item in reversed(task.get("events") or []) if item.get("kind") in QUESTION_KINDS), None
        )
        if question:
            lines += ["", "*Вопрос:*", str(question.get("message"))[:1500]]
    if task.get("commit_url"):
        lines.append(f"[🔗 Коммит]({task['commit_url']})")
    lines += [f"[📋 Открыть карточку]({task_url(SimpleNamespace(id=task_id))})", f"`Task {task_id}`"]
    return "\n".join(lines)


def _projects(token, only_waiting):
    from .telegram_bot import COLUMNS, task_label

    code, projects = call(token, "GET", "/api/projects")
    if code != 200:
        return "Нет доступа к фабрике: " + _detail(code, projects)
    lines, waiting = [], []
    for project in projects[:20]:
        code, tasks = call(token, "GET", f"/api/projects/{project['id']}/tasks")
        tasks = tasks if code == 200 and isinstance(tasks, list) else []
        waiting += [task for task in tasks if task["state"] in {"needs_human", "review_required"}]
        counts = " · ".join(
            f"{icon} {sum(task['state'] in states for task in tasks)}" for icon, _, states in COLUMNS
        )
        lines.append(f"📁 *{project['name']}* — {counts}")
    if only_waiting:
        if not waiting:
            return "🙋 Никто не ждёт ответа."
        rows = [f"• {task_label(task)} — `!task {task['id']}`" for task in waiting[:15]]
        return f"🙋 *Ждут ответа* · {len(waiting)}\n" + "\n".join(rows)
    if not lines:
        return "📁 Проектов пока нет."
    legend = " · ".join(f"{icon} {title}" for icon, title, _ in COLUMNS)
    return "🏭 *Проекты*\n" + "\n".join(lines) + f"\n_{legend}_"


def run_bang(channel_id, name, argument, ref, reply_to, thread_task, sender):
    """A Rocket.Chat "!" command; in a task notification's thread it acts on that task."""
    command = ALIASES.get(name)
    with Session() as db:
        channel = db.get(NotificationChannel, channel_id)
        token = _config(channel).get("director_token")
        key = "bang:" + _key(channel, ref)
        if (
            not token
            or db.query(NotificationDelivery.id).filter_by(channel_id=channel_id, dedupe_key=key).first()
        ):
            return
        try:
            _access(db, channel, token)
        except HTTPException:
            token = None
    task_id = argument if re.fullmatch(r"[0-9a-f]{32}", argument) else thread_task
    if command is None:
        result = f"Не знаю команду `!{name}`. Список команд: `!help`."
    elif not token:
        result = "Токен команд канала недействителен или без прав."
    elif command == "help":
        result = HELP
    elif command in {"projects", "waiting"}:
        result = _projects(token, command == "waiting")
    elif not task_id:
        result = f"Укажи задачу: `!{name} <id>` или напиши команду в треде уведомления о задаче."
    elif command == "task":
        result = _card(token, task_id)
    else:
        code, value = call(token, "POST", f"/api/tasks/{task_id}/actions", {"action": command})
        if code == 200:
            record_sender(task_id, "rocketchat", sender)
            result = {
                "resume": "▶️ Задача снова в очереди.",
                "cancel": "🚫 Задача отменена.",
                "merge": "✅ Слияние запущено.",
            }[command]
        else:
            result = "Не выполнено: " + _detail(code, value)
    with transaction() as db:
        _reply(db, db.get(NotificationChannel, channel_id), key, result, reply_to)


def queue_director_replies():
    """Post each finished chat-started dialogue's answer back where the command came from.

    `origin_replied` turns true once the chat accepted that answer; a failed delivery leaves it false.
    """
    with transaction() as db:
        for row in db.scalars(
            select(Dialogue).where(
                Dialogue.origin.is_not(None),
                Dialogue.origin_replied.is_(False),
                Dialogue.status.in_(FINISHED),
            )
        ):
            channel = db.get(NotificationChannel, row.origin.get("channel_id"))
            if channel is None:
                continue
            key = "director:" + row.id
            state = db.scalar(
                select(NotificationDelivery.state).filter_by(channel_id=channel.id, dedupe_key=key)
            )
            if state is not None:
                # Replied means the chat accepted the answer, not merely that it was queued.
                row.origin_replied = state == "sent"
                continue
            reply_to = {
                name: value for name, value in row.origin.items() if name not in {"channel_id", "sender"}
            }
            _reply(db, channel, key, "Директор: " + (row.reply or row.status), reply_to)


def exchange(*, client=None):
    """One notification tick: queue director answers, deliver the outbox, then mark answers delivered."""
    if client is None:
        with httpx.Client(follow_redirects=False, trust_env=False) as http:
            return exchange(client=http)
    queue_director_replies()
    processed = dispatch_notifications(limit=10, client=client)
    queue_director_replies()
    return processed
