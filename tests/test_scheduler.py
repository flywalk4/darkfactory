import json

import pytest

from darkfactory import db, service


@pytest.fixture
def scheduler(setup_project):
    project, pool = setup_project
    with db.Session() as session:
        yield session, session.get(db.Project, project["id"]), session.get(db.Pool, pool["id"])


def task(session, project, **values):
    task_id = db.uid()
    fields = {
        "id": task_id,
        "project_id": project.id,
        "org_id": project.org_id,
        "title": "Scheduler regression",
        "request": "Keep the scheduler moving safely",
        "branch": "darkfactory/" + task_id,
        "state": "queued",
        "phase": "prepare",
    }
    fields.update(values)
    result = db.Task(**fields)
    session.add(result)
    session.flush()
    return result


def connection(session, project, pool, provider, credentials=None, **values):
    fields = {
        "org_id": project.org_id,
        "name": provider,
        "account_key": db.uid(),
        "provider": provider,
        "pool_ids": [pool.id],
        "secret": db.encrypt(json.dumps(credentials or {})),
    }
    fields.update(values)
    result = db.Connection(**fields)
    session.add(result)
    session.flush()
    return result


def test_incident_preempts_idle_reservation_then_normal_task_reintegrates(scheduler):
    session, project, _ = scheduler
    normal = task(
        session,
        project,
        phase="verify",
        candidate_sha="a" * 40,
        verified_sha="a" * 40,
        reviewed_sha="a" * 40,
    )
    repair = task(session, project, incident=True, phase="integrate")
    project.integration_task, project.incident_id = normal.id, repair.id
    claimed = service.claim(session, 2)
    assert claimed[0] == repair.id
    assert project.integration_task == repair.id
    assert normal.phase == "integrate"
    assert normal.state == "waiting_integration"
    assert normal.verified_sha is None and normal.reviewed_sha is None
    assert normal.candidate_sha == "a" * 40
    repair.state, repair.lease_owner, repair.lease_until = "merged", None, None
    project.incident_id = project.integration_task = None
    assert service.claim(session, 2)[0] == normal.id
    assert normal.phase == "integrate"


@pytest.mark.parametrize("state, lease_until", [("running", 60), ("recovering", -60)])
def test_incident_never_steals_running_or_recovering_reservation(scheduler, state, lease_until):
    session, project, _ = scheduler
    normal = task(
        session,
        project,
        state=state,
        phase="verify",
        lease_owner="live-owner",
        lease_until=db.now() + lease_until,
        verified_sha="a" * 40,
    )
    repair = task(session, project, incident=True, phase="integrate")
    project.integration_task, project.incident_id = normal.id, repair.id
    assert service.claim(session, 2) is None
    assert project.integration_task == normal.id
    assert normal.verified_sha == "a" * 40
    assert normal.phase == "verify"
    normal.state, normal.lease_owner, normal.lease_until = "queued", None, None
    assert service.claim(session, 2)[0] == repair.id
    assert project.integration_task == repair.id
    assert normal.verified_sha is None


def test_organization_limit_does_not_block_other_organizations(scheduler):
    session, project, _ = scheduler
    organization = session.get(db.Organization, project.org_id)
    organization.max_active = 1
    task(session, project, state="running", phase="implement")
    blocked = task(session, project, phase="implement")
    other_org = db.Organization(name="Independent organization", max_active=1)
    session.add(other_org)
    session.flush()
    other_project = db.Project(org_id=other_org.id, name="Other", remote="https://example.invalid/other.git")
    session.add(other_project)
    session.flush()
    permitted = task(session, other_project)
    assert service.claim(session, 5)[0] == permitted.id
    assert blocked.state == "queued"


