"""Visual verification: the `project-verify` skill and the one-off task that writes its source document.

docs/darkfactory/VERIFY.md says how to start the project's app in the container and what to capture. A reviewed
copy is kept as a project artifact; its text becomes the `project-verify` skill. Only visual tasks must attach
screenshots (see `visual`)."""

from sqlalchemy import select

from . import attachments, registry
from .db import Artifact, ArtifactVersion, Task, event, uid

PATH = "docs/darkfactory/VERIFY.md"
KEY = "project-verify"
TITLE = "Визуальная проверка проекта"
WIDTHS = (390, 1440)
SKILL_CHARS = 6000
DESCRIPTION = (
    "Читай, когда меняешь внешний вид: как поднять приложение, какие ширины снимать и куда класть скриншоты."
)
REQUEST = f"""Описать визуальную проверку проекта.
Изучи репозиторий и создай или дополни файл {PATH}. Опиши на русском:
- как поднять приложение внутри контейнера исполнителя (команды из файлов проекта, порт, ожидание готовности,
  тестовые данные без секретов);
- какие страницы и состояния снимать; ширины экрана 390 и 1440 пикселей;
- как снять скриншоты (Chromium и Playwright уже есть в образе) и что на них проверять;
- скриншоты класть в каталог /run/evidence, а не в репозиторий.
Пиши только команды, подтверждённые файлами проекта; пробелы отмечай как неизвестные. Не копируй токены, пароли
и значения переменных окружения. Меняй только {PATH}: не изменяй код, настройки и проверки.
Файл должен быть непустым и не длиннее {registry.MAX_CONTENT} символов.
"""
REWORK = (
    "Это визуальная задача, а скриншотов в /run/evidence нет. Подними приложение по скиллу project-verify, "
    f"сними изменённые экраны в ширинах {WIDTHS[0]} и {WIDTHS[1]} и сохрани скриншоты в /run/evidence "
    "(не в репозиторий). Затем закончи задачу как обычно."
)

NOTE = (
    "Визуальная задача: скриншотов в /run/evidence нет даже после доработки. Ревью: проверь внешний вид "
    "по коду и отметь, что снимков нет."
)


def active(task):
    return bool((task.role_config or {}).get("verify_guide"))


def queue(db, project, principal_id):
    existing = next(
        (
            task
            for task in db.scalars(select(Task).where(Task.project_id == project.id))
            if active(task) and task.state not in {"merged", "cancelled", "failed"}
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
        title="Описать визуальную проверку проекта",
        request=REQUEST,
        branch=f"darkfactory/{ident}",
        role_config={"verify_guide": True, "verify_guide_author": principal_id},
    )
    db.add(task)
    db.flush()
    event(db, task, "created", task.title)
    return task


def validate(repo):
    changed = repo.git("diff", "--name-only", "-z", "HEAD", cwd=repo.work).split("\0")
    changed += repo.git("ls-files", "--others", "--exclude-standard", "-z", cwd=repo.work).split("\0")
    if set(filter(None, changed)) - {PATH}:
        raise ValueError(f"Описание визуальной проверки может менять только {PATH}")
    path = repo.work / PATH
    if any(part.is_symlink() for part in (path, path.parent, path.parent.parent)) or not path.is_file():
        raise ValueError(f"Файл {PATH} не создан")
    text = path.read_text()
    if not text.strip() or len(text) > registry.MAX_CONTENT:
        raise ValueError(f"Файл {PATH} пуст или превышает {registry.MAX_CONTENT} символов")


def publish(db, task, repo):
    """The reviewed VERIFY.md becomes a version of the `project-verify` artifact."""
    if not active(task) or task.state not in {"merged", "review_required"}:
        return
    if not task.candidate_sha or task.reviewed_sha != task.candidate_sha:
        return
    process = repo.git("show", f"{task.candidate_sha}:{PATH}", cwd=repo.work, check=False)
    content = process.stdout if process.returncode == 0 else ""
    if not content.strip() or len(content) > registry.MAX_CONTENT:
        return
    artifact = db.scalar(select(Artifact).where(Artifact.project_id == task.project_id, Artifact.key == KEY))
    if artifact is None:
        artifact = Artifact(project_id=task.project_id, key=KEY, title=TITLE, kind="document")
        db.add(artifact)
        db.flush()
    if db.scalar(
        select(ArtifactVersion.id).where(
            ArtifactVersion.artifact_id == artifact.id,
            ArtifactVersion.source_task_id == task.id,
            ArtifactVersion.source_sha == task.candidate_sha,
        )
    ):
        return
    previous = registry.versions(db, artifact)
    db.add(
        ArtifactVersion(
            artifact_id=artifact.id,
            number=previous[0].number + 1 if previous else 1,
            content=content,
            source_task_id=task.id,
            source_sha=task.candidate_sha,
            created_by=task.role_config.get("verify_guide_author"),
        )
    )
    db.flush()


def project_verify(project_id):
    """The `project-verify` skill from the project's VERIFY.md, or None until it was written and reviewed."""
    from .db import Session

    with Session() as db:
        latest = db.scalar(
            select(ArtifactVersion)
            .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
            .where(Artifact.project_id == project_id, Artifact.key == KEY)
            .order_by(ArtifactVersion.number.desc())
            .limit(1)
        )
        if not latest:
            return None
        body = latest.content[:SKILL_CHARS]
    return {
        "name": KEY,
        "description": DESCRIPTION,
        "content": f"---\nname: {KEY}\ndescription: {DESCRIPTION}\n---\n\nИсточник: {PATH}.\n\n{body}\n",
    }


def visual(task):
    """Visual: the owner ticked the flag, or a stand tester's report that carries its screenshots."""
    config = task.role_config or {}
    if config.get("visual"):
        return True
    return bool(config.get("qa")) and any("-qa-" in item["name"] for item in attachments.listing(task.id))
