"""Pure aggregation helpers of scripts/factory_metrics.py: no database, no artifacts."""

from darkfactory import metrics as fm


def entry(at, kind, text=""):
    return {"at": at, "kind": kind, "text": text}


def test_phase_pairs_ignore_unfinished_and_restarted_phases():
    events = [
        (0, "phase_started", "plan"),
        (60, "phase_finished", "plan"),
        (60, "phase_started", "implement"),
        (100, "phase_started", "implement"),
        (400, "phase_finished", "implement"),
        (400, "phase_started", "checks"),
    ]
    assert fm.phase_durations(events) == [("plan", 60), ("implement", 300)]


def test_command_key_unwraps_shells_env_cd_and_heredocs():
    assert fm.command_key("$ /bin/sh -lc 'cd src && npm test -- --ci'") == "npm test"
    assert fm.command_key("$ NODE_OPTIONS=--max-old-space-size=3072 npx jest a") == "npx jest"
    assert fm.command_key("$ /bin/sh -lc \"apply_patch <<'PATCH'\n*** Begin Patch\n\"") == "apply_patch"
    assert fm.command_key("$ /bin/sh -lc 'pwd; cat a b'") == "pwd"
    assert fm.command_key("$ (timeout 590 npm test)") == "timeout 590"
    assert fm.command_key("$ dotnet build x -c Release") == "dotnet build"
    assert fm.command_key('Bash {"command": "cd /w && git status --short"}') == "git status"
    assert fm.command_key('Read {"file_path": "/x"}') == "Read"


def test_command_times_pair_tool_with_next_output_only():
    entries = [
        entry(0, "tool", "$ git status"),
        entry(2, "output", "ok"),
        entry(3, "tool", "$ ls -la"),
        entry(4, "tool", "$ npm test"),
        entry(14, "message", "running"),
        entry(20, "output", "done"),
    ]
    assert fm.command_times(entries) == [("git status", 2), ("npm test", 16)]


def test_run_profile_setup_and_resume():
    entries = [
        entry(10, "status", "Запуск: implement · codex"),
        entry(11, "status", "Зависимости не менялись с прошлой установки — setup пропущен"),
        entry(40, "message", "[darkfactory] Продолжаю прежнюю сессию агента"),
        entry(41, "tool", "$ ls"),
    ]
    profile = fm.run_profile(entries)
    assert profile["agent"] == "codex"
    assert profile["setup_seconds"] == 30
    assert profile["setup_skipped"] and profile["resumed"]
    assert profile["tool_calls"] == 1


def usage(total, cached=0, output=0):
    return {"input_tokens": total, "cached_input_tokens": cached, "output_tokens": output}


def test_resumed_codex_run_subtracts_previous_run_of_same_role():
    runs = [
        {"role": "implement", "agent": "codex", "start": 2, "resumed": True, "usage": usage(1500, 1000, 70)},
        {"role": "implement", "agent": "codex", "start": 1, "resumed": False, "usage": usage(1000, 800, 50)},
        {"role": "review", "agent": "claude", "start": 3, "resumed": True, "usage": usage(300)},
    ]
    fm.adjust_resumed(runs)
    assert runs[0]["tokens"] == usage(500, 200, 20) and runs[0]["adjusted"]
    assert runs[1]["tokens"] == usage(1000, 800, 50)
    assert runs[2]["tokens"] == usage(300) and "adjusted" not in runs[2]


def task(state, events, runs=(), corrections=0):
    return {"id": state, "state": state, "corrections": corrections, "events": events, "runs": list(runs)}


def test_report_splits_merged_and_counts_human_checks_and_tokens():
    merged = task(
        "merged",
        [
            (0, "created", ""),
            (0, "phase_started", "checks"),
            (120, "phase_finished", "checks"),
            (120, "phase_started", "checks"),
            (180, "phase_finished", "checks"),
            (600, "accepted", ""),
        ],
        runs=[
            {
                "role": "implement",
                "agent": "codex",
                "start": 0,
                "usage": usage(1000, 900),
                "tool_calls": 4,
                "setup_seconds": 60,
                "setup_skipped": True,
                "resumed": False,
                "entries": [entry(0, "tool", "$ npm test"), entry(30, "output", "")],
            }
        ],
        corrections=1,
    )
    stopped = task("cancelled", [(0, "stopped", ""), (60, "correction", "")])
    data = fm.report([merged, stopped])
    group = data["merged"]
    assert group["tasks"] == 1
    assert group["input_per_task"] == 1000
    assert group["roles"]["implement"]["cached_pct"] == 90
    assert group["busy_minutes"] == 3 and group["wall_minutes"] == 10
    assert group["checks_per_task"] == 2
    assert group["no_human_share"] == 1
    assert group["implement"]["tokens_per_tool_call_median"] == 250
    assert group["implement"]["setup_skipped_share"] == 1
    assert group["commands"]["implement"] == [{"command": "npm test", "calls": 1, "minutes": 0.5}]
    everything = data["all"]
    assert everything["tasks"] == 2
    assert everything["no_human_share"] == 0.5
    assert everything["correction_events_per_task"] == 0.5
    assert "== merged: 1 tasks" in fm.render(data)


def test_factory_page_reports_merged_work_and_director_decisions(client, setup_project, tmp_path):
    import json as _json

    from conftest import connect
    from test_director_stops import Decides, stop
    from test_pipeline import DeterministicExecutor, drive, seed

    from darkfactory import db, director_stops
    from darkfactory.worker import Worker

    project, pool = setup_project
    connect(client, pool)
    remote = seed(tmp_path)
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        row.remote, row.checks = str(remote), []
    task = client.post(
        f"/api/projects/{project['id']}/tasks", json={"title": "Done", "request": "Make value 42"}
    ).json()
    assert drive(Worker(DeterministicExecutor()), task["id"]).state == "merged"
    stuck = client.post(
        f"/api/projects/{project['id']}/tasks", json={"title": "Stuck", "request": "r"}
    ).json()
    stop(stuck["id"])
    assert director_stops.handle_stop(Decides(decision="escalate", message="Нужен ключ"))
    page = client.get(f"/api/projects/{project['id']}/metrics", params={"days": 7}).json()
    assert page["merged"] == 1 and page["tasks"] == 2
    assert page["director"] == {"escalate": 1}
    assert {row["phase"] for row in page["phases"]} >= {"implement", "review"}
    assert _json.dumps(page)


def test_resumed_codex_runs_are_not_double_counted_without_transcripts(tmp_path):
    import json as _json

    base = tmp_path / "artifacts" / "t" / "browser"
    for name, started, resumed, tokens in (("a", 1.0, False, 100), ("b", 2.0, True, 250)):
        run = base / name
        run.mkdir(parents=True)
        (run / "execution.json").write_text(
            _json.dumps({"role": "implement", "started_at": started, "resumed": resumed, "agent": "codex"})
        )
        (run / "usage.json").write_text(
            _json.dumps({"input_tokens": tokens, "cached_input_tokens": 0, "output_tokens": 0})
        )
    runs = fm.adjust_resumed(fm.load_runs(tmp_path, "t", transcripts=False))
    assert sorted(run["tokens"]["input_tokens"] for run in runs) == [100, 150]
