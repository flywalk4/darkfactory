"""Persist provider observations without presenting unknown limits as available quota."""

import pytest
from conftest import connect, make_task
from test_pipeline import seed
from test_scheduler import connection as scheduler_connection
from test_scheduler import task as scheduler_task

from darkfactory import db, service
from darkfactory.worker import Worker


@pytest.mark.parametrize(("status", "remaining"), [("ok", 37), ("quota", 0), ("quota", None), ("auth", None)])
def test_provider_quota_survives_worker_session_and_is_visible_in_api(
    client, setup_project, tmp_path, status, remaining
):
    project, pool = setup_project
    connection = connect(client, pool, "codex")
    reset = db.now() + 3600
    snapshot = (
        {"remaining_percent": remaining, "reset_at": reset, "source": "provider"}
        if remaining is not None
        else {}
    )
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(seed(tmp_path))
        row = session.get(db.Connection, connection["id"])
        row.quota_details = {"remaining_percent": 91}
        row.quota_source, row.quota_observed = "old observation", db.now() - 100
    task = make_task(client, project, plan=True)

    class Executor:
        def stop(self, *args):
            pass

        def run(self, task, project, connection, role, heartbeat, timeout):
            assert role == "plan" and heartbeat()
            return {
                "status": status,
                "quota": snapshot,
                "plan": {"plan": "Set value", "questions": []},
            }

    worker = Worker(Executor())
    for _ in range(2):  # Real Git preparation, then model planning result.
        with db.transaction() as session:
            claimed = service.claim(session, 2)
        assert claimed and claimed[0] == task["id"]
        worker.run(*claimed)
    observed = client.get("/api/connections").json()[0]
    assert observed["quota_details"] == {
        key: snapshot[key] for key in ("remaining_percent", "reset_at") if key in snapshot
    }
    assert observed["quota_observed"] > db.now() - 30
    assert observed["status"] == ("ready" if status == "ok" else status)
    assert observed["quota_source"] == (
        "provider" if status == "ok" else "provider_reset" if remaining == 0 else "provider_error"
    )
    if status == "quota":
        assert observed["retry_at"] >= reset if remaining == 0 else observed["retry_at"] < reset
    assert "secret" not in observed


def test_reconnect_clears_observations_from_previous_credentials(client, setup_project):
    _, pool = setup_project
    connection = connect(client, pool)
    with db.transaction() as session:
        row = session.get(db.Connection, connection["id"])
        row.status, row.retry_at = "quota", db.now() + 3600
        row.quota_details = {"remaining_percent": 0, "reset_at": row.retry_at}
        row.quota_source, row.quota_observed = "provider", db.now()
    response = client.put(
        f"/api/connections/{connection['id']}/credentials", json={"oauth_token": "new-test-credential"}
    )
    assert response.status_code == 200, response.text
    row = client.get("/api/connections").json()[0]
    assert row["status"] == "ready" and row["retry_at"] is None
    assert row["quota_details"] == {} and row["quota_observed"] is None
    assert row["quota_source"] == "unknown"


@pytest.fixture
def quota_pool(setup_project):
    project, pool = setup_project
    with db.Session() as session:
        yield session, session.get(db.Project, project["id"]), session.get(db.Pool, pool["id"])


def observed_connection(session, project, pool, remaining, *, provider="codex", age=0, **values):
    return scheduler_connection(
        session,
        project,
        pool,
        provider,
        quota_details={"remaining_percent": remaining},
        quota_observed=db.now() - age,
        quota_source="provider",
        **values,
    )


@pytest.mark.parametrize("phase", ["plan", "implement", "review"])
def test_fresh_low_quota_cannot_start_an_agent_turn(quota_pool, phase):
    session, project, pool = quota_pool
    waiting = scheduler_task(session, project, phase=phase)
    observed_connection(session, project, pool, 9, priority=0)
    safe = observed_connection(session, project, pool, 10, priority=20)
    assert service.choose_connection(session, waiting, project).id == safe.id


