"""Provider logins and connections."""

import hashlib
import json
import math
import os
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth_sessions import ACTIVE as LOGIN_ACTIVE
from ..auth_sessions import LoginSession, public_login
from ..db import (
    Connection,
    Pool,
    Session,
    decrypt,
    encrypt,
    now,
    transaction,
)
from ..onboarding import AUTHORIZATION_CODE
from ..web import access, public

router = APIRouter()


class LoginInput(BaseModel):
    provider: str = Field(pattern="^(codex|claude)$")
    name: str = Field(min_length=1, max_length=150)
    account_key: str = Field(default="", max_length=200)
    pool_ids: list[str] = Field(min_length=1, max_length=50)
    model: str | None = Field(default=None, max_length=150)
    priority: int = 100


class LoginCode(BaseModel):
    code: str = Field(min_length=1, max_length=2000)


def owned_login(db, a, login_id):
    a.organization_owner()
    row = db.get(LoginSession, login_id)
    if row is None or row.org_id != a.org_id:
        raise HTTPException(404)
    return row


@router.get("/api/login-sessions")
def login_sessions(a=Depends(access)):
    a.organization_owner()
    with Session() as db:
        return [
            public_login(row)
            for row in db.scalars(
                select(LoginSession)
                .where(LoginSession.org_id == a.org_id)
                .order_by(LoginSession.created.desc())
                .limit(30)
            )
        ]


@router.post("/api/login-sessions", status_code=201)
def create_login(value: LoginInput, a=Depends(access)):
    a.organization_owner()
    if value.provider == "codex" and os.getenv("DARKFACTORY_PERSONAL_MODE") != "1":
        raise HTTPException(
            400, "Вход Codex через CLI доступен только в персональном режиме; для компании подключи SIWC"
        )
    if value.provider == "claude" and not value.account_key.strip():
        raise HTTPException(400, "Укажи email аккаунта Claude")
    with transaction() as db:
        for pool_id in value.pool_ids:
            pool = db.get(Pool, pool_id)
            if pool is None or pool.org_id != a.org_id:
                raise HTTPException(404, "Пул не найден")
        active = list(
            db.scalars(
                select(LoginSession).where(
                    LoginSession.org_id == a.org_id, LoginSession.status.in_(LOGIN_ACTIVE)
                )
            )
        )
        if len(active) >= 3:
            raise HTTPException(409, "Сначала заверши или отмени текущие входы")
        row = LoginSession(org_id=a.org_id, **value.model_dump())
        row.pool_ids = sorted(set(value.pool_ids))
        db.add(row)
        db.flush()
        return public_login(row)


@router.get("/api/login-sessions/{login_id}")
def login_session(login_id: str, a=Depends(access)):
    with Session() as db:
        return public_login(owned_login(db, a, login_id))


@router.post("/api/login-sessions/{login_id}/code")
def submit_login_code(login_id: str, value: LoginCode, a=Depends(access)):
    with transaction() as db:
        row = owned_login(db, a, login_id)
        if row.provider != "claude" or row.status != "waiting" or row.expires <= now():
            raise HTTPException(409, "Этот вход не ожидает код")
        if row.command_secret:
            raise HTTPException(409, "Предыдущий код ещё обрабатывается")
        code = value.code.strip()
        if not AUTHORIZATION_CODE.fullmatch(code):
            raise HTTPException(400, "Вставь полный одноразовый код из браузера: code#state")
        row.command_secret = encrypt(json.dumps({"action": "code", "code": code}))
        return public_login(row)


@router.post("/api/login-sessions/{login_id}/cancel")
def cancel_login(login_id: str, a=Depends(access)):
    with transaction() as db:
        row = owned_login(db, a, login_id)
        if row.status == "done":
            raise HTTPException(409, "Аккаунт уже подключён")
        row.status, row.command_secret, row.snapshot = "cancelled", None, {}
        return public_login(row)


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    provider: str = Field(pattern="^(codex|claude|api)$")
    account_key: str = Field(min_length=1, max_length=200)
    pool_ids: list[str] = Field(min_length=1)
    priority: int = 100
    model: str | None = None
    credentials: dict


