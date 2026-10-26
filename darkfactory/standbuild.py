"""Stand builds: the tester asks the platform's own app builder to make an app from a plain request, then uses
the result like a person and scores it. Problems become fix tasks of the platform's project, and the scores add
up to a week-by-week measure of how well the platform builds apps.

It is written for a low-code platform whose Studio has a builder assistant; the owner's build instructions
(`build_instructions`) say how to reach it on that platform. Requests rotate through `build_tasks`.
"""

import json
import logging
import os
import time
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from . import service
from .backlog import checkout
from .db import Project, Task, decrypt, event, now, transaction, uid
from .standqa import SEVERITY, attach, fingerprint, known_bugs, known_line, latest_run, stand

log = logging.getLogger(__name__)

HISTORY = 60
MAX_TASKS = 3
CATEGORIES = ("dsl", "compiler", "builder", "components", "studio", "runtime", "other")
DEFAULT_TASKS = [
    "Заявки на отпуск: сотрудник подаёт заявку с датами и комментарием, руководитель согласует или отклоняет "
    "с причиной, сотрудник видит статус и историю своих заявок.",
    "Обращения клиентов: форма обращения с темой, описанием и файлом, список обращений с фильтром по статусу, "
    "карточка обращения со сменой статуса и комментариями специалиста.",
    "Запись к врачу: выбор специалиста и свободного слота, подтверждение записи, личный список записей с "
    "отменой, расписание врача на день.",
    "Каталог товаров с заказом: список товаров с поиском и категориями, корзина с количеством, оформление "
    "заказа с контактами, список заказов для менеджера со сменой статуса.",
    "Опрос сотрудников: конструктор опроса из нескольких вопросов разных типов, прохождение опроса, отчёт с "
    "распределением ответов по каждому вопросу.",
]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "built", "minutes", "compile_errors", "test_failures", "scores", "problems"],
    "properties": {
        "summary": {"type": "string"},
        "built": {"type": "boolean"},
        "minutes": {"type": "number"},
        "compile_errors": {"type": "integer"},
        "test_failures": {"type": "integer"},
        "scores": {
            "type": "object",
            "additionalProperties": False,
            "required": ["completeness", "correctness", "usability", "visual"],
            "properties": {
                key: {"type": "integer", "minimum": 0, "maximum": 5}
                for key in ("completeness", "correctness", "usability", "visual")
            },
        },
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "category",
                    "severity",
                    "steps",
                    "expected",
                    "actual",
                    "area",
                    "screenshots",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "severity": {"type": "string", "enum": sorted(SEVERITY)},
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


class Scores(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completeness: int = Field(ge=0, le=5)
    correctness: int = Field(ge=0, le=5)
    usability: int = Field(ge=0, le=5)
    visual: int = Field(ge=0, le=5)


class Problem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    category: str = Field(pattern="^(" + "|".join(CATEGORIES) + ")$")
    severity: str = Field(pattern="^(critical|high|normal|low)$")
    steps: str = Field(max_length=8000)
    expected: str = Field(max_length=4000)
    actual: str = Field(max_length=4000)
    area: str = Field(max_length=4000)
    screenshots: list[str] = Field(max_length=20)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=8000)
    built: bool
    minutes: float = Field(ge=0)
    compile_errors: int = Field(ge=0)
    test_failures: int = Field(ge=0)
    scores: Scores
    problems: list[Problem] = Field(max_length=20)


PROMPT = """You are a demanding new user of a low-code platform and the factory's QA engineer at once. Your job:
make the platform build an app from a plain request, then judge what came out, exactly as a customer would.

Stand: {url}
Reachable hosts: {hosts}. The sandbox blocks every other host: such a refused request is not a defect.
Sign-in and settings are environment variables of your shell: {env}. Read them with `printenv NAME`; never write
their values into your answer. The platform's code is mounted read-only in /workspace: use it to name where a
problem probably lives.
{instructions}
The request (give it to the platform's builder assistant word for word, as a customer would):
---
{task}
---

Steps:
1. Sign in to your own tenant. Start a fresh workspace or app for this request; name what you create with the
   prefix "df-qa-".
2. Give the request to the builder assistant. Answer its questions briefly as a customer; approve its plan.
   Wait until it reports the result (up to 20 minutes); note compile errors and failing tests it shows.
3. Publish or preview the app the way the platform offers and open it.
4. Use the app end to end as its users would: every role, every main flow, valid and invalid input, empty
   states, 390px and 1440px viewports, console errors and failed requests. Take screenshots of what you judge.
5. Score 0-5: completeness (everything the request asked for), correctness (flows give right results),
   usability (a person manages without help), visual (clean, readable, no broken layout).
6. Report at most {limit} problems, the ones that cost the customer most, each with the category of its root
   cause: dsl (the language cannot express it), compiler (valid input rejected or wrong output), builder (the
   assistant misunderstood, gave up or produced something broken), components (a page block or widget renders
   or behaves wrong), studio (the editor itself), runtime (the running app or its API), other. One problem per
   root cause; skip anything in the known list. Steps must reproduce from the stand URL.
If the build never finishes, built=false and report why as a problem.
Respond with JSON only.
{known}{previous}"""


def config(project):
    return json.loads(decrypt(project.secret) or "{}")


def tasks(project):
    return [item for item in config(project).get("build_tasks") or DEFAULT_TASKS if item.strip()]


def max_tasks(project):
    return max(1, min(int(config(project).get("build_max_tasks", MAX_TASKS)), 10))


def state(project):
    return dict((project.health or {}).get("build") or {})


def request(db, project, reason):
    if not stand(project):
        return False
    project.health = {
        **(project.health or {}),
        "build": {**state(project), "requested": now(), "reason": reason},
    }
    return True


def due(project):
    return bool(stand(project) and state(project).get("requested"))


def prompt(db, project, current, task_text):
    url, hosts = stand(project)
    settings = config(project)
    known = known_bugs(db, project)
    history = current.get("history") or []
    return PROMPT.format(
        url=url,
        hosts=", ".join(hosts),
        env=", ".join(sorted(settings.get("test_env") or {})) or "none",
        instructions=(
            f"\nOwner's instructions for building on this platform:\n{settings['build_instructions'].strip()}\n"
            if (settings.get("build_instructions") or "").strip()
            else ""
        ),
        task=task_text,
        limit=max_tasks(project),
        known=(
            "\nKnown problems (do not report again):\n"
            + "\n".join(known_line(t) for t in known[:40])
            + "\n"
            if known
            else ""
        ),
        previous=(
            "\nLast runs of the same request: "
            + "; ".join(
                f"{time.strftime('%Y-%m-%d', time.gmtime(item['at']))} built={item['built']} "
                f"scores={item['scores']}"
                for item in history
                if item.get("task") == task_text
            )[-600:]
            + "\n"
            if any(item.get("task") == task_text for item in history)
            else ""
        ),
    )


def problem_request(url, task_text, problem):
    return (
        f"Тестировщик фабрики попросил платформу на стенде {url} собрать приложение по заказу:\n«{task_text}»\n\n"
        f"Категория причины: {problem.category}.\n\nШаги:\n{problem.steps}\n\nОжидается:\n{problem.expected}\n\n"
        f"Фактически:\n{problem.actual}\n\nВероятное место: {problem.area}\n\n"
        "Исправь причину в платформе (язык, компилятор, ассистент сборки, компоненты, Studio или среда "
        "выполнения — по категории), а не в одном собранном приложении. Добавь тест или пример, который ловит "
        "проблему. Скриншоты приложены. Критерий приёмки: тот же заказ больше не приводит к проблеме; проверки "
        "проекта проходят."
    )


def create(db, project, result, task_text, run, probe_id):
    url, _ = stand(project)
    known = {(item.role_config or {})["qa"].get("fingerprint") for item in known_bugs(db, project)}
    created = []
    for problem in result.problems[: max_tasks(project)]:
        key = fingerprint(problem.title)
        if not key or key in known:
            continue
        known.add(key)
        ident = uid()
        task = Task(
            id=ident,
            org_id=project.org_id,
            project_id=project.id,
            title=problem.title[:300],
            request=problem_request(url, task_text, problem),
            branch=f"darkfactory/{ident}",
            priority=SEVERITY[problem.severity],
            role_config={
                "qa": {"stand": url, "fingerprint": key, "run": probe_id, "build": problem.category},
            },
        )
        db.add(task)
        db.flush()
        stored = attach(ident, run, problem.screenshots)
        event(db, task, "created", task.title)
        event(
            db,
            task,
            "qa_reported",
            f"Сборка на стенде ({problem.category}): {problem.title[:300]}",
            files=stored,
        )
        created.append(ident)
    return created


def finish(db, project, **fields):
    current = state(project)
    dropped = {"running"} | (
        {"requested"} if current.get("requested", 0) <= current.get("running", 0) else set()
    )
    project.health = {
        **(project.health or {}),
        "build": {**{k: v for k, v in current.items() if k not in dropped}, **fields},
    }


def summary(history, last=10):
    """Share of built apps and mean scores over the latest runs, for the schedules page."""
    recent = history[-last:]
    if not recent:
        return None
    scores = [item["scores"] for item in recent if item.get("scores")]
    return {
        "runs": len(recent),
        "built": sum(1 for item in recent if item.get("built")) / len(recent),
        "scores": {
            key: round(sum(item[key] for item in scores) / len(scores), 1)
            for key in ("completeness", "correctness", "usability", "visual")
        }
        if scores
        else None,
    }


def run(executor=None):
    """One stand build per call; True when a run was attempted."""
    from .execution import DockerExecutor

    with transaction() as db:
        project = next((row for row in db.scalars(select(Project)) if due(row)), None)
        if project is None:
            return False
        current = state(project)
        catalog = tasks(project)
        index = int(current.get("next", 0)) % len(catalog)
        task_text = catalog[index]
        probe = SimpleNamespace(
            id="build-" + project.id,
            org_id=project.org_id,
            project_id=project.id,
            phase="director",
            plan_depth=0,
            role_config={},
            explicit_api=False,
            last_connection=None,
            api_reserved_usd=0,
            title="Сборка на стенде",
            request="",
            lease_owner=uid(),
            candidate_sha=None,
            target_sha=None,
            verified_sha=None,
            corrections=0,
        )
        connection = service.choose_connection(db, probe, project, {"api_budget": 0}, allow_quota_probe=True)
        if connection is None:
            return False
        text = prompt(db, project, current, task_text)
        project.health = {**(project.health or {}), "build": {**current, "running": now(), "task": task_text}}
        db.flush()
        project_id = project.id
        db.expunge(project)
        db.expunge(connection)
    started = time.monotonic()
    try:
        checkout(project, probe.id)
        output = (executor or DockerExecutor()).run(
            probe,
            project,
            connection,
            "qa",
            lambda: True,
            int(os.getenv("DARKFACTORY_BUILD_SECONDS", "3000")),
            prompt_override=text,
            output_schema=SCHEMA,
        )
        if output.get("status") != "ok":
            raise ValueError(f"исполнитель вернул {output.get('status')}")
        result = Result.model_validate_json(output["text"])
    except Exception as exc:
        log.warning("Stand build failed for %s: %s", project_id, exc)
        with transaction() as db:
            finish(db, db.get(Project, project_id), at=now(), error=f"{type(exc).__name__}: {exc}"[:300])
        return True
    with transaction() as db:
        row = db.get(Project, project_id)
        created = create(db, row, result, task_text, latest_run(probe.id), probe.id)
        history = (state(row).get("history") or []) + [
            {
                "at": now(),
                "task": task_text,
                "built": result.built,
                "minutes": result.minutes,
                "compile_errors": result.compile_errors,
                "test_failures": result.test_failures,
                "scores": result.scores.model_dump(),
                "problems": len(result.problems),
                "categories": sorted({problem.category for problem in result.problems}),
            }
        ]
        finish(
            db,
            row,
            at=now(),
            error=None,
            next=index + 1,
            tasks=len(created),
            task_ids=created,
            summary=result.summary[:2000],
            history=history[-HISTORY:],
        )
        log.info("Stand build %s → %d tasks in %.0fs", project_id, len(created), time.monotonic() - started)
    return True
