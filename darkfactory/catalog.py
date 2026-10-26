"""Ready-to-use agent profiles, skills and bounded project-only MCP servers."""

import argparse
import json
import os
import select
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

WORKSPACE = Path("/workspace")
MAX_BYTES = 65536
GIT_TIMEOUT = 5
GIT_EXECUTABLE = "/usr/bin/git"

# Agents see only each skill's description and read the file when it fits: the description says when.
SKILLS = [
    (
        "browser-testing",
        "Проверка интерфейса в браузере",
        "Читай, когда задача меняет видимый интерфейс и нужно проверить его в браузере.",
        """
Браузер — только для видимого изменения интерфейса, один прогон после того, как код готов.
Подними приложение на 127.0.0.1 внутри контейнера. С браузерным MCP: snapshot, затем действия по видимым
элементам. Проверь основной сценарий, ошибочный ввод и пустое состояние; размеры 1440×1000 и 390×844, без
горизонтальной прокрутки, ошибки консоли и запросов.
Скриншоты, трейсы и отчёты — в $DARKFACTORY_EVIDENCE_DIR, не в репозиторий. Воспроизводимый
Playwright-тест добавляй, если в проекте уже есть e2e-тесты. Конфигурация: executablePath
'/usr/local/bin/darkfactory-chromium', args ['--no-sandbox', '--disable-dev-shm-usage'],
outputDir process.env.DARKFACTORY_EVIDENCE_DIR, сервер через webServer.
В итоге назови проверенные сценарии и то, что проверить не удалось. Production не используй.
""",
    ),
    (
        "task-planning",
        "Планирование задачи",
        "Читай, когда нужно составить план или разбить задачу на подзадачи.",
        """
Опиши наблюдаемый результат и проверяемые критерии приёмки — они должны проверяться в песочнице
(код, тесты, локальная сборка). Найди нужные места точечным поиском, не обходи весь репозиторий.
Подзадачи — только для независимо поставляемых частей, которые каждая займут часы; баг с исправлением и
регрессионным тестом — одна задача. Не создавай подзадачи «проверить», «отревьюить», «написать отчёт»:
проверки и ревью фабрика делает сама. Вопрос владельцу — только если без ответа нельзя сделать верно.
Перед ответом сверь план с запросом: каждый критерий покрыт, лишнего нет.
""",
    ),
    (
        "code-navigation",
        "Разбор проекта",
        "Читай, когда надо найти, где в коде меняется поведение, и не тратить на это много шагов.",
        """
Начни с подсказки «Likely places to change», если она есть в задаче, и проверь её.
Ищи точные символы (rg -n 'Name' путь), читай короткие диапазоны вокруг найденного, а не файлы целиком.
Проследи путь данных от входа до изменения состояния и найди ближайший существующий пример — повторяй
его, а не строй новый механизм. Отделяй факт, проверенный кодом или запуском, от гипотезы.
Итог — список файлов и строк, которые нужно менять, и тесты, которые это покрывают.
""",
    ),
    (
        "safe-change",
        "Аккуратная разработка",
        "Читай перед первым изменением кода: как менять минимально и не сломать соседнее.",
        """
Меняй ближайший ответственный слой и только нужные файлы; используй существующие функции и соглашения.
Сохраняй публичные контракты и чужую работу в рабочей копии. Новые зависимости — только если без них
нельзя. Ошибки и отмена не должны оставлять процессов, блокировок и наполовину выполненных действий.
Не выполняй commit, push, merge и deploy: Git ведёт фабрика.
После изменения запусти тесты изменённых файлов и их прямых пользователей, посмотри diff и закончи.
""",
    ),
    (
        "regression-tests",
        "Регрессионные тесты",
        "Читай, когда добавляешь или чинишь тест на исправленное поведение.",
        """
Тест должен падать на исходной ошибке и проходить после исправления. Проверяй внешний результат и
инварианты, а не повторяй реализацию. Покрой основной сценарий, значимую ошибку и соседнее поведение,
которое изменение могло задеть. Git, файлы и процессы — настоящие локальные; платные внешние API —
тестовые двойники, явно помеченные. Запусти только затронутые тесты (фильтр по файлу или имени), полный
набор прогонит фабрика. Не расширяй тесты после зелёного результата без причины.
""",
    ),
    (
        "code-review",
        "Независимое ревью",
        "Читай перед ревью: что блокирует слияние, а что уходит в бэклог.",
        """
Обязательные проверки уже прошли — не запускай их снова. Ищи то, чего тесты не видят.
Начинай с кода, а не с рассказа о нём: handoff, отчёты в docs/ и материалы прогона — утверждения
разработчика, их нужно сверить с кодом.
По каждому изменённому файлу кода прочитай diff и код вокруг: вызывающих изменённую функцию, все пути,
по которым идёт изменённый код (рабочий рантайм, а не только новая функция), и тесты, которые должны это
покрывать. В ответе в `files` для каждого такого файла — что прочитано (функции, строки, вызовы) и почему
это верно или что не так. Файл без такого доказательства считается непроверенным.
Каждый критерий приёмки сверь с кодом, который его выполняет. Критерий, закрытый только заметкой,
документом или проверкой, которая ничего не проверяет, не выполнен.
Каждое замечание: файл и строка, условие срабатывания, неверное поведение, последствие.
severity: blocker — функция не работает, затронутый код сломан для прежних вызывающих, под угрозой данные
или безопасность; major — не выполнен критерий или результат; minor — стиль, рефакторинг, лишние тесты,
документация, крайние случаи вне запроса. confidence ниже 80 и minor не блокируют и уходят в бэклог.
env=true — пробел песочницы (нет инструмента, сервиса, сети, стенда), а не дефект кода.
Повторное ревью одобряет слияние: проверь, что прошлые замечания закрыты, и снова просмотри всё
изменение, а не только правки. Код не меняй.
""",
    ),
    (
        "integration-conflicts",
        "Разрешение конфликтов",
        "Читай, когда в рабочей копии есть конфликт слияния.",
        """
Прочитай все конфликтующие файлы, обе стороны и задачи, которые их внесли. Сохрани нужное поведение обеих
сторон, удали маркеры, проверь связанные вызовы и запусти тесты затронутого сценария.
Не выполняй Git-команды и не меняй .git: фабрика сохранит index и MERGE_HEAD и создаст merge commit.
Если намерения сторон несовместимы и выбор не следует из задач, задай владельцу один точный вопрос.
""",
    ),
]

