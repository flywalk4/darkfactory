"""Subscription pools."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth_sessions import ACTIVE as LOGIN_ACTIVE
from ..auth_sessions import LoginSession
from ..db import (
    Connection,
    Pool,
    Project,
    Session,
    transaction,
)
from ..web import access, public

router = APIRouter()


class PoolInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    cross_runtime: bool = True
    allowed_providers: list[str] = Field(default=["codex", "claude", "api"], min_length=1)
    allowed_models: list[str] = Field(default_factory=list)
    max_active: int = Field(default=2, ge=1, le=100)
    max_switches: int = Field(default=8, ge=1, le=100)
    retry_seconds: int = Field(default=300, ge=30, le=86400)
    min_remaining_percent: float = Field(default=10, ge=0, le=100, allow_inf_nan=False)
    quota_max_age_seconds: int = Field(default=300, ge=30, le=86400)
    require_known_quota: bool = False
    sessions_per_connection: int = Field(default=3, ge=1, le=10)
    light_models: dict[str, str] = Field(default_factory=dict)


def pool_policy(value):
    if not set(value.allowed_providers) <= {"codex", "claude", "api"}:
        raise HTTPException(400, "Неизвестный исполнитель в политике пула")
    if not set(value.light_models) <= {"codex", "claude"}:
        raise HTTPException(400, "Лёгкие модели задаются только для Codex и Claude")
    policy = value.model_dump(exclude={"name", "cross_runtime"})
    policy["light_models"] = {k: v.strip()[:150] for k, v in value.light_models.items() if v.strip()}
    return policy


@router.get("/api/pools")
def pools(a=Depends(access)):
    with Session() as db:
        return [public(p) for p in db.scalars(select(Pool).where(Pool.org_id == a.org_id))]


@router.post("/api/pools", status_code=201)
def create_pool(value: PoolInput, a=Depends(access)):
    a.organization_owner()
    with transaction() as db:
        pool = Pool(
            name=value.name, cross_runtime=value.cross_runtime, policy=pool_policy(value), org_id=a.org_id
        )
        db.add(pool)
        db.flush()
        return public(pool)


@router.put("/api/pools/{pool_id}")
def update_pool(pool_id: str, value: PoolInput, a=Depends(access)):
    a.organization_owner()
    with transaction() as db:
        pool = db.get(Pool, pool_id)
        if not pool or pool.org_id != a.org_id:
            raise HTTPException(404)
        pool.name, pool.cross_runtime = value.name, value.cross_runtime
        pool.policy = {
            **pool_policy(value),
            **({"default_for_projects": True} if (pool.policy or {}).get("default_for_projects") else {}),
        }
        return public(pool)


@router.delete("/api/pools/{pool_id}")
def delete_pool(pool_id: str, a=Depends(access)):
    a.organization_owner()
    with transaction() as db:
        pool = db.get(Pool, pool_id)
        if not pool or pool.org_id != a.org_id:
            raise HTTPException(404, "Пул не найден")
        projects = list(
            db.scalars(
                select(Project).where(
                    Project.org_id == a.org_id,
                    (Project.writer_pool == pool_id) | (Project.reviewer_pool == pool_id),
                )
            )
        )
        if projects:
            names = ", ".join(project.name for project in projects)
            raise HTTPException(409, f"Сначала выбери другой пул в проектах: {names}")
        logins = db.scalars(
            select(LoginSession).where(LoginSession.org_id == a.org_id, LoginSession.status.in_(LOGIN_ACTIVE))
        )
        if any(pool_id in (login.pool_ids or []) for login in logins):
            raise HTTPException(409, "Сначала заверши или отмени вход в подписку для этого пула")
        for connection in db.scalars(select(Connection).where(Connection.org_id == a.org_id)):
            if pool_id in (connection.pool_ids or []):
                connection.pool_ids = [ident for ident in connection.pool_ids if ident != pool_id]
        db.delete(pool)
        return {"deleted": pool_id}