def test_known_quota_precedes_priority_but_preserves_provider_preference(quota_pool):
    session, project, pool = quota_pool
    project.writer = "codex"
    waiting = scheduler_task(session, project, phase="implement")
    unknown = scheduler_connection(session, project, pool, "codex", priority=0)
    safe = observed_connection(session, project, pool, 50, priority=100)
    other_runtime = observed_connection(session, project, pool, 90, provider="claude", priority=-1)
    assert service.choose_connection(session, waiting, project).id == safe.id
    safe.status = "disabled"
    assert service.choose_connection(session, waiting, project).id == unknown.id
    unknown.status = "disabled"
    assert service.choose_connection(session, waiting, project).id == other_runtime.id


def test_low_quota_falls_back_to_runtime_only_when_pool_allows(quota_pool):
    session, project, pool = quota_pool
    project.writer = "codex"
    waiting = scheduler_task(session, project, phase="implement")
    observed_connection(session, project, pool, 0)
    fallback = observed_connection(session, project, pool, 75, provider="claude")
    assert service.choose_connection(session, waiting, project).id == fallback.id
    pool.cross_runtime = False
    assert service.choose_connection(session, waiting, project) is None


@pytest.mark.parametrize("remaining,age", [(0, 301), (99, 301), (None, 0), (True, 0), (101, 0)])
def test_stale_or_unknown_reports_never_satisfy_known_quota_policy(quota_pool, remaining, age):
    session, project, pool = quota_pool
    waiting = scheduler_task(session, project, phase="implement")
    unverified = observed_connection(session, project, pool, remaining, age=age)
    assert service.choose_connection(session, waiting, project).id == unverified.id
    pool.policy = {"require_known_quota": True}
    assert service.choose_connection(session, waiting, project) is None
    unverified.quota_details = {"remaining_percent": 40}
    unverified.quota_observed = db.now()
    assert service.choose_connection(session, waiting, project).id == unverified.id


def test_custom_reserve_and_observation_age_are_applied(quota_pool):
    session, project, pool = quota_pool
    waiting = scheduler_task(session, project, phase="implement")
    pool.policy = {"min_remaining_percent": 30, "quota_max_age_seconds": 30}
    observed_connection(session, project, pool, 29, priority=0)
    stale = observed_connection(session, project, pool, 90, age=31, priority=1)
    safe = observed_connection(session, project, pool, 30, priority=100)
    assert service.choose_connection(session, waiting, project).id == safe.id
    safe.status = "disabled"
    assert service.choose_connection(session, waiting, project).id == stale.id


@pytest.mark.parametrize("cooldown_expired", [False, True])
def test_expired_reset_allows_recheck_without_claiming_known_quota(quota_pool, cooldown_expired):
    session, project, pool = quota_pool
    waiting = scheduler_task(session, project, phase="implement")
    expired = observed_connection(session, project, pool, 0)
    expired.quota_details = {"remaining_percent": 0, "reset_at": db.now() - 1}
    if cooldown_expired:
        expired.status, expired.retry_at = "quota", db.now() - 1
    assert service.choose_connection(session, waiting, project).id == expired.id
    assert expired.status == "ready"
    pool.policy = {"require_known_quota": True}
    assert service.choose_connection(session, waiting, project) is None


def test_expired_cooldown_without_reset_does_not_reuse_zero_report(quota_pool):
    session, project, pool = quota_pool
    waiting = scheduler_task(session, project, phase="implement")
    expired = observed_connection(session, project, pool, 0, status="quota", retry_at=db.now() - 1)
    assert service.choose_connection(session, waiting, project).id == expired.id
    assert expired.quota_source == "recheck_due"


def test_group_cooldown_blocks_known_and_unknown_accounts(quota_pool):
    session, project, pool = quota_pool
    waiting = scheduler_task(session, project, phase="implement")
    blocked = observed_connection(session, project, pool, 90, credentials={"quota_group": "shared"})
    scheduler_connection(
        session,
        project,
        pool,
        "claude",
        credentials={"quota_group": "shared"},
        status="quota",
        retry_at=db.now() + 600,
    )
    assert service.choose_connection(session, waiting, project) is None
    blocked.quota_details, blocked.quota_observed = {}, None
    assert service.choose_connection(session, waiting, project) is None


