"""Agent-prepared project environment.

After a repository is connected the factory queues an environment task. Its agent studies the project, works out
what it needs to build and test, and commits `.darkfactory/environment.json`. The checks phase builds the executor
image from that file and runs its setup and check commands in it; a failure goes back to the agent as a correction.
Once merged, every later task of the project runs in that image, and the verified checks become project checks
when the owner has not set their own.
"""

import json
import re

from sqlalchemy import select

from .db import Task, event, uid

SPEC = ".darkfactory/environment.json"
MAX_COMMANDS = 20
PACKAGE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,80}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9. _-]{0,60}$")
SERVICE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,29}$")
IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,199}$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_SERVICES = 5
AUTHORITY = re.compile(r"^[a-z0-9.-]+\.[a-z0-9-]+:\d{1,5}$")
REQUEST = """Подготовка окружения проекта для автономной разработки.
Изучи репозиторий: языки, сборку, менеджеры пакетов, тесты, линтеры, CI-конфигурации
(.github/workflows, .gitlab-ci.yml, Jenkinsfile, Dockerfile, Makefile, README).
Создай или обнови файл .darkfactory/environment.json со структурой:
{
  "toolchains": {"dotnet": "10.0"},
  "apt": ["libicu-dev"],
  "feeds": ["nuget.example.com:443"],
  "setup": ["dotnet restore", "npm ci --prefix src/frontend"],
  "checks": ["dotnet build --no-restore", "dotnet test --no-build",
             {"run": "npm test --prefix src/frontend", "paths": ["src/frontend/*"]}]
}
- toolchains: только нужные из dotnet (каналы через пробел: "8.0 10.0"), go (точная версия
  "1.23.4"), java (мажорная версия), maven, rust, node (мажорная версия, если не 22), ruby,
  php. Python 3.12, Node 22, git, Chromium/Playwright уже есть в базовом образе.
- apt: системные пакеты Debian, без которых сборка не работает.
- feeds: адреса host:port реестров пакетов, которые проект использует, но которых нет
  в его конфигурации (публичные npm/PyPI/NuGet/Maven/Go/crates уже открыты).
- setup: установка зависимостей, идемпотентно, без интерактива и без секретов. Фабрика
  запускает setup сама и пропускает его, пока манифесты не менялись; в checks установку
  не дублируй. Для .NET — один restore по решению (.sln) вместо restore каждого проекта.
- services: до 5 сервисов для интеграционных и e2e-тестов (PostgreSQL, Redis, брокеры):
  [{"name": "db", "image": "postgres:17", "env": {"POSTGRES_PASSWORD": "test"}, "command": []}].
  Они стартуют рядом с песочницей в закрытой сети и доступны по имени (db:5432); только
  тестовые значения, без настоящих секретов. Добавь проверки, которым они нужны.
- env: переменные песочницы для подключения к services, например
  {"DATABASE_URL": "postgres://postgres:test@db:5432/test"}; только тестовые значения.
- checks: быстрые обязательные проверки, которые должны проходить на текущем коде:
  сборка, юнит-тесты, линтер. Не включай тесты, которым нужны внешние стенды, базы или
  сервисы, недоступные в песочнице; если без них никак, опиши это в итоговом сообщении.
  Проверку, которая касается только части репозитория, задай объектом {"run": команда,
  "paths": [шаблоны путей]}: фабрика запустит её, только если задача меняет такие файлы.
  Шаблон должен покрывать всё, от чего проверка зависит, включая общие библиотеки.
  Проверки — быстрые: .NET собирай в Debug с параллельной сборкой (без -m:1), тесты —
  `dotnet test --no-build --no-restore` после сборки; вывод тестов минимальный
  (`--logger "console;verbosity=minimal"`, `pytest -q`).
Команды пиши по имени инструмента (go, mvn, cargo, dotnet): фабрика кладёт тулчейн в PATH и
готовит временные каталоги сама. Без абсолютных путей к бинарникам и без подмены TMPDIR/GOTMPDIR;
если команда в песочнице не находит инструмент, это дефект фабрики — опиши его в итоговом
сообщении, а не обходи в файле.
Проверь команды в песочнице сам. После твоего коммита фабрика соберёт образ по файлу и
выполнит setup и checks; при ошибке ты получишь вывод и исправишь файл.
Меняй только .darkfactory/environment.json. Не изменяй код проекта и не делегируй подзадачи.
"""


def active(task):
    return bool((task.role_config or {}).get("environment"))


def queue(db, project, principal_id):
    existing = next(
        (
            task
            for task in db.scalars(select(Task).where(Task.project_id == project.id))
            if active(task) and task.state not in {"merged", "cancelled", "duplicate", "checked"}
        ),
        None,
    )
    if existing:
        return existing
    ident = uid()
    task = Task(
        id=ident,
        org_id=project.org_id,
        project_id=project.id,
        title="Подготовка окружения проекта",
        request=REQUEST,
        branch=f"darkfactory/{ident}",
        role_config={"environment": True, "environment_author": principal_id},
    )
    db.add(task)
    db.flush()
    event(db, task, "created", task.title)
    return task


