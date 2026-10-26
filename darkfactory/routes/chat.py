"""Director configuration chat."""

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from ..db import (
    Session,
    decrypt,
    encrypt,
    transaction,
    uid,
)
from ..director import ACTIVE as CHAT_ACTIVE
from ..director import ChatThread, Dialogue
from ..director import image_files as dialogue_image_files
from ..director import messages as dialogue_messages
from ..director import release as release_dialogue
from ..director import save_images as save_dialogue_images
from ..web import access

router = APIRouter()


class ChatImage(BaseModel):
    media_type: str = Field(pattern="^image/(png|jpeg|gif|webp)$")
    data: str = Field(min_length=1, max_length=7_000_000)


class ChatInput(BaseModel):
    message: str = Field(default="", max_length=12000)
    project_id: str | None = None
    thread_id: str | None = None
    images: list[ChatImage] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def not_empty(self):
        if not self.message.strip() and not self.images:
            raise ValueError("Напиши сообщение или приложи картинку")
        return self


class ChatThreadInput(BaseModel):
    project_id: str | None = None


def owned_chat_thread(db, a, thread_id, project_id):
    thread = db.get(ChatThread, thread_id)
    if not thread or thread.token_id != a.token.id or thread.project_id != project_id:
        raise HTTPException(404, "Чат не найден")
    return thread


@router.get("/api/config-chat/threads")
def config_chat_threads(project_id: str | None = None, a=Depends(access)):
    with Session() as db:
        if project_id:
            a.project(db, project_id)
        rows = db.scalars(
            select(ChatThread)
            .where(ChatThread.token_id == a.token.id, ChatThread.project_id == project_id)
            .order_by(ChatThread.created.desc())
        )
        return [{"id": row.id, "title": row.title, "created": row.created} for row in rows]


@router.post("/api/config-chat/threads", status_code=201)
def new_config_chat(value: ChatThreadInput, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        if value.project_id:
            a.project(db, value.project_id)
        row = ChatThread(id=uid(), org_id=a.org_id, token_id=a.token.id, project_id=value.project_id)
        db.add(row)
        db.flush()
        return {"id": row.id, "title": row.title, "created": row.created}


@router.get("/api/config-chat")
def config_chat(project_id: str | None = None, thread_id: str | None = None, a=Depends(access)):
    with Session() as db:
        if project_id:
            a.project(db, project_id)
        if thread_id:
            owned_chat_thread(db, a, thread_id, project_id)
        rows = list(
            db.scalars(
                select(Dialogue)
                .where(
                    Dialogue.token_id == a.token.id,
                    Dialogue.project_id == project_id,
                    Dialogue.thread_id == thread_id,
                )
                .order_by(Dialogue.created.desc())
                .limit(30)
            )
        )
        result = dialogue_messages(list(reversed(rows)))
        active = db.scalar(
            select(Dialogue).where(Dialogue.token_id == a.token.id, Dialogue.status.in_(CHAT_ACTIVE))
        )
        result["busy"] = active is not None
        result["active_message_id"] = active.id if active else None
        result["active_status"] = active.status if active else None
        result["active_thread_id"] = active.thread_id if active else None
        result["active_project_id"] = active.project_id if active else None
        return result


@router.post("/api/config-chat", status_code=202)
def send_config_chat(value: ChatInput, authorization: str = Header(default=""), a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        if value.project_id:
            a.project(db, value.project_id)
        thread = owned_chat_thread(db, a, value.thread_id, value.project_id) if value.thread_id else None
        if db.scalar(
            select(Dialogue.id).where(Dialogue.token_id == a.token.id, Dialogue.status.in_(CHAT_ACTIVE))
        ):
            raise HTTPException(409, "Дождись ответа директора")
        row = Dialogue(
            id=uid(),
            org_id=a.org_id,
            token_id=a.token.id,
            project_id=value.project_id,
            thread_id=value.thread_id,
            request=value.message or "(см. картинку)",
            credential_secret=encrypt(authorization.removeprefix("Bearer ")),
        )
        save_dialogue_images(row.id, value.images)
        db.add(row)
        if thread and thread.title == "Новый чат":
            thread.title = " ".join(value.message.split())[:100] or "Новый чат"
        return {"id": row.id, "status": "queued"}


@router.get("/api/config-chat/messages/{message_id}/images/{index}")
def config_chat_image(message_id: str, index: int, a=Depends(access)):
    with Session() as db:
        row = db.get(Dialogue, message_id)
        if not row or row.token_id != a.token.id:
            raise HTTPException(404, "Сообщение не найдено")
        if row.project_id:
            a.project(db, row.project_id)
    images = dialogue_image_files(message_id)
    if not 0 <= index < len(images):
        raise HTTPException(404, "Картинка не найдена")
    path, media_type = images[index]
    return Response(
        path.read_bytes(),
        media_type=media_type,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
    )


@router.post("/api/config-chat/messages/{message_id}/cancel")
def cancel_config_chat(message_id: str, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        row = db.get(Dialogue, message_id)
        if not row or row.token_id != a.token.id:
            raise HTTPException(404, "Сообщение не найдено")
        if row.project_id:
            a.project(db, row.project_id)
        if row.status in CHAT_ACTIVE and row.status != "cancelling":
            row.reply = "Сообщение отменено. Уже начатые действия могут завершиться; журнал сохранён."
            if row.lease_owner:
                row.status = "cancelling"
                row.credential_secret = None
            else:
                row.status = "cancelled"
                release_dialogue(db, row)
        return {"id": row.id, "status": row.status}


@router.post("/api/config-chat/messages/{message_id}/organization-access")
def director_organization_access(message_id: str, a=Depends(access)):
    a.organization_owner()
    with transaction() as db:
        row = db.get(Dialogue, message_id)
        if not row or row.token_id != a.token.id or not row.organization_secret:
            raise HTTPException(404, "Одноразовый доступ не найден")
        raw = decrypt(row.organization_secret)
        row.organization_secret = None
        return {"token": raw}
