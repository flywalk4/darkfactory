import json
import os
import time

from conftest import make_task

from darkfactory import runner
from darkfactory.db import ROOT, Session, Task


def write_run(task_id, generation, role, entries, mtime):
    directory = ROOT / "artifacts" / task_id / "browser" / generation
    directory.mkdir(parents=True)
    (directory / "execution.json").write_text(json.dumps({"generation": generation, "role": role}))
    os.utime(directory / "execution.json", (mtime, mtime))
    (directory / "transcript.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries) + "not json\n"
    )
    return directory


def test_transcript_returns_latest_run_incrementally_and_scoped(client, setup_project):
    project, _ = setup_project
    task = make_task(client, project)
    base = f"/api/tasks/{task['id']}/transcript"
    assert client.get(base).json() == {
        "generation": None,
        "live": False,
        "runs": [],
        "entries": [],
        "next": 0,
        "more": False,
    }
    write_run(task["id"], "old", "plan", [{"at": 1, "kind": "message", "text": "plan"}], 100)
    write_run(
        task["id"],
        "new",
        "implement",
        [{"at": 2, "kind": "tool", "text": "$ ls"}, {"at": 3, "kind": "output", "text": "exit=0"}],
        200,
    )
    lock = ROOT / "locks" / task["id"]
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "generation").write_text("new")
    with Session() as db:
        db.get(Task, task["id"]).state = "running"
        db.commit()
    data = client.get(base).json()
    assert data["generation"] == "new" and data["live"] is True
    assert [run["role"] for run in data["runs"]] == ["plan", "implement"]
    # The cursor is a byte offset into the transcript file, past the unparseable last line too.
    first = len(json.dumps({"at": 2, "kind": "tool", "text": "$ ls"}) + "\n")
    size = (ROOT / "artifacts" / task["id"] / "browser" / "new" / "transcript.jsonl").stat().st_size
    assert [entry["text"] for entry in data["entries"]] == ["$ ls", "exit=0"] and data["next"] == size
    assert client.get(base, params={"after": first}).json()["entries"] == [
        {"at": 3.0, "kind": "output", "text": "exit=0"}
    ]
    assert client.get(base, params={"after": size}).json()["entries"] == []
    old = client.get(base, params={"generation": "old"}).json()
    assert old["live"] is False and old["entries"][0]["text"] == "plan"
    assert client.get(base, params={"generation": "../x"}).status_code == 404
    (ROOT / "artifacts" / task["id"] / "browser" / "link").symlink_to(ROOT, target_is_directory=True)
    assert len(client.get(base).json()["runs"]) == 2
    other = client.post("/api/organizations", json={"name": "Transcript org"}).json()
    assert client.get(base, headers={"Authorization": "Bearer " + other["token"]}).status_code == 404


def test_runner_transcript_redacts_and_maps_agent_events(tmp_path, monkeypatch):
    path = tmp_path / "transcript.jsonl"
    monkeypatch.setattr(runner, "note", runner.Transcript(str(path), ["supersecret-token"]))
    runner.note_codex(
        {"method": "item/started", "params": {"item": {"type": "commandExecution", "command": "pytest"}}}
    )
    runner.note_codex(
        {
            "method": "item/completed",
            "params": {"item": {"type": "commandExecution", "exitCode": 1, "aggregatedOutput": "boom"}},
        }
    )
    runner.note_codex(
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "key supersecret-token"}},
        }
    )
    runner.note_claude(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]},
        }
    )
    runner.note_claude(
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"text": "a.py"}]}]}}
    )
    runner.note("message", "x" * 10000)
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(e["kind"], e["text"]) for e in entries[:5]] == [
        ("tool", "$ pytest"),
        ("output", "exit=1\nboom"),
        ("message", "key [REDACTED]"),
        ("tool", 'Bash {"command": "ls"}'),
        ("output", "a.py"),
    ]
    assert len(entries[5]["text"]) < runner.Transcript.ENTRY + 5
    runner.Transcript(None)("message", "ignored")


