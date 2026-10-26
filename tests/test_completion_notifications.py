from types import SimpleNamespace

import pytest

from darkfactory import db
from darkfactory.notifications import NotificationDelivery, completion_summary, queue_notification
from darkfactory.task_links import commit_url, task_url

SHA = "a" * 40


@pytest.mark.parametrize(
    "forge,url,repo,remote,expected",
    [
        ("github", "https://api.github.com", "team/app", "", "https://github.com/team/app/commit/"),
        ("github", "https://git.corp/api/v3", "team/app", "", "https://git.corp/team/app/commit/"),
        (
            "gitlab",
            "https://git.corp/api/v4",
            "group/sub/app",
            "",
            "https://git.corp/group/sub/app/-/commit/",
        ),
        ("gitlab", "https://corp/gitlab/api/v4/", "team/app", "", "https://corp/gitlab/team/app/-/commit/"),
        ("git", None, None, "https://github.com/team/app.git", "https://github.com/team/app/commit/"),
        ("git", None, None, "git@gitlab.com:team/app.git", "https://gitlab.com/team/app/-/commit/"),
        ("git", None, None, "ssh://git@github.com/team/app.git", "https://github.com/team/app/commit/"),
    ],
)
def test_commit_links(forge, url, repo, remote, expected):
    project = SimpleNamespace(forge=forge, forge_url=url, forge_repo=repo, remote=remote)
    assert commit_url(project, SimpleNamespace(id="task", result_sha=SHA)) == expected + SHA


@pytest.mark.parametrize(
    "forge,url,repo,remote",
    [
        ("git", None, None, "/repos/local.git"),
        ("git", None, None, "https://git.unknown/team/app.git"),
        ("git", None, None, "https://token@github.com/team/app.git"),
        ("git", None, None, "https://github.com/team/app.git?token=secret"),
        ("gitlab", "javascript:alert(1)", "team/app", ""),
        ("gitlab", "https://token@git.corp/api/v4", "team/app", ""),
        ("github", "https://git.corp/api/v3?token=secret", "team/app", ""),
        ("github", "https://git.corp/api/v3", "../app", ""),
    ],
)
def test_local_unknown_and_unsafe_links_use_authenticated_factory(forge, url, repo, remote, monkeypatch):
    monkeypatch.setenv("DARKFACTORY_PUBLIC_URL", "https://factory.corp")
    project = SimpleNamespace(forge=forge, forge_url=url, forge_repo=repo, remote=remote)
    assert commit_url(project, SimpleNamespace(id="task", result_sha=SHA)) == (
        "https://factory.corp/?task=task&commit=" + SHA
    )


def test_invalid_sha_and_display_url(monkeypatch):
    task = SimpleNamespace(id="task", result_sha=None)
    assert commit_url(None, task) is None
    task.result_sha = "<invalid>"
    assert commit_url(None, task) is None
    monkeypatch.setenv("DARKFACTORY_PUBLIC_URL", "https://user:secret@factory.corp")
    assert task_url(task) == "http://127.0.0.1:8080/?task=task"


def test_completion_comment_redacts_before_shortening():
    secret = "x" * 500
    project = SimpleNamespace(secret=db.encrypt('{"git_token":"' + secret + '"}'))
    assert completion_summary(project, secret + "\nИсправлено") == "[redacted] Исправлено"
    assert len(completion_summary(project, "Изменено " * 200)) <= 350


def test_completion_reads_latest_successful_implementation(client, setup_project, monkeypatch):
    from conftest import make_task
    from test_notifications import channel

    monkeypatch.setenv("DARKFACTORY_PUBLIC_URL", "https://factory.corp")
    project, _ = setup_project
    value = make_task(client, project)
    with db.transaction() as session:
        task = session.get(db.Task, value["id"])
        task.state, task.result_sha = "merged", SHA
        channel(task)
        db.event(session, task, "implementation_summary", "Первый вариант")
        db.event(session, task, "implementation_summary", "Исправил форму. Проверил мобильную версию.")
        db.event(session, task, "accepted", "Проверенный результат слит в preprod", commit=SHA)
    with db.Session() as session:
        notification = session.query(NotificationDelivery).one()
        assert "Исправил форму. Проверил мобильную версию." in notification.message
        assert "Первый вариант" not in notification.message
        assert "https://factory.corp/?task=" + value["id"] + "&commit=" + SHA in notification.message
    detail = client.get("/api/tasks/" + value["id"]).json()
    assert detail["commit_url"] == "https://factory.corp/?task=" + value["id"] + "&commit=" + SHA
    assert queue_notification(None, SimpleNamespace(state="running"), "accepted", "Not completed") == 0
