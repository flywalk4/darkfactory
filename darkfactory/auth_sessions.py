"""Durable guided logins backed by disposable containers and private files.

The API owns authorization and creation/cancellation of LoginSession rows. This
controller never starts logins at import time. ``advance_logins`` is worker-only.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import docker
from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db import ROOT, Base, Connection, Pool, decrypt, encrypt, now, transaction, uid
from .egress import egress_proxy, local_proxy
from .execution import scoped_mount
from .onboarding import CLAUDE_QUERY_KEYS, MAX_OUTPUT, LoginFlow

AUTH_HOSTS = {
    ("auth.openai.com", 443),
    ("chatgpt.com", 443),
    ("claude.com", 443),
    ("platform.claude.com", 443),
}
ACTIVE = {"queued", "starting", "waiting"}
TERMINAL = {"done", "failed", "cancelled", "expired"}
_OWNER = uid()
_PROXIES = {}


class LoginSession(Base):
    __tablename__ = "login_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    pool_ids: Mapped[list] = mapped_column(JSON, default=list)
    name: Mapped[str] = mapped_column(String(150))
    account_key: Mapped[str] = mapped_column(String(200), default="")
    model: Mapped[str | None] = mapped_column(String(150))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    command_secret: Mapped[str | None] = mapped_column(Text)
    connection_id: Mapped[str | None] = mapped_column(ForeignKey("connections.id"))
    container_id: Mapped[str | None] = mapped_column(String(100))
    controller_id: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(200))
    created: Mapped[float] = mapped_column(Float, default=now)
    expires: Mapped[float] = mapped_column(Float, default=lambda: now() + 900)


def _safe_url(provider, value):
    if not isinstance(value, str) or len(value) > 8192 or "sk-ant-" in value:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.fragment
            or any(ord(char) <= 32 for char in value)
        ):
            return None
        if provider == "codex":
            return (
                value
                if (
                    parsed.hostname == "auth.openai.com"
                    and parsed.path == "/codex/device"
                    and not parsed.query
                )
                else None
            )
        if (
            provider == "claude"
            and parsed.hostname in {"claude.com", "claude.ai"}
            and parsed.path in {"/cai/oauth/authorize", "/oauth/authorize"}
            and all(key in CLAUDE_QUERY_KEYS for key, _ in parse_qsl(parsed.query))
        ):
            return value
    except ValueError:
        pass
    return None


def public_login(row):
    snapshot = row.snapshot if isinstance(row.snapshot, dict) else {}
    status = row.status if row.status in ACTIVE | TERMINAL else "failed"
    code = snapshot.get("user_code")
    if (
        row.provider != "codex"
        or not isinstance(code, str)
        or not re.fullmatch(r"[A-Z0-9]{3,8}-[A-Z0-9]{3,8}", code)
    ):
        code = None
    prompts = {
        "queued": "Ожидаем запуска входа",
        "starting": "Подготавливаем вход",
        "waiting": "Открой ссылку и заверши вход в браузере",
        "done": "Аккаунт подключён",
        "failed": "Вход не выполнен. Начни новую попытку",
        "cancelled": "Вход отменён",
        "expired": "Время входа истекло. Начни новую попытку",
    }
    return {
        "id": row.id,
        "provider": row.provider,
        "name": row.name,
        "pool_ids": list(row.pool_ids or []),
        "model": row.model,
        "priority": row.priority,
        "status": status,
        "verification_url": _safe_url(row.provider, snapshot.get("verification_url"))
        if status == "waiting"
        else None,
        "user_code": code if status == "waiting" else None,
        "prompt": prompts[status],
        "connection_id": row.connection_id,
        "created": row.created,
        "expires": row.expires,
        "error": prompts[status] if status in {"failed", "expired"} else None,
    }


def _directory(row):
    if not re.fullmatch(r"[a-f0-9]{32}", row.id):
        raise ValueError("Invalid login id")
    directory = ROOT / "auth" / row.id
    if directory.is_symlink():
        raise ValueError("Unsafe auth directory")
    return directory


def _write_private(path, value, owner=False):
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    if owner and os.geteuid() == 0:
        os.chown(temporary, 1000, 1000)
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_private(path):
    if path.is_symlink() or path.stat().st_size > MAX_OUTPUT:
        raise ValueError("Invalid auth response")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid auth response")
    return value


def _marker_id(path):
    if not path.exists():
        return None
    value = _read_private(path).get("id")
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Invalid command marker")
    return value


def _pending_command(row, directory):
    """Keep API's encrypted pending flag until this exact command is acknowledged.

    Ciphertext is random and remains stable until acknowledgement. Its digest
    identifies delivery without exposing a guessable digest of the browser code.
    A durable claim closes the unlink/ack window after controller restarts.
    """
    if not row.command_secret:
        return
    identity = hashlib.sha256((row.id + "\0" + row.command_secret).encode()).hexdigest()
    if _marker_id(directory / "ack.json") == identity:
        row.command_secret = None
        return
    if _marker_id(directory / "claimed.json") == identity:
        return
    path = directory / "command.json"
    if path.exists():
        if _read_private(path).get("id") != identity:
            raise ValueError("Unexpected pending command")
        return
    command = json.loads(decrypt(row.command_secret))
    if command.get("action") not in {"code", "submit_code"}:
        raise ValueError("Invalid login command")
    _write_private(path, {**command, "id": identity}, owner=True)


def _proxy(row, directory):
    if row.id not in _PROXIES:
        socket = directory / "egress.sock"
        socket.unlink(missing_ok=True)
        context = egress_proxy(socket, AUTH_HOSTS)
        context.__enter__()
        _PROXIES[row.id] = context


def _container(client, row):
    try:
        return client.containers.get(row.container_id or "df-auth-" + row.id)
    except docker.errors.NotFound:
        return None


def _cleanup(client, row):
    container = _container(client, row)
    if container is not None:
        container.remove(force=True)
    context = _PROXIES.pop(row.id, None)
    if context:
        context.__exit__(None, None, None)
    directory = _directory(row)
    if directory.exists():
        shutil.rmtree(directory)
    row.container_id, row.controller_id, row.lease_until, row.command_secret = None, None, None, None


def _import_connection(db, row, credentials):
    from .api import check_credentials, unique_credentials

    check_credentials(row.provider, credentials)
    for pool_id in row.pool_ids:
        pool = db.get(Pool, pool_id)
        if pool is None or pool.org_id != row.org_id:
            raise ValueError("Invalid login pool")
    account = (
        credentials["chatgptAccountId"] if row.provider == "codex" else row.account_key.strip().casefold()
    )
    if not account:
        raise ValueError("Missing account identity")
    key = hashlib.sha256(account.encode()).hexdigest()
    connection = db.scalar(
        select(Connection).where(Connection.provider == row.provider, Connection.account_key == key)
    )
    if connection and (connection.org_id != row.org_id or connection.lease_task):
        raise ValueError("Account cannot be reconnected")
    unique_credentials(db, row.provider, credentials, connection.id if connection else None)
    if connection is None:
        connection = Connection(
            org_id=row.org_id,
            provider=row.provider,
            account_key=key,
            name=row.name,
            pool_ids=list(row.pool_ids),
            model=row.model,
            priority=row.priority,
            secret=encrypt(json.dumps(credentials)),
        )
        db.add(connection)
    else:
        connection.secret = encrypt(json.dumps(credentials))
        connection.pool_ids = sorted(set(connection.pool_ids or []) | set(row.pool_ids))
        connection.status, connection.retry_at = "ready", None
        connection.quota_details, connection.quota_observed, connection.quota_source = {}, None, "unknown"
    db.flush()
    row.connection_id, row.status, row.error = connection.id, "done", None
    row.snapshot, row.command_secret = {}, None


def _advance(client, db, row):
    directory = _directory(row)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    if os.geteuid() == 0:
        os.chown(directory, 1000, 1000)
    if row.provider not in {"codex", "claude"} or (
        row.provider == "codex" and os.getenv("DARKFACTORY_PERSONAL_MODE") != "1"
    ):
        raise ValueError("Unsupported login mode")
    _proxy(row, directory)
    container = _container(client, row)
    if container is None:
        # Losing an already-started container loses PKCE state. Never silently
        # start a second authorization flow under an old browser link.
        if row.status != "queued":
            raise ValueError("Login container disappeared")
        _write_private(
            directory / "job.json",
            {"provider": row.provider, "personal_mode": os.getenv("DARKFACTORY_PERSONAL_MODE") == "1"},
            owner=True,
        )
        container = client.containers.create(
            os.getenv("EXECUTOR_IMAGE", "darkfactory-executor:local"),
            # The executor image's entrypoint is the task runner; the login runner replaces it.
            entrypoint=["python", "-m", "darkfactory.auth_sessions", "--runner"],
            name="df-auth-" + row.id,
            user="1000:1000",
            working_dir="/tmp",
            detach=True,
            mounts=[scoped_mount("auth/" + row.id, "/run/auth")],
            read_only=True,
            tmpfs={"/tmp": "rw,nosuid,size=128m,mode=1777"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=128,
            mem_limit="512m",
            nano_cpus=1_000_000_000,
            network_mode="none",
            environment={"HOME": "/tmp/home"},
            labels={"darkfactory.auth": row.id},
            log_config=docker.types.LogConfig(type="none"),
        )
    row.container_id = container.id
    container.reload()
    if container.status == "created":
        container.start()
    if row.status == "queued":
        row.status = "starting"
    if (directory / "result.json").exists():
        credentials = _read_private(directory / "result.json")
        with db.begin_nested():
            _import_connection(db, row, credentials)
        # Commit the encrypted connection before deleting the only durable
        # authentication result. A crash then resumes terminal cleanup safely.
        return
    _pending_command(row, directory)
    if (directory / "state.json").exists():
        snapshot = _read_private(directory / "state.json")
        status = snapshot.get("status")
        if status in {"starting", "waiting"}:
            row.status = status
            row.snapshot = {
                key: value for key, value in public_login_snapshot(row.provider, snapshot).items()
            }
        elif status in {"failed", "cancelled", "expired"}:
            reason = snapshot.get("reason") if status == "failed" else None
            row.status, row.snapshot = status, {}
            row.error = "Вход не выполнен" + (f" ({reason[:200]})" if isinstance(reason, str) else "")
            _cleanup(client, row)
            return
    container.reload()
    if container.status in {"exited", "dead", "removing"}:
        raise ValueError("Login container stopped")
    row.controller_id, row.lease_until = _OWNER, now() + 30


def public_login_snapshot(provider, snapshot):
    code = snapshot.get("user_code")
    return {
        "verification_url": _safe_url(provider, snapshot.get("verification_url")),
        "user_code": code
        if provider == "codex"
        and isinstance(code, str)
        and re.fullmatch(r"[A-Z0-9]{3,8}-[A-Z0-9]{3,8}", code)
        else None,
    }


def advance_logins(client=None):
    """Advance at most one active login; recover by deterministic container name.

    A filesystem controller lock plus renewable database ownership prevents two
    workers starting the same authorization session. No vendor output is logged.
    """
    ROOT.mkdir(parents=True, exist_ok=True)
    with ExitStack() as resources, (ROOT / "auth-controller.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with transaction() as db:
            rows = list(db.scalars(select(LoginSession).order_by(LoginSession.created)))
            pending = [
                row
                for row in rows
                if row.status in ACTIVE or row.container_id or row.command_secret or row.id in _PROXIES
            ]
            if not pending:
                return
            if client is None:
                client = docker.from_env()
                resources.callback(client.close)
            for row in pending:
                if row.status in TERMINAL or row.expires <= now():
                    if row.status in ACTIVE:
                        row.status, row.error = "expired", "Время входа истекло"
                    row.snapshot = {}
                    _cleanup(client, row)
            active = [row for row in pending if row.status in ACTIVE]
            if not active:
                return
            # A running authorization always precedes newly queued sessions.
            row = next((row for row in active if row.status != "queued"), active[0])
            if row.controller_id not in {None, _OWNER} and (row.lease_until or 0) > now():
                return
            try:
                _advance(client, db, row)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"[:200]
                print(f"login {row.id} failed: {reason}", flush=True)
                row.status, row.error, row.snapshot, row.command_secret = (
                    "failed",
                    f"Вход не выполнен ({reason})",
                    {},
                    None,
                )
                _cleanup(client, row)
        with transaction() as db:
            completed = select(LoginSession).where(
                LoginSession.status.in_(TERMINAL), LoginSession.container_id.is_not(None)
            )
            for row in db.scalars(completed):
                _cleanup(client, row)


def _publish_flow(flow, directory):
    snapshot = flow.snapshot()
    if snapshot["status"] == "done":
        credentials = flow.take_credentials()
        if credentials:
            _write_private(directory / "result.json", credentials)
        elif not (directory / "result.json").exists():
            raise ValueError("Missing successful login result")
    _write_private(directory / "state.json", snapshot)
    return snapshot["status"] in TERMINAL


def _consume_command(flow, directory):
    path = directory / "command.json"
    if not path.exists():
        return
    command = _read_private(path)
    if command.get("action") == "cancel":
        path.unlink()
        flow.cancel()
        return
    identity = command.get("id")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("Invalid command identity")
    if _marker_id(directory / "ack.json") == identity:
        # A stale delivery after a controller crash must never enter the CLI.
        path.unlink()
        return
    if _marker_id(directory / "claimed.json") == identity:
        # A runner crash cannot recover the CLI's in-memory PKCE flow. Fail
        # closed instead of replaying a possibly already-submitted one-time code.
        raise ValueError("Interrupted command delivery")
    _write_private(directory / "claimed.json", {"id": identity})
    path.unlink()
    if command.get("action") not in {"code", "submit_code"}:
        raise ValueError("Invalid login command")
    try:
        flow.submit_code(command.get("code"))
    except ValueError:
        # CLI may finish between our initial snapshot and this input. Preserve
        # its issued token rather than replacing success with a stale-code error.
        if flow.snapshot()["status"] != "done":
            raise
    _write_private(directory / "ack.json", {"id": identity})


def file_runner(directory=Path("/run/auth")):
    """Trusted container entrypoint. Never prints or logs vendor output/secrets."""
    flow = None
    with ExitStack() as stack:
        try:
            job = _read_private(directory / "job.json")
            proxy = stack.enter_context(local_proxy(str(directory / "egress.sock")))
            home = stack.enter_context(tempfile.TemporaryDirectory(prefix="auth-home-"))
            flow = LoginFlow(job["provider"], home, job.get("personal_mode") is True, proxy_url=proxy)
            flow.start()
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                # Completion always wins over late browser input, including
                # input left on disk while the worker was restarting.
                if _publish_flow(flow, directory):
                    return
                _consume_command(flow, directory)
                if _publish_flow(flow, directory):
                    return
                time.sleep(0.25)
            flow.cancel()
            _write_private(directory / "state.json", {"status": "expired"})
        except Exception as exc:
            # Only the exception class and our own message: vendor output never reaches it.
            reason = f"{type(exc).__name__}: {exc}"[:200]
            _write_private(directory / "state.json", {"status": "failed", "reason": reason})
        finally:
            if flow:
                flow.cancel()


if __name__ == "__main__":
    if sys.argv[1:] == ["--runner"]:
        file_runner()
    else:
        raise SystemExit("Worker-only module; use --runner inside an isolated auth container")
