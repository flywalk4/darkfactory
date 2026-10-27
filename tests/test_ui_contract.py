import json
from pathlib import Path

from darkfactory import db, service


def test_director_limit_defaults_to_the_backend_value(client, setup_project):
    project, _ = setup_project
    assert project["director_limit"] == 6
    # A save that leaves the limit out keeps the backend default instead of storing an old UI value.
    saved = client.put(f"/api/projects/{project['id']}", json={**project, "director_limit": None}).json()
    assert saved["director_limit"] == 6
    saved = client.put(f"/api/projects/{project['id']}", json={**project, "director_limit": 9}).json()
    assert saved["director_limit"] == 9


def test_board_and_task_page_say_when_the_director_will_decide(client, setup_project):
    project, _ = setup_project
    task = client.post(f"/api/projects/{project['id']}/tasks", json={"title": "Stop", "request": "r"}).json()
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        row.state, row.phase, row.reason = "needs_human", "review", "correction_limit"
        db.event(session, row, "limit", "Исчерпаны циклы исправления")
    board = client.get(f"/api/projects/{project['id']}/tasks").json()
    assert next(t for t in board if t["id"] == task["id"])["awaits_director"] is True
    assert client.get(f"/api/tasks/{task['id']}").json()["awaits_director"] is True


def test_review_verdict_is_served_and_hold_is_reported(client, setup_project, monkeypatch, tmp_path):
    project, _ = setup_project
    task = client.post(
        f"/api/projects/{project['id']}/tasks", json={"title": "Review", "request": "r"}
    ).json()
    artifacts = db.ROOT / "artifacts" / task["id"]
    artifacts.mkdir(parents=True, exist_ok=True)
    verdict = {"approved": False, "findings": ["a.py:3 — bug"], "verdict": {"checklist": {}, "findings": []}}
    (artifacts / "review.json").write_text(json.dumps(verdict))
    assert client.get(f"/api/tasks/{task['id']}/artifacts/review.json").json() == verdict
    hold = tmp_path / "hold"
    monkeypatch.setattr(service, "HOLD", hold)
    assert client.get("/api/me").json()["held"] is False
    hold.write_text("")
    assert client.get("/api/me").json()["held"] is True


def test_board_says_what_a_waiting_task_waits_for(client, setup_project):
    import time

    project, _ = setup_project
    task = client.post(f"/api/projects/{project['id']}/tasks", json={"title": "Held", "request": "r"}).json()
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        row.role_config = {**(row.role_config or {}), "wait_files_until": time.time() + 60}
        db.event(
            session,
            row,
            "deferred",
            "Ждёт: другая задача правит те же файлы — src/a/Executor.cs, src/b.ts, c.py",
        )
    board = client.get(f"/api/projects/{project['id']}/tasks").json()
    assert (
        next(t for t in board if t["id"] == task["id"])["waiting_for"]
        == "свободные файлы (Executor.cs, b.ts)"
    )
    assert (
        client.get(f"/api/tasks/{task['id']}").json()["waiting_for"] == "свободные файлы (Executor.cs, b.ts)"
    )


def test_muted_review_notes_can_be_listed_and_removed(client, setup_project):
    project, _ = setup_project
    client.post(f"/api/projects/{project['id']}/review-muted", json={"text": "Rename variables"})
    shown = next(p for p in client.get("/api/projects").json() if p["id"] == project["id"])
    assert shown["review_muted"] == ["Rename variables"]
    saved = client.put(f"/api/projects/{project['id']}", json={**shown, "review_muted": []}).json()
    assert saved["review_muted"] == []


def test_board_names_who_asked_for_each_task(client, setup_project):
    project, _ = setup_project
    me = client.get("/api/me").json()
    task = client.post(f"/api/projects/{project['id']}/tasks", json={"title": "Mine", "request": "r"}).json()
    assert task["created_by"] == me["id"]
    with db.transaction() as session:
        session.add(
            db.Task(
                id="factory1",
                project_id=project["id"],
                org_id=me["org_id"],
                title="Auto",
                request="r",
                branch="b",
            )
        )
    board = {t["id"]: t for t in client.get(f"/api/projects/{project['id']}/tasks").json()}
    assert board[task["id"]]["author"] == me["name"]
    # Work the factory started itself has no author, and the board says so instead of guessing.
    assert board["factory1"]["created_by"] is None and board["factory1"]["author"] is None


def test_board_load_drops_responses_of_a_previous_project_or_load():
    source = (Path(__file__).parent.parent / "darkfactory" / "static" / "app.js").read_text()
    body = source[source.index("async function load()") : source.index("const openParents")]
    assert "const seq=++loadSeq" in body
    # A late reply must not touch the list once another load started or the project changed.
    assert "if(seq!==loadSeq||projectId!==(project?.id||''))return;" in body
    assert body.index("return;") < body.index("taskList=nextTasks")
    assert "/projects/${project.id}/tasks" in body
