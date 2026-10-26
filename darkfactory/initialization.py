"""Bootstrap project knowledge through the normal task review pipeline."""

import json
from pathlib import PurePosixPath

from sqlalchemy import select

from . import registry
from .db import Artifact, ArtifactVersion, Task, event, uid

DOCUMENTS = {
    "INDEX.md": ("project-index", "Карта проекта", "analysis"),
    "ARCHITECTURE.md": ("project-architecture", "Архитектура проекта", "architecture"),
    "DEVELOPMENT.md": ("project-development", "Разработка и проверки", "document"),
    "CONTRACTS.md": ("project-contracts", "Контракты и интеграции", "contract"),
}
DIRECTORY = "docs/darkfactory"
REQUEST = """Первичная инициализация существующего проекта.
Изучи репозиторий и существующую документацию. Карта отслеживаемых файлов уже
подготовлена в docs/darkfactory/INDEX.md. Проверь и дополни её точками входа,
назначением модулей и ссылками на реальные пути. Создай или дополни:
- docs/darkfactory/ARCHITECTURE.md: компоненты, зависимости, данные, границы системы;
- docs/darkfactory/DEVELOPMENT.md: установка, запуск, конфигурация без значений
  секретов, проверки и команды, подтверждённые файлами проекта;
- docs/darkfactory/CONTRACTS.md: обнаруженные API, события и внешние интеграции.
Пиши Markdown на русском. Сохраняй полезный существующий текст. Отделяй факты
от предположений; пробелы отмечай как неизвестные. Не выдумывай бизнес-требования.
Не копируй токены, пароли, персональные данные и значения переменных окружения.
Меняй только эти четыре Markdown-файла. Не изменяй код, настройки и проверки.
Каждый документ должен быть непустым и не длиннее 100000 символов.
Не делегируй подзадачи. Ревью должно проверить точность ссылок и утверждений.
"""


def active(task):
    return bool((task.role_config or {}).get("initialization"))


def queue(db, project, principal_id):
    existing = next(
        (task for task in db.scalars(select(Task).where(Task.project_id == project.id)) if active(task)),
        None,
    )
    if existing:
        return existing
    ident = uid()
    task = Task(
        id=ident,
        org_id=project.org_id,
        project_id=project.id,
        title="Инициализация проекта: индекс и документация",
        request=REQUEST,
        branch=f"darkfactory/{ident}",
        role_config={"initialization": True, "initialization_author": principal_id},
    )
    db.add(task)
    db.flush()
    event(db, task, "created", task.title)
    return task


def document_path(repo, name):
    path = repo.work / DIRECTORY / name
    if any(part.is_symlink() for part in (path, path.parent, path.parent.parent)):
        raise ValueError("Документы инициализации не могут использовать символические ссылки")
    return path


def prepare(repo, sha):
    paths = repo.git("ls-tree", "-r", "--name-only", "-z", sha, cwd=repo.work).split("\0")
    paths = sorted(path for path in paths if path and not path.startswith(DIRECTORY + "/"))
    lines = [f"# Карта проекта\n\nИсходный коммит: `{sha}`.\n", f"Отслеживаемых файлов: {len(paths)}.\n"]
    lines += ["- " + json.dumps(path, ensure_ascii=False) for path in paths[:1000]]
    if len(paths) > 1000:
        lines.append(
            f"\nПоказаны первые 1000 файлов из {len(paths)}; полный список доступен через git ls-files."
        )
    content = "\n".join(lines)
    (repo.artifacts / "repository-index.md").write_text(content)
    index = document_path(repo, "INDEX.md")
    index.parent.mkdir(parents=True, exist_ok=True)
    if not index.exists():
        index.write_text(content)
    repo.own_files()


def validate(repo):
    allowed = {f"{DIRECTORY}/{name}" for name in DOCUMENTS}
    changed = repo.git("diff", "--name-only", "-z", "HEAD", cwd=repo.work).split("\0")
    changed += repo.git("ls-files", "--others", "--exclude-standard", "-z", cwd=repo.work).split("\0")
    if set(filter(None, changed)) - allowed:
        raise ValueError("Инициализация может менять только четыре документа docs/darkfactory/*.md")
    for name in DOCUMENTS:
        path = document_path(repo, name)
        if not path.is_file() or not 0 < path.stat().st_size <= registry.MAX_CONTENT * 4:
            raise ValueError(f"Документ {name} отсутствует, пуст или превышает лимит")
        content = path.read_text()
        if not content.strip() or len(content) > registry.MAX_CONTENT:
            raise ValueError(f"Документ {name} пуст или превышает 100000 символов")