def test_stall_detection_records_once_and_resumes(client, setup_project, monkeypatch, tmp_path):
    from darkfactory import execution
    from darkfactory.db import Event

    project, _ = setup_project
    task = make_task(client, project)
    with Session() as db:
        db.get(Task, task["id"]).state = "running"
        db.commit()
    row = type("Row", (), {"id": task["id"]})()
    monkeypatch.setenv("DARKFACTORY_STALL_SECONDS", "60")
    started = execution.time.time() - 120
    assert execution.watch_activity(row, "director", tmp_path, started, False) is False
    assert execution.watch_activity(row, "implement", tmp_path, started, False) is True
    assert execution.watch_activity(row, "implement", tmp_path, started, True) is True
    (tmp_path / "transcript.jsonl").touch()
    assert execution.watch_activity(row, "implement", tmp_path, started, True) is False
    with Session() as db:
        kinds = [e.kind for e in db.query(Event).filter(Event.task_id == task["id"]).order_by(Event.id)]
    assert kinds[-2:] == ["stalled", "activity_resumed"] and kinds.count("stalled") == 1
    # A stall names the step it happened in, so a long install is not mistaken for a hung model.
    (tmp_path / "transcript.jsonl").write_text(
        '{"kind": "output", "text": "exit=0"}\n{"kind": "tool", "text": "$ npm ci --prefix web"}\n'
    )
    assert execution.last_step(tmp_path) == "$ npm ci --prefix web"
    (tmp_path / "transcript.jsonl").write_text(
        '{"kind": "tool", "text": "$ ls"}\n{"kind": "output", "text": "a"}\n'
    )
    assert execution.last_step(tmp_path) is None


def test_stall_restart_requeues_then_escalates(client, setup_project, monkeypatch):
    from darkfactory import worker
    from darkfactory.db import Event

    project, _ = setup_project
    task = make_task(client, project)
    monkeypatch.setenv("DARKFACTORY_STALL_RESTARTS", "1")
    with Session() as db:
        row = db.get(Task, task["id"])
        worker.stall_restart(db, row, "Нет активности агента 30 мин (implement)")
        db.commit()
        assert row.state == "queued"
        worker.stall_restart(db, row, "Нет активности агента 30 мин (implement)")
        db.commit()
        assert row.state == "needs_human" and "зависал 2" in row.reason
        kinds = [e.kind for e in db.query(Event).filter(Event.task_id == task["id"])]
    assert kinds.count("stall_restart") == 1
    assert (ROOT / "artifacts" / task["id"] / "handoff.md").read_text().count("зависла") == 1


def test_stall_restart_threshold(monkeypatch):
    from darkfactory import execution

    monkeypatch.setenv("DARKFACTORY_STALL_SECONDS", "600")
    monkeypatch.setenv("DARKFACTORY_STALL_RESTART_SECONDS", "120")
    assert execution.stall_restart_seconds() == 600
    monkeypatch.setenv("DARKFACTORY_STALL_RESTART_SECONDS", "0")
    assert execution.stall_restart_seconds() == 0


def test_running_task_exposes_last_activity(client, setup_project):
    project, _ = setup_project
    task = make_task(client, project)
    assert "last_activity" not in client.get(f"/api/tasks/{task['id']}").json()
    directory = write_run(task["id"], "live", "implement", [], 300)
    lock = ROOT / "locks" / task["id"]
    lock.mkdir(parents=True, exist_ok=True)
    (lock / "generation").write_text("live")
    os.utime(directory / "transcript.jsonl", (500, 500))
    with Session() as db:
        db.get(Task, task["id"]).state = "running"
        db.commit()
    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["last_activity"] == 500 and detail["stall_seconds"] >= 60
    listed = next(
        t for t in client.get(f"/api/projects/{project['id']}/tasks").json() if t["id"] == task["id"]
    )
    assert listed["last_activity"] == 500


def test_runner_pulse_touches_transcript_without_entries(tmp_path):
    path = tmp_path / "transcript.jsonl"
    transcript = runner.Transcript(str(path))
    transcript.pulse()
    assert path.exists() and path.read_text() == ""


class FakeProcess:
    def __init__(self, messages):
        import io

        self.stdin = io.StringIO()
        self.stdin.close = lambda: setattr(self, "closed", True)
        self.stdout = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        self.returncode, self.closed = 0, False

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def completed(text):
    item = {"id": text, "type": "agentMessage", "phase": "final_answer", "text": text}
    return [
        {"method": "item/completed", "params": {"item": item}},
        {"method": "turn/completed", "params": {"turn": {"id": "t", "status": "completed", "items": []}}},
    ]


