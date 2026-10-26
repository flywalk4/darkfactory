import json

import pytest
from fastapi import HTTPException

from darkfactory import db, service


@pytest.fixture
def group_session(setup_project):
    project, pool = setup_project
    with db.Session() as session:
        yield session, session.get(db.Project, project["id"]), session.get(db.Pool, pool["id"])


def make_task(session, project, parent=None, **values):
    task_id = db.uid()
    fields = {
        "id": task_id,
        "org_id": project.org_id,
        "project_id": project.id,
        "title": "Group regression",
        "request": "Preserve shared limits across decomposition",
        "branch": "darkfactory/" + task_id,
        "state": "needs_human",
        "phase": "prepare",
    }
    fields.update(values)
    result = db.Task(**fields)
    session.add(result)
    session.flush()
    if parent:
        session.add(db.Link(task_id=parent.id, other_id=result.id, kind="parent"))
        session.flush()
    return result


def add_connection(session, project, pool, provider):
    connection = db.Connection(
        org_id=project.org_id,
        name=provider,
        account_key=db.uid(),
        provider=provider,
        pool_ids=[pool.id],
        secret=db.encrypt("{}"),
    )
    session.add(connection)
    session.flush()
    return connection


def test_limits_sum_entire_tree_and_use_root_project_base(group_session):
    session, project, _ = group_session
    project.max_seconds, project.max_corrections = 100, 3
    root = make_task(
        session,
        project,
        active_seconds=10,
        time_credit=2,
        corrections=1,
        correction_credit=1,
        switches=2,
        switch_credit=1,
        api_budget_usd=10,
        api_spent_usd=1,
    )
    child = make_task(
        session,
        project,
        parent=root,
        active_seconds=20,
        time_credit=3,
        corrections=1,
        correction_credit=2,
        switches=3,
        switch_credit=2,
        api_spent_usd=2,
        api_reserved_usd=4,
        time_reserved_seconds=7,
    )
    other_project = db.Project(
        org_id=project.org_id,
        name="Nested project",
        remote="https://example.invalid/nested.git",
        max_seconds=999,
        max_corrections=99,
    )
    session.add(other_project)
    session.flush()
    grandchild = make_task(session, other_project, parent=child, active_seconds=5, api_budget_usd=2)
    sibling = make_task(session, project, parent=root, active_seconds=4)
    independent = make_task(session, project, active_seconds=500, api_budget_usd=900)
    limits = service.execution_limits(session, grandchild)
    assert limits["root"].id == root.id
    assert {member.id for member in limits["members"]} == {root.id, child.id, grandchild.id, sibling.id}
    assert limits["active_seconds"] == 39
    assert limits["time_reserved"] == 7
    assert limits["time_budget"] == 105
    assert limits["corrections"] == 2
    assert limits["correction_limit"] == 6
    assert limits["switches"] == 5
    assert limits["switch_credit"] == 3
    assert limits["api_budget"] == 12
    assert limits["api_spent"] == 3
    assert limits["api_reserved"] == 4
    assert service.execution_limits(session, independent)["root"].id == independent.id


@pytest.mark.parametrize("exhausted", ["time", "switches"])
def test_decomposition_never_resets_time_or_switch_limit(group_session, exhausted):
    session, project, pool = group_session
    project.max_seconds = 60
    pool.policy = {"max_switches": 3}
    root = make_task(session, project, active_seconds=60 if exhausted == "time" else 0)
    make_task(session, project, parent=root, switches=3 if exhausted == "switches" else 0)
    child = make_task(session, project, parent=root, state="queued")
    assert service.claim(session, 2) is None
    assert child.state == "needs_human"


def test_child_deadline_uses_remaining_tree_time(group_session):
    session, project, _ = group_session
    project.max_seconds = 100
    root = make_task(session, project, active_seconds=60)
    child = make_task(session, project, parent=root, state="queued", active_seconds=10, time_credit=5)
    before = db.now()
    assert service.claim(session, 2)[0] == child.id
    assert before + 35 <= child.execution_deadline <= db.now() + 35
    assert child.time_reserved_seconds == 35


def test_parallel_children_split_remaining_time_without_over_reserving(group_session):
    session, project, _ = group_session
    project.max_seconds = 100
    root = make_task(session, project, state="waiting_dependency", active_seconds=10)
    first = make_task(session, project, parent=root, state="queued")
    second = make_task(session, project, parent=root, state="queued")
    assert service.claim(session, 2)[0] == first.id
    assert first.time_reserved_seconds == 45
    assert service.claim(session, 2)[0] == second.id
    assert second.time_reserved_seconds == 45
    limits = service.execution_limits(session, root)
    assert limits["active_seconds"] + limits["time_reserved"] == limits["time_budget"]