def only_documents(repo, base, sha):
    """Initialization may merge without project checks only while it touches its own documents."""
    allowed = {f"{DIRECTORY}/{name}" for name in DOCUMENTS}
    changed = repo.git("diff", "--name-only", "-z", base, sha, cwd=repo.work).split("\0")
    if set(filter(None, changed)) - allowed:
        raise ValueError("Инициализация может менять только четыре документа docs/darkfactory/*.md")


def documents(repo, sha):
    result = {}
    for name in DOCUMENTS:
        path = str(PurePosixPath(DIRECTORY) / name)
        entry = repo.git("ls-tree", sha, "--", path, cwd=repo.work)
        if not entry.startswith(("100644 blob ", "100755 blob ")):
            raise ValueError(f"Документ {name} отсутствует или не является обычным файлом")
        process = repo.git("show", f"{sha}:{path}", cwd=repo.work, check=False)
        process.check_returncode()
        content = process.stdout
        if not content.strip() or len(content) > registry.MAX_CONTENT:
            raise ValueError(f"Нельзя опубликовать пустой или слишком большой {name}")
        result[name] = content
    return result


def publish(db, task, repo):
    if not active(task) or task.state not in {"merged", "review_required"}:
        return
    if not task.candidate_sha or task.reviewed_sha != task.candidate_sha:
        raise ValueError("Документы инициализации ещё не прошли ревью")
    contents = documents(repo, task.candidate_sha)
    for name, (key, title, kind) in DOCUMENTS.items():
        content = contents[name]
        artifact = db.scalar(
            select(Artifact).where(Artifact.project_id == task.project_id, Artifact.key == key)
        )
        if artifact is None:
            artifact = Artifact(project_id=task.project_id, key=key, title=title, kind=kind)
            db.add(artifact)
            db.flush()
        if db.scalar(
            select(ArtifactVersion.id).where(
                ArtifactVersion.artifact_id == artifact.id,
                ArtifactVersion.source_task_id == task.id,
                ArtifactVersion.source_sha == task.candidate_sha,
            )
        ):
            continue
        previous = registry.versions(db, artifact)
        db.add(
            ArtifactVersion(
                artifact_id=artifact.id,
                number=previous[0].number + 1 if previous else 1,
                content=content,
                source_task_id=task.id,
                source_sha=task.candidate_sha,
                created_by=task.role_config["initialization_author"],
            )
        )
    db.flush()


GUIDE_CHARS = 4000
GUIDE_DESCRIPTION = "Читай первым: как устроен этот проект, где код и тесты, как собирать и проверять."


def project_guide(project_id):
    """A `project-guide` skill built from the project's reviewed initialization documents: where each lives
    in the repository and its outline. Agents read the documents themselves instead of re-surveying the
    repository on every task. None until the project was initialized."""
    from .db import Session

    sections = []
    with Session() as db:
        for name, (key, title, _kind) in DOCUMENTS.items():
            latest = db.scalar(
                select(ArtifactVersion)
                .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
                .where(Artifact.project_id == project_id, Artifact.key == key)
                .order_by(ArtifactVersion.number.desc())
                .limit(1)
            )
            if not latest:
                continue
            outline = [
                line.strip()
                for line in latest.content.splitlines()
                if line.startswith(("## ", "### ")) and len(line) < 160
            ]
            sections.append(f"## {title}: {DIRECTORY}/{name}\n" + "\n".join(outline)[:900])
    if not sections:
        return None
    body = (
        "Документы проекта прошли ревью и лежат в репозитории. Перед поиском по коду открой нужный раздел: "
        f"сборка и проверки — {DIRECTORY}/DEVELOPMENT.md, устройство — {DIRECTORY}/ARCHITECTURE.md, "
        f"карта файлов — {DIRECTORY}/INDEX.md.\n\n" + "\n\n".join(sections)
    )[:GUIDE_CHARS]
    return {
        "name": "project-guide",
        "description": GUIDE_DESCRIPTION,
        "content": f"---\nname: project-guide\ndescription: {GUIDE_DESCRIPTION}\n---\n\n{body}\n",
    }
