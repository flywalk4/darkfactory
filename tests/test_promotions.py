"""Manual main decisions against real local Git, with model calls replaced by a test double."""

from types import SimpleNamespace

import pytest
from conftest import connect, make_task
from sqlalchemy import select
from test_pipeline import DeterministicExecutor, drive, git, seed

from darkfactory import db, director
from darkfactory.notifications import NotificationDelivery
from darkfactory.worker import Worker, accept_merge


@pytest.fixture
def accepted(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool, "codex", "writer")
    connect(client, pool, "claude", "reviewer")
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    channel = client.post(
        "/api/notification-channels",
        json={
            "name": "Completed",
            "provider": "telegram",
            "project_id": project["id"],
            "credentials": {"bot_token": "123456:fake-test-token", "chat_id": "-100123456"},
        },
    )
    assert channel.status_code == 201
    task = make_task(client, project)
    result = drive(Worker(DeterministicExecutor()), task["id"])
    assert result.state == "merged"
    initial = git(remote, "rev-parse", "main")
    assert initial != result.result_sha
    path = f"/api/tasks/{task['id']}/promotion"
    return SimpleNamespace(task=result, project=project, remote=remote, initial=initial, path=path)


def decision(preview, action="merge"):
    return {
        "action": action,
        "proposal_id": preview["id"],
        "source_sha": preview["source_sha"],
        "main_sha": preview.get("main_sha"),
    }


def test_root_completion_link_preview_manual_merge_and_repeat(client, accepted):
    item = accepted
    detail = client.get(f"/api/tasks/{item.task.id}").json()
    assert detail["promotion"]["state"] == "pending"
    with db.Session() as session:
        message = session.query(NotificationDelivery).one().message
        assert "&promotion=1" in message
        assert item.task.result_sha in message
    preview = client.get(item.path)
    assert preview.status_code == 200, preview.text
    value = preview.json()
    assert value["can_merge"] and not value["already_merged"]
    assert "+VALUE = 42" in value["diff"]
    assert value["main_sha"] == item.initial
    assert value["source_sha"] == item.task.result_sha
    assert git(item.remote, "rev-parse", "main") == item.initial  # GET never merges.
    for _ in range(2):
        merged = client.post(item.path, json=decision(value))
        assert merged.status_code == 200, merged.text
        assert merged.json()["state"] == "merged"
    assert git(item.remote, "rev-parse", "main") == item.task.result_sha
    assert git(item.remote, "rev-parse", "preprod") == item.task.result_sha
    with db.Session() as session:
        events = list(session.scalars(select(db.Event).where(db.Event.kind == "promotion_merged")))
        assert len(events) == 1 and events[0].data["actor"]
    assert client.post(item.path, json=decision(value, "reject")).status_code == 409


def test_reject_keeps_preprod_and_main(client, accepted):
    item = accepted
    preview = client.get(item.path).json()
    for _ in range(2):
        response = client.post(item.path, json=decision(preview, "reject"))
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "rejected"
    assert git(item.remote, "rev-parse", "main") == item.initial
    assert git(item.remote, "rev-parse", "preprod") == item.task.result_sha
    assert client.post(item.path, json=decision(preview)).status_code == 409


def test_changed_main_rejects_stale_preview_and_divergent_main(client, accepted, tmp_path):
    item = accepted
    preview = client.get(item.path).json()
    external = tmp_path / "external"
    git(tmp_path, "clone", str(item.remote), str(external))
    (external / "parallel.txt").write_text("External main change\n")
    git(external, "add", ".")
    git(external, "commit", "-m", "Independent main")
    git(external, "push", "origin", "HEAD:main")
    changed = git(item.remote, "rev-parse", "main")
    assert client.post(item.path, json=decision(preview)).status_code == 409
    fresh = client.get(item.path).json()
    # Independent main work merges cleanly: the owner gets a merge commit, nothing is lost.
    assert fresh["main_sha"] == changed and fresh["can_merge"] and fresh["mode"] == "merge"
    assert "parallel.txt" not in fresh["diff"]
    response = client.post(item.path, json=decision(fresh))
    assert response.status_code == 200, response.text
    main = git(item.remote, "rev-parse", "main")
    assert git(item.remote, "rev-parse", f"{main}^1") == changed
    assert git(item.remote, "rev-parse", f"{main}^2") == item.task.result_sha


