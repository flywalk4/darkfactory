import json
import subprocess
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


LOAD_HARNESS = r"""
const source = require('fs').readFileSync(process.argv[2], 'utf8');
const body = source.slice(source.indexOf('let loadSeq='), source.indexOf('const openParents'));
let projectId = 'A', projects = [], poolList = [], taskList = [], boardConnections = null;
const calls = [], renders = [], pending = {}, nodes = {};
const $ = s => nodes[s] ||= {hidden: true, textContent: '', innerHTML: ''};
const document = {querySelector: () => ({click() {}}), querySelectorAll: () => [], body: {classList: {contains: () => false}}};
const tr = s => s, esc = s => s, remember = () => {}, collapseNav = () => {}, toast = () => {};
const renderBoard = () => renders.push(taskList.map(t => t.id));
const data = {'/projects': [{id: 'A', name: 'A', checks: []}, {id: 'B', name: 'B', checks: []}], '/pools': [], '/connections': []};
function api(path) {
  calls.push(path);
  const match = path.match(/^\/projects\/(\w+)\/tasks$/);
  if (!match) return Promise.resolve(data[path]);
  const tasks = [{id: match[1] + '1', project_id: match[1]}];
  // The first request for project A hangs until the test releases it.
  if (match[1] === 'A' && !pending.A) return new Promise(resolve => { pending.A = () => resolve(tasks); });
  return Promise.resolve(tasks);
}
const tick = () => new Promise(resolve => setTimeout(resolve));
eval(body + `
(async () => {
  const slow = load();
  while (!pending.A) await tick();
  projectId = 'B';
  await load();
  pending.A();
  await slow;
  const afterSwitch = {projectId, tasks: taskList.map(t => t.project_id), renders: renders.slice()};
  calls.length = 0;
  await load({full: false});
  console.log(JSON.stringify({...afterSwitch, pollCalls: calls}));
})();`);
"""


def test_board_load_keeps_only_the_new_project_when_the_switch_happens_mid_request(tmp_path):
    # Runs the real load() from app.js: project A's /tasks reply arrives after the switch to B.
    app = Path(__file__).parent.parent / "darkfactory" / "static" / "app.js"
    harness = tmp_path / "harness.cjs"
    harness.write_text(LOAD_HARNESS)
    run = subprocess.run(["node", str(harness), str(app)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["projectId"] == "B"
    assert result["tasks"] == ["B"]
    assert result["renders"] == [["B1"]]
    # A poll refreshes only the board data, not the project and pool lists.
    assert sorted(result["pollCalls"]) == ["/connections", "/projects/B/tasks"]
