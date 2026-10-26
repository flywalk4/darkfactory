"""Project lessons: the mistakes agents stumbled on become short project rules.

A merged task with review returns, a repeated review finding, a supervisor stop or a stand tester's find
leaves a short record in `project.health["lessons"]`. The `lessons` schedule turns the records into one
task that rewrites docs/darkfactory/LESSONS.md; agents get that file as the `project-lessons` skill, so the
prompt carries one line and the rules stay on disk.
"""

import json
import re

from sqlalchemy import select

from .db import ROOT, Event, Task, event, uid
from .leaks import SECRETS

DIRECTORY = "docs/darkfactory"
PATH = f"{DIRECTORY}/LESSONS.md"
TITLE = "Обновить правила проекта"
RECORDS = 50
LINES = 60
CLOSED = {"merged", "cancelled", "duplicate", "checked"}
SKILL_CHARS = 8000
DESCRIPTION = (
    "Читай перед изменением: правила проекта из прошлых ошибок агентов — что делать и чего не делать."
)
REQUEST = """Обнови правила проекта в docs/darkfactory/LESSONS.md по случаям ниже, где агенты ошиблись.
Дополни и сожми файл: не больше 60 строк, одно правило — одна строка «делай / не делай — почему».
Объедини дубли, убери устаревшее и правила, которые код проекта уже не подтверждает.
Пиши правило о проекте, а не пересказ задачи; не копируй токены, пароли и персональные данные.
Меняй только docs/darkfactory/LESSONS.md. Не изменяй код, настройки и проверки.

Случаи:
"""


def active(task):
    return bool((task.role_config or {}).get("lessons"))


def findings(task_id, limit=2):
    """The first notes of the task's last review, short."""
    try:
        review = json.loads((ROOT / "artifacts" / task_id / "review.json").read_text())
        items = review.get("findings") or []
    except (OSError, ValueError, AttributeError):
        return []
    notes = [item.get("text", "") if isinstance(item, dict) else str(item) for item in items]
    # The notes reach a task request and then a committed file: a token quoted by the reviewer stays out.
    return [hide(" ".join(note.split()))[:300] for note in notes if note.strip()][:limit]


def hide(text):
    for pattern in SECRETS.values():
        text = re.sub(pattern, "<secret>", text)
    return text


def record(db, task, project):
    """Keep a short case for the lessons task when the merged task cost the factory a detour."""
    if active(task):
        return None
    kinds = set(
        db.scalars(
            select(Event.kind).where(
                Event.task_id == task.id, Event.kind.in_(("review_repeated", "supervisor"))
            )
        )
    )
    reasons = []
    if task.corrections:
        reasons.append(f"возвратов ревью: {task.corrections}")
    if "review_repeated" in kinds:
        reasons.append("ревью повторило замечание")
    if "supervisor" in kinds:
        reasons.append("остановку решал директор")
    if (task.role_config or {}).get("qa"):
        reasons.append("дефект найден тестировщиком стенда")
    if not reasons:
        return None
    entry = {"task_id": task.id, "title": task.title[:200], "reasons": reasons, "notes": findings(task.id)}
    health = project.health or {}
    project.health = {**health, "lessons": [*(health.get("lessons") or []), entry][-RECORDS:]}
    return entry


def describe(entry):
    lines = [f"- {entry['task_id']} «{entry['title']}»: " + "; ".join(entry.get("reasons") or [])]
    lines += [f"  - {note}" for note in entry.get("notes") or []]
    return "\n".join(lines)


def open_task(db, project):
    return next(
        (
            task
            for task in db.scalars(
                select(Task).where(Task.project_id == project.id, Task.state.not_in(CLOSED))
            )
            if active(task)
        ),
        None,
    )


def queue(db, project, schedule_id=None):
    """One task from the collected cases; returns (task, short result for the schedule page)."""
    cases = (project.health or {}).get("lessons") or []
    if not cases:
        return None, "Новых уроков нет"
    if existing := open_task(db, project):
        return None, f"Пропущено: прошлая задача {existing.id} ещё не закончена"
    ident = uid()
    task = Task(
        id=ident,
        org_id=project.org_id,
        project_id=project.id,
        title=TITLE,
        request=REQUEST + "\n".join(describe(entry) for entry in cases),
        branch=f"darkfactory/{ident}",
        role_config={"lessons": True, "quick": True, **({"schedule": schedule_id} if schedule_id else {})},
    )
    db.add(task)
    db.flush()
    event(db, task, "created", task.title)
    project.health = {**(project.health or {}), "lessons": []}
    return task, f"Создана задача {ident}"


def changed_files(repo, *spec):
    return set(filter(None, repo.git("diff", "--name-only", "-z", *spec, cwd=repo.work).split("\0")))


def check_file(path):
    if any(part.is_symlink() for part in (path, path.parent, path.parent.parent)):
        raise ValueError("LESSONS.md не может быть символической ссылкой")
    if not path.is_file():
        raise ValueError("Файл docs/darkfactory/LESSONS.md отсутствует")
    content = path.read_text()
    if not content.strip() or len(content.splitlines()) > LINES:
        raise ValueError(f"LESSONS.md пуст или длиннее {LINES} строк")


def validate(repo):
    changed = changed_files(repo, "HEAD")
    changed |= set(
        filter(None, repo.git("ls-files", "--others", "--exclude-standard", "-z", cwd=repo.work).split("\0"))
    )
    if changed - {PATH}:
        raise ValueError("Задача уроков может менять только docs/darkfactory/LESSONS.md")
    check_file(repo.work / PATH)


def only_lessons(repo, base, sha):
    """A lessons task merges only while it touches its own file."""
    if changed_files(repo, base, sha) - {PATH}:
        raise ValueError("Задача уроков может менять только docs/darkfactory/LESSONS.md")


def skill(work):
    """The `project-lessons` skill from the task workspace's LESSONS.md; None when the project has none."""
    path = work / PATH
    try:
        if any(part.is_symlink() for part in (path, path.parent, path.parent.parent)) or not path.is_file():
            return None
        body = path.read_text(errors="replace").strip()[:SKILL_CHARS]
    except OSError:
        return None
    if not body:
        return None
    return {
        "name": "project-lessons",
        "description": DESCRIPTION,
        "content": f"---\nname: project-lessons\ndescription: {DESCRIPTION}\n---\n\n{body}\n",
    }