def test_conflicting_main_blocks_promotion(client, accepted, tmp_path):
    item = accepted
    external = tmp_path / "external"
    git(tmp_path, "clone", str(item.remote), str(external))
    git(external, "checkout", item.task.result_sha)
    changed_files = git(external, "diff", "--name-only", item.initial, "HEAD").split()
    git(external, "checkout", "main")
    (external / changed_files[0]).write_text("Conflicting main content\n")
    git(external, "add", ".")
    git(external, "commit", "-m", "Conflicting main")
    git(external, "push", "origin", "HEAD:main")
    changed = git(item.remote, "rev-parse", "main")
    fresh = client.get(item.path).json()
    assert not fresh["can_merge"] and fresh["conflicts"] == [changed_files[0]]
    assert client.post(item.path, json=decision(fresh)).status_code == 409
    assert git(item.remote, "rev-parse", "main") == changed
    assert client.get(item.path).json()["state"] == "pending"


def test_freezes_source_even_when_preprod_advances(client, accepted, tmp_path):
    item = accepted
    external = tmp_path / "external"
    git(tmp_path, "clone", str(item.remote), str(external))
    git(external, "checkout", "preprod")
    (external / "later.txt").write_text("Not part of the offered snapshot\n")
    git(external, "add", ".")
    git(external, "commit", "-m", "Later preprod")
    git(external, "push", "origin", "preprod")
    newer = git(item.remote, "rev-parse", "preprod")
    preview = client.get(item.path).json()
    assert "later.txt" not in preview["diff"]
    response = client.post(item.path, json=decision(preview))
    assert response.status_code == 200, response.text
    assert git(item.remote, "rev-parse", "main") == item.task.result_sha
    assert git(item.remote, "rev-parse", "preprod") == newer


@pytest.mark.parametrize("change", ["review", "health", "dependencies", "checks"])
def test_evidence_health_and_dependency_guards(client, accepted, change):
    item = accepted
    preview = client.get(item.path).json()
    with db.transaction() as session:
        task = session.get(db.Task, item.task.id)
        if change == "review":
            task.reviewed_sha = None
        elif change == "health":
            session.get(db.Project, item.project["id"]).health = {"state": "broken"}
        elif change == "checks":
            session.get(db.Project, item.project["id"]).checks = ["changed required check"]
        else:
            task.dependency_commits = {"revoked-input": "b" * 40}
    assert client.post(item.path, json=decision(preview)).status_code == 409
    assert git(item.remote, "rev-parse", "main") == item.initial
    # Rejection remains available even when evidence is stale or preprod is broken.
    assert client.post(item.path, json=decision(preview, "reject")).status_code == 200


@pytest.mark.parametrize("role", ["operator", "observer"])
def test_only_owner_can_decide(client, accepted, role):
    preview = client.get(accepted.path).json()
    token = client.post("/api/tokens", json={"role": role, "project_ids": []}).json()["token"]
    client.headers["Authorization"] = "Bearer " + token
    assert client.get(accepted.path).status_code == 200
    assert client.post(accepted.path, json=decision(preview)).status_code == 403
    assert client.post(accepted.path, json=decision(preview, "reject")).status_code == 403


def test_wrong_scope_missing_auth_and_stale_proposal(client, accepted):
    preview = client.get(accepted.path).json()
    changed = {**decision(preview), "source_sha": "b" * 40}
    assert client.post(accepted.path, json=changed).status_code == 409
    assert client.post(accepted.path, json={**decision(preview), "proposal_id": 999999}).status_code == 409
    assert client.post(accepted.path, json={**decision(preview), "main_sha": None}).status_code == 400
    project = client.post(
        "/api/projects", json={"name": "Other", "remote": "https://example.invalid/git"}
    ).json()
    token = client.post("/api/tokens", json={"role": "owner", "project_ids": [project["id"]]}).json()["token"]
    client.headers["Authorization"] = "Bearer " + token
    assert client.get(accepted.path).status_code == 404
    assert client.post(accepted.path, json=decision(preview)).status_code == 404
    client.headers.pop("Authorization")
    assert client.get(accepted.path).status_code == 401