def parse(text):
    """Validated spec; raises ValueError with a message the agent can act on."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{SPEC}: неверный JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{SPEC}: ожидается объект")
    from .toolchains import INSTALL

    unknown = set(raw) - {"toolchains", "apt", "feeds", "setup", "checks", "services", "env"}
    if unknown:
        raise ValueError(f"{SPEC}: неизвестные поля {sorted(unknown)}")
    toolchains = raw.get("toolchains") or {}
    if not isinstance(toolchains, dict) or set(toolchains) - set(INSTALL):
        raise ValueError(f"{SPEC}: toolchains поддерживает только {sorted(INSTALL)}")
    if not all(isinstance(v, str) and VERSION.match(v) for v in toolchains.values()):
        raise ValueError(f"{SPEC}: версия toolchain должна быть короткой строкой")
    spec = {"toolchains": dict(sorted(toolchains.items()))}
    for key, pattern in (("apt", PACKAGE), ("feeds", AUTHORITY)):
        values = raw.get(key) or []
        if (
            not isinstance(values, list)
            or len(values) > 30
            or not all(isinstance(v, str) and pattern.match(v) for v in values)
        ):
            raise ValueError(f"{SPEC}: {key} — список строк вида {pattern.pattern}")
        spec[key] = sorted(set(values))
    checks, scopes = [], {}
    for item in raw.get("checks") or []:
        # {"run": command, "paths": [globs]}: the check runs only when a task changes a matching file.
        if isinstance(item, dict) and set(item) <= {"run", "paths"}:
            paths = item.get("paths") or []
            if not isinstance(paths, list) or not all(
                isinstance(v, str) and 0 < len(v) <= 200 for v in paths
            ):
                raise ValueError(f"{SPEC}: checks[].paths — список шаблонов путей")
            item = item.get("run")
            if paths and isinstance(item, str):
                scopes[item] = paths
        checks.append(item)
    for key in ("setup", "checks"):
        values = checks if key == "checks" else raw.get(key) or []
        if (
            not isinstance(values, list)
            or len(values) > MAX_COMMANDS
            or not all(isinstance(v, str) and 0 < len(v) <= 1000 and "\n" not in v for v in values)
        ):
            raise ValueError(f"{SPEC}: {key} — до {MAX_COMMANDS} однострочных команд")
        spec[key] = values
    spec["check_paths"] = scopes
    spec["services"] = services(raw.get("services") or [])
    env = raw.get("env") or {}
    if (
        not isinstance(env, dict)
        or len(env) > 30
        or not all(ENV_KEY.match(k) and isinstance(v, str) and len(v) <= 500 for k, v in env.items())
    ):
        raise ValueError(f"{SPEC}: env — до 30 тестовых переменных песочницы для services")
    spec["env"] = env
    return spec


def services(values):
    """Sidecars (databases, brokers) reachable from the sandbox by name on a private network."""
    message = f"{SPEC}: services — до {MAX_SERVICES} объектов {{name, image, env, command}}"
    if not isinstance(values, list) or len(values) > MAX_SERVICES:
        raise ValueError(message)
    result = []
    for item in values:
        if not isinstance(item, dict) or set(item) - {"name", "image", "env", "command"}:
            raise ValueError(message)
        env, command = item.get("env") or {}, item.get("command") or []
        if (
            not isinstance(item.get("name"), str)
            or not SERVICE_NAME.match(item["name"])
            or not isinstance(item.get("image"), str)
            or not IMAGE.match(item["image"])
            or not isinstance(env, dict)
            or len(env) > 30
            or not all(ENV_KEY.match(k) and isinstance(v, str) and len(v) <= 500 for k, v in env.items())
            or not isinstance(command, list)
            or len(command) > 30
            or not all(isinstance(v, str) and len(v) <= 500 for v in command)
        ):
            raise ValueError(message)
        result.append({"name": item["name"], "image": item["image"], "env": env, "command": command})
    if len({item["name"] for item in result}) != len(result):
        raise ValueError(f"{SPEC}: имена services должны быть уникальны")
    return result


def read(root):
    """The committed spec of a workspace, or None when it is missing or invalid."""
    path = root / SPEC
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return None
        return parse(path.read_text())
    except (OSError, ValueError):
        return None


def validate(repo):
    changed = repo.git("diff", "--name-only", "-z", "HEAD", cwd=repo.work).split("\0")
    changed += repo.git("ls-files", "--others", "--exclude-standard", "-z", cwd=repo.work).split("\0")
    if set(filter(None, changed)) - {SPEC}:
        raise ValueError(f"Подготовка окружения может менять только {SPEC}")
    path = repo.work / SPEC
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Файл {SPEC} не создан")
    parse(path.read_text())


def commands(root):
    spec = read(root) or {}
    return spec.get("setup", []) + spec.get("checks", [])


def adopt(db, task, project, repo):
    """A merged environment task hands its verified checks to the project when the owner set none."""
    if not active(task) or task.state != "merged" or project.checks:
        return
    process = repo.git("show", f"{task.candidate_sha}:{SPEC}", cwd=repo.work, check=False)
    try:
        spec = parse(process.stdout) if process.returncode == 0 else None
    except ValueError:
        spec = None
    if spec and spec["checks"]:
        # Setup stays in the toolchain state: as checks it reinstalled every dependency on every run.
        project.checks = spec["checks"]
        event(db, task, "checks_adopted", "Обязательные проверки проекта: " + "; ".join(project.checks))
