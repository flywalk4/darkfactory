import json
import time

from conftest import make_task
from test_transcript import write_run

from darkfactory import toolchains
from darkfactory.db import ROOT, Session, Task, event
from darkfactory.routes import tasks as task_routes


def test_task_list_pages_hides_old_finished_and_previews_request(client, setup_project):
    project, _ = setup_project
    base = f"/api/projects/{project['id']}/tasks"
    created = [make_task(client, project, title=f"Task {index}", request="x" * 1000) for index in range(5)]
    with Session() as db:
        for index, item in enumerate(created):
            db.get(Task, item["id"]).created = 1000 + index
        old = db.get(Task, created[0]["id"])
        old.state, old.updated = "merged", time.time() - 8 * 86400
        recent = db.get(Task, created[1]["id"])
        recent.state, recent.updated = "cancelled", time.time()
        db.commit()

    listed = client.get(base).json()
    assert [t["title"] for t in listed] == ["Task 4", "Task 3", "Task 2", "Task 1"]
    assert len(listed[0]["request"]) == 300 and listed[0]["request_truncated"] is True
    assert "parent_id" in listed[0] and "promotion_pending" in listed[0]
    assert len(client.get(base, params={"all": 1}).json()) == 5

    first = client.get(base, params={"limit": 2})
    assert [t["title"] for t in first.json()] == ["Task 4", "Task 3"]
    second = client.get(base, params={"limit": 2, "before": first.headers["X-Next-Before"]})
    assert [t["title"] for t in second.json()] == ["Task 2", "Task 1"]
    assert "X-Next-Before" not in second.headers
    assert client.get(base, params={"limit": 0}).status_code == 422
    assert client.get(f"/api/tasks/{created[4]['id']}").json()["request"] == "x" * 1000


def test_task_detail_returns_latest_events_and_pages_older(client, setup_project):
    project, _ = setup_project
    task = make_task(client, project)
    other = make_task(client, project, title="Other")
    with Session() as db:
        row = db.get(Task, task["id"])
        for index in range(120):
            event(db, row, "human_message", f"message {index}")
        db.commit()
    assert client.post(
        f"/api/tasks/{task['id']}/links", json={"other_id": other["id"], "kind": "related_to"}
    ).is_success

    detail = client.get(f"/api/tasks/{task['id']}").json()
    events = detail["events"]
    assert len(events) == task_routes.EVENT_PAGE and detail["events_more"] is True
    assert detail["event_count"] >= 121
    assert events[-2]["message"] == "message 119" and [e["id"] for e in events] == sorted(
        e["id"] for e in events
    )
    assert [r["title"] for r in detail["relations"]] == ["Other"]

    seen = list(events)
    before, more = events[0]["id"], True
    while more:
        page = client.get(f"/api/tasks/{task['id']}/events", params={"before": before, "limit": 30}).json()
        assert len(page["events"]) <= 30
        seen = page["events"] + seen
        before, more = page["events"][0]["id"], page["more"]
    assert len(seen) == detail["event_count"] and seen[0]["kind"] == "created"
    assert len({e["id"] for e in seen}) == len(seen)

    stranger = client.post("/api/organizations", json={"name": "Events org"}).json()
    response = client.get(
        f"/api/tasks/{task['id']}/events", headers={"Authorization": "Bearer " + stranger["token"]}
    )
    assert response.status_code == 404


def test_transcript_cursor_is_a_byte_offset_and_waits_for_partial_lines(client, setup_project, monkeypatch):
    project, _ = setup_project
    task = make_task(client, project)
    base = f"/api/tasks/{task['id']}/transcript"
    directory = write_run(task["id"], "run", "implement", [], 100)
    path = directory / "transcript.jsonl"
    lines = [
        json.dumps({"at": index, "kind": "message", "text": f"line {index}"}) + "\n" for index in range(5)
    ]
    path.write_text("".join(lines) + '{"at": 9, "kind": "mess')

    data = client.get(base).json()
    assert [e["text"] for e in data["entries"]] == [f"line {index}" for index in range(5)]
    assert data["next"] == len("".join(lines)) and data["more"] is False

    with path.open("a") as stream:
        stream.write('age", "text": "done"}\n')
    tail = client.get(base, params={"after": data["next"]}).json()
    assert [e["text"] for e in tail["entries"]] == ["done"] and tail["next"] == path.stat().st_size

    monkeypatch.setattr(task_routes, "TRANSCRIPT_ENTRIES", 2)
    cursor, texts = 0, []
    while True:
        page = client.get(base, params={"after": cursor}).json()
        texts += [e["text"] for e in page["entries"]]
        cursor = page["next"]
        if not page["more"]:
            break
    assert texts == [f"line {index}" for index in range(5)] + ["done"]

    monkeypatch.setattr(task_routes, "TRANSCRIPT_BYTES", 32)
    cursor, texts = 0, []
    for _ in range(100):
        page = client.get(base, params={"after": cursor}).json()
        texts += [e["text"] for e in page["entries"]]
        cursor = page["next"]
        if not page["more"]:
            break
    # Lines longer than the window are skipped instead of stalling the cursor.
    assert cursor == path.stat().st_size and texts == []


def test_transcript_runs_are_cached_until_a_new_run_appears(client, setup_project, monkeypatch):
    project, _ = setup_project
    task = make_task(client, project)
    base = f"/api/tasks/{task['id']}/transcript"
    write_run(task["id"], "first", "plan", [], 100)
    assert [run["generation"] for run in client.get(base).json()["runs"]] == ["first"]
    calls = []
    original = task_routes.run_usage
    monkeypatch.setattr(task_routes, "run_usage", lambda *args: calls.append(args) or original(*args))
    client.get(base)
    assert calls == []
    write_run(task["id"], "second", "implement", [], 200)
    assert [run["generation"] for run in client.get(base).json()["runs"]] == ["first", "second"]


def test_project_settings_cache_follows_saved_settings_and_environment(client, setup_project):
    project, _ = setup_project
    listed = next(p for p in client.get("/api/projects").json() if p["id"] == project["id"])
    assert listed["environment"] == {}
    toolchains._save(project["id"], {"status": "ready", "spec": {"python": "3.12"}})
    listed = next(p for p in client.get("/api/projects").json() if p["id"] == project["id"])
    assert listed["environment"] == {"status": "ready", "spec": {"python": "3.12"}}
    listed["allowed_hosts"].append("mutated.example:443")
    again = next(p for p in client.get("/api/projects").json() if p["id"] == project["id"])
    assert "mutated.example:443" not in again["allowed_hosts"]
    assert (ROOT / "system" / "toolchains").is_dir()


def test_index_sends_content_security_policy(client):
    response = client.get("/")
    policy = response.headers["Content-Security-Policy"]
    assert (
        "default-src 'self'" in policy
        and "frame-ancestors 'none'" in policy
        and "object-src 'none'" in policy
    )
    assert response.headers["X-Frame-Options"] == "DENY"
