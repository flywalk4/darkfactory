import hashlib
import os
import secrets
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.now(UTC).timestamp()


ROOT = Path(os.getenv("DATA_ROOT", ".data")).resolve()
engine = create_engine(os.getenv("DATABASE_URL", "sqlite:///.data/dev.db"), pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(150))
    max_active: Mapped[int] = mapped_column(Integer, default=2, server_default="2")


class Principal(Base):
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(150))
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    role: Mapped[str] = mapped_column(String(20), default="observer")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    project_ids: Mapped[list] = mapped_column(JSON, default=list)


class Token(Base):
    __tablename__ = "tokens"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    role: Mapped[str] = mapped_column(String(20))
    project_ids: Mapped[list] = mapped_column(JSON, default=list)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set for the owner token taken from DARKFACTORY_ADMIN_TOKEN; a new value revokes the old one.
    from_env: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    remote: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(String(150), default="preprod")
    seed_branch: Mapped[str] = mapped_column(String(150), default="main")
    checks: Mapped[list] = mapped_column(JSON, default=list)
    writer_pool: Mapped[str | None] = mapped_column(String(32))
    reviewer_pool: Mapped[str | None] = mapped_column(String(32))
    writer: Mapped[str] = mapped_column(String(20), default="codex")
    reviewer: Mapped[str] = mapped_column(String(20), default="claude")
    max_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    max_corrections: Mapped[int] = mapped_column(Integer, default=3)
    integration_task: Mapped[str | None] = mapped_column(String(32))
    incident_id: Mapped[str | None] = mapped_column(String(32))
    health: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    forge: Mapped[str] = mapped_column(String(20), default="git")
    forge_url: Mapped[str | None] = mapped_column(Text)
    forge_repo: Mapped[str | None] = mapped_column(Text)
    secret: Mapped[str | None] = mapped_column(Text)


class Pool(Base):
    __tablename__ = "pools"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    cross_runtime: Mapped[bool] = mapped_column(Boolean, default=True)
    policy: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("provider", "account_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    provider: Mapped[str] = mapped_column(String(20))
    account_key: Mapped[str] = mapped_column(String(200))
    pool_ids: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    model: Mapped[str | None] = mapped_column(String(150))
    secret: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="ready")
    quota_source: Mapped[str] = mapped_column(String(50), default="unknown")
    quota_observed: Mapped[float | None] = mapped_column(Float)
    quota_details: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    retry_at: Mapped[float | None] = mapped_column(Float)
    lease_task: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[float | None] = mapped_column(Float)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    request: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(10), default="normal", server_default=text("'normal'"))
    state: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    phase: Mapped[str] = mapped_column(String(30), default="prepare")
    reason: Mapped[str | None] = mapped_column(Text)
    branch: Mapped[str] = mapped_column(String(100))
    created: Mapped[float] = mapped_column(Float, default=now)
    updated: Mapped[float] = mapped_column(Float, default=now)
    active_seconds: Mapped[float] = mapped_column(Float, default=0)
    time_credit: Mapped[int] = mapped_column(Integer, default=0)
    correction_credit: Mapped[int] = mapped_column(Integer, default=0)
    switch_credit: Mapped[int] = mapped_column(Integer, default=0)
    api_budget_usd: Mapped[float] = mapped_column(Float, default=0)
    api_spent_usd: Mapped[float] = mapped_column(Float, default=0)
    api_reserved_usd: Mapped[float] = mapped_column(Float, default=0)
    corrections: Mapped[int] = mapped_column(Integer, default=0)
    switches: Mapped[int] = mapped_column(Integer, default=0)
    connection_id: Mapped[str | None] = mapped_column(String(32))
    last_connection: Mapped[str | None] = mapped_column(String(32))
    lease_owner: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[float | None] = mapped_column(Float)
    heartbeat: Mapped[float | None] = mapped_column(Float)
    execution_deadline: Mapped[float | None] = mapped_column(Float)
    time_reserved_seconds: Mapped[float] = mapped_column(Float, default=0, server_default=text("0"))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    target_sha: Mapped[str | None] = mapped_column(String(64))
    candidate_sha: Mapped[str | None] = mapped_column(String(64))
    verified_sha: Mapped[str | None] = mapped_column(String(64))
    reviewed_sha: Mapped[str | None] = mapped_column(String(64))
    result_sha: Mapped[str | None] = mapped_column(String(64))
    change_url: Mapped[str | None] = mapped_column(Text)
    incident: Mapped[bool] = mapped_column(Boolean, default=False)
    incident_key: Mapped[str | None] = mapped_column(String(64))
    system_check: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    role_config: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    dependency_commits: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    plan_depth: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    explicit_api: Mapped[bool] = mapped_column(Boolean, default=False)
    # Who asked for it; subtasks inherit it. Empty for work the factory started on its own.
    created_by: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(150))
    __table_args__ = (UniqueConstraint("project_id", "idempotency_key"),)