def test_explicit_api_keeps_budget_protection_and_ignores_subscription_reserve(quota_pool):
    session, project, pool = quota_pool
    pool.policy = {"min_remaining_percent": 100, "require_known_quota": True}
    waiting = scheduler_task(session, project, phase="implement", explicit_api=True, api_budget_usd=5)
    observed_connection(session, project, pool, 100)
    api = scheduler_connection(session, project, pool, "api")
    assert service.choose_connection(session, waiting, project).id == api.id
    waiting.api_budget_usd = 0
    assert service.choose_connection(session, waiting, project) is None


@pytest.mark.parametrize("personal_mode", ["0", "1"])
@pytest.mark.parametrize("report", ["unknown", "stale", "expired_reset"])
def test_claim_can_schedule_strict_personal_codex_metadata_probe(
    quota_pool, monkeypatch, personal_mode, report
):
    session, project, pool = quota_pool
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", personal_mode)
    pool.policy = {"require_known_quota": True}
    waiting = scheduler_task(session, project, phase="implement")
    personal = scheduler_connection(
        session, project, pool, "codex", credentials={"auth_method": "codex_local"}
    )
    if report != "unknown":
        personal.quota_details = {"remaining_percent": 0}
        personal.quota_observed, personal.quota_source = db.now(), "provider"
        if report == "stale":
            personal.quota_observed -= 301
        else:
            personal.quota_details = {"remaining_percent": 0, "reset_at": db.now() - 1}
    assert service.choose_connection(session, waiting, project) is None
    claimed = service.claim(session, 2)
    if personal_mode == "1":
        assert claimed[0] == waiting.id
        assert waiting.state == "running"
        assert waiting.connection_id == personal.id
        assert personal.lease_task == waiting.id
    else:
        assert claimed is None
        assert waiting.state == "waiting_pool"
        assert personal.lease_task is None
    # Selection never fabricates a successful quota observation.
    assert personal.quota_details.get("remaining_percent") in (None, 0)


@pytest.mark.parametrize(
    "provider,auth_method", [("codex", "siwc"), ("claude", "codex_local"), ("api", "codex_local")]
)
def test_quota_probe_cannot_bypass_other_executor_or_paid_permissions(
    quota_pool, monkeypatch, provider, auth_method
):
    session, project, pool = quota_pool
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    pool.policy = {"require_known_quota": True}
    waiting = scheduler_task(session, project, phase="implement")
    scheduler_connection(session, project, pool, provider, credentials={"auth_method": auth_method})
    assert service.choose_connection(session, waiting, project, allow_quota_probe=True) is None
    assert service.claim(session, 2) is None
    assert waiting.state == "waiting_pool"


def test_known_safe_runtime_always_precedes_personal_quota_probe(quota_pool, monkeypatch):
    session, project, pool = quota_pool
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    pool.policy = {"require_known_quota": True}
    project.writer = "codex"
    waiting = scheduler_task(session, project, phase="implement")
    probe = scheduler_connection(
        session, project, pool, "codex", credentials={"auth_method": "codex_local"}, priority=-100
    )
    safe = observed_connection(session, project, pool, 40, provider="claude", priority=100)
    assert service.choose_connection(session, waiting, project, allow_quota_probe=True).id == safe.id
    safe.status = "disabled"
    assert service.choose_connection(session, waiting, project, allow_quota_probe=True).id == probe.id
    project.writer, pool.cross_runtime = "claude", False
    assert service.choose_connection(session, waiting, project, allow_quota_probe=True) is None


def test_claim_does_not_attempt_probe_when_normal_selection_has_safe_runtime(quota_pool, monkeypatch):
    session, project, pool = quota_pool
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    pool.policy = {"require_known_quota": True}
    project.writer = "codex"
    waiting = scheduler_task(session, project, phase="implement")
    scheduler_connection(session, project, pool, "codex", credentials={"auth_method": "codex_local"})
    safe = observed_connection(session, project, pool, 30, provider="claude")
    selections = []
    original = service.choose_connection

    def choose(*args, **kwargs):
        selections.append(kwargs.get("allow_quota_probe", False))
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "choose_connection", choose)
    assert service.claim(session, 2)[0] == waiting.id
    assert waiting.connection_id == safe.id
    assert selections == [False]