@pytest.mark.parametrize("sibling_state", ["running", "recovering"])
def test_reserved_tree_time_waits_without_blocking_independent_tree(group_session, sibling_state):
    session, project, _ = group_session
    project.max_seconds = 100
    root = make_task(session, project, active_seconds=10)
    make_task(session, project, parent=root, state=sibling_state, time_reserved_seconds=90)
    child = make_task(session, project, parent=root, state="queued")
    independent = make_task(session, project, state="queued")
    assert service.claim(session, 3)[0] == independent.id
    assert child.state == "waiting_pool"
    assert child.time_reserved_seconds == 0
    assert independent.time_reserved_seconds == 100


@pytest.mark.parametrize("sibling_state", ["running", "recovering"])
@pytest.mark.parametrize("explicit_api", [True, False])
def test_api_reservation_waits_for_active_sibling_and_retries(group_session, sibling_state, explicit_api):
    session, project, pool = group_session
    session.get(db.Organization, project.org_id).max_active = 3
    root = make_task(session, project, api_budget_usd=5)
    sibling = make_task(
        session,
        project,
        parent=root,
        state=sibling_state,
        api_reserved_usd=5,
        api_spent_usd=5,
        explicit_api=True,
    )
    child = make_task(
        session, project, parent=root, state="queued", phase="implement", explicit_api=explicit_api
    )
    if not explicit_api:
        project.secret = db.encrypt(json.dumps({"allow_paid_fallback": True}))
    api = add_connection(session, project, pool, "api")
    assert service.choose_connection(session, child, project).id == api.id
    assert service.claim(session, 3) is None
    assert child.state == "waiting_pool"
    sibling.state, sibling.api_reserved_usd, sibling.api_spent_usd = "merged", 0, 0.1
    assert service.claim(session, 3)[0] == child.id


@pytest.mark.parametrize("explicit_api", [True, False])
def test_api_reservation_is_not_charged_twice_when_group_has_budget_left(group_session, explicit_api):
    session, project, pool = group_session
    root = make_task(session, project, api_budget_usd=5)
    make_task(session, project, parent=root, state="running", api_reserved_usd=3, api_spent_usd=3)
    child = make_task(
        session, project, parent=root, state="queued", phase="implement", explicit_api=explicit_api
    )
    if not explicit_api:
        project.secret = db.encrypt(json.dumps({"allow_paid_fallback": True}))
    add_connection(session, project, pool, "api")
    assert service.claim(session, 2)[0] == child.id


@pytest.mark.parametrize("sibling_state", ["merged", "needs_human", "cancelled"])
def test_api_crash_charge_without_active_owner_requires_human(group_session, sibling_state):
    session, project, pool = group_session
    root = make_task(session, project, api_budget_usd=5)
    make_task(session, project, parent=root, state=sibling_state, api_spent_usd=5, api_reserved_usd=0)
    child = make_task(session, project, parent=root, state="queued", phase="implement", explicit_api=True)
    add_connection(session, project, pool, "api")
    assert service.claim(session, 2) is None
    assert child.state == "needs_human"


@pytest.mark.parametrize("connection_available", [True, False])
def test_permanently_spent_api_budget_requires_human(group_session, connection_available):
    session, project, pool = group_session
    root = make_task(session, project, api_budget_usd=5)
    make_task(session, project, parent=root, api_spent_usd=5)
    child = make_task(session, project, parent=root, state="queued", phase="implement", explicit_api=True)
    if connection_available:
        add_connection(session, project, pool, "api")
    assert service.claim(session, 2) is None
    assert child.state == "needs_human"


@pytest.mark.parametrize(
    "api_role, phase, other_phase", [("writer", "implement", "review"), ("reviewer", "review", "implement")]
)
def test_api_role_override_authorizes_only_that_role(group_session, api_role, phase, other_phase):
    session, project, pool = group_session
    root = make_task(session, project, api_budget_usd=5)
    child = make_task(session, project, parent=root, phase=phase, role_config={api_role: "api"})
    api = add_connection(session, project, pool, "api")
    add_connection(session, project, pool, "codex")
    add_connection(session, project, pool, "claude")
    assert service.choose_connection(session, child, project).id == api.id
    child.phase = other_phase
    assert service.choose_connection(session, child, project).provider != "api"
    root.api_budget_usd = 0
    child.phase = phase
    assert service.choose_connection(session, child, project) is None


