"""Accepted dependency commits fence both queued evidence and an active executor."""

import pytest
from conftest import connect, make_task
from sqlalchemy import select
from test_pipeline import DeterministicExecutor, drive, git, seed

from darkfactory import db, service
from darkfactory.worker import Worker


def artifact_link(client, task, source):
    response = client.post(
        f"/api/tasks/{task['id']}/links",
        json={"other_id": source["id"], "kind": "depends_on", "artifact": True},
    )
    assert response.status_code == 201, response.text


def test_artifact_without_a_reviewed_commit_never_unblocks(client, setup_project):
    project, _ = setup_project
    source = make_task(client, project)
    dependent = make_task(client, project)
    artifact_link(client, dependent, source)
    with db.transaction() as session:
        session.get(db.Task, source["id"]).state = "review_required"
        task = session.get(db.Task, dependent["id"])
        assert service.dependency_reason(session, task)
        assert service.claim(session, 2) is None
        assert task.state == "waiting_dependency"


def test_new_artifact_commit_repeats_implementation_checks_and_review(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    producer = client.post(
        "/api/projects",
        json={
            "name": "Artifact producer",
            "remote": "https://example.invalid/producer.git",
            "checks": [],
            "merge_without_checks": False,
            "writer_pool": pool["id"],
            "reviewer_pool": pool["id"],
        },
    ).json()
    with db.transaction() as session:
        session.get(db.Project, producer["id"]).remote = str(remote)
    source = make_task(client, producer)

    class RevisingExecutor(DeterministicExecutor):
        revision = 0

        def run(self, task, project, connection, role, heartbeat, timeout):
            if task.id == source["id"] and role == "implement":
                self.revision += 1
                (db.ROOT / "workspaces" / task.id / "contract.txt").write_text(str(self.revision))
            return super().run(task, project, connection, role, heartbeat, timeout)

    executor = RevisingExecutor()
    worker = Worker(executor)
    first_source = drive(worker, source["id"])
    assert first_source.state == "review_required"
    dependent = make_task(client, project)
    artifact_link(client, dependent, source)
    for _ in range(20):
        with db.transaction() as session:
            task = session.get(db.Task, dependent["id"])
            if task.phase == "merge":
                first_inputs = dict(task.dependency_commits)
                counters = task.active_seconds, task.corrections, task.switches
                break
            claimed = service.claim(session, 2)
        assert claimed
        worker.run(*claimed)
    else:
        raise AssertionError("Dependent task did not reach pending merge")
    calls_before = list(executor.calls)
    assert (
        client.post(
            f"/api/tasks/{source['id']}/actions",
            json={"action": "resume", "message": "Update contract to revision 2"},
        ).status_code
        == 200
    )
    with db.transaction() as session:
        task = session.get(db.Task, dependent["id"])
        assert service.refresh_dependencies(session, task)
        assert task.state == "waiting_dependency"
        assert task.verified_sha is None and task.reviewed_sha is None
        assert session.get(db.Project, project["id"]).integration_task is None
    second_source = drive(worker, source["id"])
    assert second_source.candidate_sha != first_source.candidate_sha
    with db.transaction() as session:
        claimed = service.claim(session, 2)
        assert claimed and claimed[0] == dependent["id"]
        task = session.get(db.Task, dependent["id"])
        assert task.phase == "collect"
        assert task.dependency_commits != first_inputs
        assert list(task.dependency_commits.values()) == [second_source.reviewed_sha]
        assert (task.active_seconds, task.corrections, task.switches) == counters
    worker.run(*claimed)
    result = drive(worker, dependent["id"])
    assert result.state == "merged", result.reason
    assert result.result_sha == result.verified_sha == result.reviewed_sha
    assert git(remote, "rev-parse", "preprod") == result.result_sha
    # One new review belongs to producer; another belongs to the rebuilt dependent.
    new_calls = executor.calls[len(calls_before) :]
    assert sum(role == "implement" for role, _ in new_calls) == 2
    # Producer has no checks; dependent reruns checks once: preprod brought nothing new, so the merged
    # result is the checked candidate and the post-integration run is skipped.
    assert sum(role == "checks" for role, _ in new_calls) == 1
    assert sum(role == "review" for role, _ in new_calls) == 2


@pytest.mark.parametrize("stop_during_execution", [True, False])
def test_changing_dependency_stops_active_writer_and_preserves_work(
    client, setup_project, tmp_path, stop_during_execution
):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    source = make_task(client, project, plan=True)
    dependent = make_task(client, project, plan=True)
    artifact_link(client, dependent, source)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
        producer = session.get(db.Task, source["id"])
        producer.state = "review_required"
        producer.candidate_sha = producer.reviewed_sha = "a" * 40

    class InterruptingExecutor(DeterministicExecutor):
        stopped = []

        def stop(self, task_id, owner):
            self.stopped.append(task_id)

        def run(self, task, project, connection, role, heartbeat, timeout):
            if role == "implement":
                (db.ROOT / "workspaces" / task.id / "draft.txt").write_text("Preserve this work")
                with db.transaction() as session:
                    producer = session.get(db.Task, source["id"])
                    producer.state, producer.reviewed_sha = "needs_human", None
                if stop_during_execution:
                    assert not heartbeat()
                    from darkfactory.execution import Stopped

                    raise Stopped("Inputs revoked")
                # A completed remote call can return before the next heartbeat.
                return {"ok": True}
            return super().run(task, project, connection, role, heartbeat, timeout)

    executor = InterruptingExecutor()
    worker = Worker(executor)
    for _ in range(3):
        with db.transaction() as session:
            claimed = service.claim(session, 2)
        assert claimed and claimed[0] == dependent["id"]
        worker.run(*claimed)
    with db.Session() as session:
        task = session.get(db.Task, dependent["id"])
        assert task.state == "waiting_dependency"
        assert task.phase == "collect"
        assert task.lease_owner is None and task.corrections == 0
        assert list(session.scalars(select(db.Connection)))[0].lease_task is None
    assert executor.stopped == ([dependent["id"]] if stop_during_execution else [])
    assert (db.ROOT / "workspaces" / dependent["id"] / "draft.txt").read_text() == "Preserve this work"


@pytest.mark.parametrize("artifact", [False, True])
def test_commit_change_invalidates_old_acceptance_without_resetting_budgets(client, setup_project, artifact):
    project, pool = setup_project
    connect(client, pool)
    source, dependent = make_task(client, project), make_task(client, project)
    assert (
        client.post(
            f"/api/tasks/{dependent['id']}/links",
            json={"other_id": source["id"], "kind": "depends_on", "artifact": artifact},
        ).status_code
        == 201
    )
    with db.transaction() as session:
        producer = session.get(db.Task, source["id"])
        producer.state = "merged"
        producer.result_sha = producer.candidate_sha = producer.reviewed_sha = "a" * 40
        task = session.get(db.Task, dependent["id"])
        service.refresh_dependencies(session, task)
        task.phase = "merge"
        task.candidate_sha = task.verified_sha = task.reviewed_sha = "c" * 40
        task.active_seconds, task.corrections, task.switches = 123, 2, 1
        session.get(db.Project, project["id"]).integration_task = task.id
        producer.result_sha = producer.candidate_sha = producer.reviewed_sha = "b" * 40
        claimed = service.claim(session, 2)
        assert claimed and claimed[0] == task.id
        assert task.phase == "collect"
        assert task.verified_sha is None and task.reviewed_sha is None
        assert list(task.dependency_commits.values()) == ["b" * 40]
        assert (task.active_seconds, task.corrections, task.switches) == (123, 2, 1)
        assert session.get(db.Project, project["id"]).integration_task is None


def test_revoked_dependency_between_heartbeat_and_merge_cannot_reach_git(
    client, setup_project, tmp_path, monkeypatch
):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    source, dependent = make_task(client, project), make_task(client, project)
    artifact_link(client, dependent, source)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
        producer = session.get(db.Task, source["id"])
        producer.state = "review_required"
        producer.candidate_sha = producer.reviewed_sha = "a" * 40
    worker = Worker(DeterministicExecutor())
    for _ in range(20):
        with db.transaction() as session:
            task = session.get(db.Task, dependent["id"])
            pending_merge = task.phase == "merge"
            claimed = service.claim(session, 2)
        assert claimed and claimed[0] == dependent["id"]
        if pending_merge:
            break
        worker.run(*claimed)
    else:
        raise AssertionError("Dependent task did not reach pending merge")
    target = git(remote, "rev-parse", "preprod")
    original_heartbeat = worker.heartbeat

    def revoke_after_heartbeat(*args):
        authorized = original_heartbeat(*args)
        if authorized:
            with db.transaction() as session:
                producer = session.get(db.Task, source["id"])
                producer.state, producer.reviewed_sha = "needs_human", None
        return authorized

    monkeypatch.setattr(worker, "heartbeat", revoke_after_heartbeat)
    worker.run(*claimed)
    assert git(remote, "rev-parse", "preprod") == target
    with db.Session() as session:
        task = session.get(db.Task, dependent["id"])
        assert task.state == "waiting_dependency"
        assert task.phase == "collect"
        assert task.result_sha is None and task.reviewed_sha is None


def test_transitive_artifact_cannot_hide_a_changed_upstream_commit(client, setup_project):
    project, _ = setup_project
    source, middle, dependent = [make_task(client, project) for _ in range(3)]
    artifact_link(client, middle, source)
    artifact_link(client, dependent, middle)
    with db.transaction() as session:
        producer, intermediate, task = [
            session.get(db.Task, item["id"]) for item in (source, middle, dependent)
        ]
        producer.state = intermediate.state = "review_required"
        producer.candidate_sha = producer.reviewed_sha = "a" * 40
        service.refresh_dependencies(session, intermediate)
        intermediate.candidate_sha = intermediate.reviewed_sha = "b" * 40
        assert service.refresh_dependencies(session, task) is None
        assert not service.dependencies_changed(session, task)
        task.phase = "merge"
        task.verified_sha = task.reviewed_sha = "c" * 40
        producer.candidate_sha = producer.reviewed_sha = "d" * 40
        assert service.dependencies_changed(session, task)
        assert service.refresh_dependencies(session, task)
        assert task.state == "waiting_dependency" and task.phase == "collect"
        assert task.verified_sha is None and task.reviewed_sha is None
        # The intermediate artifact's SHA did not change; its inputs did.
        assert intermediate.candidate_sha == intermediate.reviewed_sha == "b" * 40


def link(client, task, other, kind="depends_on"):
    response = client.post(f"/api/tasks/{task['id']}/links", json={"other_id": other["id"], "kind": kind})
    assert response.status_code == 201, response.text


def test_cancelled_dependency_is_dropped_and_its_goal_handed_over(client, setup_project):
    project, _ = setup_project
    source = make_task(client, project, title="Reproduce", request="Find where the branch comes from.")
    dependent = make_task(client, project)
    link(client, dependent, source)
    with db.transaction() as session:
        session.get(db.Task, source["id"]).state = "cancelled"
        task = session.get(db.Task, dependent["id"])
        assert not service.refresh_dependencies(session, task)
        assert task.state != "waiting_dependency"
        assert "Find where the branch comes from." in task.request
        assert not session.scalars(select(db.Link).where(db.Link.task_id == task.id)).all()
        kinds = [e.kind for e in session.scalars(select(db.Event).where(db.Event.task_id == task.id))]
        assert "dependency_dropped" in kinds


def test_duplicate_dependency_is_dropped_like_cancelled(client, setup_project):
    project, _ = setup_project
    source = make_task(client, project, title="Twin", request="Collect the logs.")
    dependent = make_task(client, project)
    link(client, dependent, source, kind="depends_on")
    with db.transaction() as session:
        duplicate = session.get(db.Task, source["id"])
        duplicate.state = "duplicate"
        duplicate.reason = "same as another task"
        task = session.get(db.Task, dependent["id"])
        service.release_dropped_dependencies(session, task)
        assert "Collect the logs." in task.request
        assert "same as another task" in task.request
        assert not session.scalars(select(db.Link).where(db.Link.task_id == task.id)).all()
