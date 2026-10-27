import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import connect, make_task
from sqlalchemy import create_engine, select, text
from test_pipeline import DeterministicExecutor, drive, git, seed

from darkfactory import agents, coordinator, db, service
from darkfactory.worker import Worker


class DelegatingExecutor(DeterministicExecutor):
    def run(self, task, project, connection, role, heartbeat, timeout):
        if role == "plan" and task.plan_depth == 0:
            return {
                "status": "ok",
                "plan": {
                    "plan": "Add independent components, then verify integration",
                    "questions": [],
                    "subtasks": [
                        {
                            "key": key,
                            "title": key,
                            "request": "Create " + key + ".txt",
                            "acceptance": key + ".txt exists",
                            "depends_on": [],
                        }
                        for key in ("alpha", "beta")
                    ],
                },
            }
        if role == "implement":
            work = db.ROOT / "workspaces" / task.id
            if not task.plan_depth:
                assert (work / "alpha.txt").is_file() and (work / "beta.txt").is_file()
            (work / (task.title + ".txt")).write_text("implemented\n")
            return {"status": "ok", "text": "Complete"}
        return super().run(task, project, connection, role, heartbeat, timeout)


def test_planner_delegates_parallel_work_then_parent_accepts_integrated_result(
    client, setup_project, tmp_path
):
    project, pool = setup_project
    remote = seed(tmp_path)
    source = tmp_path / "source"
    (source / "value.py").write_text("VALUE = 42\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "healthy starting point")
    git(source, "push", str(remote), "main")
    initial_main = git(tmp_path, "--git-dir", str(remote), "rev-parse", "main")
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    connect(client, pool, account="first")
    connect(client, pool, account="second")
    parent = make_task(client, project, plan=True, title="assembled")
    worker = Worker(DelegatingExecutor())
    for _ in range(2):
        with db.transaction() as session:
            claim = service.claim(session, 2)
        worker.run(*claim)
    with db.transaction() as session:
        children = list(session.scalars(select(db.Task).where(db.Task.plan_depth == 1)))
        assert len(children) == 2
        assert all(child.api_budget_usd == 0 for child in children)
        assert session.get(db.Task, parent["id"]).phase == "collect"
        first = service.claim(session, 2)
    with db.transaction() as session:
        second = service.claim(session, 2)
        assert first and second and first[0] != second[0]
        limits = service.execution_limits(session, session.get(db.Task, parent["id"]))
        assert limits["active_seconds"] + limits["time_reserved"] <= limits["time_budget"]
    worker.run(*first)
    worker.run(*second)
    accepted = drive(worker, parent["id"], limit=70)
    assert accepted.state == "merged"
    assert accepted.result_sha == accepted.verified_sha == accepted.reviewed_sha
    assert git(tmp_path, "--git-dir", str(remote), "rev-parse", "main") == initial_main
    with db.Session() as session:
        children = list(session.scalars(select(db.Task).where(db.Task.plan_depth == 1)))
        assert all(child.state == "merged" for child in children)


def test_additive_migrations_preserve_existing_data_and_are_idempotent():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for table in (
            "organizations",
            "pools",
            "projects",
            "tasks",
            "connections",
            "director_threads",
            "director_dialogues",
            "notification_channels",
            "notification_deliveries",
            "tokens",
        ):
            conn.execute(text(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)"))
            conn.execute(text(f"INSERT INTO {table} (id) VALUES ('legacy')"))
        db.expand_schema(conn)
        db.expand_schema(conn)
        assert conn.execute(text("SELECT id, max_active FROM organizations")).one() == ("legacy", 2)
        assert conn.execute(
            text("SELECT id, system_check, plan_depth, time_reserved_seconds FROM tasks")
        ).one() == ("legacy", 0, 0, 0)
        assert conn.execute(text("SELECT id FROM projects")).scalar_one() == "legacy"
        assert conn.execute(text("SELECT id, from_env FROM tokens")).one() == ("legacy", 0)
        assert conn.execute(text("SELECT dependency_commits FROM tasks")).scalar_one() == "{}"
        assert conn.execute(text("SELECT id, quota_details FROM connections")).one() == ("legacy", "{}")
        assert conn.execute(text("SELECT id, thread_id FROM director_dialogues")).one() == ("legacy", None)
        assert conn.execute(text("SELECT id, recipient FROM notification_channels")).one() == ("legacy", None)
        assert conn.execute(text("SELECT origin, origin_replied FROM director_dialogues")).one() == (None, 0)
        assert conn.execute(text("SELECT reply_to, message_ref FROM notification_deliveries")).one() == (
            None,
            None,
        )


def role_settings(**overrides):
    return {
        "profiles": [
            {"name": "manager", "provider": "codex"},
            {"name": "analyst", "provider": "claude"},
            {"name": "developer", "provider": "codex"},
            {"name": "helper", "provider": "claude", "can_delegate": False},
            {"name": "reviewer", "provider": "claude"},
        ],
        "assignments": {"planner": "manager", "writer": "developer", "reviewer": "reviewer"},
        **overrides,
    }


def subtask(key, profile=None, depends_on=None):
    return {
        "key": key,
        "title": key,
        "request": "Create " + key + ".md",
        "acceptance": key + " accepted",
        "depends_on": depends_on or [],
        "agent_profile": profile,
    }


def structured_plan(*items):
    return {
        "plan": "Deliver bounded tasks and collect accepted results",
        "questions": [],
        "subtasks": list(items),
    }


def save_roles(session, project_id, settings):
    project = session.get(db.Project, project_id)
    project.secret = db.encrypt(json.dumps({"agents": settings}))
    session.flush()


def child_tasks(session, parent):
    ids = list(
        session.scalars(
            select(db.Link.other_id).where(
                db.Link.task_id == parent.id,
                db.Link.kind == "parent",
            )
        )
    )
    return {task.title: task for task in session.scalars(select(db.Task).where(db.Task.id.in_(ids)))}


def test_role_delegation_waits_for_accepted_analysis_and_preserves_shared_budgets(client, setup_project):
    project, _ = setup_project
    parent = make_task(client, project)
    with db.transaction() as session:
        save_roles(session, project["id"], role_settings())
        root = session.get(db.Task, parent["id"])
        root.role_config = {"writer": "claude", "reviewer": "codex", "profiles": {"reviewer": "reviewer"}}
        original_roles = deepcopy(root.role_config)
        root.active_seconds, root.corrections, root.switches = 70, 2, 3
        root.time_credit, root.correction_credit, root.switch_credit = 12, 4, 5
        root.api_budget_usd, root.api_spent_usd, root.api_reserved_usd = 5, 1, 0.25
        before = service.execution_limits(session, root)
        plan = structured_plan(
            subtask("analysis", "analyst"),
            subtask("code", "developer", ["analysis"]),
        )
        assert coordinator.delegate(session, root, plan)
        children = child_tasks(session, root)
        analysis, code = children["analysis"], children["code"]
        assert analysis.role_config["profiles"] == {
            "planner": "analyst",
            "writer": "analyst",
            "reviewer": "reviewer",
        }
        assert code.role_config["profiles"]["writer"] == "developer"
        assert code.role_config["writer"] == "claude"
        assert code.role_config["reviewer"] == "codex"
        assert root.role_config == original_roles
        config = json.loads(db.decrypt(session.get(db.Project, project["id"]).secret))
        assert agents.resolve(config, "plan", analysis.role_config["profiles"])["name"] == "analyst"
        assert agents.resolve(config, "review", analysis.role_config["profiles"])["name"] == "reviewer"
        assert service.dependency_reason(session, code)
        analysis.state, analysis.candidate_sha, analysis.reviewed_sha = "review_required", "a" * 40, "a" * 40
        assert service.dependency_reason(session, code)
        analysis.state, analysis.result_sha = "merged", "b" * 40
        assert service.dependency_reason(session, code) is None
        assert list(service.dependency_commits(session, code).values()) == ["b" * 40]
        after = service.execution_limits(session, code)
        for key in before.keys() - {"root", "members", "time_budget"}:
            assert after[key] == before[key]
        assert (
            after["time_budget"]
            == before["time_budget"] + 2 * session.get(db.Project, project["id"]).max_seconds
        )
        assert len(after["members"]) == 3
        assert all(child.api_budget_usd == 0 and child.plan_depth == 1 for child in children.values())
        ids = {child.id for child in children.values()}
        assert coordinator.delegate(session, root, plan)
        assert {child.id for child in child_tasks(session, root).values()} == ids
        service.cancel_delegated(session, root)
        assert code.state == "cancelled"
        assert analysis.state == "merged"
        assert root.active_seconds == 70 and root.corrections == 2 and root.switches == 3


def test_nested_delegation_and_depth_boundary(client, setup_project):
    project, _ = setup_project
    parent = make_task(client, project)
    with db.transaction() as session:
        save_roles(session, project["id"], role_settings())
        root = session.get(db.Task, parent["id"])
        assert coordinator.delegate(session, root, structured_plan(subtask("analysis", "analyst")))
        analysis = child_tasks(session, root)["analysis"]
        assert coordinator.delegate(session, analysis, structured_plan(subtask("helper", "helper")))
        helper = child_tasks(session, analysis)["helper"]
        assert helper.plan_depth == 2
        with pytest.raises(ValueError, match="decomposition"):
            coordinator.delegate(session, helper, structured_plan(subtask("too-deep", "developer")))
        assert child_tasks(session, helper) == {}
        assert not coordinator.delegate(session, helper, structured_plan())
        service.cancel_delegated(session, root)
        assert helper.state == "cancelled" and analysis.state == "cancelled"


def test_shared_descendant_cap_counts_cousins_and_resume_does_not_create_more(client, setup_project):
    project, _ = setup_project
    parent = make_task(client, project)
    with db.transaction() as session:
        save_roles(session, project["id"], role_settings(max_delegated_tasks=3))
        root = session.get(db.Task, parent["id"])
        plan = structured_plan(subtask("analysis", "analyst"), subtask("code", "developer"))
        assert coordinator.delegate(session, root, plan)
        children = child_tasks(session, root)
        assert coordinator.delegate(
            session, children["analysis"], structured_plan(subtask("helper", "helper"))
        )
        with pytest.raises(ValueError, match="shared task tree limit"):
            coordinator.delegate(session, children["code"], structured_plan(subtask("overflow", "helper")))
        assert child_tasks(session, children["code"]) == {}
        assert len(service.execution_limits(session, root)["members"]) == 4
        assert coordinator.delegate(session, root, plan)
        assert len(service.execution_limits(session, root)["members"]) == 4


@pytest.mark.parametrize("invalid", ["missing-profile", "inherited-profile"])
def test_invalid_profiles_validate_whole_batch_before_any_insert(client, setup_project, invalid):
    project, _ = setup_project
    parent = make_task(client, project)
    with db.transaction() as session:
        settings = role_settings()
        profile = "missing" if invalid == "missing-profile" else "developer"
        save_roles(session, project["id"], settings)
        root = session.get(db.Task, parent["id"])
        if invalid == "inherited-profile":
            root.role_config = {"profiles": {"reviewer": "deleted"}}
        before_events = session.query(db.Event).count()
        with pytest.raises(ValueError):
            coordinator.delegate(
                session,
                root,
                structured_plan(
                    subtask("valid-first", "analyst"),
                    subtask("invalid-second", profile),
                ),
            )
        session.flush()
        assert child_tasks(session, root) == {}
        assert session.query(db.Task).count() == 1
        assert session.query(db.Event).count() == before_events


def test_write_mcp_profile_writes_and_project_planner_plans(client, setup_project):
    project, _ = setup_project
    parent = make_task(client, project)
    with db.transaction() as session:
        settings = role_settings()
        settings["mcp_servers"] = [
            {
                "name": "writes",
                "url": "https://tools.example.invalid/mcp",
                "enabled_tools": ["write"],
                "read_only": False,
            }
        ]
        settings["profiles"][2]["mcp_servers"] = ["writes"]
        save_roles(session, project["id"], settings)
        root = session.get(db.Task, parent["id"])
        root.role_config = {"profiles": {"planner": "analyst"}}
        assert coordinator.delegate(
            session, root, structured_plan(subtask("browser", "developer"), subtask("notes", "analyst"))
        )
        children = child_tasks(session, root)
        assert children["browser"].role_config["profiles"] == {"writer": "developer"}
        assert children["notes"].role_config["profiles"] == {"planner": "analyst", "writer": "analyst"}
        config = {"agents": settings}
        overrides = children["browser"].role_config["profiles"]
        assert agents.resolve(config, "plan", overrides)["name"] == "manager"
        assert agents.resolve(config, "implement", overrides)["name"] == "developer"


def test_legacy_configuration_keeps_single_delegation_level(client, setup_project):
    project, _ = setup_project
    # New projects now have a team. Reproduce the legacy configuration explicitly.
    with db.transaction() as session:
        session.get(db.Project, project["id"]).secret = db.encrypt("{}")
    parent = make_task(client, project)
    with db.transaction() as session:
        root = session.get(db.Task, parent["id"])
        assert coordinator.delegate(session, root, structured_plan(subtask("child")))
        child = child_tasks(session, root)["child"]
        with pytest.raises(ValueError, match="decomposition"):
            coordinator.delegate(session, child, structured_plan(subtask("grandchild")))
        assert child_tasks(session, child) == {}


def test_profile_can_disable_delegation_before_children_exist(client, setup_project):
    project, _ = setup_project
    parent = make_task(client, project)
    with db.transaction() as session:
        save_roles(session, project["id"], role_settings())
        root = session.get(db.Task, parent["id"])
        root.role_config = {"profiles": {"planner": "helper"}}
        with pytest.raises(ValueError, match="decomposition"):
            coordinator.delegate(session, root, structured_plan(subtask("child", "analyst")))
        assert child_tasks(session, root) == {}


class RoleWorkflowTestExecutor(DeterministicExecutor):
    """Deterministic model fixture; Git and configured project checks run locally."""

    def __init__(self):
        super().__init__()
        self.role_calls = []
        self.analysis_inputs = []
        self.check_calls = []

    def run(self, task, project, connection, role, heartbeat, timeout):
        assert heartbeat()
        work = db.ROOT / "workspaces" / task.id
        config = json.loads(db.decrypt(project.secret))
        profile = agents.resolve(config, role, (task.role_config or {}).get("profiles"))
        if role in {"plan", "implement", "review"}:
            assert profile is not None
            assert connection.provider == profile["provider"]
            self.role_calls.append((task.title, role, profile["name"]))
        if task.title == "development" and role in {"plan", "implement"}:
            with db.Session() as session:
                analysis = session.scalar(select(db.Task).where(db.Task.title == "analysis"))
                assert analysis.state == "merged"
                assert analysis.result_sha == analysis.verified_sha == analysis.reviewed_sha
                assert analysis.result_sha
                accepted = session.scalar(
                    select(db.Event).where(
                        db.Event.task_id == analysis.id,
                        db.Event.kind == "accepted",
                    )
                )
                assert accepted is not None
                self.analysis_inputs.append((role, analysis.result_sha, accepted.id))
            assert (work / "analysis.md").read_text() == "Contract: calculate() must return 42.\n"
        if role == "plan" and task.plan_depth == 0:
            return {
                "status": "ok",
                "plan": structured_plan(
                    subtask("analysis", "analyst"),
                    subtask("development", "developer", ["analysis"]),
                ),
            }
        if role == "implement":
            if task.title == "analysis":
                (work / "analysis.md").write_text("Contract: calculate() must return 42.\n")
            elif task.title == "development":
                (work / "value.py").write_text("VALUE = 42\n\ndef calculate():\n    return VALUE\n")
                (work / "test_calculation.py").write_text(
                    "import unittest\nfrom value import calculate\n\n"
                    "class CalculationTest(unittest.TestCase):\n"
                    "    def test_contract(self):\n        self.assertEqual(calculate(), 42)\n"
                )
                (work / "implementation.md").write_text("Implemented the accepted analysis contract.\n")
            else:
                assert task.title == "manager"
                assert (work / "analysis.md").is_file()
                assert (work / "implementation.md").is_file()
                assert "def calculate():" in (work / "value.py").read_text()
                (work / "completion.md").write_text("Analysis and implementation accepted together.\n")
            return {"status": "ok", "text": "Deterministic model fixture completed its bounded goal"}
        if role not in {"plan", "review"}:
            output = []
            for command in project.checks:
                self.check_calls.append((task.title, role, command))
                result = subprocess.run(
                    command,
                    shell=True,
                    cwd=work,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env={
                        **os.environ,
                        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
                    },
                )
                output.append(result.stdout + result.stderr)
                if result.returncode:
                    return {"status": "checks_failed", "text": "\n".join(output)}
            return {"status": "ok", "text": "\n".join(output)}
        return super().run(task, project, connection, role, heartbeat, timeout)


def test_manager_analysis_development_local_git_workflow(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    source = tmp_path / "source"
    (source / "value.py").write_text("VALUE = 42\n")
    (source / "test_value.py").write_text(
        "import unittest\nfrom value import VALUE\n\n"
        "class ValueTest(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(VALUE, 42)\n"
    )
    git(source, "add", ".")
    git(source, "commit", "-m", "healthy project with mandatory checks")
    git(source, "push", str(remote), "main")
    main_sha = git(remote, "rev-parse", "main")
    connect(client, pool, "codex", "role-codex")
    connect(client, pool, "claude", "role-claude")
    with db.transaction() as session:
        save_roles(session, project["id"], role_settings())
        current = session.get(db.Project, project["id"])
        current.remote = str(remote)
        assert current.checks == ["python -m unittest"]
    parent = make_task(
        client,
        project,
        plan=True,
        title="manager",
        agent_profiles={
            "planner": "manager",
            "writer": "manager",
            "reviewer": "reviewer",
        },
    )
    executor = RoleWorkflowTestExecutor()
    result = drive(Worker(executor), parent["id"], limit=70)
    assert result.state == "merged", result.reason
    with db.Session() as session:
        members = service.execution_limits(session, session.get(db.Task, parent["id"]))["members"]
        assert {task.title for task in members} == {"manager", "analysis", "development"}
        for task in members:
            assert task.state == "merged"
            assert task.result_sha == task.verified_sha == task.reviewed_sha
            assert task.result_sha
            assert git(remote, "merge-base", "--is-ancestor", task.result_sha, "preprod") == ""
        analysis = next(task for task in members if task.title == "analysis")
        development = next(task for task in members if task.title == "development")
        assert list(development.dependency_commits.values()) == [analysis.result_sha]
    assert executor.role_calls == [
        ("manager", "plan", "manager"),
        ("analysis", "implement", "analyst"),
        ("analysis", "review", "reviewer"),
        ("development", "implement", "developer"),
        ("development", "review", "reviewer"),
        ("manager", "implement", "manager"),
        ("manager", "review", "reviewer"),
    ]
    assert [role for role, _sha, _event in executor.analysis_inputs] == ["implement"]
    assert len({sha for _role, sha, _event in executor.analysis_inputs}) == 1
    assert {title for title, _role, _command in executor.check_calls} == {
        "manager",
        "analysis",
        "development",
    }
    assert all(command == "python -m unittest" for _title, _role, command in executor.check_calls)
    assert git(remote, "rev-parse", "main") == main_sha
    assert git(remote, "rev-parse", "preprod") == result.result_sha
    assert git(remote, "show", "preprod:analysis.md") == "Contract: calculate() must return 42."
    assert git(remote, "show", "preprod:implementation.md") == "Implemented the accepted analysis contract."
    assert git(remote, "show", "preprod:completion.md") == "Analysis and implementation accepted together."
    assert git(remote, "show", "main:value.py") == "VALUE = 42"


class SplittingExecutor(DelegatingExecutor):
    """No planner run: the writer itself asks to split before editing."""

    split = False

    def run(self, task, project, connection, role, heartbeat, timeout):
        if role == "implement" and task.plan_depth == 0 and not self.split:
            self.split = True
            plan = super().run(task, project, connection, "plan", heartbeat, timeout)["plan"]
            return {"status": "ok", "text": "", "request": {"split": plan}}
        assert role != "plan"
        return super().run(task, project, connection, role, heartbeat, timeout)


def test_writer_splits_large_work_without_a_planner(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    source = tmp_path / "source"
    (source / "value.py").write_text("VALUE = 42\n")
    git(source, "commit", "-am", "healthy starting point")
    git(source, "push", str(remote), "main")
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    connect(client, pool)
    parent = make_task(client, project, title="assembled")
    accepted = drive(Worker(SplittingExecutor()), parent["id"], limit=70)
    assert accepted.state == "merged"
    with db.transaction() as session:
        children = list(session.scalars(select(db.Task).where(db.Task.plan_depth == 1)))
        assert sorted(child.title for child in children) == ["alpha", "beta"]
        assert all(child.state == "merged" for child in children)


def test_writer_question_stops_for_the_owner(client, setup_project, tmp_path):
    project, pool = setup_project
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(seed(tmp_path))
    connect(client, pool)
    task = make_task(client, project)

    class Asking(DeterministicExecutor):
        def run(self, task, project, connection, role, heartbeat, timeout):
            if role == "implement":
                return {"status": "ok", "text": "", "request": {"questions": ["Какой цвет кнопки?"]}}
            return super().run(task, project, connection, role, heartbeat, timeout)

    stopped = drive(Worker(Asking()), task["id"])
    assert (stopped.state, stopped.phase, stopped.candidate_sha) == ("needs_human", "implement", None)
    assert "Какой цвет кнопки?" in stopped.reason


def test_owner_request_file_is_validated(tmp_path):
    from darkfactory.execution import OWNER_REQUEST, owner_request

    assert owner_request(tmp_path, True) is None
    (tmp_path / OWNER_REQUEST).write_text('{"questions": ["  ", "Какой API?"]}')
    assert owner_request(tmp_path, True) == {"questions": ["Какой API?"]}
    subtasks = [{"key": "a", "title": "a", "request": "r", "acceptance": "x", "depends_on": []}]
    (tmp_path / OWNER_REQUEST).write_text(json.dumps({"plan": "split", "subtasks": subtasks}))
    assert owner_request(tmp_path, False) is None
    assert owner_request(tmp_path, True)["split"]["subtasks"][0]["key"] == "a"
    (tmp_path / OWNER_REQUEST).write_text('{"subtasks": "nonsense"}')
    assert owner_request(tmp_path, True) is None


def test_planner_files_become_each_subtasks_search_and_separate_overlaps(client, setup_project):
    from darkfactory import worker
    from darkfactory.execution import LOCATE_FILE

    project, _ = setup_project
    parent = make_task(client, project)
    first = {**subtask("first"), "files": ["src/shared.cs", "src/a.cs"]}
    second = {**subtask("second"), "files": ["src/shared.cs"]}
    third = {**subtask("third"), "files": ["src/c.cs"]}
    with db.transaction() as session:
        root = session.get(db.Task, parent["id"])
        assert coordinator.delegate(session, root, structured_plan(first, second, third))
        children = child_tasks(session, root)
        for child in children.values():
            child.state, child.phase = "queued", "implement"
        ids = {key: child.id for key, child in children.items()}
    found = json.loads((db.ROOT / "artifacts" / ids["first"] / LOCATE_FILE).read_text())
    assert [item["path"] for item in found["files"]] == ["src/shared.cs", "src/a.cs"]
    with db.Session() as session:
        tasks = {key: session.get(db.Task, ident) for key, ident in ids.items()}
        # Siblings are compared by the planner's files: the later one on a shared file waits.
        assert worker.overlapping_work(tasks["second"]) == {"src/shared.cs"}
        assert worker.overlapping_work(tasks["third"]) == set()


def test_parent_search_stops_holding_files_once_it_delegates(client, setup_project):
    from darkfactory.execution import LOCATE_FILE

    project, _ = setup_project
    parent = make_task(client, project)
    artifacts = db.ROOT / "artifacts" / parent["id"]
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / LOCATE_FILE).write_text(json.dumps({"files": [{"path": "src/everything.cs"}]}))
    with db.transaction() as session:
        assert coordinator.delegate(
            session, session.get(db.Task, parent["id"]), structured_plan(subtask("one"))
        )
    assert not (artifacts / LOCATE_FILE).exists() and (artifacts / "locate-plan.json").exists()