def test_runtime_override_and_explicit_whole_task_api(group_session):
    session, project, pool = group_session
    root = make_task(session, project, api_budget_usd=5)
    child = make_task(session, project, parent=root, phase="implement", role_config={"writer": "claude"})
    add_connection(session, project, pool, "codex")
    claude = add_connection(session, project, pool, "claude")
    api = add_connection(session, project, pool, "api")
    assert service.choose_connection(session, child, project).id == claude.id
    child.explicit_api = True
    assert service.choose_connection(session, child, project).id == api.id


def test_fallback_uses_group_budget_but_still_requires_project_opt_in(group_session):
    session, project, pool = group_session
    root = make_task(session, project, api_budget_usd=5)
    child = make_task(session, project, parent=root, phase="implement")
    api = add_connection(session, project, pool, "api")
    assert service.choose_connection(session, child, project) is None
    project.secret = db.encrypt(json.dumps({"allow_paid_fallback": True}))
    assert service.choose_connection(session, child, project).id == api.id


def test_exhausted_tree_does_not_consume_independent_tree_budget(group_session):
    session, project, _ = group_session
    project.max_seconds = 60
    root = make_task(session, project, active_seconds=60)
    blocked = make_task(session, project, parent=root, state="queued")
    independent = make_task(session, project, state="queued")
    assert service.claim(session, 2)[0] == independent.id
    assert blocked.state == "needs_human"
    assert service.execution_limits(session, independent)["active_seconds"] == 0


def test_corrupt_parent_cycle_fails_closed_without_blocking_independent_tasks(group_session):
    session, project, _ = group_session
    first = make_task(session, project, state="queued")
    second = make_task(session, project, parent=first)
    session.add(db.Link(task_id=second.id, other_id=first.id, kind="parent"))
    session.flush()
    with pytest.raises(ValueError, match="Cycle"):
        service.execution_limits(session, first)
    independent = make_task(session, project, state="queued")
    assert service.claim(session, 2)[0] == independent.id
    assert first.state == "needs_human"


def test_corrupt_multiple_parents_cannot_share_limits_between_trees(group_session):
    session, project, _ = group_session
    first = make_task(session, project)
    second = make_task(session, project)
    child = make_task(session, project, parent=first)
    session.add(db.Link(task_id=second.id, other_id=child.id, kind="parent"))
    session.flush()
    for member in (first, second, child):
        with pytest.raises(ValueError, match="multiple parents"):
            service.execution_limits(session, member)


def test_delegated_repair_child_then_parent_can_integrate_during_incident(group_session):
    session, project, _ = group_session
    normal = make_task(
        session, project, state="queued", phase="verify", candidate_sha="a" * 40, verified_sha="a" * 40
    )
    repair = make_task(session, project, state="queued", phase="collect", incident=True)
    child = make_task(session, project, parent=repair, state="queued", phase="integrate", plan_depth=1)
    project.incident_id, project.integration_task = repair.id, normal.id
    assert service.claim(session, 2)[0] == child.id
    assert repair.state == "waiting_dependency"
    assert project.integration_task == child.id
    assert normal.phase == "integrate" and normal.verified_sha is None
    child.state = "merged"
    child.candidate_sha = child.verified_sha = child.reviewed_sha = child.result_sha = "b" * 40
    child.time_reserved_seconds = 0
    child.lease_owner = child.lease_until = None
    project.integration_task = None
    assert service.dependency_reason(session, repair) is None
    assert service.claim(session, 2)[0] == repair.id
    repair.state, repair.phase, repair.time_reserved_seconds = "queued", "integrate", 0
    assert service.claim(session, 2)[0] == repair.id
    assert project.integration_task == repair.id
    unrelated = make_task(session, project)
    session.add(db.Link(task_id=unrelated.id, other_id=child.id, kind="depends_on"))
    session.flush()
    assert "preprod" in service.dependency_reason(session, unrelated)


def test_nested_incident_does_not_authorize_siblings_with_same_root(group_session):
    session, project, _ = group_session
    common = make_task(session, project)
    repair = make_task(session, project, parent=common, incident=True)
    child = make_task(session, project, parent=repair, state="merged", result_sha="a" * 40)
    sibling = make_task(session, project, parent=common, state="queued", phase="integrate")
    session.add(db.Link(task_id=sibling.id, other_id=child.id, kind="depends_on"))
    session.flush()
    project.incident_id = repair.id
    assert service.in_incident_tree(session, repair, repair.id)
    assert service.in_incident_tree(session, child, repair.id)
    assert not service.in_incident_tree(session, sibling, repair.id)
    assert not service.in_incident_tree(session, common, repair.id)
    assert service.dependency_reason(session, repair) is None
    assert "preprod" in service.dependency_reason(session, sibling)
    assert service.claim(session, 2) is None
    assert sibling.state == "waiting_dependency"


