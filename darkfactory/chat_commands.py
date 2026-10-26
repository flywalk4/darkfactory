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

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from . import service
from .db import Session, Task, decrypt, encrypt, event, transaction
from .director import ACTIVE, Dialogue, call
from .notifications import NotificationChannel, NotificationDelivery, dispatch_notifications

# Rocket.Chat rejects unknown slash commands, so a plain "director" prefix works too.
COMMAND = re.compile(r"^/?director(?:@\w+)?[:,]?(?:\s+(.*))?$", re.S | re.I)
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
    if command := COMMAND.match(text.strip()):
        submit_command(channel_id, command.group(1), message_id, reply_to, sender=sender)
    elif thread:
        with Session() as db:
            task_id = db.scalar(
                select(NotificationDelivery.task_id).where(
                    NotificationDelivery.channel_id == channel_id,
                    NotificationDelivery.message_ref == thread,
                    NotificationDelivery.task_id.is_not(None),
                )
            )
        if task_id:
            answer_task(channel_id, task_id, text, message_id, reply_to, sender=sender)
    return True


def queue_director_replies():
    """Post each finished chat-started dialogue's answer back where the command came from."""
    with transaction() as db:
        for row in db.scalars(
            select(Dialogue).where(
                Dialogue.origin.is_not(None),
                Dialogue.origin_replied.is_(False),
                Dialogue.status.in_(FINISHED),
            )
        ):
            row.origin_replied = True
            channel = db.get(NotificationChannel, row.origin.get("channel_id"))
            if channel is None:
                continue
            reply_to = {
                key: value for key, value in row.origin.items() if key not in {"channel_id", "sender"}
            }
            _reply(db, channel, "director:" + row.id, "Директор: " + (row.reply or row.status), reply_to)


def exchange(*, client=None):
    """One notification tick: queue director answers, then deliver the outbox."""
    if client is None:
        with httpx.Client(follow_redirects=False, trust_env=False) as http:
            return exchange(client=http)
    queue_director_replies()
    return dispatch_notifications(limit=10, client=client)
