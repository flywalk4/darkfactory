"""Stand tester: an agent explores the project's deployed stand in a browser and turns the defects it
reproduces into ordinary fix tasks.

It runs every `qa_hours` and after each successful deploy. After a deploy it first re-checks the stand bugs whose
fixes went out, then explores. The code of preprod is mounted read-only so the tester can name where a defect
lives. Test accounts come from the project's `test_env` (E2E_*/APP_*/TEST_*), never from the prompt.
"""

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import service
from .backlog import checkout, overview
from .db import ROOT, Link, Project, Task, decrypt, event, now, transaction, uid

log = logging.getLogger(__name__)

INTERVAL_HOURS = 4
MAX_TASKS = 3
# Unfixed stand bugs past this count pause the timer: more reports would only queue behind them.
MAX_OPEN = 8
SCREENSHOTS = 4
CLOSED = {"merged", "cancelled", "duplicate", "checked"}
SEVERITY = {"critical": "critical", "high": "high", "normal": "normal", "low": "low"}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "checked", "verified", "bugs"],
    "properties": {
        "summary": {"type": "string"},
        "checked": {"type": "array", "items": {"type": "string"}},
        "verified": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_id", "fixed", "note"],
                "properties": {
                    "task_id": {"type": "string"},
                    "fixed": {"type": "boolean"},
                    "note": {"type": "string"},
                },
            },
        },
        "bugs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "severity",
                    "url",
                    "steps",
                    "expected",
                    "actual",
                    "area",
                    "screenshots",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": sorted(SEVERITY)},
                    "url": {"type": "string"},
                    "steps": {"type": "string"},
                    "expected": {"type": "string"},
                    "actual": {"type": "string"},
                    "area": {"type": "string"},
                    "screenshots": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