@pytest.mark.parametrize("normal_state", ["running", "recovering"])
def test_repair_descendant_never_steals_live_normal_reservation(group_session, normal_state):
    session, project, _ = group_session
    normal = make_task(
        session,
        project,
        state=normal_state,
        phase="verify",
        lease_owner="live-worker",
        candidate_sha="a" * 40,
        verified_sha="a" * 40,
    )
    repair = make_task(session, project, incident=True)
    child = make_task(session, project, parent=repair, state="queued", phase="integrate")
    project.incident_id, project.integration_task = repair.id, normal.id
    assert service.claim(session, 2) is None
    assert child.state == "waiting_integration"
    assert project.integration_task == normal.id
    assert normal.verified_sha == "a" * 40
    normal.state, normal.lease_owner = "queued", None
    assert service.claim(session, 2)[0] == child.id
    assert project.integration_task == child.id
    assert normal.verified_sha is None


@pytest.mark.parametrize("active_state", ["running", "recovering"])
@pytest.mark.parametrize("active_side", ["left", "right"])
def test_parent_link_cannot_change_tree_budget_while_descendant_runs(
    group_session, active_state, active_side
):
    session, project, _ = group_session
    left = make_task(session, project, state="queued", verified_sha="a" * 40, reviewed_sha="a" * 40)
    right = make_task(session, project)
    make_task(session, project, parent=left if active_side == "left" else right, state=active_state)
    access = service.authenticate(session, (db.ROOT / "bootstrap-token").read_text())
    with pytest.raises(HTTPException) as raised:
        service.add_link(session, access, left.id, right.id, "parent")
    assert raised.value.status_code == 409
    assert left.verified_sha == left.reviewed_sha == "a" * 40


def test_parent_link_still_invalidates_acceptance_after_stopping_tree(group_session):
    session, project, _ = group_session
    left = make_task(
        session, project, state="queued", phase="verify", verified_sha="a" * 40, reviewed_sha="a" * 40
    )
    right = make_task(session, project)
    access = service.authenticate(session, (db.ROOT / "bootstrap-token").read_text())
    service.add_link(session, access, left.id, right.id, "parent")
    assert left.verified_sha is None and left.reviewed_sha is None
    assert left.phase == "implement"


def test_cancel_delegated_stops_only_generated_descendants(group_session):
    session, project, _ = group_session
    root = make_task(session, project)
    queued = make_task(session, project, parent=root, state="queued", plan_depth=1)
    grandchild = make_task(session, project, parent=queued, state="needs_human", plan_depth=2)
    running = make_task(session, project, parent=root, state="running", plan_depth=1)
    recovering = make_task(session, project, parent=root, state="recovering", plan_depth=1)
    manual = make_task(session, project, parent=root, state="queued")
    manual_child = make_task(session, project, parent=manual, state="queued", plan_depth=1)
    other_project = db.Project(
        org_id=project.org_id, name="Independent", remote="https://example.invalid/independent.git"
    )
    session.add(other_project)
    session.flush()
    cross_project = make_task(session, other_project, parent=root, state="queued", plan_depth=1)
    project.integration_task = queued.id
    service.cancel_delegated(session, root)
    assert queued.state == grandchild.state == "cancelled"
    assert queued.cancel_requested and grandchild.cancel_requested
    assert running.state == "running" and running.cancel_requested
    assert recovering.state == "recovering" and recovering.cancel_requested
    assert manual.state == manual_child.state == "queued"
    assert not manual.cancel_requested and not manual_child.cancel_requested
    assert cross_project.state == "queued" and not cross_project.cancel_requested
    assert project.integration_task is None
    project.integration_task = running.id
    service.cancel_delegated(session, root)
    assert project.integration_task == running.id


def test_duplicate_cancels_delegated_work(group_session):
    session, project, _ = group_session
    root = make_task(session, project, state="queued")
    other = make_task(session, project)
    child = make_task(session, project, parent=root, state="running", plan_depth=1)
    access = service.authenticate(session, (db.ROOT / "bootstrap-token").read_text())
    service.add_link(session, access, root.id, other.id, "duplicate")
    assert root.state == "duplicate"
    assert child.state == "running" and child.cancel_requested
