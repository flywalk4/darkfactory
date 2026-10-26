"""Notification channels and deliveries."""

import json

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import service
from ..chat_commands import handle_rocketchat
from ..db import (
    Project,
    Session,
    Task,
    decrypt,
    encrypt,
    event,
    transaction,
)
from ..notifications import (
    IMPORTANT_KINDS,
    NotificationChannel,
    NotificationDelivery,
    public_channel,
    validate_channel_config,
)
from ..web import access, public

router = APIRouter()


class ChannelInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    provider: str = Field(pattern="^(rocketchat|telegram|jira)$")
    recipient: str | None = Field(default=None, pattern=r"^[@#][^\s,@#]{1,199}$")
    project_id: str | None = None
    enabled: bool = True
    events: list[str] = Field(default_factory=lambda: sorted(IMPORTANT_KINDS), max_length=30)
    credentials: dict | None = None


def channel_scope(db, a, project_id):
    if project_id:
        a.require("owner")
        a.project(db, project_id)
    else:
        a.organization_owner()


@router.get("/api/notification-events")
def notification_events(a=Depends(access)):
    a.require("owner")
    return sorted(IMPORTANT_KINDS)


@router.get("/api/notification-channels")
def notification_channels(a=Depends(access)):
    a.require("owner")
    with Session() as db:
        rows = db.scalars(select(NotificationChannel).where(NotificationChannel.org_id == a.org_id))
        result = []
        for row in rows:
            project = db.get(Project, row.project_id) if row.project_id else None
            if (project and a.permits(project)) or (
                not project and not a.principal.project_ids and not a.token.project_ids
            ):
                result.append(public_channel(row))
        return result


@router.post("/api/notification-channels", status_code=201)
@router.put("/api/notification-channels/{channel_id}")
def save_notification_channel(value: ChannelInput, channel_id: str | None = None, a=Depends(access)):
    a.require("owner")
    with transaction() as db:
        row = db.get(NotificationChannel, channel_id) if channel_id else None
        if channel_id and (not row or row.org_id != a.org_id):
            raise HTTPException(404)
        if row:
            channel_scope(db, a, row.project_id)
            if row.provider != value.provider and value.credentials is None:
                raise HTTPException(400, "Новый канал требует новых реквизитов")
        channel_scope(db, a, value.project_id)
        if value.provider == "jira" and not value.project_id:
            # The token may come from the project's Jira MCP, and issues belong to one project's tasks.
            raise HTTPException(400, "Канал Jira настраивается для проекта")
        if not set(value.events) <= IMPORTANT_KINDS:
            raise HTTPException(400, "Неизвестное событие уведомления")
        if value.credentials is not None:
            credentials = value.credentials
            if row and row.provider == value.provider:
                # Omitted keys keep their saved values; "" clears an optional command token.
                credentials = {**json.loads(decrypt(row.secret) or "{}"), **credentials}
            try:
                config = validate_channel_config(value.provider, credentials)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            if config.get("director_token"):
                try:
                    commands = service.authenticate(db, config["director_token"])
                    commands.require("operator")
                    if commands.org_id != a.org_id or commands.token.role == "owner":
                        raise HTTPException(403)
                except HTTPException:
                    raise HTTPException(
                        400, "Токену команд нужна роль оператора (не владельца) в этой организации"
                    ) from None
            secret = encrypt(json.dumps(config))
        elif row:
            secret = row.secret
        else:
            raise HTTPException(400, "Нужны реквизиты канала")
        if row is None:
            row = NotificationChannel(org_id=a.org_id)
            db.add(row)
        previous_provider = row.provider
        for key, item in value.model_dump(exclude={"credentials", "recipient"}).items():
            setattr(row, key, item)
        row.known_events = sorted(IMPORTANT_KINDS)
        if value.provider in {"telegram", "jira"}:
            row.recipient = None
        elif "recipient" in value.model_fields_set or previous_provider != value.provider:
            row.recipient = value.recipient
        row.secret = secret
        db.flush()
        return public_channel(row)


@router.get("/api/notification-deliveries")
def notification_deliveries(a=Depends(access)):
    a.require("owner")
    with Session() as db:
        rows = db.scalars(
            select(NotificationDelivery)
            .join(NotificationChannel, NotificationChannel.id == NotificationDelivery.channel_id)
            .where(NotificationChannel.org_id == a.org_id)
            .order_by(NotificationDelivery.created.desc())
            .limit(100)
        )
        return [
            public(row)
            for row in rows
            if row.task_id is None or a.permits(db.get(Project, db.get(Task, row.task_id).project_id))
        ]


class HumanMessage(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@router.post("/api/tasks/{task_id}/notify")
def human_message(task_id: str, value: HumanMessage, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        task = a.task(db, task_id)
        event(db, task, "human_message", value.message)
        return {"queued": True}


@router.post("/api/chat-commands/rocketchat/{channel_id}")
def rocketchat_command(channel_id: str, payload: dict = Body(...)):
    """Rocket.Chat outgoing webhook: /director commands and thread answers to task notifications."""
    if not handle_rocketchat(channel_id, payload):
        raise HTTPException(403)
    return {}