# Defaults shipped before; a project's copy that still equals one of these is upgraded in place
# (agents.upgrade_defaults). Copies the owner edited are never touched.
LEGACY_SKILLS = {
    "browser-testing": {"43514923c748bd2b"},
    "task-planning": {"63da3d96d9116eb1"},
    "code-navigation": {"a568f106097b362d"},
    "safe-change": {"dc741a9685be9ff8"},
    "regression-tests": {"d43a49054f3b8a55"},
    "code-review": {"26b8034a0d5d57cd", "478a5b9e606cff17"},
    "integration-conflicts": {"366a32c30cbda49b"},
}
LEGACY_PROFILES = {
    "manager": {"5cad083683a2ffd9"},
    "analyst": {"fcef03346b5c7c63"},
    "developer": {"00e15ecab919f8a1"},
    "reviewer": {"3ebb74c23cf35ca9", "b4eef2904d5f773a"},
}


# When the planner should give a subtask to the profile: it chooses by this line, not by the instructions.
USE_WHEN = {
    "manager": "Большая работа, которую надо разбить на части и собрать результат.",
    "analyst": "Разобраться в существующем поведении и записать выводы или критерии, почти без правок кода.",
    "developer": "Любая правка кода и тестов: ошибка, новая возможность, рефакторинг.",
    "reviewer": "Независимая проверка изменения перед слиянием.",
    "director": "Остановки задач и поручения владельца в чате; подзадачам не назначается.",
}


