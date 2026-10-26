import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from . import (
    disk,
    service,
)
from .db import (
    Organization,
    Session,
    initialize,
)
from .routes import artifacts, chat, connections, members, notifications, pools, projects, tasks
from .routes import backlog as backlog_routes
from .routes import schedules as schedule_routes
from .routes.connections import check_credentials, unique_credentials  # noqa: F401  (auth_sessions)
from .web import access, agent_activity, public  # noqa: F401


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield


app = FastAPI(title="Darkfactory", version="0.1.0", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"


@app.exception_handler(IntegrityError)
async def conflict(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=409, content={"detail": "Запись или подключение уже существует"})


@app.get("/health")
def health():
    with Session() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok"}


# Scripts and styles load only from /static. Generated markup still sets inline style attributes
# (progress widths, nesting depth), and screenshots and chat images are shown from blob: URLs.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
    "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
)


@app.get("/")
def index():
    return FileResponse(
        STATIC / "index.html",
        headers={"Content-Security-Policy": CONTENT_SECURITY_POLICY, "X-Frame-Options": "DENY"},
    )


@app.get("/api/me")
def me(a=Depends(access)):
    with Session() as db:
        organization = db.get(Organization, a.org_id)
        return {
            **public(a.principal),
            "token_role": a.token.role,
            "token_projects": a.token.project_ids,
            "organization": public(organization),
            "personal_mode": os.getenv("DARKFACTORY_PERSONAL_MODE") == "1",
            "disk": disk.public(),
            "held": service.held(),
        }


@app.get("/api/executors")
def executor_capabilities(a=Depends(access)):
    return {
        provider: {
            "transport": transport,
            "structured_output": True,
            "resume": "artifact_handoff",
            "native_session_resume": False,
            "cancel": "stop_container",
            "quota": quota,
            **(
                {
                    "auth_methods": ["siwc", "codex_local"]
                    if os.getenv("DARKFACTORY_PERSONAL_MODE") == "1"
                    else ["siwc"]
                }
                if provider == "codex"
                else {}
            ),
        }
        for provider, transport, quota in [
            ("codex", "app_server_stdio_jsonrpc", "provider_events_or_unknown"),
            ("claude", "headless_stream_json", "provider_error_or_unknown"),
            ("api", "https_chat_completions", "provider_error_or_unknown"),
        ]
    }


app.include_router(projects.router)
app.include_router(backlog_routes.router)
app.include_router(schedule_routes.router)
app.include_router(chat.router)
app.include_router(tasks.router)
app.include_router(artifacts.router)
app.include_router(pools.router)
app.include_router(notifications.router)
app.include_router(connections.router)
app.include_router(members.router)

app.mount("/static", StaticFiles(directory=STATIC), name="static")