def test_children_and_old_completions_have_no_offer(client, accepted):
    child = make_task(client, accepted.project)
    with db.transaction() as session:
        task = session.get(db.Task, child["id"])
        task.candidate_sha = accepted.task.result_sha
        session.add(db.Link(task_id=accepted.task.id, other_id=task.id, kind="parent"))
        session.flush()
        accept_merge(session, task)
    assert client.get(f"/api/tasks/{child['id']}").json()["promotion"] is None
    assert client.get(f"/api/tasks/{child['id']}/promotion").status_code == 404
    old = make_task(client, accepted.project)
    with db.transaction() as session:
        task = session.get(db.Task, old["id"])
        task.state, task.result_sha = "merged", accepted.task.result_sha
        db.event(session, task, "accepted", "Old accepted event", commit=task.result_sha)
    assert client.get(f"/api/tasks/{old['id']}/promotion").status_code == 404


def test_director_cannot_decide_main_merge():
    action = director.Action(method="POST", path="/api/tasks/abc/promotion", body_json="{}")
    with pytest.raises(ValueError, match="недоступна директору"):
        director.validate_action(action)


def test_retry_after_push_before_database_commit(client, accepted, monkeypatch):
    from darkfactory.routes import tasks as api

    preview = client.get(accepted.path).json()
    original = api.event

    def crash_after_push(session, task, kind, message, **data):
        if kind == "promotion_merged":
            raise RuntimeError("simulated crash after main push")
        return original(session, task, kind, message, **data)

    monkeypatch.setattr(api, "event", crash_after_push)
    with pytest.raises(RuntimeError, match="simulated crash after main push"):
        client.post(accepted.path, json=decision(preview))
    assert git(accepted.remote, "rev-parse", "main") == accepted.task.result_sha
    assert client.get(accepted.path).json()["state"] == "pending"
    monkeypatch.setattr(api, "event", original)
    response = client.post(accepted.path, json=decision(preview))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "merged"


def test_main_push_lease_prevents_overwriting_concurrent_commit(client, accepted, tmp_path, monkeypatch):
    from darkfactory.gitops import Repository

    preview = client.get(accepted.path).json()
    external = tmp_path / "concurrent"
    git(tmp_path, "clone", str(accepted.remote), str(external))
    (external / "concurrent.txt").write_text("Must survive the promotion race\n")
    git(external, "add", ".")
    git(external, "commit", "-m", "Concurrent main")
    concurrent_sha = git(external, "rev-parse", "HEAD")
    original = Repository.git

    def push_between_preview_and_update(self, *args, **kwargs):
        if args[0] == "push" and any(
            str(arg).startswith("--force-with-lease=refs/heads/main:") for arg in args
        ):
            git(external, "push", "origin", "HEAD:main")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Repository, "git", push_between_preview_and_update)
    assert client.post(accepted.path, json=decision(preview)).status_code == 409
    assert git(accepted.remote, "rev-parse", "main") == concurrent_sha
    assert client.get(accepted.path).json()["state"] == "pending"


def test_preview_lists_the_tasks_the_promotion_carries(client, accepted):
    item = accepted
    artifacts = db.ROOT / "artifacts" / item.task.id
    (artifacts / "review.json").write_text('{"approved": true, "findings": ["[LATER] a.py:3 — rename"]}')
    preview = client.get(item.path).json()
    assert [row["task_id"] for row in preview["tasks"]] == [item.task.id]
    row = preview["tasks"][0]
    assert row["files"] >= 1 and row["notes"] == ["[LATER] a.py:3 — rename"]
    assert row["commit"] == item.task.result_sha
    # After a merge to main the next promotion starts from there.
    assert client.post(item.path, json=decision(preview)).status_code == 200
    with db.Session() as session:
        from darkfactory import promotions

        task = session.get(db.Task, item.task.id)
        assert promotions.included(session, task, preview["id"]) == []


def soak(project_id, hours, healthy=True, age=None):
    """Turn on auto-promotion, age the acceptance by `age` hours and set the preprod health."""
    import json

    from darkfactory.db import decrypt, encrypt

    with db.transaction() as session:
        row = session.get(db.Project, project_id)
        config = json.loads(decrypt(row.secret) or "{}")
        row.secret = encrypt(json.dumps({**config, "auto_promote_hours": hours}))
        row.health = {**(row.health or {}), "state": "healthy" if healthy else "broken"}
        if age is not None:
            for item in session.query(db.Event).filter_by(kind="accepted"):
                item.at = db.now() - age * 3600


