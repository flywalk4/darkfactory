"""Initial project knowledge uses reviewed Git commits and draft registry versions."""

from types import SimpleNamespace

import pytest
from conftest import connect
from sqlalchemy import select
from test_pipeline import drive, git, seed

from darkfactory import db, initialization
from darkfactory.gitops import Repository
from darkfactory.worker import Worker


def start(client, project):
    response = client.post(f"/api/projects/{project['id']}/initialize")
    assert response.status_code == 200, response.text
    return response.json()


class DocumentationExecutor:
    def __init__(self, invalid_once=False):
        self.invalid_once = invalid_once
        self.attempts = 0

    def stop(self, *_):
        pass

    def run(self, task, project, connection, role, heartbeat, timeout):
        assert heartbeat()
        work = db.ROOT / "workspaces" / task.id
        if role == "plan":
            assert (work / initialization.DIRECTORY / "INDEX.md").is_file()
            return {
                "status": "ok",
                "plan": {
                    "plan": "Inspect repository and document actual architecture",
                    "questions": [],
                    "tasks": [{"title": "Unwanted child", "request": "Do not delegate initialization"}],
                },
            }
        if role == "implement":
            self.attempts += 1
            for name in initialization.DOCUMENTS:
                (work / initialization.DIRECTORY / name).write_text(f"# {name}\n\nSource: value.py\n\n")
            if self.invalid_once:
                self.invalid_once = False
                (work / "value.py").write_text("VALUE = 42\n")
            else:
                (work / "value.py").write_text("VALUE = 0\n")
            return {"status": "ok", "text": "Four Markdown documents prepared"}
        if role == "review":
            assert (work / "value.py").read_text() == "VALUE = 0\n"
            return {"status": "ok", "review": {"approved": True, "findings": []}}
        return {"status": "ok", "text": "Repository checks passed"}


def test_new_project_initializes_by_default_and_can_opt_out(client):
    response = client.post(
        "/api/projects",
        json={"name": "Existing", "remote": "https://example.invalid/repo.git", "prepare_environment": False},
    )
    assert response.status_code == 201
    project = response.json()
    task = start(client, project)
    assert task["title"].startswith("Инициализация проекта")
    assert task["role_config"]["initialization"] is True
    assert start(client, project)["id"] == task["id"]
    other = client.post(
        "/api/projects",
        json={
            "name": "Skip",
            "remote": "https://example.invalid/skip.git",
            "initialize": False,
            "prepare_environment": False,
        },
    ).json()
    with db.Session() as session:
        assert len(list(session.scalars(select(db.Task)))) == 1
        assert not session.scalar(select(db.Task).where(db.Task.project_id == other["id"]))
    update = client.put(f"/api/projects/{other['id']}", json={"name": "Updated", "remote": other["remote"]})
    assert update.status_code == 200
    assert client.get(f"/api/projects/{other['id']}/tasks").json() == []


@pytest.mark.parametrize("checks", [True, False])
def test_initialization_reviews_commit_and_publishes_four_drafts(client, setup_project, tmp_path, checks):
    project, pool = setup_project
    remote = seed(tmp_path)
    main = git(remote, "rev-parse", "main")
    connect(client, pool, "codex", "writer")
    connect(client, pool, "claude", "reviewer")
    existing = client.post(
        f"/api/projects/{project['id']}/artifacts",
        json={"key": "project-architecture", "title": "Architecture", "content": "Existing accepted design"},
    ).json()
    old = existing["versions"][0]
    assert (
        client.post(f"/api/project-artifacts/{existing['id']}/versions/{old['id']}/accept").status_code == 200
    )
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        row.remote = str(remote)
        if not checks:
            row.checks = []
    task = start(client, project)
    result = drive(Worker(DocumentationExecutor()), task["id"])
    # Documentation-only initialization merges after review even without project checks.
    assert result.state == "merged", result.reason
    assert result.candidate_sha == result.reviewed_sha
    index = (db.ROOT / "artifacts" / task["id"] / "repository-index.md").read_text()
    assert main in index and '"value.py"' in index
    response = client.get(f"/api/tasks/{task['id']}/artifacts/repository-index.md")
    assert response.status_code == 200
    assert response.text == index
    listed = client.get(f"/api/projects/{project['id']}/artifacts").json()
    assert len(listed) == 4
    with db.transaction() as session:
        repo = Repository(session.get(db.Project, project["id"]), result)
        assert set(git(repo.work, "diff", "--name-only", main, result.candidate_sha).splitlines()) == {
            f"{initialization.DIRECTORY}/{name}" for name in initialization.DOCUMENTS
        }
        initialization.publish(session, session.get(db.Task, task["id"]), repo)
        assert len(list(session.scalars(select(db.Task)))) == 1
    for artifact in listed:
        version = artifact["versions"][0]
        assert version["status"] == "draft"
        assert version["source_task_id"] == task["id"]
        assert version["source_sha"] == result.reviewed_sha
        content = client.get(f"/api/project-artifacts/{artifact['id']}/versions/{version['id']}").json()[
            "content"
        ]
        assert content.endswith("\n\n")
        assert artifact["accepted_version_id"] == (
            old["id"] if artifact["key"] == "project-architecture" else None
        )
        assert len(artifact["versions"]) == (2 if artifact["key"] == "project-architecture" else 1)
    assert start(client, project)["id"] == task["id"]
    assert len(client.get(f"/api/projects/{project['id']}/artifacts").json()) == 4
    assert git(remote, "rev-parse", "main") == main
    assert git(remote, "rev-parse", "preprod") == result.result_sha


