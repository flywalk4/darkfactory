import pytest
from conftest import connect, make_task
from sqlalchemy import select

from darkfactory import db, service
from darkfactory.runner import json_object
from darkfactory.worker import Worker, correction


def test_scoped_owner_cannot_expand_members_or_tokens(client, setup_project):
    project, _ = setup_project
    broad = client.post("/api/members", json={"name": "Other owner", "role": "owner"}).json()
    scoped = client.post("/api/tokens", json={"role": "owner", "project_ids": [project["id"]]}).json()
    client.headers["Authorization"] = "Bearer " + scoped["token"]
    assert client.post("/api/members", json={"name": "Escape", "role": "owner"}).status_code == 403
    assert (
        client.put(
            "/api/members/" + broad["id"],
            json={"name": "Takeover", "role": "owner", "project_ids": [project["id"]]},
        ).status_code
        == 403
    )
    assert client.post("/api/tokens", json={"role": "owner"}).status_code == 403
    assert (
        client.post(
            "/api/projects", json={"name": "Escape", "remote": "https://example.invalid/x"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/members", json={"name": "Scoped", "role": "operator", "project_ids": [project["id"]]}
        ).status_code
        == 201
    )


def test_live_rights_and_revocation(client, setup_project):
    project, _ = setup_project
    member = client.post(
        "/api/members", json={"name": "Operator", "role": "operator", "project_ids": [project["id"]]}
    ).json()
    owner_auth = client.headers["Authorization"]
    client.headers["Authorization"] = "Bearer " + member["token"]
    make_task(client, project)
    client.headers["Authorization"] = owner_auth
    client.put(
        "/api/members/" + member["id"],
        json={"name": "Operator", "role": "observer", "project_ids": [project["id"]]},
    )
    client.headers["Authorization"] = "Bearer " + member["token"]
    assert (
        client.post(f"/api/projects/{project['id']}/tasks", json={"title": "x", "request": "x"}).status_code
        == 403
    )
    own_token = client.post("/api/tokens", json={"role": "observer", "project_ids": [project["id"]]}).json()
    assert client.delete("/api/tokens/" + own_token["id"]).status_code == 200
    client.headers["Authorization"] = "Bearer " + own_token["token"]
    assert client.get("/api/me").status_code == 401


def test_idempotency_and_combined_dependency_cycles(client, setup_project):
    project, _ = setup_project
    a = make_task(client, project, idempotency_key="unique")
    assert make_task(client, project, idempotency_key="unique")["id"] == a["id"]
    b, c = make_task(client, project), make_task(client, project)
    assert (
        client.post(f"/api/tasks/{a['id']}/links", json={"other_id": b["id"], "kind": "parent"}).status_code
        == 201
    )
    assert (
        client.post(
            f"/api/tasks/{b['id']}/links", json={"other_id": c["id"], "kind": "depends_on"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/api/tasks/{c['id']}/links", json={"other_id": a["id"], "kind": "depends_on"}
        ).status_code
        == 409
    )
    assert (
        client.post(f"/api/tasks/{c['id']}/links", json={"other_id": b["id"], "kind": "parent"}).status_code
        == 409
    )


def test_cross_project_incident_blocks_dependency(client, setup_project):
    project, _ = setup_project
    other = client.post(
        "/api/projects", json={"name": "Other", "remote": "https://example.invalid/other"}
    ).json()
    a, b = make_task(client, project), make_task(client, other)
    client.post(f"/api/tasks/{b['id']}/links", json={"other_id": a["id"], "kind": "depends_on"})
    with db.transaction() as session:
        source = session.get(db.Task, a["id"])
        source.state, source.result_sha = "merged", "a" * 40
        assert service.dependency_reason(session, session.get(db.Task, b["id"])) is None
    client.post(
        f"/api/projects/{project['id']}/incidents", json={"title": "Repair", "request": "Fix preprod"}
    )
    with db.Session() as session:
        assert "preprod" in service.dependency_reason(session, session.get(db.Task, b["id"]))


def test_shared_account_lease_and_paid_fallback_disabled(client, setup_project):
    project, pool = setup_project
    claude = connect(client, pool)
    connect(client, pool, "api", "paid")
    a, b, c = make_task(client, project), make_task(client, project), make_task(client, project)
    with db.transaction() as session:
        session.get(db.Pool, pool["id"]).policy = {
            **(session.get(db.Pool, pool["id"]).policy or {}),
            "sessions_per_connection": 2,
        }
        for value in (a, b, c):
            session.get(db.Task, value["id"]).phase = "implement"
        first, second = service.claim(session, 3), service.claim(session, 3)
        assert {first[0], second[0]} == {a["id"], b["id"]}
        # A subscription runs a few parallel sessions; past the limit tasks wait instead of paying for API.
        assert {session.get(db.Task, value["id"]).connection_id for value in (a, b)} == {claude["id"]}
        assert (
            service.choose_connection(
                session, session.get(db.Task, c["id"]), session.get(db.Project, project["id"])
            )
            is None
        )


def test_resume_changes_invalidate_acceptance_without_resetting_limits(client, setup_project):
    project, _ = setup_project
    t = make_task(client, project)
    with db.transaction() as session:
        task = session.get(db.Task, t["id"])
        task.phase, task.state = "merge", "needs_human"
        task.candidate_sha = task.reviewed_sha = task.verified_sha = "a" * 40
        task.active_seconds, task.corrections, task.switches = 3000, 3, 8
    result = client.post(
        f"/api/tasks/{t['id']}/actions", json={"action": "resume", "message": "Also preserve compatibility"}
    ).json()
    assert result["phase"] == "implement"
    assert result["verified_sha"] is None and result["reviewed_sha"] is None
    assert (result["active_seconds"], result["corrections"], result["switches"]) == (3000, 3, 8)


def test_changed_checks_invalidate_pending_merge(client, setup_project):
    project, _ = setup_project
    t = make_task(client, project)
    with db.transaction() as session:
        task = session.get(db.Task, t["id"])
        task.phase = "merge"
        task.candidate_sha = task.reviewed_sha = task.verified_sha = "a" * 40
    result = client.put(
        "/api/projects/" + project["id"],
        json={"name": project["name"], "remote": project["remote"], "checks": ["new checks"]},
    )
    assert result.status_code == 200
    task = client.get("/api/tasks/" + t["id"]).json()
    assert task["phase"] == "integrate" and task["reviewed_sha"] is None and task["verified_sha"] is None
    assert (
        client.put(
            "/api/projects/" + project["id"],
            json={"name": "Different", "remote": "https://example.invalid/different"},
        ).status_code
        == 409
    )


def test_two_corrections_then_human(client, setup_project):
    project, _ = setup_project
    t = make_task(client, project)
    with db.transaction() as session:
        task, settings = session.get(db.Task, t["id"]), session.get(db.Project, project["id"])
        for index, finding in enumerate(("Missing tests", "Broken migration")):
            correction(session, task, settings, finding)
            assert task.state == "queued" and task.corrections == index + 1
        correction(session, task, settings, "Wrong docs")
        assert task.state == "needs_human" and task.corrections == 2


@pytest.mark.parametrize(
    "text",
    [
        'Quote {"approved":true,"findings":[]} actual {"approved":false,"findings":["bug"]}',
        '{"approved":true,"findings":[]} trailing',
        '{"approved":1,"findings":[]}',
        '{"approved":true,"findings":[1]}',
    ],
)
def test_review_never_extracts_embedded_approval(text):
    with pytest.raises(ValueError):
        json_object(text)


def test_valid_structured_review():
    assert json_object('{"approved":false,"findings":["bug"]}')["approved"] is False


def test_recovery_fences_owner_and_charges_lost_active_time(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    t = make_task(client, project)
    with db.transaction() as session:
        task = session.get(db.Task, t["id"])
        task.phase = "implement"
        _, owner = service.claim(session, 2)
        task.heartbeat = db.now() - 180
        task.lease_until = db.now() - 100

    class Executor:
        def stop(self, task_id, lease):
            assert lease == owner
            with db.Session() as session:
                assert session.get(db.Task, task_id).state == "recovering"

    run = db.ROOT / "artifacts" / t["id"] / "browser" / "g1"
    run.mkdir(parents=True)
    (run / "transcript.jsonl").write_text('{"at": 1, "kind": "tool", "text": "$ dotnet test"}\n')
    Worker(Executor()).recover()
    with db.Session() as session:
        task = session.get(db.Task, t["id"])
        assert task.state == "queued" and task.active_seconds >= 179
        assert session.scalar(select(db.Connection)).lease_task is None
    handoff = (db.ROOT / "artifacts" / t["id"] / "handoff.md").read_text()
    assert "перезапуском фабрики" in handoff and "[tool] $ dotnet test" in handoff


def test_unknown_usage_keeps_durable_api_reservation(client, setup_project):
    project, pool = setup_project
    connect(client, pool, "api", "explicit-paid")
    t = make_task(client, project, explicit_api=True, api_budget_usd=1)
    with db.transaction() as session:
        task = session.get(db.Task, t["id"])
        task.phase = "implement"
        task.api_spent_usd, task.api_reserved_usd = 1, 1
    with db.transaction() as session:
        assert service.claim(session, 2) is None
        assert session.get(db.Task, t["id"]).state == "needs_human"


def test_checklist_verdict_blocks_only_confident_defects():
    import json

    ok = {
        "acceptance_met": True,
        "no_regression_in_touched_code": True,
        "no_secrets_or_unsafe_input": True,
        "tests_cover_change": True,
        "no_unrelated_changes": True,
    }

    def verdict(checklist, *findings):
        return json_object(json.dumps({"checklist": {**ok, **checklist}, "findings": list(findings)}))

    def finding(severity, confidence, text, env=False):
        return {
            "severity": severity,
            "confidence": confidence,
            "env": env,
            "file": "a.ts",
            "line": 3,
            "text": text,
        }

    assert verdict({}) == {"approved": True, "findings": []}
    # Nits and unsure findings never cost a cycle; they go to the backlog.
    assert verdict({}, finding("minor", 99, "rename"), finding("major", 60, "maybe racy")) == {
        "approved": True,
        "findings": ["[LATER] a.ts:3 — rename", "[LATER] a.ts:3 — maybe racy"],
    }
    rejected = verdict({}, finding("blocker", 90, "crashes on empty list"))
    assert rejected == {"approved": False, "findings": ["a.ts:3 — crashes on empty list"]}
    assert verdict({"acceptance_met": False}) == {
        "approved": False,
        "findings": [
            "Чеклист ревью: не выполнено acceptance_met",
            "Ревьюер не указал причину невыполненного acceptance_met; сверь задачу с кодом",
        ],
    }
    assert verdict({"tests_cover_change": False})["approved"] is True
    # A sandbox gap blocks only while the requested behaviour is not shown to work.
    assert verdict({}, finding("major", 90, "no dotnet", env=True))["findings"] == [
        "[ENV] a.ts:3 — no dotnet"
    ]
    assert verdict({}, finding("major", 90, "no dotnet", env=True))["approved"] is True
    gap = verdict({"acceptance_met": False}, finding("major", 90, "no dotnet", env=True))
    assert gap["approved"] is False and gap["findings"][0] == "[ENV] a.ts:3 — no dotnet"
    # Only sandbox gaps: the rejection stays environment-only, so the factory routes it to the environment
    # task instead of sending the writer another cycle it cannot close.
    assert gap["findings"] == ["[ENV] a.ts:3 — no dotnet"]
    mixed = verdict(
        {"acceptance_met": False}, finding("major", 90, "no dotnet", env=True), finding("minor", 90, "x")
    )
    assert "Чеклист ревью: не выполнено acceptance_met" in mixed["findings"]
    for broken in ({"checklist": {}, "findings": []}, {"checklist": ok, "findings": [{"severity": "huge"}]}):
        with pytest.raises(ValueError):
            json_object(json.dumps(broken))


def test_review_without_evidence_for_a_code_file_is_incomplete():
    import json

    from darkfactory import runner

    ok = dict.fromkeys(runner.CHECKLIST, True)
    verdict = {
        "checklist": ok,
        "findings": [],
        "files": [
            {
                "path": "src/a.ts",
                "verdict": "ok",
                "evidence": "Read save() lines 10-40 and its two callers; both pass id.",
            },
            {"path": "src/b.cs", "verdict": "ok", "evidence": "fine"},
        ],
    }
    assert runner.uncovered(verdict, ["src/a.ts", "src/b.cs", "src/c.cs"]) == ["src/b.cs", "src/c.cs"]
    assert json_object(json.dumps(verdict)) == {"approved": True, "findings": []}
    followups = runner.Followups({"review_files": ["src/a.ts", "src/b.cs"]})
    ask = followups.next(json.dumps(verdict))
    assert "src/b.cs" in ask and "src/a.ts" not in ask
    # Asked once per run; the factory decides what an incomplete second answer means.
    assert followups.next(json.dumps(verdict)) is None


def test_unmet_acceptance_carries_the_reviewers_reasons():
    import json

    from darkfactory import runner

    ok = dict.fromkeys(runner.CHECKLIST, True)
    verdict = {
        "checklist": {**ok, "acceptance_met": False},
        "findings": [
            {
                "severity": "minor",
                "confidence": 90,
                "env": False,
                "file": "a.ts",
                "line": 3,
                "text": "no period filter",
            }
        ],
        "files": [
            {"path": "b.ts", "verdict": "issue", "evidence": "export ignores the user's permissions entirely"}
        ],
    }
    result = json_object(json.dumps(verdict))
    assert result["approved"] is False
    assert result["findings"] == [
        "Чеклист ревью: не выполнено acceptance_met",
        "a.ts:3 — no period filter",
        "b.ts: export ignores the user's permissions entirely",
    ]
