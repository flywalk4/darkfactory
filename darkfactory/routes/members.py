"""Members and API tokens."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import (
    service,
)
from ..db import (
    Principal,
    Session,
    Token,
    transaction,
)
from ..web import access, public

router = APIRouter()


class MemberInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    role: str = Field(pattern="^(owner|operator|observer)$")
    project_ids: list[str] = Field(default_factory=list)
    enabled: bool = True


@router.get("/api/members")
def members(a=Depends(access)):
    a.require("owner")
    with Session() as db:
        return [public(m) for m in db.scalars(select(Principal).where(Principal.org_id == a.org_id))]


@router.post("/api/members", status_code=201)
def create_member(value: MemberInput, a=Depends(access)):
    a.require("owner")
    a.require_scope(value.project_ids)
    with transaction() as db:
        for ident in value.project_ids:
            a.project(db, ident)
        member = Principal(**value.model_dump(), org_id=a.org_id)
        db.add(member)
        db.flush()
        token = service.issue_token(db, member, member.role, member.project_ids)
        return {**token, **public(member), "token_id": token["id"]}


@router.put("/api/members/{member_id}")
def update_member(member_id: str, value: MemberInput, a=Depends(access)):
    a.require("owner")
    with transaction() as db:
        member = db.get(Principal, member_id)
        if not member or member.org_id != a.org_id:
            raise HTTPException(404)
        a.require_scope(member.project_ids)
        a.require_scope(value.project_ids)
        if member.id == a.principal.id and (not value.enabled or value.role != "owner"):
            raise HTTPException(400, "Нельзя лишить себя доступа владельца")
        for ident in value.project_ids:
            a.project(db, ident)
        for key, item in value.model_dump().items():
            setattr(member, key, item)
        return public(member)


@router.post("/api/members/{member_id}/token", status_code=201)
def reissue_member_token(member_id: str, a=Depends(access)):
    """A fresh token with the member's role and projects; the member's earlier tokens stop working.

    The DARKFACTORY_ADMIN_TOKEN token stays: it changes only through the environment.
    """
    a.require("owner")
    with transaction() as db:
        member = db.get(Principal, member_id)
        if not member or member.org_id != a.org_id:
            raise HTTPException(404)
        if member.id == a.principal.id:
            raise HTTPException(400, "Свои токены выпускай через /api/tokens")
        a.require_scope(member.project_ids)
        for token in db.scalars(select(Token).where(Token.principal_id == member.id, ~Token.from_env)):
            token.revoked = True
        return service.issue_token(db, member, member.role, member.project_ids)


class TokenInput(BaseModel):
    role: str = Field(pattern="^(owner|operator|observer)$")
    project_ids: list[str] = Field(default_factory=list)


@router.post("/api/tokens", status_code=201)
def create_token(value: TokenInput, a=Depends(access)):
    with transaction() as db:
        a.require(value.role)
        a.require_scope(value.project_ids)
        for ident in value.project_ids:
            a.project(db, ident)
        if a.token.project_ids and (
            not value.project_ids or not set(value.project_ids) <= set(a.token.project_ids)
        ):
            raise HTTPException(403, "Нельзя расширить область исходного токена")
        return service.issue_token(db, a.principal, value.role, value.project_ids)


@router.delete("/api/tokens/{token_id}")
def revoke(token_id: str, a=Depends(access)):
    with transaction() as db:
        token = db.get(Token, token_id)
        if not token or token.principal_id != a.principal.id:
            raise HTTPException(404)
        if not manageable_token(a, token):
            raise HTTPException(403, "Токен не может управлять более широкими правами")
        token.revoked = True
    return {"ok": True}


@router.get("/api/tokens")
def tokens(a=Depends(access)):
    with Session() as db:
        return [
            public(token)
            for token in db.scalars(select(Token).where(Token.principal_id == a.principal.id))
            if not token.revoked and manageable_token(a, token)
        ]


def manageable_token(a, token):
    if token.id == a.token.id:
        return True
    try:
        a.require(token.role)
        a.require_scope(token.project_ids)
    except HTTPException:
        return False
    return True