def test_codex_steers_hint_and_falls_back_to_follow_up_turn(tmp_path, monkeypatch):
    (tmp_path / "hints.jsonl").write_text(json.dumps({"text": "use pytest -x"}) + "\n")
    monkeypatch.setattr(runner, "note", runner.Transcript(str(tmp_path / "transcript.jsonl")))
    monkeypatch.setattr(runner, "hints", runner.Hints(str(tmp_path / "hints.jsonl")))
    proc = FakeProcess(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"thread": {"id": "thread"}}},
            {"id": 4, "result": {"turn": {"id": "turn-1"}}},
            {"method": "item/started", "params": {"item": {"type": "reasoning"}}},
            {"id": 5, "error": {"code": -32601, "message": "unknown method"}},
            *completed("first"),
            {"id": 6, "result": {"turn": {"id": "turn-2"}}},
            *completed("second"),
        ]
    )
    monkeypatch.setattr(runner.subprocess, "Popen", lambda args, **kwargs: proc)
    result = runner.codex({"credentials": {"accessToken": "x"}, "role": "implement", "prompt": "Fix"})
    sent = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
    steer = next(m for m in sent if m["method"] == "turn/steer")
    assert steer["params"]["expectedTurnId"] == "turn-1" and "use pytest -x" in json.dumps(steer)
    follow = [m for m in sent if m["method"] == "turn/start"][-1]
    assert follow["id"] == 6 and "use pytest -x" in follow["params"]["input"][0]["text"]
    assert result["status"] == "ok" and result["text"] == "second"
    kinds = [json.loads(line)["kind"] for line in (tmp_path / "transcript.jsonl").read_text().splitlines()]
    assert kinds.count("hint") == 1


def test_claude_delivers_hint_at_turn_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "hints", runner.Hints(str(tmp_path / "hints.jsonl")))
    (tmp_path / "hints.jsonl").write_text(json.dumps({"text": "also update README"}) + "\n")
    proc = FakeProcess(
        [
            {"type": "result", "is_error": False, "result": "one"},
            {"type": "result", "is_error": False, "result": "two"},
        ]
    )
    calls = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda args, **kwargs: calls.append(args) or proc)
    result = runner.claude({"credentials": {"oauth_token": "t"}, "role": "implement", "prompt": "Fix"})
    sent = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
    assert calls[0][calls[0].index("--input-format") + 1] == "stream-json"
    assert [m["message"]["content"] for m in sent][0] == "Fix"
    assert "also update README" in sent[1]["message"]["content"] and len(sent) == 2
    assert proc.closed and result["text"] == "two"


def test_hint_endpoint_requires_live_run_and_appends(client, setup_project):
    from darkfactory.db import Event

    project, _ = setup_project
    task = make_task(client, project)
    assert client.post(f"/api/tasks/{task['id']}/hint", json={"text": "x"}).status_code == 409
    lockdir = ROOT / "locks" / task["id"]
    lockdir.mkdir(parents=True, exist_ok=True)
    (lockdir / "generation").write_text("g1")
    with Session() as db:
        db.get(Task, task["id"]).state = "running"
        db.commit()
    for text in ("first", "second"):
        assert client.post(f"/api/tasks/{task['id']}/hint", json={"text": text}).json()["generation"] == "g1"
    lines = [json.loads(line)["text"] for line in (lockdir / "hints-g1.jsonl").read_text().splitlines()]
    assert lines == ["first", "second"]
    hints = runner.Hints(str(lockdir / "hints-g1.jsonl"))
    assert hints.take(force=True) == ["first", "second"] and hints.take(force=True) == []
    with Session() as db:
        assert db.query(Event).filter(Event.task_id == task["id"], Event.kind == "agent_hint").count() == 2


