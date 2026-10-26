import json

from conftest import connect, make_task
from sqlalchemy import select

from darkfactory import db, director_stops, service
from darkfactory.db import decrypt


class Decides:
    def __init__(self, **decision):
        self.decision = {
            "message": "",
            "extra_corrections": 0,
            "extend_minutes": 0,
            "hosts": [],
            "rework": True,
            **decision,
        }
        self.calls = []

    def run(
        self, task, project, connection, role, heartbeat, timeout, *, prompt_override=None, output_schema=None
    ):
        self.calls.append((role, prompt_override))
        assert heartbeat()
        return {"status": "ok", "text": json.dumps(self.decision)}


def stop(task_id, reason="correction_limit"):
    with db.transaction() as session:
        task = session.get(db.Task, task_id)
        task.state, task.reason, task.phase = "needs_human", reason, "review"
        db.event(session, task, "needs_human", "Исчерпан лимит исправлений")


def kinds(task_id):
    with db.Session() as session:
        return [
            e.kind
            for e in session.scalars(
                select(db.Event).where(db.Event.task_id == task_id).order_by(db.Event.id)
            )
        ]


def test_supervisor_resumes_with_guidance_and_extra_cycle(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    task = make_task(client, project)
    stop(task["id"])
    executor = Decides(decision="resume", message="Проверь обработку пустой ветки", extra_corrections=1)
    assert director_stops.handle_stop(executor)
    role, prompt = executor.calls[0]
    assert role == "director" and "correction_limit" in prompt
    with db.Session() as session:
        row = session.get(db.Task, task["id"])
        assert (row.state, row.phase, row.correction_credit) == ("queued", "implement", 1)
        assert "Решение директора:\nПроверь обработку пустой ветки" in row.request
    assert kinds(task["id"])[-1] == "supervisor"
    assert not director_stops.handle_stop(executor)  # Nothing else stopped.


def test_supervisor_allows_hosts_and_validates_them(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    task = make_task(client, project)
    stop(task["id"], "network_blocked")
    assert director_stops.handle_stop(
        Decides(decision="allow_hosts", hosts=["nuget.example.com:443"], message="Фид NuGet")
    )
    with db.Session() as session:
        config = json.loads(decrypt(session.get(db.Project, project["id"]).secret))
        assert config["allowed_hosts"] == ["nuget.example.com:443"]
        assert session.get(db.Task, task["id"]).state == "queued"
    other = make_task(client, project)
    stop(other["id"], "network_blocked")
    assert director_stops.handle_stop(
        Decides(decision="allow_hosts", hosts=["https://bad/path"], message="x")
    )
    with db.Session() as session:
        assert session.get(db.Task, other["id"]).state == "needs_human"
    assert kinds(other["id"])[-1] == "supervisor_escalated"


def test_escalation_pages_owner_once_and_limit_stops_supervisor(client, setup_project, monkeypatch):
    project, pool = setup_project
    connect(client, pool)
    task = make_task(client, project)
    stop(task["id"])
    executor = Decides(decision="escalate", message="Нужен платный сервис")
    assert director_stops.handle_stop(executor)
    assert not director_stops.handle_stop(executor)  # Same stop is handled once.
    assert kinds(task["id"]).count("supervisor_escalated") == 1

    monkeypatch.setenv("DARKFACTORY_DIRECTOR_LIMIT", "1")
    second = make_task(client, project)
    stop(second["id"])
    assert director_stops.handle_stop(Decides(decision="resume", message="Продолжай"))
    stop(second["id"])
    executor = Decides(decision="resume", message="ещё раз")
    director_stops.handle_stop(executor)
    assert executor.calls == []  # Budget of one intervention per tree is spent.
    with db.Session() as session:
        assert session.get(db.Task, second["id"]).state == "needs_human"
    assert kinds(second["id"])[-1] == "supervisor_escalated"


def test_owner_action_during_supervision_wins(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    task = make_task(client, project)
    stop(task["id"])

    class OwnerActs(Decides):
        def run(self, *args, **kwargs):
            client.post(f"/api/tasks/{task['id']}/actions", json={"action": "resume", "message": "сам"})
            return {"status": "ok", "text": json.dumps(self.decision)}

    assert director_stops.handle_stop(OwnerActs(decision="cancel", message="дубликат"))
    with db.Session() as session:
        assert session.get(db.Task, task["id"]).state == "queued"


def test_infrastructure_stop_resumes_in_place(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    task = make_task(client, project)
    stop(task["id"], reason="Task execution deadline exceeded")
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        row.phase, row.reviewed_sha = "checks", "abc"
    executor = Decides(decision="resume", message="Сбой фабрики, продолжай", rework=False)
    assert director_stops.handle_stop(executor)
    with db.Session() as session:
        row = session.get(db.Task, task["id"])
        assert (row.state, row.phase, row.reviewed_sha) == ("queued", "checks", "abc")
        assert "Сбой фабрики" in row.request


def test_failed_supervisor_run_is_retried_before_paging_owner(client, setup_project, monkeypatch):
    # A worker restart left the old supervisor container holding the task lock; one failure paged the owner.
    project, pool = setup_project
    connect(client, pool)
    task = make_task(client, project)
    stop(task["id"])

    class Fails(Decides):
        def run(self, *args, **kwargs):
            self.calls.append(1)
            with db.transaction() as session:  # The probe run itself records denied hosts.
                db.event(session, session.get(db.Task, task["id"]), "egress_denied", "github.com:443")
            raise RuntimeError("Executor exited without a result (exit 1): BlockingIOError")

    executor = Fails()
    assert director_stops.handle_stop(executor)
    assert not director_stops.handle_stop(executor)  # Waits before retrying.
    assert "supervisor_escalated" not in kinds(task["id"])
    monkeypatch.setattr(director_stops, "RETRY_SECONDS", 0)
    assert director_stops.handle_stop(executor)
    assert "supervisor_escalated" not in kinds(task["id"])
    assert director_stops.handle_stop(executor)
    assert len(executor.calls) == director_stops.RETRIES
    assert kinds(task["id"]).count("supervisor_escalated") == 1
    assert not director_stops.handle_stop(executor)


def test_looping_subtask_out_of_budget_folds_into_waiting_task(client, setup_project, monkeypatch):
    project, pool = setup_project
    connect(client, pool)
    monkeypatch.setenv("DARKFACTORY_DIRECTOR_LIMIT", "0")
    stuck = make_task(client, project, title="Analysis", request="Explain the 404.")
    waiting = make_task(client, project)
    assert (
        client.post(
            f"/api/tasks/{waiting['id']}/links", json={"other_id": stuck["id"], "kind": "depends_on"}
        ).status_code
        == 201
    )
    stop(stuck["id"], "review_repeated")
    executor = Decides(decision="escalate")
    assert not director_stops.handle_stop(executor)
    assert not executor.calls
    assert kinds(stuck["id"])[-1] == "folded"
    with db.transaction() as session:
        assert session.get(db.Task, stuck["id"]).state == "cancelled"
        row = session.get(db.Task, waiting["id"])
        service.refresh_dependencies(session, row)
        assert row.state != "waiting_dependency" and "Explain the 404." in row.request


def test_looping_subtask_without_waiters_still_pages_owner(client, setup_project, monkeypatch):
    project, pool = setup_project
    connect(client, pool)
    monkeypatch.setenv("DARKFACTORY_DIRECTOR_LIMIT", "0")
    task = make_task(client, project)
    stop(task["id"], "review_repeated")
    assert not director_stops.handle_stop(Decides(decision="escalate"))
    assert kinds(task["id"])[-1] == "supervisor_escalated"


def test_supervisor_sends_a_wrong_rejection_back_to_review(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    task = make_task(client, project)
    stop(task["id"], "review_repeated")
    executor = Decides(decision="rereview", message="docs/no-dsl.md:1677 уже исправлен в кандидате")
    assert director_stops.handle_stop(executor)
    with db.Session() as session:
        row = session.get(db.Task, task["id"])
        # No new development run: the writer's work stands and only the verdict is redone.
        assert (row.state, row.phase, row.reviewed_sha) == ("queued", "review", None)
        assert row.correction_credit == 1
        assert "уже исправлен в кандидате" in row.request
    assert kinds(task["id"])[-1] == "supervisor"


def test_escalated_stop_is_looked_at_again_after_a_factory_deploy(client, setup_project, monkeypatch):
    project, pool = setup_project
    connect(client, pool)
    task = make_task(client, project)
    stop(task["id"], "execution_error")
    assert director_stops.handle_stop(Decides(decision="escalate", message="ФАБРИКА: модель недоступна"))
    with db.Session() as session:
        assert session.get(db.Task, task["id"]).state == "needs_human"
    # Same build: the owner already has it, nothing to redo.
    assert not director_stops.handle_stop(Decides(decision="resume", message="x"))
    monkeypatch.setattr(director_stops, "BUILD", "next-build")
    assert director_stops.handle_stop(
        Decides(decision="resume", message="Модель исправлена, продолжай", rework=False)
    )
    with db.Session() as session:
        assert session.get(db.Task, task["id"]).state == "queued"


def test_projects_saved_before_the_rename_keep_their_settings(client, setup_project, monkeypatch):
    from darkfactory.db import decrypt, encrypt

    project, _ = setup_project
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        values = json.loads(decrypt(row.secret) or "{}")
        values.update(supervisor=False, supervisor_limit=4)
        row.secret = encrypt(json.dumps(values))
    with db.Session() as session:
        row = session.get(db.Project, project["id"])
        assert director_stops.autonomous(row) is False and director_stops.limit(row) == 4
    shown = next(p for p in client.get("/api/projects").json() if p["id"] == project["id"])
    assert shown["director_autonomy"] is False and shown["director_limit"] == 4
    monkeypatch.delenv("DARKFACTORY_DIRECTOR_LIMIT", raising=False)
    monkeypatch.setenv("DARKFACTORY_SUPERVISOR_LIMIT", "2")
    assert director_stops.default_limit() == 2


def test_old_projects_get_the_director_stage_once():
    from darkfactory import agents

    settings = agents.default_settings()
    settings["profiles"] = [p for p in settings["profiles"] if p["name"] != "director"]
    del settings["assignments"]["director"]
    assert agents.upgrade_defaults(settings)
    assert settings["assignments"]["director"] == "director"
    assert agents.resolve({"agents": settings}, "director")["name"] == "director"
    assert not agents.upgrade_defaults(settings)


def test_director_journal_lists_decisions_and_open_escalations(client, setup_project, monkeypatch):
    project, pool = setup_project
    connect(client, pool)
    monkeypatch.setenv("DARKFACTORY_DIRECTOR_LIMIT", "2")
    resumed = make_task(client, project)
    stop(resumed["id"])
    assert director_stops.handle_stop(Decides(decision="resume", message="Продолжай"))
    asked = make_task(client, project)
    stop(asked["id"])
    assert director_stops.handle_stop(Decides(decision="escalate", message="Нужен платный сервис"))
    journal = client.get(f"/api/projects/{project['id']}/director-journal").json()
    assert [(row["task_id"], row["kind"], row["open"]) for row in journal] == [
        (asked["id"], "supervisor_escalated", True),
        (resumed["id"], "supervisor", False),
    ]
    assert journal[1]["decision"] == "resume"


def test_director_upgrade_never_breaks_an_owners_team():
    from darkfactory import agents

    settings = agents.default_settings()
    settings["mcp_servers"] = [
        {
            "name": "writer-tool",
            "transport": "http",
            "url": "https://mcp.example/x",
            "enabled_tools": ["a"],
            "read_only": False,
        }
    ]
    for profile in settings["profiles"]:
        if profile["name"] == "director":
            profile["mcp_servers"] = ["writer-tool"]
    del settings["assignments"]["director"]
    before = json.dumps(settings, sort_keys=True)
    assert not agents.upgrade_defaults(settings)
    assert json.dumps(settings, sort_keys=True) == before


def test_director_runs_route_by_the_director_profile(client, setup_project):
    from darkfactory import agents

    project, _ = setup_project
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        config = json.loads(decrypt(row.secret) or "{}")
        for profile in config["agents"]["profiles"]:
            if profile["name"] == "director":
                profile["provider"] = "claude"
        from darkfactory.db import encrypt

        row.secret = encrypt(json.dumps(config))
    with db.Session() as session:
        row = session.get(db.Project, project["id"])
        probe = type("Probe", (), {"phase": "director", "role_config": {}, "explicit_api": False})()
        assert service._routing(probe, row) == ("claude", False)
        assert agents.resolve(json.loads(decrypt(row.secret)), "director")["provider"] == "claude"
