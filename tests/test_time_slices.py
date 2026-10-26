import pytest
from conftest import connect, make_task

from darkfactory import db, service
from darkfactory.worker import finish_time_slice, release


def test_short_sibling_frees_budget_for_automatic_continuation(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    parent = make_task(client, project)
    child = make_task(client, project)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).max_seconds = 100
        root = session.get(db.Task, parent["id"])
        root.phase, root.state, root.active_seconds = "collect", "waiting_dependency", 5
        current = session.get(db.Task, child["id"])
        current.phase, current.state, current.active_seconds = "implement", "running", 50
        current.heartbeat = current.execution_deadline = db.now()
        current.lease_owner, current.lease_until = "old-slice", db.now() + 45
        session.add(db.Link(task_id=root.id, other_id=current.id, kind="parent"))
        session.flush()
        finish_time_slice(session, current, "Slice exhausted")
        assert current.state == "queued" and current.phase == "implement"
        release(session, current)
    with db.transaction() as session:
        claimed = service.claim(session, 2)
        assert claimed and claimed[0] == child["id"]
        current = session.get(db.Task, child["id"])
        assert 44 < current.time_reserved_seconds <= 45
        assert current.active_seconds >= 50


@pytest.mark.parametrize("phase", ["prepare", "integrate"])
def test_git_deadline_continues_with_unused_group_time(client, setup_project, monkeypatch, phase):
    from darkfactory.gitops import GitError
    from darkfactory.worker import Worker

    project, _ = setup_project
    parent = make_task(client, project)
    child = make_task(client, project)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).max_seconds = 100
        root = session.get(db.Task, parent["id"])
        root.phase, root.state, root.active_seconds = "collect", "waiting_dependency", 5
        current = session.get(db.Task, child["id"])
        current.phase = phase
        session.add(db.Link(task_id=root.id, other_id=current.id, kind="parent"))

    class InterruptedRepository:
        def __init__(self, project, task):
            self.task = task

        def interrupt(self):
            with db.transaction() as session:
                current = session.get(db.Task, self.task.id)
                current.active_seconds = 50
                current.execution_deadline = current.heartbeat = db.now() - 0.01
            raise GitError("Git command deadline exceeded")

        prepare = integrate = interrupt

    class Executor:
        def stop(self, task_id, owner):
            pass

    monkeypatch.setattr("darkfactory.worker.Repository", InterruptedRepository)
    with db.transaction() as session:
        claim = service.claim(session, 2)
    Worker(Executor()).run(*claim)
    with db.Session() as session:
        current = session.get(db.Task, child["id"])
        assert current.state == "queued" and current.phase == phase
        assert current.corrections == 0