def auto_run(project_id):
    from darkfactory import promotions

    with db.transaction() as session:
        promotions.auto_request(session, session.get(db.Project, project_id))
    return dict(promotions.auto_promote(force=True))[project_id]


def test_auto_promotion_waits_for_the_soak_and_a_healthy_preprod(client, accepted):
    item = accepted
    pid = item.project["id"]
    assert "выключен" in auto_run(pid)
    soak(pid, 6, age=1)
    assert "Выдержка" in auto_run(pid)
    soak(pid, 6, healthy=False, age=7)
    assert "Preprod" in auto_run(pid)
    with db.transaction() as session:
        task = session.get(db.Task, item.task.id)
        db.event(session, task, "incident_detected", "test incident")
    soak(pid, 6, age=7)
    assert "инцидент" in auto_run(pid)
    with db.transaction() as session:
        session.query(db.Event).filter_by(kind="incident_detected").update({"at": db.now() - 8 * 3600})
    assert git(item.remote, "rev-parse", "main") == item.initial
    result = auto_run(pid)
    assert result.startswith("Перенесено в main"), result
    assert git(item.remote, "rev-parse", "main") != item.initial
    detail = client.get(f"/api/tasks/{item.task.id}").json()
    assert detail["promotion"]["state"] == "merged" and detail["promotion"]["decided_by"] == "factory"
    assert "Нечего переносить" in auto_run(pid)


def test_auto_promotion_refuses_a_leak(client, accepted, monkeypatch):
    item = accepted
    pid = item.project["id"]
    with db.transaction() as session:
        import json

        from darkfactory.db import decrypt, encrypt

        row = session.get(db.Project, pid)
        config = json.loads(decrypt(row.secret) or "{}")
        # The deterministic writer's change sets VALUE = 42: treat that text as an internal name.
        row.secret = encrypt(json.dumps({**config, "test_env": {"TEST_LEAK_PATTERNS": "VALUE = 42"}}))
    soak(pid, 1, age=2)
    result = auto_run(pid)
    assert result.startswith("Не перенесено: проверка утечек"), result
    assert git(item.remote, "rev-parse", "main") == item.initial