def test_runner_records_usage_per_provider_and_api_sums_runs(client, setup_project, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "note", runner.Transcript(str(tmp_path / "transcript.jsonl")))
    usage = tmp_path / "usage.json"
    runner.codex_usage({"method": "item/completed", "params": {}})
    assert not usage.exists()
    total = {"inputTokens": 900, "cachedInputTokens": 400, "outputTokens": 50}
    runner.codex_usage({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": total}}})
    assert json.loads(usage.read_text()) == {
        "input_tokens": 900,
        "cached_input_tokens": 400,
        "output_tokens": 50,
    }
    runner.claude_usage(
        {
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7},
            "total_cost_usd": 0.25,
        }
    )
    assert json.loads(usage.read_text()) == {
        "input_tokens": 100,
        "cached_input_tokens": 90,
        "output_tokens": 7,
        "cost_usd": 0.25,
    }

    project, _ = setup_project
    task = make_task(client, project)
    first = write_run(task["id"], "a", "implement", [], 100)
    (first / "usage.json").write_text(json.dumps({"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.5}))
    second = write_run(task["id"], "b", "review", [], 200)
    (second / "usage.json").write_text(json.dumps({"input_tokens": 50, "output_tokens": "x"}))
    write_run(task["id"], "c", "checks", [], 300)
    runs = client.get(f"/api/tasks/{task['id']}/transcript").json()["runs"]
    assert [run["usage"] for run in runs] == [
        {"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.5},
        {"input_tokens": 50},
        None,
    ]
    assert client.get(f"/api/tasks/{task['id']}").json()["usage"] == {
        "input_tokens": 150,
        "output_tokens": 10,
        "cost_usd": 0.5,
    }


def test_stalled_run_keeps_runner_stacks(tmp_path):
    from darkfactory import execution

    class Container:
        signals = []

        def kill(self, signal):
            self.signals.append(signal)

        def logs(self, tail):
            return b'Thread 0x1 (most recent call first):\n  File "runner.py", line 540 in codex\n'

    container = Container()
    execution.dump_stacks(container, tmp_path)
    assert container.signals == ["SIGUSR1"]
    assert "runner.py" in (tmp_path / "stall-stacks.txt").read_text()


def test_file_changes_are_noted_and_diffable(client, setup_project, tmp_path, monkeypatch):
    import subprocess

    path = tmp_path / "transcript.jsonl"
    monkeypatch.setattr(runner, "note", runner.Transcript(str(path), []))
    runner.note_codex(
        {
            "method": "item/completed",
            "params": {
                "item": {"type": "fileChange", "changes": [{"path": "/workspace/a.py"}, {"path": "b.py"}]}
            },
        }
    )
    runner.note_claude(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/workspace/c.py"}}]
            },
        }
    )
    kinds = [(e["kind"], e["text"]) for e in map(json.loads, path.read_text().splitlines())]
    assert ("files", "/workspace/a.py\nb.py") in kinds and ("files", "/workspace/c.py") in kinds

    project, _ = setup_project
    task = make_task(client, project)
    work = ROOT / "workspaces" / task["id"]
    work.mkdir(parents=True)

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args], cwd=work, text=True
        ).strip()

    git("init", "-q")
    (work / "a.py").write_text("A = 1\n")
    git("add", ".")
    git("commit", "-qm", "seed")
    with Session() as db, db.begin():
        db.get(Task, task["id"]).target_sha = git("rev-parse", "HEAD")
    # Live, uncommitted work: a modified tracked file and a new untracked one.
    (work / "a.py").write_text("A = 2\n")
    (work / "new.py").write_text("NEW = 1\n")
    (tmp_path / "outside.txt").write_text("secret\n")
    (work / "link").symlink_to(tmp_path)
    url = f"/api/tasks/{task['id']}/changes"
    files = client.get(url).json()["files"]
    assert {"status": "M", "path": "a.py"} in files and {"status": "A", "path": "new.py"} in files
    assert "+A = 2" in client.get(url, params={"path": "/workspace/a.py"}).json()["diff"]
    assert "+NEW = 1" in client.get(url, params={"path": "new.py"}).json()["diff"]
    assert client.get(url, params={"path": "link/outside.txt"}).json()["diff"] == ""
    for bad in ("../x", ".git/config", "/etc/passwd"):
        assert client.get(url, params={"path": bad}).status_code == 400


def test_checks_pulse_and_do_not_wait_for_inherited_pipes(tmp_path, monkeypatch):
    from darkfactory import runner

    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / "transcript.jsonl"
    monkeypatch.setattr(runner, "note", runner.Transcript(str(transcript)))
    pulses = []
    monkeypatch.setattr(runner.note, "pulse", lambda: pulses.append(1))
    # A daemon left behind (like an MSBuild node) holds stdout open long after the check exits.
    started = time.monotonic()
    passed, text = runner.run_checks(["(sleep 60 &) ; echo built", "echo second; exit 3"])
    assert time.monotonic() - started < 20
    assert not passed
    assert "built" in text and "exit=3" in text and "second" in text
    assert pulses


def test_setup_timeout_kills_the_command(tmp_path, monkeypatch):
    from darkfactory import runner

    monkeypatch.setattr(runner, "note", runner.Transcript())
    code, output = runner.shell("echo started; sleep 30", cwd=tmp_path, limit=0.5)
    assert code is None
    assert "started" in output


def test_install_shims_answer_only_for_the_installs_setup_ran(tmp_path, monkeypatch):
    from darkfactory import runner

    real = tmp_path / "real"
    real.mkdir()
    for name in ("npm", "dotnet"):
        tool = real / name
        tool.write_text(f'#!/bin/sh\necho "real {name} $*"\n')
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{real}:/usr/bin:/bin")
    runner.install_shims(
        ["dotnet restore src/App --disable-parallel", "npm ci --prefix src/frontend --no-audit"],
        str(tmp_path / "shims"),
    )

    def run(command, **env):
        import subprocess

        return subprocess.run(
            ["sh", "-c", command], capture_output=True, text=True, env={**os.environ, **env}
        ).stdout

    assert "skipped" in run("npm ci --prefix src/frontend")
    assert "skipped" in run("npm --prefix ./src/frontend/ install")
    assert "skipped" in run("dotnet restore src/App/")
    # A project the writer creates, or a package it adds, needs the real installer.
    assert run("dotnet restore src/NewProject.Tests") == "real dotnet restore src/NewProject.Tests\n"
    assert run("npm ci") == "real npm ci\n"
    assert (
        run("npm install lodash --prefix src/frontend") == "real npm install lodash --prefix src/frontend\n"
    )
    assert run("dotnet build src/App").startswith("real dotnet build")
    assert (
        run("npm ci --prefix src/frontend", DARKFACTORY_REAL_INSTALL="1")
        == "real npm ci --prefix src/frontend\n"
    )


def test_setup_reports_failures_and_stamps_only_success(tmp_path, monkeypatch):
    import subprocess

    from darkfactory import runner

    monkeypatch.setattr(runner, "note", runner.Transcript())
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "package.json").write_text("{}")
    marker = tmp_path / "marker"
    assert runner.setup(["true", "exit 4"], str(marker), str(tmp_path)) == ["exit 4 (exit 4)"]
    assert not marker.exists()
    # The install itself may rewrite a manifest; the stamp reflects the tree after it.
    assert runner.setup(["echo '{\"a\":1}' > package.json"], str(marker), str(tmp_path)) == []
    stamp = marker.read_text()
    assert stamp == runner.setup_stamp(["echo '{\"a\":1}' > package.json"], str(tmp_path))


