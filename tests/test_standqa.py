import json

from conftest import connect
from sqlalchemy import select

from darkfactory import db, standqa

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class StandAgent:
    def __init__(self, report):
        self.report = report
        self.calls = []

    def run(
        self, task, project, connection, role, heartbeat, timeout, *, prompt_override=None, output_schema=None
    ):
        self.calls.append((role, prompt_override, output_schema))
        run = db.ROOT / "artifacts" / task.id / "browser" / f"gen{len(self.calls)}"
        run.mkdir(parents=True, exist_ok=True)
        (run / "execution.json").write_text("{}")
        (run / "login-error.png").write_bytes(PNG)
        if isinstance(self.report, Exception):
            raise self.report
        return {"status": "ok", "text": json.dumps(self.report)}


def bug(title, severity="high", screenshots=("login-error.png",)):
    return {
        "title": title,
        "severity": severity,
        "url": "https://studio.example.invalid/login",
        "steps": "1. Open /login\n2. Sign in as $E2E_USER",
        "expected": "Workspace list",
        "actual": "500 Internal Server Error",
        "area": "Builder API /api/workspaces, src/frontend/studio/login",
        "screenshots": list(screenshots),
    }


def report(*bugs, verified=()):
    return {
        "summary": "Signed in, opened workspaces",
        "checked": ["login"],
        "verified": list(verified),
        "bugs": list(bugs),
    }


def configure(client, project, **extra):
    response = client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": project["name"],
            "remote": project["remote"],
            "checks": project["checks"],
            "stand_url": "https://studio.example.invalid/",
            "stand_hosts": ["auth.example.invalid:443"],
            "test_env": {"E2E_USER": "qa-user", "E2E_PASSWORD": "secret-password"},
            "qa_instructions": "Проверяй редактор страниц",
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def project_tasks(project_id):
    with db.Session() as session:
        return list(
            session.scalars(select(db.Task).where(db.Task.project_id == project_id).order_by(db.Task.created))
        )


def test_timer_runs_tester_and_turns_reproduced_bugs_into_fix_tasks(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    assert not standqa.run(StandAgent(report()))  # No stand configured.
    view = configure(client, project)
    assert view["stand_url"] == "https://studio.example.invalid/" and view["qa_hours"] == 4

    tester = StandAgent(
        report(
            bug("Вход падает с 500"),
            bug("Вход  падает с 500!"),  # Same root cause, worded differently.
            bug("Обрезан заголовок", severity="low", screenshots=("../../etc/passwd",)),
        )
    )
    assert standqa.run(tester)
    role, prompt, schema = tester.calls[0]
    assert role == "qa" and schema == standqa.SCHEMA
    assert "https://studio.example.invalid/" in prompt and "E2E_PASSWORD" in prompt
    assert "secret-password" not in prompt and "Проверяй редактор страниц" in prompt
    tasks = project_tasks(project["id"])
    assert [(task.title, task.priority) for task in tasks] == [
        ("Вход падает с 500", "high"),
        ("Обрезан заголовок", "low"),
    ]
    assert "500 Internal Server Error" in tasks[0].request
    assert [path.name for path in (db.ROOT / "artifacts" / tasks[0].id / "attachments").iterdir()] == [
        "01-qa-login-error.png"
    ]
    assert not (db.ROOT / "artifacts" / tasks[1].id / "attachments").exists()
    with db.Session() as session:
        assert session.query(db.Event).filter_by(task_id=tasks[0].id, kind="qa_reported").count() == 1
        state = standqa.state(session.get(db.Project, project["id"]))
    assert state["tasks"] == 2 and state["checked"] == ["login"] and not state.get("running")

    # Not due again until the interval passes; the next run is told what is already open.
    assert not standqa.run(tester)
    client.post(f"/api/projects/{project['id']}/qa")
    assert standqa.run(tester)
    assert "Вход падает с 500" in tester.calls[1][1] and "covered: login" in tester.calls[1][1]
    assert len(project_tasks(project["id"])) == 2


def test_after_deploy_tester_rechecks_fixed_bugs(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    configure(client, project, qa_hours=0)
    client.post(f"/api/projects/{project['id']}/qa")
    assert standqa.run(StandAgent(report(bug("Вход падает с 500"), bug("Пустой список"))))
    fixed, broken = project_tasks(project["id"])
    with db.transaction() as session:
        for task in session.scalars(select(db.Task).where(db.Task.project_id == project["id"])):
            task.state = "merged"
        assert standqa.request(session, session.get(db.Project, project["id"]), "deploy", sha="b" * 40)

    tester = StandAgent(
        report(
            verified=[
                {"task_id": fixed.id, "fixed": True, "note": "Вход работает"},
                {"task_id": broken.id, "fixed": False, "note": "Список всё ещё пуст"},
            ]
        )
    )
    assert standqa.run(tester)
    assert fixed.id in tester.calls[0][1] and "re-check" in tester.calls[0][1]
    tasks = {task.id: task for task in project_tasks(project["id"])}
    assert tasks[fixed.id].role_config["qa"]["verified"] is True
    assert tasks[broken.id].role_config["qa"]["verified"] is False
    again = next(task for task in tasks.values() if task.role_config["qa"].get("again") == broken.id)
    assert again.state == "queued" and "Список всё ещё пуст" in again.request
    with db.Session() as session:
        assert (
            session.query(db.Link).filter_by(task_id=again.id, other_id=broken.id, kind="related_to").count()
        )
    # Verified bugs are not offered again; with the timer off nothing runs until the next deploy or click.
    assert not standqa.run(tester)


def test_failed_run_answers_the_request_and_shows_the_error(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    configure(client, project, qa_hours=0)
    client.post(f"/api/projects/{project['id']}/qa")
    assert standqa.run(StandAgent(RuntimeError("login page did not load")))
    state = client.get("/api/projects").json()[0]["health"]["qa"]
    assert "login page did not load" in state["error"] and not state.get("requested")
    assert not standqa.run(StandAgent(report()))


def test_tester_profile_reaches_only_the_stand_through_the_task_proxy(client, setup_project):
    project, _ = setup_project
    profile = standqa.profile(
        {
            "stand_url": "https://studio.example.invalid/",
            "stand_hosts": ["auth.example.invalid:443", "api.example.invalid:8443"],
        }
    )
    server = profile["mcp_definitions"][0]
    assert server["hosts"] == [
        "studio.example.invalid:443",
        "auth.example.invalid:443",
        "api.example.invalid:8443",
    ]
    args = server["args"]
    assert args[args.index("--proxy-server") + 1] == "${HTTPS_PROXY}"
    assert args[args.index("--allowed-origins") + 1] == (
        "https://studio.example.invalid;https://auth.example.invalid;https://api.example.invalid:8443"
    )
    refused = client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": "Pilot",
            "remote": project["remote"],
            "checks": project["checks"],
            "stand_url": "ftp://x",
        },
    )
    assert refused.status_code == 400
    assert client.post(f"/api/projects/{project['id']}/qa").status_code == 409