def check_credentials(provider, credentials):
    required = {
        "codex": {"accessToken"},
        "claude": {"oauth_token"},
        "api": {"api_key", "base_url"},
    }[provider]
    if not required <= credentials.keys() or any(not credentials[k] for k in required):
        raise HTTPException(400, f"Нужны поля: {', '.join(sorted(required))}")
    if provider == "codex":
        method = credentials.get("auth_method")
        if method == "codex_local" and os.getenv("DARKFACTORY_PERSONAL_MODE") == "1":
            if (
                not isinstance(credentials.get("chatgptAccountId"), str)
                or not credentials["chatgptAccountId"].strip()
            ):
                raise HTTPException(400, "Для локального входа нужен chatgptAccountId")
            if "base_url" in credentials:
                try:
                    endpoint = urlsplit(str(credentials["base_url"]))
                    valid = (
                        endpoint.scheme == "http"
                        and endpoint.hostname in {"127.0.0.1", "localhost", "host.docker.internal"}
                        and not endpoint.username
                        and not endpoint.password
                        and not endpoint.query
                        and not endpoint.fragment
                        and 1 <= (endpoint.port if endpoint.port is not None else 80) <= 65535
                        and not any(ord(char) <= 32 or char == "\\" for char in credentials["base_url"])
                    )
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    raise HTTPException(
                        400, "Личный шлюз должен использовать HTTP на локальном хосте без реквизитов в URL"
                    )
        elif method != "siwc":
            raise HTTPException(
                400, "Для размещаемого сервиса нужен подтверждённый Sign in with ChatGPT (auth_method=siwc)"
            )
    if provider == "api":
        parsed = urlsplit(str(credentials["base_url"]))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(400, "Нужен HTTPS OpenAI-compatible endpoint без реквизитов в URL")
        for name in ("input_usd_per_million", "output_usd_per_million"):
            if name in credentials:
                try:
                    price = float(credentials[name])
                except (ValueError, TypeError):
                    raise HTTPException(400, "Цена токенов должна быть конечным неотрицательным числом")
                if not math.isfinite(price) or price < 0:
                    raise HTTPException(400, "Цена токенов должна быть конечным неотрицательным числом")


def unique_credentials(db, provider, credentials, excluding=None):
    key = {"api": "api_key", "codex": "accessToken", "claude": "oauth_token"}[provider]
    for connection in db.scalars(select(Connection).where(Connection.provider == provider)):
        if connection.id != excluding and json.loads(decrypt(connection.secret)).get(key) == credentials[key]:
            raise HTTPException(
                409, "Эти реквизиты уже подключены; используй существующий аккаунт в нескольких пулах"
            )


@router.get("/api/connections")
def connections(a=Depends(access)):
    a.require("owner")
    with Session() as db:
        return [public(c) for c in db.scalars(select(Connection).where(Connection.org_id == a.org_id))]


@router.post("/api/connections", status_code=201)
def create_connection(value: ConnectionInput, a=Depends(access)):
    a.organization_owner()
    check_credentials(value.provider, value.credentials)
    with transaction() as db:
        unique_credentials(db, value.provider, value.credentials)
        for pool_id in value.pool_ids:
            pool = db.get(Pool, pool_id)
            if not pool or pool.org_id != a.org_id:
                raise HTTPException(400, "Пул другой организации или не существует")
        conn = Connection(
            **value.model_dump(exclude={"credentials", "account_key"}),
            org_id=a.org_id,
            account_key=hashlib.sha256(value.account_key.strip().encode()).hexdigest(),
            secret=encrypt(json.dumps(value.credentials)),
        )
        db.add(conn)
        db.flush()
        return public(conn)


@router.put("/api/connections/{connection_id}/credentials")
def reconnect(connection_id: str, credentials: dict, a=Depends(access)):
    a.organization_owner()
    with transaction() as db:
        conn = db.get(Connection, connection_id)
        if not conn or conn.org_id != a.org_id:
            raise HTTPException(404)
        if conn.lease_task:
            raise HTTPException(409, "Подключение занято")
        check_credentials(conn.provider, credentials)
        unique_credentials(db, conn.provider, credentials, conn.id)
        conn.secret, conn.status, conn.retry_at = encrypt(json.dumps(credentials)), "ready", None
        conn.quota_details, conn.quota_observed, conn.quota_source = {}, None, "unknown"
    return {"ok": True}


@router.post("/api/connections/{connection_id}/recheck")
def recheck_connection(connection_id: str, a=Depends(access)):
    """Limits were reset outside the factory: forget the stored exhaustion and probe on the next claim."""
    a.organization_owner()
    with transaction() as db:
        conn = db.get(Connection, connection_id)
        if not conn or conn.org_id != a.org_id:
            raise HTTPException(404)
        if conn.status != "quota":
            raise HTTPException(409, "Подключение не ждёт квоту")
        conn.status, conn.retry_at, conn.quota_source = "ready", None, "recheck_due"
        conn.quota_details, conn.quota_observed = {}, None
        return public(conn)


class ConnectionSettings(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    pool_ids: list[str] = Field(min_length=1)
    priority: int = 100
    model: str | None = None
    enabled: bool = True


@router.put("/api/connections/{connection_id}")
def update_connection(connection_id: str, value: ConnectionSettings, a=Depends(access)):
    a.organization_owner()
    with transaction() as db:
        connection = db.get(Connection, connection_id)
        if not connection or connection.org_id != a.org_id:
            raise HTTPException(404)
        if connection.lease_task:
            raise HTTPException(409, "Подключение занято")
        for ident in value.pool_ids:
            pool = db.get(Pool, ident)
            if not pool or pool.org_id != a.org_id:
                raise HTTPException(400, "Пул другой организации или не существует")
        for key, item in value.model_dump(exclude={"enabled"}).items():
            setattr(connection, key, item)
        if not value.enabled:
            connection.status = "disabled"
        elif connection.status == "disabled":
            connection.status = "ready"
        return public(connection)