class Link(Base):
    __tablename__ = "links"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    other_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[str] = mapped_column(String(20))
    artifact: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("task_id", "other_id", "kind"),)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    at: Mapped[float] = mapped_column(Float, default=now)
    kind: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Artifact(Base):
    __tablename__ = "project_artifacts"
    __table_args__ = (UniqueConstraint("project_id", "key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(50))
    accepted_version_id: Mapped[str | None] = mapped_column(String(32))
    created: Mapped[float] = mapped_column(Float, default=now)


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (UniqueConstraint("artifact_id", "number"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("project_artifacts.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    source_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    source_sha: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    created: Mapped[float] = mapped_column(Float, default=now)
    accepted_by: Mapped[str | None] = mapped_column(ForeignKey("principals.id"))
    accepted_at: Mapped[float | None] = mapped_column(Float)


class ArtifactInput(Base):
    __tablename__ = "artifact_inputs"
    __table_args__ = (UniqueConstraint("task_id", "artifact_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("project_artifacts.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("artifact_versions.id"))


def event(db, task, kind, message, **data):
    task.updated = now()
    record = Event(task_id=task.id, kind=kind, message=message[:12000], data=data)
    db.add(record)
    from .notifications import IMPORTANT_KINDS, JIRA_KINDS, queue_notification

    if kind in IMPORTANT_KINDS or kind in JIRA_KINDS:
        db.flush()
        queue_notification(db, task, kind, message, event_id=record.id)


def guard(db):
    """Serialize queue, lease and graph mutations across all control processes."""
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(73119420)"))


@contextmanager
def transaction():
    with Session.begin() as db:
        guard(db)
        yield db


def file_secret(name, factory):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = ROOT / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(factory())
    except FileExistsError:
        pass
    return path.read_text().strip()


def cipher():
    return Fernet(file_secret("encryption.key", lambda: Fernet.generate_key().decode()).encode())


def encrypt(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt(value):
    return cipher().decrypt(value.encode()).decode() if value else ""


def expand_schema(conn):
    """Additive, idempotent expansion; prior releases safely ignore these columns on rollback."""
    expansions = {
        "organizations": {"max_active": "INTEGER NOT NULL DEFAULT 2"},
        "pools": {"policy": "JSON NOT NULL DEFAULT '{}'"},
        "projects": {"health": "JSON NOT NULL DEFAULT '{}'"},
        "connections": {"quota_details": "JSON NOT NULL DEFAULT '{}'"},
        "tokens": {"from_env": "BOOLEAN NOT NULL DEFAULT false"},
        "director_dialogues": {
            "thread_id": "VARCHAR(32) REFERENCES director_threads(id)",
            "origin": "JSON",
            "origin_replied": "BOOLEAN NOT NULL DEFAULT false",
        },
        "notification_channels": {
            "recipient": "VARCHAR(200)",
            "known_events": "JSON NOT NULL DEFAULT '[]'",
            "command_offset": "INTEGER",
        },
        "notification_deliveries": {"reply_to": "JSON", "message_ref": "VARCHAR(64)"},
        "tasks": {
            "priority": "VARCHAR(10) NOT NULL DEFAULT 'normal'",
            "system_check": "BOOLEAN NOT NULL DEFAULT false",
            "incident_key": "VARCHAR(64)",
            "role_config": "JSON NOT NULL DEFAULT '{}'",
            "dependency_commits": "JSON NOT NULL DEFAULT '{}'",
            "plan_depth": "INTEGER NOT NULL DEFAULT 0",
            "time_reserved_seconds": "FLOAT NOT NULL DEFAULT 0",
            "created_by": "VARCHAR(32)",
        },
    }
    for table, columns in expansions.items():
        present = {column["name"] for column in inspect(conn).get_columns(table)}
        for name, declaration in columns.items():
            if name not in present:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}'))


def initialize():
    from . import auth_sessions, backlog, director, notifications  # noqa: F401  Register durable controllers before create_all.

    ROOT.mkdir(parents=True, exist_ok=True)
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(73119421)"))
        Base.metadata.create_all(conn)
        expand_schema(conn)
        if conn.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE notification_deliveries ALTER COLUMN task_id DROP NOT NULL"))
    env_token = os.getenv("DARKFACTORY_ADMIN_TOKEN", "").strip()
    with transaction() as db:
        if db.bind.dialect.name == "postgresql":
            # app and worker both start here; one of them adds the owner token.
            db.execute(text("SELECT pg_advisory_xact_lock(73119421)"))
        if not db.query(Principal).first():
            raw = env_token or file_secret("bootstrap-token", lambda: secrets.token_urlsafe(32))
            org = Organization(name="Моя компания")
            db.add(org)
            db.flush()
            principal = Principal(name="Owner", org_id=org.id, role="owner")
            db.add(principal)
            db.flush()
            db.add(
                Token(
                    digest=hashlib.sha256(raw.encode()).hexdigest(),
                    principal_id=principal.id,
                    role="owner",
                    from_env=bool(env_token),
                )
            )
        elif env_token:
            admin_token(db, env_token)


def admin_token(db, raw):
    """Keep DARKFACTORY_ADMIN_TOKEN valid as an owner token; a changed value replaces the previous one.

    The token goes to the principal that held the previous env token, or else the bootstrap-token owner.
    A token the owners revoked stays revoked: set a new value to restore access.
    """
    digest = hashlib.sha256(raw.encode()).hexdigest()
    if db.scalar(select(Token).where(Token.digest == digest)):
        return
    previous = db.scalars(select(Token).where(Token.from_env)).all()
    holder = previous[0].principal_id if previous else None
    bootstrap = ROOT / "bootstrap-token"
    if not holder and bootstrap.exists():
        legacy = hashlib.sha256(bootstrap.read_text().strip().encode()).hexdigest()
        holder = db.scalar(select(Token.principal_id).where(Token.digest == legacy))
    if not holder:
        raise RuntimeError(
            "DARKFACTORY_ADMIN_TOKEN: нет прежнего токена из env и файла bootstrap-token, некому выдать токен"
        )
    for token in previous:
        token.revoked = True
    db.add(Token(digest=digest, principal_id=holder, role="owner", from_env=True))