# Read tools of mcp-atlassian (Dockerfile pins the version). download_attachments is left out: it writes files.
JIRA_READ_TOOLS = (
    "get_user_profile",
    "get_issue",
    "search",
    "search_fields",
    "get_field_options",
    "get_project_issues",
    "get_transitions",
    "get_worklog",
    "get_issue_watchers",
    "get_agile_boards",
    "get_board_issues",
    "get_sprints_from_board",
    "get_sprint_issues",
    "get_link_types",
    "batch_get_changelogs",
    "get_project_issue_types",
    "get_project_versions",
    "get_project_components",
    "get_all_projects",
    "search_projects",
    "get_issue_dates",
    "get_project_epic_hierarchy",
    "get_cross_project_dependencies",
)


def catalog():
    """Return independent, AgentSettings-compatible definitions for the UI catalog."""
    skills = []
    for name, title, description, content in SKILLS:
        skills.append(
            {
                "id": name,
                "title": title,
                "description": description,
                "definition": {
                    "name": name,
                    "description": description,
                    "content": f"---\nname: {name}\ndescription: {description}\n---\n\n{content.strip()}\n",
                },
            }
        )
    profiles = [
        (
            "manager",
            "Менеджер",
            "Планирует работу и распределяет разрешённые подзадачи.",
            "Согласуй результат и зависимости. Делегируй только независимые подзадачи в пределах лимитов. "
            "Следи за проверками и обязательными вопросами. Не меняй код и не выполняй Git-запись.",
            ["task-planning", "code-navigation"],
            True,
        ),
        (
            "analyst",
            "Аналитик",
            "Разбирает проект и уточняет критерии приёмки.",
            "Изучи существующее поведение, входные данные и ограничения. Укажи проверяемые критерии "
            "и факты из кода. Не меняй рабочий код без необходимости и не делегируй; отчёты и документы, "
            "которые требует задача, записывай в файлы.",
            ["task-planning", "code-navigation"],
            False,
        ),
        (
            "developer",
            "Разработчик",
            "Вносит изменения и проверяет нужное поведение.",
            "Внеси минимальное изменение и тесты, которые его покрывают; запусти только затронутые тесты. "
            "Сохраняй чужую работу и совместимость. Не выполняй commit, push, merge или deploy.",
            ["safe-change", "regression-tests", "integration-conflicts"],
            False,
        ),
        (
            "director",
            "Директор",
            "Решает остановки задач вместо владельца и выполняет его поручения в чате.",
            "Действуй как опытный руководитель разработки: реши сам всё, что инженер решил бы без владельца, "
            "опираясь на код и историю задачи. Владельца зови только для решений про доступы, деньги, "
            "продуктовый выбор и разрушительные действия. Пиши коротко и по-русски.",
            ["code-navigation"],
            False,
        ),
        (
            "reviewer",
            "Ревьюер",
            "Независимо проверяет diff и результаты тестов.",
            "Проверь задачу и diff в независимой сессии; результаты проверок уже есть в задаче. Сверяй код, "
            "а не отчёт разработчика. Верни чеклист, замечания и доказательства по каждому изменённому файлу "
            "кода. Не меняй файлы и не делегируй.",
            ["code-review", "code-navigation"],
            False,
        ),
    ]
    return {
        "profiles": [
            {
                "id": name,
                "title": title,
                "description": description,
                "definition": {
                    "name": name,
                    "provider": "codex",
                    "instructions": instructions,
                    "use_when": USE_WHEN.get(name, ""),
                    "skills": names,
                    "mcp_servers": ["project-files", "project-git"],
                    "can_delegate": delegate,
                },
            }
            for name, title, description, instructions, names, delegate in profiles
        ],
        "skills": skills,
        "mcp_servers": [
            {
                "id": name,
                "title": title,
                "description": description,
                "definition": {
                    "name": name,
                    "transport": "stdio",
                    "command": "python",
                    "args": ["-m", "darkfactory.catalog", "--serve", name],
                    "env": {"PYTHONPATH": "/app", "PYTHONSAFEPATH": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                    "enabled_tools": tools,
                    "read_only": True,
                },
            }
            for name, title, description, tools in (
                (
                    "project-files",
                    "Файлы проекта",
                    "Чтение, список и поиск внутри /workspace.",
                    ["files.list", "files.read", "files.search"],
                ),
                (
                    "project-git",
                    "Git проекта",
                    "Только чтение status, diff и истории проекта.",
                    ["git.status", "git.diff", "git.log"],
                ),
            )
        ]
        + [
            {
                "id": "project-browser",
                "title": "Браузер проекта",
                "description": "Chromium и Playwright: действия, снимки и диагностика локального приложения. Только для роли разработчика.",
                "definition": {
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
                        "--save-session",
                        "--no-webmcp",
                        "--allowed-origins",
                        "http://127.0.0.1:*;http://localhost:*",
                    ],
                    "enabled_tools": [
                        "browser_navigate",
                        "browser_snapshot",
                        "browser_click",
                        "browser_type",
                        "browser_fill_form",
                        "browser_select_option",
                        "browser_press_key",
                        "browser_resize",
                        "browser_take_screenshot",
                        "browser_console_messages",
                        "browser_network_requests",
                        "browser_wait_for",
                        "browser_close",
                    ],
                    "read_only": False,
                },
            },
            {
                "id": "jira",
                "title": "Jira",
                "description": (
                    "Чтение задач, поиска JQL, досок и спринтов Jira Server/Data Center или Cloud. После добавления "
                    "укажи в настройках адрес Jira (например jira.example.com:443) и секретное окружение "
                    "JIRA_URL и JIRA_PERSONAL_TOKEN."
                ),
                "definition": {
                    "name": "jira",
                    "transport": "stdio",
                    "command": "mcp-atlassian",
                    # --read-only disables write tools in the server itself, not only in the gateway's list;
                    # without --toolsets newer versions expose only a core subset.
                    "args": ["--transport", "stdio", "--read-only", "--toolsets", "all"],
                    "hosts": [],
                    "enabled_tools": ["jira_" + name for name in JIRA_READ_TOOLS],
                    "read_only": True,
                },
            },
        ],
    }


