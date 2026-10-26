"""Exercise the actual queue/Git pipeline with a deterministic executor, without paid inference."""

import json
import subprocess

import httpx
import pytest
from conftest import connect, make_task
from sqlalchemy import select

from darkfactory import db, service
from darkfactory.gitops import Repository
from darkfactory.notifications import NotificationDelivery, dispatch_notifications
from darkfactory.worker import Worker


def git(path, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args],
        cwd=path,
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def seed(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "value.py").write_text("VALUE = 0\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "seed")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(source), str(remote))
    return remote


class DeterministicExecutor:
    """Test double for model calls only. No production test-provider path exists."""

    def __init__(self, quota_once=False):
        self.calls = []
        self.quota_once = quota_once

    def stop(self, task_id, owner):
        pass

    def run(self, task, project, connection, role, heartbeat, timeout):
        assert heartbeat()
        work = db.ROOT / "workspaces" / task.id
        self.calls.append((role, connection.provider if connection else None))
        if role == "plan":
            return {
                "status": "ok",
                "text": '{"plan":"Set VALUE to 42 and verify", "questions":[]}',
                "plan": {"plan": "Set VALUE to 42 and verify", "questions": []},
            }
        if role == "implement":
            if self.quota_once:
                self.quota_once = False
                (work / "partial.txt").write_text("Keep completed work\n")
                return {"status": "quota", "text": "usage limit"}
            (work / "value.py").write_text("VALUE = 42\n")
            return {"status": "ok", "text": "Fixed value"}
        if role == "review":
            assert "VALUE = 42" in (work / "value.py").read_text()
            return {
                "status": "ok",
                "text": '{"approved":true,"findings":[]}',
                "review": {"approved": True, "findings": []},
            }
        proc = subprocess.run(
            ["python3", "-B", "-c", "from value import VALUE; assert VALUE == 42"],
            cwd=work,
            capture_output=True,
            text=True,
        )
        return {"status": "ok" if proc.returncode == 0 else "checks_failed", "text": proc.stderr}


def drive(worker, task_id, limit=40):
    for _ in range(limit):
        with db.transaction() as session:
            task = session.get(db.Task, task_id)
            if task.state in service.TERMINAL:
                return task
            claimed = service.claim(session, 2)
        assert claimed, "Task unexpectedly stalled"
        worker.run(*claimed)
    raise AssertionError("Pipeline did not finish")


def test_queue_to_verified_merge_with_quota_handoff(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    main_sha = git(remote, "rev-parse", "main")
    connect(client, pool, "codex", "writer")
    connect(client, pool, "claude", "reviewer")
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    task = make_task(client, project)
    for provider, credentials in (
        ("telegram", {"bot_token": "123456:test-bot-token", "chat_id": "-100123456"}),
        ("rocketchat", {"webhook_url": "https://chat.corp/hooks/test-webhook"}),
    ):
        response = client.post(
            "/api/notification-channels",
            json={
                "name": provider,
                "provider": provider,
                "project_id": project["id"],
                "credentials": credentials,
                **({"recipient": "@developer"} if provider == "rocketchat" else {}),
            },
        )
        assert response.status_code == 201, response.text
    executor = DeterministicExecutor(quota_once=True)
    result = drive(Worker(executor), task["id"])
    assert result.state == "merged", result.reason
    assert result.switches == 1
    assert result.result_sha == result.reviewed_sha == result.verified_sha
    assert git(remote, "rev-parse", "preprod") == result.result_sha
    assert git(remote, "rev-parse", "main") == main_sha
    assert (db.ROOT / "workspaces" / task["id"] / "partial.txt").exists()
    assert [call for call in executor.calls if call[0] == "implement"][:2] == [
        ("implement", "codex"),
        ("implement", "claude"),
    ]
    assert (db.ROOT / "artifacts" / task["id"] / "diff.patch").is_file()
    with db.Session() as session:
        project_row = session.get(db.Project, project["id"])
        assert project_row.integration_task is None
        assert Repository(project_row, result).merge(result.result_sha, result.target_sha)
        assert len(list(session.scalars(select(db.Task)))) == 1
        notifications = list(session.scalars(select(NotificationDelivery)))
        assert len(notifications) == 2
        for notification in notifications:
            assert "Fixed value" in notification.message
            assert result.result_sha in notification.message
            assert "/?task=" + task["id"] + "&commit=" + result.result_sha in notification.message
    sent = []

    def deliver(request):
        payload = json.loads(request.content)
        sent.append(payload)
        return httpx.Response(200, json={"ok": True, "success": True})

    with httpx.Client(transport=httpx.MockTransport(deliver)) as transport:
        assert dispatch_notifications(client=transport) == 2
        assert dispatch_notifications(client=transport) == 0
    assert len(sent) == 2
    assert any(payload.get("channel") == "@developer" for payload in sent)


def test_no_checks_merges_approved_review_into_preprod_by_default(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), []
    task = make_task(client, project)
    result = drive(Worker(DeterministicExecutor()), task["id"])
    assert result.state == "merged", result.reason
    assert git(remote, "rev-parse", "preprod") == result.result_sha != git(remote, "rev-parse", "main")
    # The merged task branch is removed; preprod and main stay.
    assert git(remote, "branch", "--list", "--format=%(refname:short)").split() == ["main", "preprod"]
    # Main stays a human decision: the board flags the task until the owner merges or rejects it.
    listed = {t["id"]: t for t in client.get(f"/api/projects/{project['id']}/tasks").json()}
    assert listed[task["id"]]["promotion_pending"] is True
    proposal = client.get(f"/api/tasks/{task['id']}").json()["promotion"]
    # Without required checks the owner can still review the main diff.
    assert client.get(f"/api/tasks/{task['id']}/promotion").status_code == 200
    rejected = client.post(
        f"/api/tasks/{task['id']}/promotion",
        json={"action": "reject", "proposal_id": proposal["id"], "source_sha": proposal["source_sha"]},
    )
    assert rejected.status_code == 200, rejected.text
    listed = {t["id"]: t for t in client.get(f"/api/projects/{project['id']}/tasks").json()}
    assert listed[task["id"]]["promotion_pending"] is False


def test_stale_acceptance_leaves_main_banner(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), []
    task = make_task(client, project)
    assert drive(Worker(DeterministicExecutor()), task["id"]).state == "merged"
    with db.transaction() as session:
        # Checks adopted later (e.g. by the environment task) make the old acceptance unpromotable.
        session.get(db.Project, project["id"]).checks = ["true"]
    listed = {t["id"]: t for t in client.get(f"/api/projects/{project['id']}/tasks").json()}
    assert listed[task["id"]]["promotion_pending"] is False
    assert client.get(f"/api/tasks/{task['id']}/promotion").status_code == 409


def test_no_checks_never_merges_when_project_requires_human(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), []
        current.secret = db.encrypt(json.dumps({"merge_without_checks": False}))
    task = make_task(client, project)
    result = drive(Worker(DeterministicExecutor()), task["id"])
    assert result.state == "review_required"
    assert git(remote, "rev-parse", "main") == git(remote, "rev-parse", "preprod")
    assert result.result_sha is None
    with db.Session() as session:
        kinds = [e.kind for e in session.scalars(select(db.Event).where(db.Event.task_id == task["id"]))]
    assert "review_required" in kinds
    # Owner approval merges the reviewed candidate into preprod; main stays untouched.
    assert client.post(f"/api/tasks/{task['id']}/actions", json={"action": "merge"}).status_code == 200
    merged = drive(Worker(DeterministicExecutor()), task["id"])
    assert merged.state == "merged", merged.reason
    assert git(remote, "rev-parse", "preprod") == merged.result_sha != git(remote, "rev-parse", "main")
    again = client.post(f"/api/tasks/{task['id']}/actions", json={"action": "merge"})
    assert again.status_code == 409


def test_target_change_requires_new_acceptance(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    task = make_task(client, project)
    worker = Worker(DeterministicExecutor())
    for _ in range(20):
        with db.transaction() as session:
            current = session.get(db.Task, task["id"])
            if current.phase == "merge":
                break
            claimed = service.claim(session, 2)
        assert claimed
        worker.run(*claimed)
    else:
        raise AssertionError("Merge phase not reached")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-b", "preprod", str(remote), str(other))
    (other / "unrelated.txt").write_text("Concurrent change\n")
    git(other, "add", ".")
    git(other, "commit", "-m", "concurrent")
    git(other, "push", "origin", "preprod")
    with db.transaction() as session:
        claimed = service.claim(session, 2)
    worker.run(*claimed)
    with db.Session() as session:
        current = session.get(db.Task, task["id"])
        assert current.phase == "integrate"
        # Checks rerun on the new tree; the approved review waits for integration to decide if it holds.
        assert current.verified_sha is None and current.reviewed_sha
    calls = len(worker.executor.calls)
    result = drive(worker, task["id"])
    assert result.state == "merged"
    assert git(remote, "show", "preprod:unrelated.txt") == "Concurrent change"
    # Preprod changed only a file the task never touched: the review is not repeated.
    assert "review" not in [role for role, _ in worker.executor.calls[calls:]]


def test_only_newest_main_promotion_is_offered(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), []
    first = make_task(client, project)
    assert drive(Worker(DeterministicExecutor()), first["id"]).state == "merged"
    second = make_task(client, project)
    assert drive(Worker(DeterministicExecutor()), second["id"]).state == "merged"
    listed = {t["id"]: t for t in client.get(f"/api/projects/{project['id']}/tasks").json()}
    # Promotion carries all of preprod up to its commit: the newest one covers the first.
    assert listed[first["id"]]["promotion_pending"] is False
    assert listed[second["id"]]["promotion_pending"] is True
    proposal = client.get(f"/api/tasks/{second['id']}/promotion").json()
    merged = client.post(
        f"/api/tasks/{second['id']}/promotion",
        json={
            "action": "merge",
            "proposal_id": proposal["id"],
            "source_sha": proposal["source_sha"],
            "main_sha": proposal["main_sha"],
        },
    )
    assert merged.status_code == 200, merged.text
    listed = {t["id"]: t for t in client.get(f"/api/projects/{project['id']}/tasks").json()}
    assert not listed[first["id"]]["promotion_pending"] and not listed[second["id"]]["promotion_pending"]
    assert client.get(f"/api/tasks/{first['id']}/promotion").json()["state"] == "superseded"
    assert client.get(f"/api/tasks/{second['id']}/promotion").json()["state"] == "merged"


def test_quick_task_and_leaf_subtask_skip_planning(client, setup_project, tmp_path):
    from darkfactory.coordinator import needs_plan

    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), []
    executor = DeterministicExecutor()
    quick = make_task(client, project, quick=True)
    assert drive(Worker(executor), quick["id"]).state == "merged"
    assert "plan" not in [role for role, _ in executor.calls]
    regular = make_task(client, project)
    with db.transaction() as session:
        task = session.get(db.Task, regular["id"])
        # One writer by default; a planner only for work marked as large.
        assert not needs_plan(session, task)
        task.role_config = {"plan": True}
        assert needs_plan(session, task)
        # Subtasks only write; their writer may ask to split further.
        task.plan_depth = 1
        assert not needs_plan(session, task)
        task.plan_depth, task.incident = 0, True
        assert not needs_plan(session, task)


def test_main_reaches_preprod_and_conflicts_become_a_task(client, setup_project, tmp_path):
    from darkfactory import mainsync

    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), []
    clone = tmp_path / "owner"
    git(tmp_path, "clone", str(remote), str(clone))
    git(clone, "push", "origin", "main:preprod")

    def commit(branch, name, text):
        git(clone, "checkout", "-q", branch)
        git(clone, "pull", "-q", "origin", branch)
        (clone / name).write_text(text)
        git(clone, "add", name)
        git(clone, "commit", "-qm", f"{branch}: {name}")
        git(clone, "push", "-q", "origin", branch)

    def run():
        return dict(mainsync.sync(force=True))[project["id"]]

    git(clone, "checkout", "-q", "-b", "preprod", "origin/preprod")
    # Hotfix straight to main: preprod has nothing of its own, so it simply moves forward.
    commit("main", "hotfix.py", "FIX = 1\n")
    assert run()["state"] == "merged"
    assert git(remote, "rev-parse", "preprod") == git(remote, "rev-parse", "main")
    assert run()["state"] == "up_to_date"
    # Both branches moved: a merge commit keeps preprod's own work.
    commit("preprod", "feature.py", "NEW = 1\n")
    commit("main", "other.py", "OTHER = 1\n")
    assert run()["state"] == "merged"
    assert git(remote, "merge-base", "--is-ancestor", "main", "preprod") == ""
    assert git(remote, "show", "preprod:feature.py") == "NEW = 1"
    # Same line on both sides: the factory hands the conflict to a writer, once.
    commit("preprod", "value.py", "VALUE = 1\n")
    commit("main", "value.py", "VALUE = 2\n")
    result = run()
    assert result["state"] == "conflict" and result["conflicts"] == ["value.py"]
    assert run()["state"] == "conflict"
    with db.Session() as session:
        tasks = [t for t in session.scalars(select(db.Task)) if (t.role_config or {}).get("merge_from")]
    assert len(tasks) == 1 and tasks[0].role_config["merge_from"] == result["main"]
    assert drive(Worker(DeterministicExecutor()), tasks[0].id).state == "merged"
    assert git(remote, "merge-base", "--is-ancestor", "main", "preprod") == ""
    assert run()["state"] == "up_to_date"


class InSessionChecksExecutor(DeterministicExecutor):
    """A writer whose own session already ran the required checks green."""

    def run(self, task, project, connection, role, heartbeat, timeout):
        result = super().run(task, project, connection, role, heartbeat, timeout)
        if role == "implement" and result["status"] == "ok":
            result["inner_checks"] = {"passed": True, "text": "$ python3 check.py\nexit=0\n"}
        return result


def test_checks_passed_in_the_writer_session_skip_the_separate_checks_run(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), ["python3 -c 'print(1)'"]
    executor = InSessionChecksExecutor()
    task = make_task(client, project)
    assert drive(Worker(executor), task["id"]).state == "merged"
    roles = [role for role, _ in executor.calls]
    # Preprod did not move after the writer's green checks, so the merged tree needs no second run.
    assert roles == ["implement", "review"], roles
    with db.transaction() as session:
        kinds = [
            item.kind for item in session.scalars(select(db.Event).where(db.Event.task_id == task["id"]))
        ]
    assert "checks_passed" in kinds


def test_preprod_moving_in_other_files_needs_no_second_check_run():
    from types import SimpleNamespace

    from darkfactory.worker import disjoint_preprod

    changed = {("base", "ours"): "src/login.ts\n", ("base", "head"): "docs/a.md\nsrc/api.cs\n"}
    repo = SimpleNamespace(work=None, git=lambda _diff, _names, a, b, cwd: changed[(a, b)])
    healthy = SimpleNamespace(health={"state": "healthy", "sha": "head"})
    assert disjoint_preprod(repo, healthy, "base", "ours", "head")
    changed[("base", "head")] += "src/login.ts\n"
    assert not disjoint_preprod(repo, healthy, "base", "ours", "head")
    changed[("base", "head")] = "docs/a.md\n"
    # Preprod head not yet checked by its own monitor: run the checks on the merged tree.
    assert not disjoint_preprod(
        repo, SimpleNamespace(health={"state": "healthy", "sha": "old"}), "base", "ours", "head"
    )
    assert not disjoint_preprod(
        repo, SimpleNamespace(health={"state": "broken", "sha": "head"}), "base", "ours", "head"
    )


def test_preprod_moving_elsewhere_during_the_task_still_merges(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), ["python3 -c 'print(1)'"]
        target = current.target

    class PreprodMoves(InSessionChecksExecutor):
        def run(self, task, project, connection, role, heartbeat, timeout):
            result = super().run(task, project, connection, role, heartbeat, timeout)
            if role == "implement":
                # Someone else lands an unrelated, already checked change on preprod meanwhile.
                other = tmp_path / "other"
                git(tmp_path, "clone", "-q", "-b", target, str(remote), str(other))
                (other / "other.py").write_text("OTHER = 1\n")
                git(other, "add", ".")
                git(other, "-c", "user.name=T", "-c", "user.email=t@localhost", "commit", "-qm", "other")
                git(other, "push", "-q", "origin", target)
                head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=other, text=True).strip()
                with db.transaction() as session:
                    session.get(db.Project, project.id).health = {"state": "healthy", "sha": head}
            return result

    executor = PreprodMoves()
    task = make_task(client, project)
    assert drive(Worker(executor), task["id"]).state == "merged"
    assert [role for role, _ in executor.calls] == ["implement", "review"]