@pytest.mark.parametrize("remaining", [0, 9])
@pytest.mark.parametrize("cooldown_expired", [False, True])
def test_strict_quota_probe_cannot_bypass_fresh_low_report_until_expiry(
    quota_pool, monkeypatch, remaining, cooldown_expired
):
    session, project, pool = quota_pool
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    pool.policy = {"require_known_quota": True}
    waiting = scheduler_task(session, project, phase="implement")
    personal = observed_connection(
        session, project, pool, remaining, credentials={"auth_method": "codex_local"}
    )
    if cooldown_expired:
        personal.status, personal.retry_at = "quota", db.now() - 1
    assert service.choose_connection(session, waiting, project, allow_quota_probe=True) is None
    assert service.claim(session, 2) is None
    personal.quota_observed = db.now() - 301
    assert service.choose_connection(session, waiting, project, allow_quota_probe=True).id == personal.id


@pytest.mark.parametrize("restriction", ["leased", "cooldown", "pool", "provider", "model", "group"])
def test_personal_quota_probe_preserves_pool_and_connection_guards(quota_pool, monkeypatch, restriction):
    session, project, pool = quota_pool
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    pool.policy = {"require_known_quota": True}
    waiting = scheduler_task(session, project, phase="implement")
    personal = scheduler_connection(
        session, project, pool, "codex", credentials={"auth_method": "codex_local", "quota_group": "shared"}
    )
    if restriction == "leased":
        # Busy with every session the pool allows.
        pool.policy = {**pool.policy, "sessions_per_connection": 1}
        busy = scheduler_task(session, project, state="running", phase="implement")
        busy.connection_id = personal.id
        personal.lease_task = busy.id
        session.flush()
    elif restriction == "cooldown":
        personal.status, personal.retry_at = "quota", db.now() + 100
    elif restriction == "pool":
        personal.pool_ids = []
    elif restriction == "provider":
        pool.policy = {**pool.policy, "allowed_providers": ["claude"]}
    elif restriction == "model":
        pool.policy = {**pool.policy, "allowed_models": ["permitted"]}
        personal.model = "denied"
    else:
        scheduler_connection(
            session,
            project,
            pool,
            "claude",
            credentials={"quota_group": "shared"},
            status="quota",
            retry_at=db.now() + 100,
        )
    assert service.choose_connection(session, waiting, project, allow_quota_probe=True) is None
    assert service.claim(session, 2) is None
    assert waiting.state == "waiting_pool"


def test_owner_rechecks_connection_after_manual_limit_reset(client, setup_project):
    _, pool = setup_project
    conn = connect(client, pool)
    assert client.post(f"/api/connections/{conn['id']}/recheck").status_code == 409
    with db.transaction() as session:
        row = session.get(db.Connection, conn["id"])
        row.status, row.retry_at = "quota", db.now() + 86400 * 5
        row.quota_details = {"remaining_percent": 0, "reset_at": db.now() + 86400 * 5}
    observed = client.post(f"/api/connections/{conn['id']}/recheck").json()
    assert (observed["status"], observed["retry_at"], observed["quota_details"]) == ("ready", None, {})
    assert observed["quota_source"] == "recheck_due"


def test_quota_retry_probes_at_least_hourly(monkeypatch):
    far = db.now() + 6 * 86400
    assert service.quota_retry(far) <= db.now() + 3600
    assert service.quota_retry(db.now() + 600) <= db.now() + 600
    assert service.quota_retry(db.now() - 50) <= db.now() + 5
    assert db.now() + 290 <= service.quota_retry(None) <= db.now() + 300
    monkeypatch.setenv("DARKFACTORY_QUOTA_RECHECK_SECONDS", "600")
    assert service.quota_retry(far) <= db.now() + 600