def test_resumed_codex_thread_counts_only_this_runs_tokens(tmp_path, monkeypatch):
    from darkfactory import runner

    monkeypatch.setattr(runner, "note", runner.Transcript(str(tmp_path / "transcript.jsonl")))
    monkeypatch.setattr(runner, "codex_totals", {"seen": {}, "base": {}})
    job = {"session_dir": str(tmp_path)}
    runner.save_session(
        job, "codex", "thread-1", {"inputTokens": 1000, "cachedInputTokens": 900, "outputTokens": 50}
    )
    runner.codex_totals["base"] = runner.session_usage(job, "codex")
    runner.codex_usage(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "tokenUsage": {"total": {"inputTokens": 1600, "cachedInputTokens": 1300, "outputTokens": 80}}
            },
        }
    )
    usage = json.loads((tmp_path / "usage.json").read_text())
    assert usage == {"input_tokens": 600, "cached_input_tokens": 400, "output_tokens": 30}
    assert runner.session_state({**job, "resume_prompt": "x"}, "codex") == "thread-1"


def test_checks_failing_after_a_skipped_setup_reinstall_once(tmp_path, monkeypatch):
    import subprocess

    from darkfactory import runner

    monkeypatch.setattr(runner, "note", runner.Transcript())
    monkeypatch.setattr(runner, "setup_skipped", {})
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "package.json").write_text("{}")
    marker = tmp_path / "marker"
    install = ["mkdir -p .state/tool && touch .state/tool/python"]
    assert runner.setup(install, str(marker), str(tmp_path)) == []
    # The workspace loses what setup made but keeps the stamp, as a copied donor workspace can.
    subprocess.run(["rm", "-rf", str(tmp_path / ".state")], check=True)
    assert runner.setup(install, str(marker), str(tmp_path)) == [] and runner.setup_skipped
    monkeypatch.chdir(tmp_path)
    passed, text = runner.checks_after_setup(["test -f .state/tool/python"])
    assert passed and (tmp_path / ".state" / "tool" / "python").exists()
    # A real failure still fails, after one reinstall at most.
    assert runner.checks_after_setup(["false"]) == (False, "$ false\nexit=1\n")
