import json

from conftest import connect
from sqlalchemy import select

from darkfactory import backlog, db, schedules


class Groups:
    def __init__(self, make):
        self.make = make
        self.calls = []

    def run(
        self, task, project, connection, role, heartbeat, timeout, *, prompt_override=None, output_schema=None
    ):
        self.calls.append((role, prompt_override, output_schema))
        return {"status": "ok", "text": json.dumps(self.make(prompt_override))}


def add(client, project, text):
    response = client.post(f"/api/projects/{project['id']}/backlog", json={"text": text})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_click_groups_related_notes_into_tasks(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    first = add(client, project, "Кнопка экспорта не видна на мобильном")
    second = add(client, project, "Экспорт CSV теряет кириллицу")
    third = add(client, project, "Опечатка в футере")
    assert client.get(f"/api/projects/{project['id']}/backlog").json()["next_at"]
    assert not backlog.plan(Groups(lambda _: {"groups": []}))  # Timer not due yet, nothing requested.

    view = client.post(f"/api/projects/{project['id']}/backlog/plan").json()
    assert view["requested"] and view["next_at"] is None
    executor = Groups(
        lambda _: {
            "groups": [
                {
                    "title": "Починить экспорт",
                    "request": "Кнопка и кодировка",
                    "items": [first, second, "unknown"],
                },
                {"title": "Дубль", "request": "x", "items": [first]},
            ]
        }
    )
    assert backlog.plan(executor)
    role, prompt, schema = executor.calls[0]
    assert role == "director" and schema == backlog.SCHEMA
    assert first in prompt and "Опечатка в футере" in prompt
    view = client.get(f"/api/projects/{project['id']}/backlog").json()
    tasks = {item["id"]: item["task"] for item in view["items"]}
    assert tasks[first]["id"] == tasks[second]["id"]
    assert tasks[first]["title"] == "Починить экспорт"
    # Skipped by the agent: recorded as dismissed, not turned into a task of its own.
    assert tasks[third] is None
    assert {item["id"]: item["state"] for item in view["items"]}[third] == "dismissed"
    assert view["requested"] is None and view["last_tasks"] == 1
    with db.Session() as session:
        assert session.scalar(select(db.Task).where(db.Task.id == tasks[first]["id"])).state == "queued"
    assert client.delete(f"/api/projects/{project['id']}/backlog/{first}").status_code == 409
    assert client.post(f"/api/projects/{project['id']}/backlog/plan").status_code == 409


def backlog_schedule(client, project):
    items = client.get(f"/api/projects/{project['id']}/schedules").json()["items"]
    return next(item for item in items if item["kind"] == "backlog")


def make_due(schedule_id):
    with db.transaction() as session:
        session.get(schedules.Schedule, schedule_id).next_at = 1


def test_schedule_plans_backlog_and_failure_keeps_notes(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    schedule = backlog_schedule(client, project)
    assert schedule["cron"] == "0 */4 * * *" and schedule["enabled"]
    make_due(schedule["id"])
    assert schedules.tick(force=True) == [schedule["id"]]
    assert not backlog.plan(Groups(lambda _: {"groups": []}))  # No notes: the schedule asked for nothing.

    note = add(client, project, "Добавить сортировку")
    assert client.get(f"/api/projects/{project['id']}/backlog").json()["next_at"]
    make_due(schedule["id"])
    schedules.tick(force=True)
    assert backlog.plan(Groups(lambda _: {"groups": "broken"}))
    view = client.get(f"/api/projects/{project['id']}/backlog").json()
    assert view["items"][0]["state"] == "open" and view["last_error"]
    assert not backlog.plan(Groups(lambda _: {"groups": []}))  # Waits for the next scheduled time.
    client.post(f"/api/schedules/{schedule['id']}/run")
    assert backlog.plan(
        Groups(lambda _: {"groups": [{"title": "Сортировка", "request": "r", "items": [note]}]})
    )
    assert client.get(f"/api/projects/{project['id']}/backlog").json()["items"][0]["state"] == "planned"


def test_manual_only_project_and_deleting_open_notes(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    schedule = backlog_schedule(client, project)
    assert client.delete(f"/api/schedules/{schedule['id']}").status_code == 200
    note = add(client, project, "Когда-нибудь")
    assert schedules.tick(force=True) == []
    assert not backlog.plan(Groups(lambda _: {"groups": []}))
    view = client.get(f"/api/projects/{project['id']}/backlog").json()
    assert view["next_at"] is None and view["cron"] is None
    assert client.delete(f"/api/projects/{project['id']}/backlog/{note}").json() == {"deleted": True}
    assert client.get(f"/api/projects/{project['id']}/backlog").json()["items"] == []


def test_later_notes_stay_on_the_task_instead_of_the_backlog(client, setup_project):
    from darkfactory import worker

    project, _ = setup_project
    with db.transaction() as session:
        owner = db.Task(
            id=db.uid(),
            org_id=project["org_id"],
            project_id=project["id"],
            title="Owner",
            request="r",
            branch="b",
        )
        session.add(owner)
        session.flush()
        worker.later_notes(
            session,
            owner,
            ["[LATER] Чеклист ревью: no_unrelated_changes", "[LATER] Реальное улучшение", "не заметка"],
            "разработка",
        )
        session.flush()
        assert session.query(backlog.BacklogItem).filter_by(project_id=project["id"]).count() == 0
        notes = session.scalar(
            select(db.Event).where(db.Event.task_id == owner.id, db.Event.kind == "later_notes")
        )
        assert notes.data["items"] == ["Реальное улучшение"]


def test_finished_notes_leave_the_backlog_after_a_day(client, setup_project, monkeypatch):
    from darkfactory.routes import backlog as routes

    project, pool = setup_project
    connect(client, pool)
    texts = ["Открытая", "Задача в работе", "Задача принята", "Отброшена", "Задача удалена"]
    ids = [add(client, project, text) for text in texts]
    with db.transaction() as session:
        for state in ("running", "merged"):
            session.add(
                db.Task(
                    id=state,
                    org_id=project["org_id"],
                    project_id=project["id"],
                    title=state,
                    request="r",
                    branch="b",
                    state=state,
                    updated=1000,
                )
            )
        session.flush()
        changes = [("planned", "running"), ("planned", "merged"), ("dismissed", None), ("planned", "gone")]
        for ident, (state, task) in zip(ids[1:], changes):
            note = session.get(backlog.BacklogItem, ident)
            note.state, note.task_id, note.planned = state, task, 1000

    def shown():
        return {item["text"] for item in client.get(f"/api/projects/{project['id']}/backlog").json()["items"]}

    monkeypatch.setattr(routes, "now", lambda: 1000 + backlog.KEEP_SECONDS - 1)
    assert shown() == set(texts)
    monkeypatch.setattr(routes, "now", lambda: 1000 + backlog.KEEP_SECONDS + 1)
    assert shown() == {"Открытая", "Задача в работе"}
