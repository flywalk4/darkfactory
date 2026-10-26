import pytest

from darkfactory import db, service, worker
from tests.test_groups import make_task


@pytest.fixture
def group_session(setup_project):
    project, pool = setup_project
    with db.Session() as session:
        yield session, session.get(db.Project, project["id"]), session.get(db.Pool, pool["id"])


def running(session, project, **values):
    return make_task(session, project, state="running", phase="implement", **values)


def test_tree_without_progress_waits_for_human(group_session):
    session, project, _ = group_session
    project.max_seconds = 100
    task = running(session, project, active_seconds=100)
    worker.finish_time_slice(session, task, "Исчерпано активное время дерева задач")
    assert task.state == "needs_human"
    assert task.time_credit == 0


def test_progress_extends_tree_until_ceiling(group_session, monkeypatch):
    session, project, _ = group_session
    project.max_seconds = 100
    monkeypatch.setenv("DARKFACTORY_MAX_TREE_SECONDS", "2000")
    task = running(session, project, active_seconds=100)
    db.event(session, task, "phase_finished", "implement", status="ok")
    worker.finish_time_slice(session, task, "Исчерпано активное время дерева задач")
    assert task.state == "queued"
    assert task.time_credit == 1800
    task.state, task.active_seconds = "running", 1900
    db.event(session, task, "phase_finished", "review", status="ok")
    worker.finish_time_slice(session, task, "Исчерпано активное время дерева задач")
    assert task.state == "queued"
    assert service.execution_limits(session, task)["time_budget"] == 2000
    task.state, task.active_seconds = "running", 2000
    db.event(session, task, "phase_finished", "review", status="ok")
    worker.finish_time_slice(session, task, "Исчерпано активное время дерева задач")
    assert task.state == "needs_human"


def test_progress_must_be_new_since_last_extension(group_session):
    session, project, _ = group_session
    project.max_seconds = 100
    task = running(session, project, active_seconds=100)
    db.event(session, task, "phase_finished", "implement", status="ok")
    worker.finish_time_slice(session, task, "limit")
    task.state, task.active_seconds = "running", 1900
    worker.finish_time_slice(session, task, "limit")
    assert task.state == "needs_human"


def test_long_tree_warns_once_per_budget(group_session):
    session, project, _ = group_session
    project.max_seconds = 1000
    task = running(session, project, active_seconds=810)
    worker.warn_budget(session, task)
    worker.warn_budget(session, task)
    warnings = session.query(db.Event).filter_by(task_id=task.id, kind="budget_warning").all()
    assert len(warnings) == 1
    assert "13 из 16 мин" in warnings[0].message


def test_subtasks_bring_their_own_allowance(group_session):
    session, project, _ = group_session
    project.max_seconds = 100
    root = make_task(session, project)
    for _ in range(3):
        make_task(session, project, parent=root, time_credit=project.max_seconds)
    assert service.execution_limits(session, root)["time_budget"] == 400


def test_live_run_is_extended_instead_of_stopped(group_session, monkeypatch):
    session, project, _ = group_session
    project.max_seconds = 100
    monkeypatch.setenv("DARKFACTORY_MAX_TREE_SECONDS", "2000")
    stamp = db.now()
    task = running(
        session,
        project,
        active_seconds=90,
        lease_owner="owner",
        heartbeat=stamp,
        execution_deadline=stamp + 10,
        time_reserved_seconds=10,
    )
    db.event(session, task, "phase_finished", "plan", status="ok")
    session.commit()
    remaining = worker.Worker(executor=object()).heartbeat(task.id, "owner")
    # The executor keeps the same container alive with the new deadline.
    assert remaining is not True and remaining > 1000
    session.expire_all()
    assert session.get(db.Task, task.id).time_credit == 1800
    assert session.query(db.Event).filter_by(task_id=task.id, kind="time_extended").count() == 1
    assert worker.Worker(executor=object()).heartbeat(task.id, "owner") > 1000
    assert session.query(db.Event).filter_by(task_id=task.id, kind="time_extended").count() == 1


def test_extension_reaches_the_running_phase_copy(client, setup_project, tmp_path, monkeypatch):
    from tests.conftest import connect
    from tests.test_pipeline import DeterministicExecutor, seed
    from tests.test_pipeline import make_task as api_task

    project, pool = setup_project
    connect(client, pool, "codex", "writer")
    connect(client, pool, "claude", "reviewer")
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(seed(tmp_path))
        session.get(db.Project, project["id"]).max_seconds = 100
    api_task(client, project)
    seen = []

    class Executor(DeterministicExecutor):
        def run(self, task, project, connection, role, heartbeat, timeout):
            heartbeat()
            seen.append(task.execution_deadline - db.now())
            return super().run(task, project, connection, role, heartbeat, timeout)

    # The worker reports a long remaining time as if an extension had just been granted.
    monkeypatch.setattr(worker.Worker, "heartbeat", lambda self, task_id, owner: 5000.0)
    runner = worker.Worker(Executor())
    for _ in range(5):
        with db.transaction() as session:
            claimed = service.claim(session, 1)
        if seen or not claimed:
            break
        runner.run(*claimed)
    assert seen and seen[0] > 1000


def test_extension_reaches_the_runner_alarm(tmp_path):
    from darkfactory import execution, runner

    assert runner.extension(tmp_path / "deadline-g") == 0
    execution.extend_runner(tmp_path, "g", execution.time.time() + 600)
    assert 590 < runner.extension(tmp_path / "deadline-g") <= 600