def test_initialization_code_changes_require_correction(client, setup_project, tmp_path):
    project, pool = setup_project
    connect(client, pool, "codex", "writer")
    connect(client, pool, "claude", "reviewer")
    remote = seed(tmp_path)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    task = start(client, project)
    executor = DocumentationExecutor(invalid_once=True)
    result = drive(Worker(executor), task["id"])
    assert result.state == "merged", result.reason
    assert executor.attempts == 2 and result.corrections == 1


def test_initialization_is_owner_only_and_scoped(client, setup_project):
    project, _ = setup_project
    other = client.post(
        "/api/projects",
        json={"name": "Other", "remote": "https://example.invalid/other.git", "initialize": False},
    ).json()
    tokens = [
        (
            role,
            client.post("/api/tokens", json={"role": role, "project_ids": [project["id"]]}).json()["token"],
        )
        for role in ("operator", "observer", "owner")
    ]
    for role, token in tokens:
        client.headers["Authorization"] = "Bearer " + token
        assert client.post(f"/api/projects/{project['id']}/initialize").status_code == (
            200 if role == "owner" else 403
        )
        if role == "owner":
            assert client.post(f"/api/projects/{other['id']}/initialize").status_code == 404


@pytest.mark.parametrize("invalid", ["missing", "empty", "large", "symlink", "outside"])
def test_initialization_validates_document_boundaries(tmp_path, invalid):
    work = tmp_path / "repo"
    work.mkdir()
    git(work, "init", "-b", "main")
    (work / "value.py").write_text("VALUE = 0\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "seed")
    repo = SimpleNamespace(work=work, git=lambda *args, **kwargs: git(work, *args))
    directory = work / initialization.DIRECTORY
    directory.mkdir(parents=True)
    for name in initialization.DOCUMENTS:
        (directory / name).write_text("Valid document\n")
    target = directory / "ARCHITECTURE.md"
    if invalid == "missing":
        target.unlink()
    elif invalid == "empty":
        target.write_text("   ")
    elif invalid == "large":
        target.write_text("x" * 100001)
    elif invalid == "symlink":
        target.unlink()
        target.symlink_to(work / "value.py")
    else:
        (work / "value.py").write_text("VALUE = 42\n")
    with pytest.raises(ValueError):
        initialization.validate(repo)


def test_reviewed_documents_become_the_project_guide_skill(client, setup_project):
    from darkfactory.initialization import project_guide

    project, _ = setup_project
    assert project_guide(project["id"]) is None
    with db.transaction() as session:
        author = session.scalar(select(db.Principal.id))
        artifact = db.Artifact(
            project_id=project["id"], key="project-development", title="Разработка", kind="document"
        )
        session.add(artifact)
        session.flush()
        session.add(
            db.ArtifactVersion(
                artifact_id=artifact.id,
                number=1,
                content="# Dev\nintro\n## Сборка\ntext\n### Тесты бэкенда\nmore",
                created_by=author,
            )
        )
    guide = project_guide(project["id"])
    assert guide["name"] == "project-guide"
    assert "docs/darkfactory/DEVELOPMENT.md" in guide["content"]
    assert "## Сборка" in guide["content"] and "### Тесты бэкенда" in guide["content"]
    assert "intro" not in guide["content"]