def test_review_frees_the_integration_queue_and_survives_unrelated_preprod_moves(
    client, setup_project, tmp_path
):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), ["python3 -c 'print(1)'"]
        target = current.target
    held = []

    class OtherTaskMergesDuringReview(InSessionChecksExecutor):
        def run(self, task, project, connection, role, heartbeat, timeout):
            if role == "review" and not held:
                with db.transaction() as session:
                    held.append(session.get(db.Project, project.id).integration_task)
                other = tmp_path / "other"
                git(tmp_path, "clone", "-q", "-b", target, str(remote), str(other))
                (other / "other.py").write_text("OTHER = 1\n")
                git(other, "add", ".")
                git(other, "-c", "user.name=T", "-c", "user.email=t@localhost", "commit", "-qm", "other")
                git(other, "push", "-q", "origin", target)
            return super().run(task, project, connection, role, heartbeat, timeout)

    executor = OtherTaskMergesDuringReview()
    task = make_task(client, project)
    assert drive(Worker(executor), task["id"]).state == "merged"
    assert held == [None]
    # The new preprod head is not checked yet, so the merged tree is checked again; the review is not repeated.
    assert [role for role, _ in executor.calls] == ["implement", "review", "checks"]


class MissingToolExecutor(DeterministicExecutor):
    """Checks fail with "command not found" a set number of times, then pass."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def run(self, task, project, connection, role, heartbeat, timeout):
        if role == "checks" and self.failures:
            self.failures -= 1
            self.calls.append((role, None))
            return {"status": "checks_failed", "text": "$ dotnet build x\nexit=127\nsh: 1: dotnet: not found"}
        return super().run(task, project, connection, role, heartbeat, timeout)


@pytest.mark.parametrize("failures", [1, 2])
def test_missing_tool_in_checks_rebuilds_the_environment_instead_of_a_code_cycle(
    client, setup_project, tmp_path, failures
):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), ["python3 -c 'print(1)'"]
    executor = MissingToolExecutor(failures)
    task = make_task(client, project)
    result = drive(Worker(executor), task["id"])
    roles = [role for role, _ in executor.calls]
    # A missing tool is never sent back to the writer: no code change installs dotnet.
    assert roles.count("implement") == 1, roles
    with db.transaction() as session:
        kinds = [
            item.kind for item in session.scalars(select(db.Event).where(db.Event.task_id == task["id"]))
        ]
    assert "correction" not in kinds and "environment_retry" in kinds
    if failures == 1:
        assert result.state == "merged", result.reason
    else:
        assert (result.state, result.reason) == ("needs_human", "environment_blocked")


class FailingInSessionChecksExecutor(DeterministicExecutor):
    """A writer whose own session ran the required checks red until its fix attempts ran out, once."""

    def __init__(self):
        super().__init__()
        self.failed = False

    def run(self, task, project, connection, role, heartbeat, timeout):
        result = super().run(task, project, connection, role, heartbeat, timeout)
        if role == "implement" and result["status"] == "ok":
            passed, self.failed = self.failed, True
            result["inner_checks"] = {
                "passed": passed,
                "text": "$ python3 check.py\nexit=1\nAssertionError\n",
            }
        return result


def test_exhausted_in_session_checks_go_straight_to_correction(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), ["python3 -c 'print(1)'"]
    executor = FailingInSessionChecksExecutor()
    task = make_task(client, project)
    assert drive(Worker(executor), task["id"]).state == "merged"
    roles = [role for role, _ in executor.calls]
    # No separate checks container repeats the failure the writer's session already reported.
    assert roles == ["implement", "implement", "review"], roles


def test_mechanical_phases_run_on_without_a_queue_trip(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), ["python3 -c 'print(1)'"]
    worker = Worker(InSessionChecksExecutor())
    task = make_task(client, project)
    runs = 0
    for _ in range(20):
        with db.transaction() as session:
            row = session.get(db.Task, task["id"])
            if row.state in service.TERMINAL:
                break
            claimed = service.claim(session, 2)
        assert claimed
        worker.run(*claimed, 2)
        runs += 1
    assert row.state == "merged"
    # prepare, implement, then review followed by integrate through merge in the same run.
    assert runs == 3, runs


class LazyReviewerExecutor(DeterministicExecutor):
    """A reviewer that approves without evidence for a changed code file, once."""

    def __init__(self):
        super().__init__()
        self.lazy = True

    def run(self, task, project, connection, role, heartbeat, timeout):
        result = super().run(task, project, connection, role, heartbeat, timeout)
        if role == "review" and self.lazy:
            self.lazy = False
            result["review"] = {**result["review"], "uncovered": ["value.py"]}
        return result


def test_approval_without_evidence_for_code_reruns_the_review(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), ["python3 -c 'print(1)'"]
    executor = LazyReviewerExecutor()
    task = make_task(client, project)
    assert drive(Worker(executor), task["id"]).state == "merged"
    roles = [role for role, _ in executor.calls]
    assert roles.count("review") == 2 and roles.count("implement") == 1, roles
    with db.Session() as session:
        kinds = [
            item.kind for item in session.scalars(select(db.Event).where(db.Event.task_id == task["id"]))
        ]
    assert "review_incomplete" in kinds and "correction" not in kinds


class FixAfterReviewExecutor(DeterministicExecutor):
    """The first candidate has a wrong comment; review rejects it and must then see the fixed diff."""

    def __init__(self):
        super().__init__()
        self.rounds = 0
        self.seen = []

    def run(self, task, project, connection, role, heartbeat, timeout):
        work = db.ROOT / "workspaces" / task.id
        if role == "implement":
            self.rounds += 1
            self.calls.append((role, None))
            note = "# wrong" if self.rounds == 1 else "# fixed"
            (work / "value.py").write_text(f"VALUE = 42  {note}\n")
            return {"status": "ok", "text": "Fixed value"}
        if role == "review":
            self.calls.append((role, None))
            diff = (db.ROOT / "artifacts" / task.id / "diff.patch").read_text()
            self.seen.append("# fixed" in diff)
            if "# wrong" in diff:
                text = '{"approved":false,"findings":["value.py:1 — wrong comment"]}'
                return {"status": "ok", "text": text, "review": json.loads(text)}
            return {
                "status": "ok",
                "text": '{"approved":true,"findings":[]}',
                "review": {"approved": True, "findings": []},
            }
        return super().run(task, project, connection, role, heartbeat, timeout)


def test_review_reads_the_diff_of_the_latest_candidate(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), ["python3 -c 'print(1)'"]
    executor = FixAfterReviewExecutor()
    task = make_task(client, project)
    assert drive(Worker(executor), task["id"]).state == "merged"
    # Review runs before integration: the second review must see the fix, not the first candidate's diff.
    assert executor.seen[:2] == [False, True], executor.seen


def test_review_diff_is_rebuilt_from_the_candidate_before_every_review(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        current = session.get(db.Project, project["id"])
        current.remote, current.checks = str(remote), ["python3 -c 'print(1)'"]
    seen = []

    class StaleDiffExecutor(DeterministicExecutor):
        def run(self, task, project, connection, role, heartbeat, timeout):
            if role == "implement":
                # Something stale lands in diff.patch after the writer's commit.
                result = super().run(task, project, connection, role, heartbeat, timeout)
                return result
            if role == "review":
                seen.append((db.ROOT / "artifacts" / task.id / "diff.patch").read_text())
            return super().run(task, project, connection, role, heartbeat, timeout)

    task = make_task(client, project)
    worker = Worker(StaleDiffExecutor())
    for _ in range(10):
        with db.transaction() as session:
            row = session.get(db.Task, task["id"])
            if row.phase == "review" and row.state == "queued":
                (db.ROOT / "artifacts" / task["id"] / "diff.patch").write_text("diff --git a/stale b/stale\n")
            if row.state in service.TERMINAL:
                break
            claimed = service.claim(session, 2)
        worker.run(*claimed)
    assert seen and "stale" not in seen[0] and "VALUE = 42" in seen[0]


def test_handoff_claims_outside_the_repository_are_not_commit_claims(tmp_path):
    from types import SimpleNamespace

    from darkfactory.worker import unbacked_claims

    (tmp_path / "diff.patch").write_text("diff --git a/src/app.py b/src/app.py\n")
    repo = SimpleNamespace(artifacts=tmp_path)
    assert unbacked_claims(repo, ["src/app.py", "/run/cache/notes.md", "/run/evidence/log.txt"]) is None
    assert unbacked_claims(repo, ["/workspace/src/app.py"]) is None
    assert "src/other.py" in unbacked_claims(repo, ["src/other.py", "/workspace/src/app.py"])


def test_translation_and_changelog_files_do_not_serialise_tasks():
    from darkfactory.worker import SHARED_FILE

    for path in ("src/app/assets/i18n/next/ru.json", "locales/en.yml", "CHANGES.rst", "CHANGELOG.md"):
        assert SHARED_FILE.search(path), path
    for path in ("src/app/Documents.cs", "docs/i18n.md", "src/i18n.ts", "package.json"):
        assert not SHARED_FILE.search(path), path