def _parts(path):
    if not isinstance(path, str) or len(path) > 500 or "\0" in path:
        raise ValueError("Некорректный путь проекта")
    value = Path(path)
    if value.is_absolute() or any(part in {"..", ".git"} for part in value.parts):
        raise ValueError("Доступ разрешён только к файлам проекта вне .git")
    return value.parts


@contextmanager
def _directory(root, parts):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_bytes(root, path, maximum):
    parts = _parts(path)
    if not parts:
        raise ValueError("Укажи файл проекта")
    with _directory(root, parts[:-1]) as parent:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Разрешено чтение только обычных файлов")
            value = os.read(descriptor, maximum + 1)
            return value[:maximum], len(value) > maximum
        finally:
            os.close(descriptor)


def _limit(arguments, name, default, maximum):
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name}: допустимый диапазон 1..{maximum}")
    return value


def _files(root, tool, arguments):
    path = arguments.get("path", ".")
    parts = _parts(path)
    if tool == "files.read":
        maximum = _limit(arguments, "max_bytes", 16000, MAX_BYTES)
        value, truncated = _read_bytes(root, path, maximum)
        return {"path": path, "text": value.decode("utf-8", errors="replace"), "truncated": truncated}
    if tool == "files.list":
        limit, entries = _limit(arguments, "limit", 100, 200), []
        truncated, deadline = False, time.monotonic() + 2
        with _directory(root, parts) as directory, os.scandir(directory) as iterator:
            for visited, entry in enumerate(iterator, 1):
                if visited > 2000 or time.monotonic() >= deadline:
                    truncated = True
                    break
                if entry.name == ".git" or entry.is_symlink():
                    continue
                entries.append(
                    {
                        "name": entry.name,
                        "type": "directory" if entry.is_dir(follow_symlinks=False) else "file",
                    }
                )
                if len(entries) > limit:
                    truncated = True
                    break
        return {"entries": sorted(entries[:limit], key=lambda item: item["name"]), "truncated": truncated}
    query = arguments.get("query")
    if not isinstance(query, str) or not 1 <= len(query) <= 200:
        raise ValueError("query: укажи текст длиной 1..200 символов")
    limit = _limit(arguments, "limit", 20, 100)
    pending, matches = [parts], []
    visited = scanned = total = 0
    truncated = False
    deadline = time.monotonic() + 2
    while pending and visited < 2000 and scanned < 200 and total < 2 * 1024 * 1024:
        current = pending.pop()
        with _directory(root, current) as directory, os.scandir(directory) as iterator:
            for entry in iterator:
                visited += 1
                if visited > 2000 or time.monotonic() >= deadline:
                    return {"matches": matches, "truncated": True}
                if entry.name == ".git" or entry.is_symlink():
                    continue
                relative = (*current, entry.name)
                if entry.is_dir(follow_symlinks=False):
                    if len(relative) < 20:
                        pending.append(relative)
                    else:
                        truncated = True
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                scanned += 1
                if scanned > 200 or total >= 2 * 1024 * 1024:
                    return {"matches": matches, "truncated": True}
                filename = str(Path(*relative))
                value, partial = _read_bytes(root, filename, min(MAX_BYTES, 2 * 1024 * 1024 - total))
                truncated = truncated or partial
                total += len(value)
                for number, line in enumerate(value.decode("utf-8", errors="replace").splitlines(), 1):
                    if query in line:
                        matches.append({"path": filename, "line": number, "text": line[:512]})
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": bool(pending) or truncated}