@pytest.fixture
def accepted_seed(client, setup_project, tmp_path):
    """A project whose seed branch is `nolang`; the repository's `main` is an old branch that diverged."""
    project, pool = setup_project
    remote = seed(tmp_path)
    source = tmp_path / "source"
    git(source, "checkout", "-b", "nolang")
    (source / "nolang.txt").write_text("Current development line\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "Seed branch work")
    git(source, "push", str(remote), "nolang")
    git(source, "checkout", "main")
    (source / "legacy.txt").write_text("Abandoned main line\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "Legacy main work")
    git(source, "push", str(remote), "main")
    connect(client, pool, "codex", "writer")
    connect(client, pool, "claude", "reviewer")
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        row.remote, row.seed_branch = str(remote), "nolang"
    task = make_task(client, project)
    result = drive(Worker(DeterministicExecutor()), task["id"])
    assert result.state == "merged"
    path = f"/api/tasks/{task['id']}/promotion"
    return SimpleNamespace(
        task=result,
        project=project,
        remote=remote,
        main=git(remote, "rev-parse", "main"),
        initial=git(remote, "rev-parse", "nolang"),
        path=path,
    )


def test_promotion_moves_only_the_seed_branch(client, accepted_seed):
    item = accepted_seed
    preview = client.get(item.path).json()
    assert preview["target"] == "nolang"
    # The preview compares preprod with the seed branch, not with the abandoned main.
    assert preview["main_sha"] == item.initial
    assert preview["mode"] == "fast_forward" and preview["can_merge"] and not preview["conflicts"]
    assert "+VALUE = 42" in preview["diff"] and "legacy.txt" not in preview["diff"]
    response = client.post(item.path, json=decision(preview))
    assert response.status_code == 200, response.text
    assert git(item.remote, "rev-parse", "nolang") == item.task.result_sha
    assert git(item.remote, "rev-parse", "main") == item.main


def test_seed_branch_merge_commit_names_the_seed_branch(client, accepted_seed, tmp_path):
    item = accepted_seed
    external = tmp_path / "external"
    git(tmp_path, "clone", "-b", "nolang", str(item.remote), str(external))
    (external / "hotfix.txt").write_text("Owner hotfix\n")
    git(external, "add", ".")
    git(external, "commit", "-m", "Hotfix on nolang")
    git(external, "push", "origin", "HEAD:nolang")
    changed = git(item.remote, "rev-parse", "nolang")
    preview = client.get(item.path).json()
    assert preview["main_sha"] == changed and preview["mode"] == "merge" and preview["can_merge"]
    assert "hotfix.txt" not in preview["diff"]
    assert client.post(item.path, json=decision(preview)).status_code == 200
    head = git(item.remote, "rev-parse", "nolang")
    assert git(item.remote, "rev-parse", f"{head}^1") == changed
    assert git(item.remote, "rev-parse", f"{head}^2") == item.task.result_sha
    assert git(item.remote, "log", "-1", "--format=%s", head) == (
        f"Merge preprod {item.task.result_sha[:12]} into nolang"
    )
    assert git(item.remote, "rev-parse", "main") == item.main


def test_seed_branch_push_lease_refuses_a_concurrent_commit(client, accepted_seed, tmp_path, monkeypatch):
    from darkfactory.gitops import Repository

    item = accepted_seed
    preview = client.get(item.path).json()
    external = tmp_path / "concurrent"
    git(tmp_path, "clone", "-b", "nolang", str(item.remote), str(external))
    (external / "concurrent.txt").write_text("Must survive the promotion race\n")
    git(external, "add", ".")
    git(external, "commit", "-m", "Concurrent nolang")
    concurrent_sha = git(external, "rev-parse", "HEAD")
    original = Repository.git

    def push_between_preview_and_update(self, *args, **kwargs):
        if args[0] == "push" and any(
            str(arg).startswith("--force-with-lease=refs/heads/nolang:") for arg in args
        ):
            git(external, "push", "origin", "HEAD:nolang")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Repository, "git", push_between_preview_and_update)
    assert client.post(item.path, json=decision(preview)).status_code == 409
    assert git(item.remote, "rev-parse", "nolang") == concurrent_sha
    assert git(item.remote, "rev-parse", "main") == item.main
    assert client.get(item.path).json()["state"] == "pending"


def test_seed_branch_moved_after_preview_is_refused(client, accepted_seed, tmp_path):
    item = accepted_seed
    preview = client.get(item.path).json()
    external = tmp_path / "external"
    git(tmp_path, "clone", "-b", "nolang", str(item.remote), str(external))
    (external / "parallel.txt").write_text("Independent change\n")
    git(external, "add", ".")
    git(external, "commit", "-m", "Independent nolang")
    git(external, "push", "origin", "HEAD:nolang")
    changed = git(item.remote, "rev-parse", "nolang")
    response = client.post(item.path, json=decision(preview))
    assert response.status_code == 409 and "nolang" in response.json()["detail"]
    assert git(item.remote, "rev-parse", "nolang") == changed


def test_missing_seed_branch_refuses_promotion_without_creating_it(client, accepted_seed):
    item = accepted_seed
    preview = client.get(item.path).json()
    git(item.remote, "update-ref", "-d", "refs/heads/nolang")
    for response in (client.get(item.path), client.post(item.path, json=decision(preview))):
        assert response.status_code == 409
        assert "nolang" in response.json()["detail"]
    assert "nolang" not in git(item.remote, "branch", "--list", "nolang")
    assert git(item.remote, "rev-parse", "main") == item.main


def test_auto_promotion_merges_into_the_seed_branch(client, accepted_seed):
    item = accepted_seed
    pid = item.project["id"]
    soak(pid, 1, age=2)
    result = auto_run(pid)
    assert result.startswith("Перенесено в nolang"), result
    assert git(item.remote, "rev-parse", "nolang") == item.task.result_sha
    assert git(item.remote, "rev-parse", "main") == item.main
    with db.Session() as session:
        merged = session.query(db.Event).filter_by(kind="promotion_merged").one()
        assert "nolang" in merged.message


def test_completion_notification_names_the_seed_branch(client, accepted_seed):
    from darkfactory import notifications

    with db.Session() as session:
        task = session.get(db.Task, accepted_seed.task.id)
        message = notifications._completion_message(session, task, "done")
    assert "Влить в nolang / отклонить" in message
