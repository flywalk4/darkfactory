"""Project revisions fence execution and remain isolated by project and role."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from conftest import connect, make_task
from sqlalchemy import select
from test_pipeline import DeterministicExecutor, drive, git, seed

from darkfactory import coordinator, db, execution, registry, service
from darkfactory.worker import Worker


def document(client, project, **values):
    response = client.post(
        f"/api/projects/{project['id']}/artifacts",
        json={
            "key": "api-contract",
            "title": "Контракт API",
            "kind": "contract",
            "content": "Revision one",
            **values,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def accept(client, artifact, version=None):
    version = version or artifact["versions"][0]
    response = client.post(f"/api/project-artifacts/{artifact['id']}/versions/{version['id']}/accept")
    assert response.status_code == 200, response.text
    return response.json()


def revise(client, artifact, **values):
    response = client.post(
        f"/api/project-artifacts/{artifact['id']}/versions",
        json={
            "content": "Revision two",
            **values,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def bind(client, task, artifact, version):
    return client.put(
        f"/api/tasks/{task['id']}/artifact-inputs/{artifact['id']}",
        json={
            "version_id": version["id"],
        },
    )


def test_immutable_history_explicit_inputs_and_consumers(client, setup_project):
    project, _ = setup_project
    artifact = document(client, project, content="Literal <script>alert(1)</script> & Markdown")
    old = artifact["versions"][0]
    assert old["status"] == "draft"
    task = make_task(client, project, artifact_version_ids=[old["id"]])
    assert task["state"] == "waiting_dependency"
    accept(client, artifact)
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        assert service.refresh_dependencies(session, row) is None
        assert row.dependency_commits == {"artifact:" + artifact["id"]: old["id"]}
    new = revise(client, artifact)
    listed = client.get(f"/api/projects/{project['id']}/artifacts").json()[0]
    assert [v["number"] for v in listed["versions"]] == [2, 1]
    assert listed["accepted_version_id"] == old["id"]
    assert listed["consumers"][0]["task_id"] == task["id"]
    assert not listed["consumers"][0]["stale"]
    accept(client, artifact, new)
    selected = client.get(f"/api/tasks/{task['id']}/artifact-inputs").json()[0]
    assert selected["id"] == old["id"] and selected["status"] == "superseded"
    assert selected["blocked_reason"]
    assert bind(client, task, artifact, new).status_code == 200
    assert client.get(f"/api/tasks/{task['id']}/artifact-inputs").json()[0]["id"] == new["id"]
    old_url = f"/api/project-artifacts/{artifact['id']}/versions/{old['id']}"
    assert client.get(old_url).json()["content"] == "Literal <script>alert(1)</script> & Markdown"
    assert client.put(old_url, json={"content": "mutated"}).status_code == 405
    assert client.delete(old_url).status_code == 405
    assert client.post(f"/api/project-artifacts/{artifact['id']}/revoke").status_code == 200
    assert client.get(f"/api/tasks/{task['id']}/artifact-inputs").json()[0]["status"] == "revoked"
    assert client.delete(f"/api/tasks/{task['id']}/artifact-inputs/{artifact['id']}").status_code == 200
    assert client.get(f"/api/tasks/{task['id']}/artifact-inputs").json() == []


def test_supersession_resets_evidence_and_keeps_budgets_and_manual_stop(client, setup_project):
    project, _ = setup_project
    artifact = document(client, project)
    accept(client, artifact)
    task = make_task(client, project, artifact_version_ids=[artifact["versions"][0]["id"]])
    stopped = make_task(client, project, artifact_version_ids=[artifact["versions"][0]["id"]])
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        row.phase, row.candidate_sha = "merge", "c" * 40
        row.verified_sha = row.reviewed_sha = row.candidate_sha
        row.active_seconds, row.corrections, row.switches = 123, 2, 1
        session.get(db.Project, project["id"]).integration_task = row.id
        human = session.get(db.Task, stopped["id"])
        human.state, human.reason = "needs_human", "Бюджет исчерпан"
    second = revise(client, artifact)
    accept(client, artifact, second)
    with db.Session() as session:
        row = session.get(db.Task, task["id"])
        assert row.phase == "collect" and row.state == "waiting_dependency"
        assert row.verified_sha is row.reviewed_sha is None
        assert (row.active_seconds, row.corrections, row.switches) == (123, 2, 1)
        assert session.get(db.Project, project["id"]).integration_task is None
    assert bind(client, stopped, artifact, second).status_code == 200
    result = client.get(f"/api/tasks/{stopped['id']}").json()
    assert result["state"] == "needs_human" and result["reason"] == "Бюджет исчерпан"


def test_source_commit_and_transitive_input_freshness(client, setup_project):
    project, _ = setup_project
    upstream = document(client, project, key="requirements")
    accept(client, upstream)
    source = make_task(client, project, artifact_version_ids=[upstream["versions"][0]["id"]])
    # An unfinished or unreviewed task cannot certify a project document.
    response = client.post(
        f"/api/projects/{project['id']}/artifacts",
        json={
            "key": "invalid",
            "title": "Invalid",
            "content": "Data",
            "source_task_id": source["id"],
        },
    )
    assert response.status_code == 409
    with db.transaction() as session:
        row = session.get(db.Task, source["id"])
        row.state, row.candidate_sha, row.reviewed_sha = "review_required", "a" * 40, "a" * 40
    artifact = document(client, project, source_task_id=source["id"])
    accept(client, artifact)
    consumer = make_task(client, project, artifact_version_ids=[artifact["versions"][0]["id"]])
    assert artifact["versions"][0]["source_sha"] == "a" * 40
    accept(client, upstream, revise(client, upstream))
    selected = client.get(f"/api/tasks/{consumer['id']}/artifact-inputs").json()[0]
    assert selected["status"] == "stale" and selected["blocked_reason"]
    with db.Session() as session:
        assert service.dependencies_changed(session, session.get(db.Task, consumer["id"]))
    assert (
        client.post(
            f"/api/project-artifacts/{artifact['id']}/versions/{artifact['versions'][0]['id']}/accept"
        ).status_code
        == 409
    )


def test_scope_roles_validation_and_atomic_task_creation(client, setup_project):
    project, _ = setup_project
    other = client.post(
        "/api/projects",
        json={
            "name": "Other",
            "remote": "https://example.invalid/x",
            "initialize": False,
            "prepare_environment": False,
        },
    ).json()
    artifact = document(client, project)
    old = artifact["versions"][0]
    task = make_task(client, project)
    foreign_task = make_task(client, other)
    assert bind(client, foreign_task, artifact, old).status_code == 404
    response = client.post(
        f"/api/projects/{other['id']}/tasks",
        json={
            "title": "Must rollback",
            "request": "x",
            "artifact_version_ids": [old["id"]],
        },
    )
    assert response.status_code == 404
    assert len(client.get(f"/api/projects/{other['id']}/tasks").json()) == 1
    assert (
        client.post(f"/api/project-artifacts/{artifact['id']}/versions", json={"content": " "}).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/project-artifacts/{artifact['id']}/versions", json={"content": "x" * 100001}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/projects/{project['id']}/artifacts",
            json={
                "key": "api-contract",
                "title": "Duplicate",
                "content": "x",
            },
        ).status_code
        == 409
    )
    operator = client.post("/api/tokens", json={"role": "operator", "project_ids": [project["id"]]}).json()
    observer = client.post("/api/tokens", json={"role": "observer", "project_ids": [project["id"]]}).json()
    scoped = client.post("/api/tokens", json={"role": "owner", "project_ids": [other["id"]]}).json()
    client.headers["Authorization"] = "Bearer " + operator["token"]
    assert (
        client.post(f"/api/project-artifacts/{artifact['id']}/versions/{old['id']}/accept").status_code == 403
    )
    assert client.post(f"/api/project-artifacts/{artifact['id']}/revoke").status_code == 403
    revise(client, artifact)
    assert bind(client, task, artifact, old).status_code == 200
    client.headers["Authorization"] = "Bearer " + observer["token"]
    assert client.get(f"/api/project-artifacts/{artifact['id']}/versions/{old['id']}").status_code == 200
    assert bind(client, task, artifact, old).status_code == 403
    client.headers["Authorization"] = "Bearer " + scoped["token"]
    assert client.get(f"/api/projects/{project['id']}/artifacts").status_code == 404
    assert client.get(f"/api/project-artifacts/{artifact['id']}/versions/{old['id']}").status_code == 404
    assert client.get(f"/api/tasks/{task['id']}/artifact-inputs").status_code == 404


def test_cycles_combine_task_links_and_registry_sources(client, setup_project):
    project, _ = setup_project
    source, consumer = make_task(client, project), make_task(client, project)
    with db.transaction() as session:
        row = session.get(db.Task, source["id"])
        row.state, row.candidate_sha, row.reviewed_sha = "review_required", "a" * 40, "a" * 40
    artifact = document(client, project, source_task_id=source["id"])
    accept(client, artifact)
    assert bind(client, consumer, artifact, artifact["versions"][0]).status_code == 200
    assert (
        client.post(
            f"/api/tasks/{source['id']}/links",
            json={
                "other_id": consumer["id"],
                "kind": "depends_on",
            },
        ).status_code
        == 409
    )
    assert client.post(f"/api/tasks/{source['id']}/actions", json={"action": "resume"}).status_code == 200
    assert bind(client, source, artifact, artifact["versions"][0]).status_code == 409
    # Reverse creation order must also reject the same mixed cycle.
    child = make_task(client, project)
    assert (
        client.post(
            f"/api/tasks/{source['id']}/links",
            json={
                "other_id": child["id"],
                "kind": "depends_on",
            },
        ).status_code
        == 201
    )
    assert bind(client, child, artifact, artifact["versions"][0]).status_code == 409


def test_delegated_tasks_inherit_exact_versions_and_stop_on_supersession(client, setup_project):
    project, _ = setup_project
    artifact = document(client, project)
    accept(client, artifact)
    parent = make_task(client, project, artifact_version_ids=[artifact["versions"][0]["id"]])
    with db.transaction() as session:
        assert coordinator.delegate(
            session,
            session.get(db.Task, parent["id"]),
            {
                "plan": "One bounded task",
                "questions": [],
                "subtasks": [
                    {
                        "key": "writer",
                        "title": "Writer",
                        "request": "Use the contract",
                        "acceptance": "Contract met",
                        "depends_on": [],
                    }
                ],
            },
        )
        child = session.scalars(select(db.Task).where(db.Task.plan_depth == 1)).one()
        child_id = child.id
        assert registry.snapshot(session, child) == {
            "artifact:" + artifact["id"]: artifact["versions"][0]["id"]
        }
        assert "Revision one" in registry.context(session, child)
    accept(client, artifact, revise(client, artifact))
    selected = client.get(f"/api/tasks/{child_id}/artifact-inputs").json()[0]
    assert selected["id"] == artifact["versions"][0]["id"] and selected["blocked_reason"]
    assert client.get(f"/api/tasks/{child_id}").json()["state"] == "waiting_dependency"


def test_schema_upgrade_preserves_existing_projects_and_tasks(client, setup_project):
    project, _ = setup_project
    task = make_task(client, project)
    for table in (db.ArtifactInput.__table__, db.ArtifactVersion.__table__, db.Artifact.__table__):
        table.drop(db.engine)
    db.initialize()
    db.initialize()
    assert client.get(f"/api/tasks/{task['id']}").json()["project_id"] == project["id"]
    assert document(client, project)["versions"][0]["number"] == 1


def test_combined_input_limit_rejects_partial_task_creation(client, setup_project):
    project, _ = setup_project
    documents = [document(client, project, key=f"spec-{index}", content="x" * 80_000) for index in range(3)]
    response = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={
            "title": "Too much context",
            "request": "Build",
            "artifact_version_ids": [item["versions"][0]["id"] for item in documents],
        },
    )
    assert response.status_code == 400
    assert client.get(f"/api/projects/{project['id']}/tasks").json() == []


@pytest.mark.parametrize("override", [None, "Director prompt"])
def test_executor_receives_exact_selected_document(client, setup_project, monkeypatch, override):
    project, _ = setup_project
    content = "HEAD\n" + "Spec line.\n" * 5000 + "TAIL"
    artifact = document(client, project, content=content)
    accept(client, artifact)
    task = make_task(client, project, artifact_version_ids=[artifact["versions"][0]["id"]])
    seen = []

    class Captured(Exception):
        pass

    def create(_image, **kwargs):
        job = db.ROOT / "jobs" / kwargs["labels"]["darkfactory.generation"] / "input.json"
        seen.append(json.loads(job.read_text())["prompt"])
        raise Captured()

    monkeypatch.setattr(
        execution.docker, "from_env", lambda: SimpleNamespace(containers=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(execution, "egress_proxy", lambda *args, **kwargs: nullcontext())
    executor = execution.DockerExecutor()
    with db.Session() as session, pytest.raises(Captured):
        executor.run(
            session.get(db.Task, task["id"]),
            session.get(db.Project, project["id"]),
            None,
            "implement",
            lambda: True,
            60,
            prompt_override=override,
        )
    assert len(seen) == 1 and content in seen[0]
    assert f"revision: {artifact['versions'][0]['id']}" in seen[0]


@pytest.mark.parametrize("stop_during_execution", [True, False])
def test_acceptance_change_fences_active_and_late_executor_results(
    client, setup_project, tmp_path, stop_during_execution
):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    artifact = document(client, project)
    accept(client, artifact)
    next_version = revise(client, artifact)
    task = make_task(client, project, plan=True, artifact_version_ids=[artifact["versions"][0]["id"]])

    class RevisingExecutor(DeterministicExecutor):
        stopped = []

        def stop(self, task_id, owner):
            self.stopped.append(task_id)

        def run(self, row, project, connection, role, heartbeat, timeout):
            if role == "implement":
                (db.ROOT / "workspaces" / row.id / "draft.txt").write_text("Preserve my work")
                accept(client, artifact, next_version)
                if stop_during_execution:
                    assert not heartbeat()
                    raise execution.Stopped("Artifact changed")
                return {"status": "ok"}
            return super().run(row, project, connection, role, heartbeat, timeout)

    executor = RevisingExecutor()
    worker = Worker(executor)
    for _ in range(3):
        with db.transaction() as session:
            claimed = service.claim(session, 2)
        assert claimed and claimed[0] == task["id"]
        worker.run(*claimed)
    with db.Session() as session:
        row = session.get(db.Task, task["id"])
        assert row.state == "waiting_dependency" and row.phase == "collect"
        assert row.lease_owner is None and row.corrections == 0
        assert session.scalars(select(db.Connection)).first().lease_task is None
    assert (db.ROOT / "workspaces" / task["id"] / "draft.txt").read_text() == "Preserve my work"
    assert executor.stopped == ([task["id"]] if stop_during_execution else [])


def test_revoked_registry_input_between_heartbeat_and_merge_cannot_reach_git(
    client, setup_project, tmp_path, monkeypatch
):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    artifact = document(client, project)
    accept(client, artifact)
    task = make_task(client, project, artifact_version_ids=[artifact["versions"][0]["id"]])
    worker = Worker(DeterministicExecutor())
    for _ in range(20):
        with db.transaction() as session:
            pending_merge = session.get(db.Task, task["id"]).phase == "merge"
            claimed = service.claim(session, 2)
        assert claimed and claimed[0] == task["id"]
        if pending_merge:
            break
        worker.run(*claimed)
    else:
        raise AssertionError("Task did not reach pending merge")
    target = git(remote, "rev-parse", "preprod")
    original = worker.heartbeat

    def revoke(*args):
        authorized = original(*args)
        if authorized:
            assert client.post(f"/api/project-artifacts/{artifact['id']}/revoke").status_code == 200
        return authorized

    monkeypatch.setattr(worker, "heartbeat", revoke)
    worker.run(*claimed)
    assert git(remote, "rev-parse", "preprod") == target
    assert client.get(f"/api/tasks/{task['id']}").json()["state"] == "waiting_dependency"
    monkeypatch.setattr(worker, "heartbeat", original)
    next_version = revise(client, artifact)
    accept(client, artifact, next_version)
    assert bind(client, task, artifact, next_version).status_code == 200
    completed = drive(worker, task["id"])
    assert completed.state == "merged"
    assert completed.result_sha == completed.verified_sha == completed.reviewed_sha
    assert git(remote, "rev-parse", "preprod") == completed.result_sha