def _git(root, tool, arguments):
    commands = {
        "git.status": ["status", "--porcelain=v1", "--untracked-files=normal"],
        "git.diff": ["diff", "--no-ext-diff", "--no-textconv", "--no-renames"],
        "git.log": [
            "log",
            "--no-show-signature",
            "--format=%h %s",
            "--max-count=" + str(_limit(arguments, "limit", 20, 50)),
        ],
    }
    args = commands[tool]
    if tool == "git.diff":
        if type(arguments.get("staged", False)) is not bool:
            raise ValueError("staged: требуется true или false")
        if arguments.get("staged"):
            args.append("--cached")
        args.append("--")
        if "path" in arguments:
            _parts(arguments["path"])
            args.append(arguments["path"])
    # Factory workspaces contain a real .git directory, never an external worktree pointer.
    with _directory(root, (".git",)):
        pass
    common = [
        "-c",
        f"safe.directory={root}",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "diff.external=",
        "-c",
        "core.attributesFile=/dev/null",
        "--literal-pathspecs",
        "--git-dir=" + str(root / ".git"),
        "--work-tree=" + str(root),
    ]
    configured = _run_git(
        root,
        [
            *common,
            "config",
            "--null",
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(clean|smudge|process|required)$",
        ],
        allow_no_match=True,
    )
    if configured["truncated"]:
        raise ValueError("Слишком много настроек Git filters")
    drivers = {key.rsplit(".", 1)[0] for key in configured["text"].split("\0") if key}
    if len(drivers) > 100:
        raise ValueError("Слишком много настроек Git filters")
    overrides = []
    for driver in sorted(drivers):
        for setting in ("clean=", "smudge=", "process=", "required=false"):
            overrides += ["-c", driver + "." + setting]
    return _run_git(root, [*common, *overrides, *args])