def test_pool_limit_applies_across_projects_and_recovers_on_release(scheduler):
    session, project, pool = scheduler
    session.get(db.Organization, project.org_id).max_active = 5
    pool.policy = {"max_active": 1}
    active = task(session, project, state="running", phase="implement")
    other = db.Project(
        org_id=project.org_id,
        name="Shares pool",
        remote="https://example.invalid/other.git",
        writer_pool=pool.id,
    )
    session.add(other)
    session.flush()
    waiting = task(session, other, phase="implement")
    assert service.claim(session, 5) is None
    active.state = "merged"
    # The pool slot is free again; the task now only waits for a connection of its own.
    assert service.claim(session, 5) is None
    assert waiting.state == "waiting_pool" and "подключения" in waiting.reason


def test_pool_switch_limit_keeps_counter_and_requires_explicit_credit(scheduler):
    session, project, pool = scheduler
    pool.policy = {"max_switches": 1}
    waiting = task(session, project, switches=1)
    assert service.claim(session, 2) is None
    assert waiting.state == "needs_human"
    assert waiting.switches == 1
    waiting.state, waiting.switch_credit = "queued", 1
    assert service.claim(session, 2)[0] == waiting.id
    assert waiting.switches == 1


@pytest.mark.parametrize("phase", ["implement", "review"])
def test_provider_and_model_allowlist_both_apply(scheduler, phase):
    session, project, pool = scheduler
    pool.policy = {"allowed_providers": ["claude"], "allowed_models": ["permitted"]}
    waiting = task(session, project, phase=phase)
    connection(session, project, pool, "codex", model="permitted", priority=0)
    connection(session, project, pool, "claude", model="denied", priority=0)
    permitted = connection(session, project, pool, "claude", model="permitted", priority=10)
    assert service.choose_connection(session, waiting, project).id == permitted.id


def test_quota_retry_remains_filtered_by_pool_policy(scheduler):
    session, project, pool = scheduler
    pool.policy = {"allowed_models": ["permitted"]}
    waiting = task(session, project, phase="implement")
    denied = connection(
        session, project, pool, "codex", model="denied", status="quota", retry_at=db.now() - 1
    )
    future = connection(
        session, project, pool, "codex", model="permitted", status="quota", retry_at=db.now() + 3600
    )
    assert service.choose_connection(session, waiting, project) is None
    assert denied.status == "ready"
    assert future.status == "quota"
    future.retry_at = db.now() - 1
    assert service.choose_connection(session, waiting, project).id == future.id
    assert future.status == "ready"
    assert future.quota_source == "recheck_due"


@pytest.mark.parametrize(
    "enabled, budget", [(False, 0), (False, 5), (True, 0), (True, 5), ("false", 5), (1, 5)]
)
def test_paid_fallback_requires_encrypted_opt_in_and_positive_budget(scheduler, enabled, budget):
    session, project, pool = scheduler
    project.secret = db.encrypt(json.dumps({"allow_paid_fallback": enabled}))
    waiting = task(session, project, phase="implement", api_budget_usd=budget)
    api = connection(session, project, pool, "api")
    selected = service.choose_connection(session, waiting, project)
    assert (selected.id if selected else None) == (api.id if enabled is True and budget > 0 else None)


def test_default_never_selects_paid_api_and_explicit_api_never_selects_subscription(scheduler):
    session, project, pool = scheduler
    waiting = task(session, project, phase="implement", api_budget_usd=5)
    api = connection(session, project, pool, "api")
    assert service.choose_connection(session, waiting, project) is None
    subscription = connection(session, project, pool, "codex", priority=0)
    assert service.choose_connection(session, waiting, project).id == subscription.id
    waiting.explicit_api = True
    assert service.choose_connection(session, waiting, project).id == api.id
    api.status = "quota"
    assert service.choose_connection(session, waiting, project) is None


def test_failed_background_job_is_logged_once_and_resubmitted(caplog):
    import concurrent.futures

    from darkfactory import worker

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient database error")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = worker.periodic(None, pool, flaky)
        concurrent.futures.wait([future])
        # The failed run must not re-raise into the tick; a fresh run replaces it.
        future = worker.periodic(future, pool, flaky)
        concurrent.futures.wait([future])
        assert future.exception() is None
    assert len(calls) == 2
    assert "Background job flaky failed" in caplog.text


