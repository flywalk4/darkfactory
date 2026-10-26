import json

from conftest import make_task
from test_coordination import child_tasks, structured_plan, subtask
from test_planner import plan as planner_plan
from test_planner import subtask as planner_subtask
from test_scheduler import connection, scheduler, task  # noqa: F401

from darkfactory import agents, coordinator, db, runner, service


def test_planner_may_mark_subtasks_light():
    value = planner_plan(
        [{**planner_subtask("docs"), "tier": "light"}, {**planner_subtask("core"), "tier": None}]
    )
    assert runner.plan_object(json.dumps(value))["subtasks"][0]["tier"] == "light"
    value["subtasks"][1]["tier"] = "cheap"
    try:
        runner.plan_object(json.dumps(value))
    except ValueError:
        pass
    else:
        raise AssertionError("unknown tier accepted")


def test_light_subtasks_are_remembered_on_the_child(client, setup_project):
    project, _ = setup_project
    parent = make_task(client, project)
    with db.transaction() as session:
        root = session.get(db.Task, parent["id"])
        assert coordinator.delegate(
            session,
            root,
            structured_plan({**subtask("docs"), "tier": "light"}, {**subtask("core"), "tier": "standard"}),
        )
        children = child_tasks(session, root)
        assert children["docs"].role_config.get("tier") == "light"
        assert "tier" not in children["core"].role_config


def test_light_work_takes_least_loaded_subscription_of_any_provider(scheduler):  # noqa: F811
    session, project, pool = scheduler
    pool.cross_runtime = False
    codex = connection(session, project, pool, "codex", priority=0)
    claude = connection(session, project, pool, "claude", priority=10)
    busy = task(session, project, state="running", phase="implement")
    busy.connection_id = codex.id
    session.flush()
    standard = task(session, project, phase="implement")
    cheap = task(session, project, phase="implement", role_config={"tier": "light"})
    assert service.choose_connection(session, standard, project).id == codex.id
    assert service.choose_connection(session, cheap, project).id == claude.id
    # Independent review keeps the strong model and the project's review runtime.
    cheap.phase = standard.phase = "review"
    assert (
        service.choose_connection(session, cheap, project).id
        == service.choose_connection(session, standard, project).id
    )
    assert not agents.light(cheap)


def test_light_model_respects_pool_override_and_allowlist(scheduler, monkeypatch):  # noqa: F811
    session, project, pool = scheduler
    monkeypatch.delenv("DARKFACTORY_LIGHT_MODEL_CLAUDE", raising=False)
    assert agents.light_model("claude") == "haiku"
    assert agents.light_model("claude", {"light_models": {"claude": "sonnet"}}) == "sonnet"
    pool.policy = {"allowed_models": ["gpt-5.1-codex-mini"], "light_models": {"codex": "gpt-5.1-codex-mini"}}
    connection(session, project, pool, "claude")
    codex = connection(session, project, pool, "codex")
    cheap = task(session, project, phase="implement", role_config={"tier": "light"})
    assert service.choose_connection(session, cheap, project).id == codex.id


def test_codex_keeps_its_own_model_unless_a_light_one_is_configured(monkeypatch):
    monkeypatch.delenv("DARKFACTORY_LIGHT_MODEL_CODEX", raising=False)
    assert agents.light_model("codex") is None
    assert agents.light_model("codex", {"light_models": {"codex": "allowed-mini"}}) == "allowed-mini"