def _run_git(root, args, *, allow_no_match=False):
    env = {
        "PATH": os.defpath,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }
    process = subprocess.Popen(
        [GIT_EXECUTABLE, *args],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    value, deadline, truncated = bytearray(), time.monotonic() + GIT_TIMEOUT, False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Превышено время чтения Git")
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                raise ValueError("Превышено время чтения Git")
            chunk = os.read(process.stdout.fileno(), min(MAX_BYTES + 1 - len(value), 65536))
            if not chunk:
                break
            value.extend(chunk)
            if len(value) > MAX_BYTES:
                truncated = True
                break
        if not truncated:
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code and not (allow_no_match and code == 1):
                raise ValueError("Не удалось прочитать Git проекта")
        return {"text": bytes(value[:MAX_BYTES]).decode("utf-8", errors="replace"), "truncated": truncated}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        process.stdout.close()


def _tools(server_id):
    properties = {
        "files.list": {
            "path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "files.read": {
            "path": {"type": "string"},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_BYTES},
        },
        "files.search": {
            "path": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "git.status": {},
        "git.diff": {"path": {"type": "string"}, "staged": {"type": "boolean"}},
        "git.log": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
    }
    descriptions = {
        "files.list": "Список файлов проекта; пути относительно /workspace, без .git и symlink.",
        "files.read": "Прочитать обычный файл внутри /workspace, не более 64 KiB.",
        "files.search": "Поиск буквального текста в ограниченном числе файлов проекта.",
        "git.status": "Прочитать состояние Git без изменения index или файлов.",
        "git.diff": "Прочитать рабочий или staged diff без внешних программ.",
        "git.log": "Прочитать до 50 записей истории без выполнения внешних программ.",
    }
    definition = next(item["definition"] for item in catalog()["mcp_servers"] if item["id"] == server_id)
    return [
        {
            "name": name,
            "description": descriptions[name],
            "inputSchema": {
                "type": "object",
                "properties": properties[name],
                "additionalProperties": False,
                "required": ["path"] if name == "files.read" else ["query"] if name == "files.search" else [],
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }
        for name in definition["enabled_tools"]
    ]


def serve(server_id, workspace=WORKSPACE, stdin=None, stdout=None):
    """Serve JSON-lines MCP. CLI always uses the fixed /workspace boundary."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    tools = {tool["name"]: tool for tool in _tools(server_id)}
    while line := stdin.readline(MAX_BYTES + 1):
        request_id = None
        try:
            if len(line.encode()) > MAX_BYTES:
                raise ValueError("MCP request is too large")
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("Invalid MCP request")
            if "id" not in message:
                continue
            request_id, method = message["id"], message.get("method")
            if method == "initialize":
                requested = (message.get("params") or {}).get("protocolVersion")
                protocol = (
                    requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2024-11-05"
                )
                result = {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "darkfactory-" + server_id, "version": "1.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(tools.values())}
            elif method == "tools/call":
                params = message.get("params") or {}
                name, arguments = params.get("name"), params.get("arguments", {})
                if name not in tools or not isinstance(arguments, dict):
                    raise ValueError("MCP tool is not permitted")
                schema = tools[name]["inputSchema"]
                if (
                    set(arguments) - schema["properties"].keys()
                    or not set(schema["required"]) <= arguments.keys()
                ):
                    raise ValueError("Invalid tool arguments")
                try:
                    value = (
                        _files(Path(workspace), name, arguments)
                        if server_id == "project-files"
                        else _git(Path(workspace), name, arguments)
                    )
                    result = {
                        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                        "isError": False,
                    }
                except (OSError, ValueError, subprocess.SubprocessError):
                    result = {
                        "content": [
                            {"type": "text", "text": "Доступ запрещён или превышен лимит операции проекта"}
                        ],
                        "isError": True,
                    }
            else:
                raise ValueError("MCP method is not permitted")
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (ValueError, TypeError, AttributeError):
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Invalid or unsupported MCP request"},
            }
        stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()
        if len(line.encode()) > MAX_BYTES:
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Built-in project MCP servers")
    parser.add_argument("--serve", required=True, choices=["project-files", "project-git"])
    serve(parser.parse_args().serve)