def test_cancelled_background_job_is_resubmitted_without_raising():
    import concurrent.futures

    from darkfactory import worker

    def job():
        return None

    cancelled = concurrent.futures.Future()
    cancelled.cancel()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = worker.periodic(cancelled, pool, job)
        concurrent.futures.wait([future])
        assert future is not cancelled
        assert future.exception() is None


def test_mechanical_phases_and_monitors_do_not_take_model_slots(scheduler):
    session, project, pool = scheduler
    session.get(db.Organization, project.org_id).max_active = 1
    pool.policy = {"max_active": 1}
    task(session, project, state="running", phase="implement")
    # Checks hold no model connection: a full pool must not hold them back.
    checks = task(session, project, phase="checks")
    assert service.claim(session, 5)[0] == checks.id
    task(session, project, state="running", phase="monitor", system_check=True)
    second = task(session, project, phase="monitor", system_check=True)
    writer = task(session, project, phase="implement")
    # One monitor at a time, and the model slot stays with the running writer.
    assert service.claim(session, 5) is None
    assert second.state == "queued" and writer.state == "queued"


def test_hold_file_stops_new_claims_until_removed(scheduler, monkeypatch, tmp_path):
    session, project, _ = scheduler
    hold = tmp_path / "hold"
    monkeypatch.setattr(service, "HOLD", hold)
    waiting = task(session, project)
    hold.write_text("")
    assert service.claim(session, 5) is None and waiting.state == "queued"
    hold.unlink()
    assert service.claim(session, 5)[0] == waiting.id


def test_a_later_writer_on_the_same_files_waits(scheduler):
    import time as clock

    from darkfactory import worker

    session, project, _ = scheduler
    first = task(session, project, state="running", phase="review", created=clock.time() - 100)
    second = task(session, project, phase="implement", created=clock.time())
    third = task(session, project, phase="implement", created=clock.time() + 1)
    session.commit()
    for item, files in ((first, ["src/a.ts"]), (second, ["src/a.ts", "src/b.ts"]), (third, ["src/c.ts"])):
        artifacts = db.ROOT / "artifacts" / item.id
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "locate.json").write_text(json.dumps({"files": [{"path": f} for f in files]}))
    assert worker.overlapping_work(second) == {"src/a.ts"}
    assert worker.overlapping_work(third) == set()
    # The earlier task is not held back by a later one that has not started writing.
    assert worker.overlapping_work(first) == set()
    # A later task whose writer already started keeps going; the earlier one waits for it.
    run = db.ROOT / "artifacts" / second.id / "browser" / "run1"
    run.mkdir(parents=True)
    (run / "execution.json").write_text(json.dumps({"role": "implement"}))
    assert worker.overlapping_work(first) == {"src/a.ts"}
    second.role_config = {"wait_files_until": clock.time() + 60}
    third.state = "running"
    session.flush()
    assert service.claim(session, 5) is None or service.claim(session, 5)[0] != second.id


def test_a_stop_awaiting_the_supervisor_keeps_its_files(scheduler):
    import time as clock

    from darkfactory import director_stops, worker

    session, project, _ = scheduler
    stopped = task(session, project, state="needs_human", phase="review", created=clock.time() - 100)
    later = task(session, project, phase="implement", created=clock.time())
    session.commit()
    for item in (stopped, later):
        artifacts = db.ROOT / "artifacts" / item.id
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "locate.json").write_text(json.dumps({"files": [{"path": "src/a.ts"}]}))
    db.event(session, stopped, "limit", "Исчерпаны циклы исправления")
    session.commit()
    assert worker.overlapping_work(later) == {"src/a.ts"}
    # A supervisor that cannot get to the stop (no free subscription) does not hold the files for good.
    updated = stopped.updated
    stopped.updated = clock.time() - director_stops.AWAIT_SECONDS - 1
    session.commit()
    assert worker.overlapping_work(later) == set()
    stopped.updated = updated
    session.commit()
    # Once the supervisor has looked (and handed the stop to the owner), the files are free.
    director_stops.mark(session, stopped)
    session.commit()
    assert worker.overlapping_work(later) == set()