class Verified(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    fixed: bool
    note: str = Field(max_length=4000)


class Bug(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    severity: str = Field(pattern="^(critical|high|normal|low)$")
    url: str = Field(max_length=2000)
    steps: str = Field(max_length=8000)
    expected: str = Field(max_length=4000)
    actual: str = Field(max_length=4000)
    area: str = Field(max_length=4000)
    screenshots: list[str] = Field(max_length=20)


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=8000)
    checked: list[str] = Field(max_length=100)
    verified: list[Verified] = Field(max_length=50)
    bugs: list[Bug] = Field(max_length=20)


PROMPT = """You are the QA engineer of an autonomous software factory. Test the live stand of the project in a
real browser (the project-browser MCP tools) and report defects you reproduced yourself.

Stand: {url}
Reachable hosts: {hosts}. The sandbox blocks every other host: such a refused request is not a defect.
Test accounts and settings are environment variables of your shell: {env}. Read them with `printenv NAME`
and type them into the login form; never write their values into your answer.
The current preprod code is mounted read-only in /workspace: use it to name where a defect probably lives.

Rules:
- Work like a careful manual tester: sign in, open the main screens, try the main user flows end to end,
  forms with valid and invalid input, navigation, reload, back button, narrow (390px) and wide (1440px)
  viewports, console errors and failed network requests (4xx/5xx) behind the screens you use.
- The stand is shared. Create only your own test data, named with the prefix "df-qa-", and do not delete or
  change data you did not create. Never change users, permissions or stand settings.
- Report a defect only when you reproduced it twice and it is clearly wrong: a crash, an error message, a broken
  flow, lost or wrong data, a 5xx, an unhandled console error, unreadable or overlapping layout. Wishes,
  style opinions and missing features are not defects.
- At most {limit} bugs, the most severe first. One bug per root cause. Skip anything already in the known list.
- For each bug take a screenshot with browser_take_screenshot and list its file name in `screenshots`.
  `steps` must let a developer reproduce it from the stand URL; `area` names the screen and the likely code
  files or API endpoint.
- `checked` lists the flows you covered, so the next run can look elsewhere.
Respond with JSON only.
{instructions}{verify}{known}{previous}"""


def config(project):
    return json.loads(decrypt(project.secret) or "{}")


def stand_of(settings):
    """(url, hosts) of a stand from project settings, or None when stand testing is off."""
    url = (settings.get("stand_url") or "").strip()
    if not url:
        return None
    endpoint = urlsplit(url)
    hosts = [f"{endpoint.hostname}:{endpoint.port or (443 if endpoint.scheme == 'https' else 80)}"]
    hosts += [item for item in settings.get("stand_hosts") or [] if item not in hosts]
    return url, hosts


def stand(project):
    return stand_of(config(project))


def browser(hosts):
    """Playwright MCP for the stand: its origins only, through the task's egress proxy; the corporate CA of an
    internal stand is not in the image, so certificate errors are ignored for these origins."""
    origins = []
    for authority in hosts:
        host, _, port = authority.rpartition(":")
        origins.append(
            f"https://{host}"
            if port == "443"
            else f"http://{host}"
            if port == "80"
            else f"https://{authority}"
        )
    return {
        "name": "project-browser",
        "transport": "stdio",
        "command": "playwright-mcp",
        "args": [
            "--headless",
            "--browser",
            "chromium",
            "--no-sandbox",
            "--isolated",
            "--executable-path",
            "/usr/local/bin/darkfactory-chromium",
            "--output-dir",
            "/run/evidence",
            "--output-max-size",
            "52428800",
            "--no-webmcp",
            "--ignore-https-errors",
            "--proxy-server",
            "${HTTPS_PROXY}",
            "--allowed-origins",
            ";".join(origins),
        ],
        "env": None,
        "headers": None,
        "url": None,
        "hosts": hosts,
        "enabled_tools": [
            "browser_navigate",
            "browser_navigate_back",
            "browser_snapshot",
            "browser_click",
            "browser_type",
            "browser_fill_form",
            "browser_select_option",
            "browser_press_key",
            "browser_hover",
            "browser_resize",
            "browser_take_screenshot",
            "browser_console_messages",
            "browser_network_requests",
            "browser_wait_for",
            "browser_tabs",
            "browser_close",
        ],
        "read_only": False,
    }


def profile(project_config):
    """The built-in tester profile the executor uses for role "qa"."""
    _, hosts = stand_of(project_config)
    return {
        "name": "stand-qa",
        "provider": "project",
        "model": None,
        "instructions": "Explore the stand in the browser; report only reproduced defects.",
        "use_when": "",
        "skills": [],
        "mcp_servers": ["project-browser"],
        "can_delegate": False,
        "skill_definitions": [],
        "mcp_definitions": [browser(hosts)],
    }


def interval_hours(project):
    return max(0, min(int(config(project).get("qa_hours", INTERVAL_HOURS)), 168))


def max_tasks(project):
    return max(1, min(int(config(project).get("qa_max_tasks", MAX_TASKS)), 10))


def state(project):
    return dict((project.health or {}).get("qa") or {})


def request(db, project, reason, sha=None):
    """Ask for a run now: after a deploy, or by the owner. The timer and requests share one path."""
    if not stand(project):
        return False
    project.health = {
        **(project.health or {}),
        "qa": {**state(project), "requested": now(), "reason": reason, **({"sha": sha} if sha else {})},
    }
    return True


def open_bugs(db, project):
    return [
        task
        for task in db.scalars(select(Task).where(Task.project_id == project.id, Task.state.notin_(CLOSED)))
        if (task.role_config or {}).get("qa")
    ]


def to_verify(db, project):
    """Stand bugs whose fix reached preprod and has not been re-checked on the stand yet."""
    return [
        task
        for task in db.scalars(select(Task).where(Task.project_id == project.id, Task.state == "merged"))
        if (qa := (task.role_config or {}).get("qa")) and qa.get("verified") is None
    ]


def due(db, project):
    if not stand(project):
        return False
    current = state(project)
    if current.get("requested"):
        return True
    hours = interval_hours(project)
    if not hours or len(open_bugs(db, project)) >= MAX_OPEN:
        return False
    return now() - current.get("at", 0) >= hours * 3600


def fingerprint(title):
    return re.sub(r"[^\w]+", " ", title.lower()).strip()


def prompt(db, project, current):
    url, hosts = stand(project)
    settings = config(project)
    names = sorted(settings.get("test_env") or {})
    instructions = (settings.get("qa_instructions") or "").strip()
    verify = ""
    if current.get("reason") == "deploy":
        pending = to_verify(db, project)
        if pending:
            verify = (
                "\nA new version was just deployed. First re-check these fixed stand bugs and report each in "
                "`verified` (fixed=false only when the original steps still fail):\n"
                + "\n".join(f"- {task.id}: {task.title}\n  {task.request[:1500]}" for task in pending[:10])
                + "\n"
            )
    known = open_bugs(db, project)
    known_text = (
        "\nKnown open stand bugs (do not report again):\n"
        + "\n".join(f"- {task.title}" for task in known[:40])
        + "\n"
        if known
        else ""
    )
    previous = current.get("checked") or []
    previous_text = (
        "\nThe previous run covered: " + "; ".join(previous[:30]) + ". Prefer other flows this time.\n"
        if previous
        else ""
    )
    return (
        PROMPT.format(
            url=url,
            hosts=", ".join(hosts),
            env=", ".join(names) or "none (test only what works without signing in)",
            limit=max_tasks(project),
            instructions=f"\nOwner's instructions for this stand:\n{instructions}\n" if instructions else "",
            verify=verify,
            known=known_text,
            previous=previous_text,
        )
        + f"\nProject: {project.name}\n{overview(project)}"
    )


def latest_run(probe_id):
    runs = sorted(
        (ROOT / "artifacts" / probe_id / "browser").glob("*/execution.json"), key=lambda p: p.stat().st_mtime
    )
    return runs[-1].parent if runs else None


def attach(task_id, run, names):
    """Copy the bug's screenshots from the tester's run into the fix task's attachments."""
    if run is None:
        return []
    target = ROOT / "artifacts" / task_id / "attachments"
    stored = []
    for index, name in enumerate(names[:SCREENSHOTS], start=1):
        source = (run / Path(name).name).resolve()
        if not source.is_relative_to(run.resolve()) or not source.is_file() or source.is_symlink():
            continue
        if source.stat().st_size > 10 * 1024 * 1024:
            continue
        target.mkdir(parents=True, exist_ok=True)
        stored_name = f"{index:02d}-qa-{re.sub(r'[^\w.-]+', '_', source.name)[:100]}"
        shutil.copyfile(source, target / stored_name)
        stored.append(stored_name)
    return stored


def bug_request(url, bug):
    return (
        f"Тестировщик фабрики воспроизвёл дефект на стенде {url}.\n\n"
        f"Где: {bug.url}\n\nШаги:\n{bug.steps}\n\nОжидается:\n{bug.expected}\n\nФактически:\n{bug.actual}\n\n"
        f"Вероятное место в коде: {bug.area}\n\n"
        "Найди причину в коде, исправь её и добавь тест, который ловит этот дефект. Скриншоты стенда приложены. "
        "Критерий приёмки: шаги выше больше не воспроизводят дефект; проверки проекта проходят."
    )


def create(db, project, report, run, probe_id):
    url, _ = stand(project)
    known = {(task.role_config or {})["qa"].get("fingerprint") for task in open_bugs(db, project)}
    created = []
    for bug in report.bugs[: max_tasks(project)]:
        key = fingerprint(bug.title)
        if not key or key in known:
            continue
        known.add(key)
        ident = uid()
        task = Task(
            id=ident,
            org_id=project.org_id,
            project_id=project.id,
            title=bug.title[:300],
            request=bug_request(url, bug),
            branch=f"darkfactory/{ident}",
            priority=SEVERITY[bug.severity],
            role_config={"qa": {"stand": url, "fingerprint": key, "run": probe_id}},
        )
        db.add(task)
        db.flush()
        stored = attach(ident, run, bug.screenshots)
        event(db, task, "created", task.title)
        event(
            db,
            task,
            "qa_reported",
            f"Тестировщик нашёл на стенде: {bug.title[:300]}\n{bug.url}",
            files=stored,
        )
        created.append(ident)
    for item in report.verified:
        task = db.get(Task, item.task_id)
        if not task or task.project_id != project.id or not (task.role_config or {}).get("qa"):
            continue
        qa = {**task.role_config["qa"], "verified": item.fixed, "verified_at": now()}
        task.role_config = {**task.role_config, "qa": qa}
        if item.fixed:
            event(
                db, task, "qa_verified", f"Тестировщик подтвердил исправление на стенде. {item.note[:1000]}"
            )
            continue
        event(db, task, "qa_not_fixed", f"На стенде дефект всё ещё воспроизводится. {item.note[:1000]}")
        ident = uid()
        again = Task(
            id=ident,
            org_id=project.org_id,
            project_id=project.id,
            title=f"Не исправлено на стенде: {task.title}"[:300],
            request=(
                f"Исправление задачи {task.id} дошло до стенда, но дефект воспроизводится.\n"
                f"Замечание тестировщика: {item.note}\n\nИсходное описание:\n{task.request}"
            ),
            branch=f"darkfactory/{ident}",
            priority=task.priority,
            role_config={
                "qa": {
                    "stand": url,
                    "fingerprint": fingerprint(task.title) + " again",
                    "run": probe_id,
                    "again": task.id,
                }
            },
        )
        db.add(again)
        db.flush()
        db.add(Link(task_id=ident, other_id=task.id, kind="related_to"))
        event(db, again, "created", again.title)
        event(db, again, "qa_reported", f"Повторный дефект на стенде: {task.title[:300]}")
        created.append(ident)
    return created


def finish(db, project, **fields):
    project.health = {
        **(project.health or {}),
        "qa": {**{k: v for k, v in state(project).items() if k not in {"requested", "running"}}, **fields},
    }


def run(executor=None):
    """Test one project's stand per call; True when a run was attempted."""
    from .execution import DockerExecutor

    with transaction() as db:
        project = next((row for row in db.scalars(select(Project)) if due(db, row)), None)
        if project is None:
            return False
        current = state(project)
        probe = SimpleNamespace(
            id="qa-" + project.id,
            org_id=project.org_id,
            project_id=project.id,
            phase="director",  # Routes on the writer pool like the backlog grouper.
            plan_depth=0,
            role_config={},
            explicit_api=False,
            last_connection=None,
            api_reserved_usd=0,
            title="Тестирование стенда",
            request="",
            lease_owner=uid(),
            candidate_sha=None,
            target_sha=None,
            verified_sha=None,
            corrections=0,
        )
        connection = service.choose_connection(db, probe, project, {"api_budget": 0}, allow_quota_probe=True)
        if connection is None:
            return False  # Retry on a later tick once a subscription is free.
        text = prompt(db, project, current)
        project.health = {**(project.health or {}), "qa": {**current, "running": now()}}
        project_id = project.id
        db.expunge(project)
        db.expunge(connection)
    started = time.monotonic()
    try:
        checkout(project, probe.id)
        result = (executor or DockerExecutor()).run(
            probe,
            project,
            connection,
            "qa",
            lambda: True,
            int(os.getenv("DARKFACTORY_QA_SECONDS", "1800")),
            prompt_override=text,
            output_schema=SCHEMA,
        )
        if result.get("status") != "ok":
            raise ValueError(f"исполнитель вернул {result.get('status')}")
        report = Report.model_validate_json(result["text"])
    except Exception as exc:
        log.warning("Stand testing failed for %s: %s", project_id, exc)
        with transaction() as db:
            # The request stays answered: a broken stand or login must not loop the tester every tick.
            finish(db, db.get(Project, project_id), at=now(), error=f"{type(exc).__name__}: {exc}"[:300])
        return True
    with transaction() as db:
        row = db.get(Project, project_id)
        created = create(db, row, report, latest_run(probe.id), probe.id)
        finish(
            db,
            row,
            at=now(),
            error=None,
            tasks=len(created),
            summary=report.summary[:2000],
            checked=report.checked[:30],
        )
        log.info("Stand QA %s → %d tasks in %.0fs", project_id, len(created), time.monotonic() - started)
    return True


def enabled():
    return os.getenv("DARKFACTORY_STAND_QA", "1") != "0"
