import io
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from darkfactory import runner


class Process:
    def __init__(self, messages):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        self.returncode = 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1])
def test_nonfinite_or_negative_prices_never_call_provider(monkeypatch, price):
    monkeypatch.setattr(
        runner.httpx, "Client", lambda **kw: pytest.fail("Invalid pricing must not launch paid request")
    )
    result = runner.api_agent(
        {"model": "unused", "credentials": {"input_usd_per_million": price, "output_usd_per_million": 1}}
    )
    assert result["status"] == "error"


def test_claude_subscription_stream_and_structured_review(monkeypatch):
    proc = Process(
        [
            {
                "type": "result",
                "is_error": False,
                "structured_output": {"approved": True, "findings": []},
                "session_id": "separate-review-session",
            }
        ]
    )
    calls = []
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)) or proc
    )
    result = runner.claude(
        {
            "credentials": {"oauth_token": "selected-subscription"},
            "role": "review",
            "prompt": "Review independently",
        }
    )
    assert result["status"] == "ok" and runner.json_object(result["text"])["approved"]
    args, options = calls[0]
    assert "--bare" not in args and "--no-session-persistence" in args
    assert args[args.index("--tools") + 1] == "Read,Glob,Grep"
    # The CLI holds a placeholder; the runner's relay adds the subscription token.
    assert options["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "darkfactory-relay"
    assert options["env"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert "selected-subscription" not in json.dumps(options["env"])
    assert options["env"]["CLAUDE_CODE_PROXY_RESOLVES_HOSTS"] == "1"


def test_codex_uses_documented_oauth_provider_and_completed_items(monkeypatch):
    proc = Process(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"thread": {"id": "thread"}}},
            {"id": 4, "result": {}},
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "comment",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "I will inspect the change.",
                    }
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "message",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": '{"approved":true,"findings":[]}',
                    }
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": "turn",
                        "status": "completed",
                        "items": [
                            {
                                "id": "message",
                                "type": "agentMessage",
                                "text": '{"approved":true,"findings":[]}',
                            },
                            {"id": "comment", "type": "agentMessage", "text": "I will inspect the change."},
                        ],
                    }
                },
            },
        ]
    )
    calls = []

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return proc

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    result = runner.codex(
        {"credentials": {"accessToken": "selected-oauth-token"}, "role": "review", "prompt": "Review"}
    )
    assert result["status"] == "ok"
    assert runner.json_object(result["text"])["approved"]
    assert calls[0][1]["env"]["ACCESS_TOKEN"] == "darkfactory-relay"
    assert any(
        item.startswith('model_providers.openai_chatgpt_plan.base_url="http://127.0.0.1:')
        for item in calls[0][0]
    )
    assert "model_providers.openai_chatgpt_plan.requires_openai_auth=false" in calls[0][0]
    # Long agent threads re-send their context every turn; both caps keep each turn bounded.
    assert "tool_output_token_limit=1500" in calls[0][0]
    assert "model_auto_compact_token_limit=60000" in calls[0][0]
    assert "features.plugins=false" in calls[0][0] and "features.apps=false" in calls[0][0]
    assert "account/login/start" not in proc.stdin.getvalue()
    assert "selected-oauth-token" not in proc.stdin.getvalue()


@pytest.mark.parametrize("status,expected", [(401, "auth"), (429, "quota")])
def test_api_auth_and_quota_errors_are_distinct(monkeypatch, status, expected):
    original = httpx.Client
    monkeypatch.setattr(
        runner.httpx,
        "Client",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, text="unauthorized" if status == 401 else "rate_limit")
            )
        ),
    )
    result = runner.api_agent(
        {
            "credentials": {
                "api_key": "test",
                "base_url": "https://model.test/v1",
                "input_usd_per_million": 1,
                "output_usd_per_million": 1,
            },
            "model": "test",
            "prompt": "Fix",
            "budget_usd": 1,
        }
    )
    assert result["status"] == expected
    assert result["cost_usd"] == 0


def test_api_stops_when_usage_is_unknown(monkeypatch):
    original = httpx.Client
    monkeypatch.setattr(
        runner.httpx,
        "Client",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})
            )
        ),
    )
    result = runner.api_agent(
        {
            "credentials": {
                "api_key": "test",
                "base_url": "https://model.test/v1",
                "input_usd_per_million": 1,
                "output_usd_per_million": 1,
            },
            "model": "test",
            "prompt": "Fix",
            "budget_usd": 1,
        }
    )
    assert result["status"] == "budget" and result["cost_usd"] > 0


def mcp_job():
    return {
        "generation": "test-generation",
        "timeout": 30,
        "provider": "claude",
        "credentials": {"oauth_token": "selected-subscription-secret"},
        "role": "implement",
        "prompt": "Fix the project",
        "agent_profile": {
            "mcp_definitions": [
                {
                    "name": "issues",
                    "transport": "http",
                    "url": "https://private-mcp.invalid/rpc",
                    "headers": {"Authorization": "Bearer private-mcp-header"},
                    "enabled_tools": ["issues.read"],
                    "read_only": True,
                },
                {
                    "name": "local",
                    "transport": "stdio",
                    "command": "project-mcp",
                    "args": [],
                    "env": {"SERVICE_TOKEN": 'private-mcp-env-value"quoted\\tail'},
                    "enabled_tools": ["local.read"],
                    "read_only": True,
                },
            ]
        },
    }


def main_files(monkeypatch, tmp_path, job):
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(job))
    generation = tmp_path / "generation"
    generation.write_text(job["generation"])
    lock = tmp_path / "lock"
    lock.touch()
    paths = {
        "/run/job/input.json": input_path,
        "/run/task-lock/generation": generation,
        "/run/job/egress.sock": tmp_path / "no-egress.sock",
    }
    monkeypatch.setattr(runner, "Path", lambda value, *rest: paths.get(str(value), Path(value, *rest)))
    monkeypatch.setattr(runner, "open", lambda *_args, **_kwargs: lock.open("rb"), raising=False)
    monkeypatch.setattr(runner.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(runner.signal, "setitimer", lambda *_args: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def observed_bridge(monkeypatch, job, events):
    @contextmanager
    def bridge(servers):
        assert servers == job["agent_profile"]["mcp_definitions"]
        events.append("bridge-enter")
        try:
            yield [
                {
                    "name": server["name"],
                    "transport": "http",
                    "url": f"http://127.0.0.1:34567/{index}",
                    "headers": None,
                    "enabled_tools": server["enabled_tools"],
                    "read_only": server["read_only"],
                }
                for index, server in enumerate(servers)
            ]
        finally:
            events.append("bridge-exit")

    monkeypatch.setattr(runner, "project_mcp", bridge)


def test_main_keeps_bridge_alive_for_native_claude_and_passes_only_loopback_config(
    monkeypatch, tmp_path, capsys
):
    job, events = mcp_job(), []
    main_files(monkeypatch, tmp_path, job)
    observed_bridge(monkeypatch, job, events)
    calls = []

    def popen(args, **kwargs):
        assert events == ["bridge-enter"]
        events.append("native-start")
        calls.append((args, kwargs))
        process = Process([{"type": "result", "is_error": False, "result": "done"}])

        def wait(timeout=None):
            if timeout is None:
                assert events == ["bridge-enter", "native-start"]
                events.append("native-wait")
            else:
                assert timeout == 3
                assert events == ["bridge-enter", "native-start", "native-wait", "native-terminate"]
                events.append("native-reaped")
            return 0

        process.wait = wait
        process.terminate = lambda: events.append("native-terminate")
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    runner.main()
    assert events == [
        "bridge-enter",
        "native-start",
        "native-wait",
        "native-terminate",
        "native-reaped",
        "bridge-exit",
    ]
    args, options = calls[0]
    assert "--strict-mcp-config" in args
    assert args[args.index("--setting-sources") + 1] == ""
    actual = json.loads(args[args.index("--mcp-config") + 1])
    assert actual == {
        "mcpServers": {
            "issues": {"type": "http", "url": "http://127.0.0.1:34567/0"},
            "local": {"type": "http", "url": "http://127.0.0.1:34567/1"},
        }
    }
    assert options["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "darkfactory-relay"
    serialized = json.dumps(args)
    for forbidden in ("private-mcp", "project-mcp", "Authorization", "SERVICE_TOKEN"):
        assert forbidden not in serialized
    result = capsys.readouterr().out.strip().removeprefix("DARKFACTORY_RESULT:")
    assert json.loads(result) == {"status": "ok", "text": "done", "session": None}


def test_main_closes_bridge_on_native_error_and_redacts_original_mcp_credentials(
    monkeypatch, tmp_path, capsys
):
    job, events = mcp_job(), []
    main_files(monkeypatch, tmp_path, job)
    observed_bridge(monkeypatch, job, events)
    definitions = job["agent_profile"]["mcp_definitions"]
    secrets = (
        definitions[0]["headers"]["Authorization"],
        definitions[1]["env"]["SERVICE_TOKEN"],
        job["credentials"]["oauth_token"],
    )

    def failed_native(_args, **_kwargs):
        assert events == ["bridge-enter"]
        raise RuntimeError("Native adapter failed: " + " ".join(secrets))

    monkeypatch.setattr(runner.subprocess, "Popen", failed_native)
    runner.main()
    assert events == ["bridge-enter", "bridge-exit"]
    output = capsys.readouterr().out.strip()
    assert output.startswith("DARKFACTORY_RESULT:")
    result = json.loads(output.removeprefix("DARKFACTORY_RESULT:"))
    assert result["status"] == "error"
    assert "[REDACTED]" in result["text"]
    assert all(secret not in output for secret in secrets)
    assert all(secret not in result["text"] for secret in secrets)


def test_main_reports_sanitized_result_when_bridge_cleanup_fails(monkeypatch, tmp_path, capsys):
    job, events = mcp_job(), []
    main_files(monkeypatch, tmp_path, job)
    observed_bridge(monkeypatch, job, events)
    ordinary_bridge = runner.project_mcp

    @contextmanager
    def failed_cleanup(servers):
        with ordinary_bridge(servers) as replacements:
            yield replacements
        raise RuntimeError("private-mcp-env-value cleanup failed")

    def native(_job):
        assert events == ["bridge-enter"]
        events.append("native-complete")
        return {"status": "ok", "text": "done"}

    monkeypatch.setattr(runner, "project_mcp", failed_cleanup)
    monkeypatch.setattr(runner, "claude", native)
    runner.main()
    assert events == ["bridge-enter", "native-complete", "bridge-exit"]
    output = capsys.readouterr().out.strip()
    result = json.loads(output.removeprefix("DARKFACTORY_RESULT:"))
    # A finished run stays valid when a bridge fails to close; the failure text never leaks.
    assert result == {"status": "ok", "text": "done"}
    assert "private-mcp" not in output


@pytest.mark.parametrize("phase", ["stdin", "stdout"])
@pytest.mark.parametrize("force_kill", [False, True])
def test_claude_timeout_reaps_native_process_before_bridge_closes(
    monkeypatch, tmp_path, capsys, phase, force_kill
):
    job, events = mcp_job(), []
    main_files(monkeypatch, tmp_path, job)
    observed_bridge(monkeypatch, job, events)
    error = "Injected Claude " + phase + " timeout"

    class WriteTimeout(io.StringIO):
        def write(self, _value):
            raise TimeoutError(error)

    def broken_stream():
        raise TimeoutError(error)
        yield ""  # Make iteration raise while consuming stdout.

    process = Process([])
    process.returncode = None
    if phase == "stdin":
        process.stdin = WriteTimeout()
    else:
        process.stdout = broken_stream()

    def terminate():
        assert events == ["bridge-enter"]
        events.append("native-terminate")

    def kill():
        assert events == ["bridge-enter", "native-terminate", "native-wait"]
        events.append("native-kill")

    def wait(timeout=None):
        assert "bridge-exit" not in events
        if timeout == 3:
            events.append("native-wait")
            if force_kill:
                raise runner.subprocess.TimeoutExpired("claude", 3)
        else:
            assert force_kill and events[-1] == "native-kill"
        process.returncode = -9 if force_kill else -15
        events.append("native-reaped")
        return process.returncode

    process.terminate, process.kill, process.wait = terminate, kill, wait
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
    runner.main()
    assert events == (
        ["bridge-enter", "native-terminate", "native-wait"]
        + (["native-kill"] if force_kill else [])
        + ["native-reaped", "bridge-exit"]
    )
    result = json.loads(capsys.readouterr().out.strip().removeprefix("DARKFACTORY_RESULT:"))
    assert result == {"status": "deadline", "text": error}
    assert process.returncode is not None


def test_setup_is_skipped_until_a_manifest_changes(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    (work / "package.json").write_text('{"a": 1}')
    (work / "app.js").write_text("1")
    marker = tmp_path / "cache" / "setup" / "task"
    log = tmp_path / "ran"
    command = f"echo x >> {log}"
    pulses = []
    quiet = lambda *args: None  # noqa: E731
    quiet.pulse = lambda: pulses.append(1)
    monkeypatch.setattr(runner, "note", quiet)

    def runs():
        return len(log.read_text().splitlines()) if log.exists() else 0

    runner.setup([command], marker, work)
    runner.setup([command], marker, work)
    assert runs() == 1
    (work / "app.js").write_text("2")
    runner.setup([command], marker, work)
    assert runs() == 1
    (work / "package.json").write_text('{"a": 2}')
    runner.setup([command], marker, work)
    assert runs() == 2
    runner.setup(["false"], marker, work)
    runner.setup(["false"], marker, work)
    assert not marker.exists()
    # A slow install keeps the agent marked alive; one over the limit is stopped.
    pulses.clear()
    monkeypatch.setattr(runner, "SETUP_SECONDS", 1)
    runner.setup(["sleep 30"], None, work)
    assert len(pulses) >= 1 and not marker.exists()


def test_codex_correction_resumes_the_writer_thread(monkeypatch, tmp_path):
    (tmp_path / "codex.json").write_text('{"session": "writer-thread"}')
    proc = Process(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"thread": {"id": "writer-thread"}}},
            {"id": 4, "result": {"turn": {"id": "turn"}}},
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn", "status": "completed", "items": []}},
            },
        ]
    )
    monkeypatch.setattr(runner.subprocess, "Popen", lambda args, **kwargs: proc)
    monkeypatch.setattr(runner, "keep_sessions", lambda *args: True)
    job = {
        "credentials": {"accessToken": "token"},
        "role": "implement",
        "prompt": "Full task prompt",
        "session_dir": str(tmp_path),
        "resume_prompt": "Close the review findings",
    }
    runner.codex(job)
    sent = proc.stdin.getvalue()
    assert '"thread/resume"' in sent and '"writer-thread"' in sent and '"thread/start"' not in sent
    assert "Close the review findings" in sent and "Full task prompt" not in sent
    # A first run (no saved session) starts a persistent thread and records it.
    (tmp_path / "codex.json").unlink()
    proc = Process(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"thread": {"id": "new-thread"}}},
            {"id": 4, "result": {"turn": {"id": "turn"}}},
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn", "status": "completed", "items": []}},
            },
        ]
    )
    runner.codex(job)
    sent = proc.stdin.getvalue()
    assert '"thread/start"' in sent and '"ephemeral": false' in sent and "Full task prompt" in sent
    assert runner.session_state(job, "codex") == "new-thread"
    assert runner.session_state({**job, "resume_prompt": None}, "codex") is None


def test_writer_session_reviews_itself_then_fixes_failing_checks(tmp_path, monkeypatch):
    from darkfactory import runner

    monkeypatch.setattr(runner, "note", runner.Transcript(None))
    # The routine-change shortcut reads the real workspace diff; this test needs the full review turn.
    monkeypatch.setattr(runner, "routine_workspace", lambda: False)
    flag = tmp_path / "fixed"
    followups = runner.Followups({"self_review": True, "inner_checks": [f"test -f {flag}"]})
    assert followups.next() == runner.SELF_REVIEW
    fix = followups.next()
    assert "required checks" in fix and f"test -f {flag}" in fix
    flag.write_text("")
    assert followups.next() is None
    assert followups.attach({"status": "ok"})["inner_checks"]["passed"] is True
    # Two fix turns at most, then the result goes to the factory as it is.
    stubborn = runner.Followups({"inner_checks": ["false"]})
    assert stubborn.next() and stubborn.next() and stubborn.next() is None
    assert stubborn.result["passed"] is False
    # Setup already installed dependencies; a writer that asked the owner gets no follow-ups.
    assert runner.Followups({"setup": ["npm ci"], "inner_checks": ["npm ci", "true"]}).checks == ["true"]
    monkeypatch.setenv("DARKFACTORY_EVIDENCE_DIR", str(tmp_path))
    (tmp_path / "owner-request.json").write_text('{"questions": ["Какая политика?"]}')
    assert runner.Followups({"self_review": True, "inner_checks": ["true"]}).next() is None


def test_small_routine_diff_gets_the_light_reviewer(tmp_path):
    from darkfactory.execution import routine_change, writer_effort

    diff = tmp_path / "diff.patch"
    diff.write_text("diff --git a/src/list.ts b/src/list.ts\n--- a/src/list.ts\n+++ b/src/list.ts\n+x\n-y\n")
    assert routine_change(diff)
    diff.write_text("diff --git a/src/auth/login.ts b/src/auth/login.ts\n+x\n")
    assert not routine_change(diff)
    diff.write_text("diff --git a/src/list.ts b/src/list.ts\n" + "+x\n" * 400)
    assert not routine_change(diff)
    assert writer_effort("implement") == "medium" and writer_effort("review") == ""


def test_checks_run_only_for_the_parts_a_task_touched(tmp_path):
    import json
    import subprocess

    from darkfactory import environment

    spec = environment.parse(
        json.dumps(
            {
                "checks": [
                    "make lint",
                    {"run": "npm test", "paths": ["web/*"]},
                    {"run": "dotnet test", "paths": ["api/*"]},
                ]
            }
        )
    )
    assert spec["checks"] == ["make lint", "npm test", "dotnet test"]
    assert spec["check_paths"] == {"npm test": ["web/*"], "dotnet test": ["api/*"]}

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args], cwd=tmp_path, check=True
        )

    git("init", "-q")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "a.ts").write_text("1\n")
    git("add", ".")
    git("commit", "-qm", "seed")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "web" / "nested").mkdir()
    (tmp_path / "web" / "nested" / "b.ts").write_text("2\n")
    kept = runner.scoped(spec["checks"], spec["check_paths"], base, root=tmp_path)
    assert kept == ["make lint", "npm test"]
    # Without a known base every check runs.
    assert runner.scoped(spec["checks"], spec["check_paths"], None, root=tmp_path) == spec["checks"]


def test_check_paths_are_derived_from_named_parts(tmp_path):
    from darkfactory import toolchains

    for part in ("src/frontend/apps/studio", "src/backend/Api", "src/mcp"):
        (tmp_path / part).mkdir(parents=True)
    (tmp_path / "src/frontend/apps/studio/tsconfig.app.json").write_text("{}")
    commands = [
        "bash scripts/guard-rails.sh",
        "dotnet build src/backend/Api -c Release --no-restore",
        "NODE_OPTIONS=--max-old-space-size=3072 tsc -p src/frontend/apps/studio/tsconfig.app.json --noEmit",
        "npm test --prefix src/frontend -- --runTestsByPath libs/a.spec.ts",
        ".state/mcp/bin/python -m pytest src/mcp -q",
        "make lint",
    ]
    assert toolchains.check_paths(commands, tmp_path) == {
        commands[1]: ["src/backend/**"],
        commands[2]: ["src/frontend/**"],
        commands[3]: ["src/frontend/**"],
        commands[4]: ["src/mcp/**"],
    }


def test_root_file_change_runs_every_scoped_check(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args], cwd=tmp_path, check=True
        )

    git("init", "-q")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "a.ts").write_text("1\n")
    git("add", ".")
    git("commit", "-qm", "seed")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "Directory.Build.props").write_text("<Project/>\n")
    checks = ["npm test", "dotnet test"]
    assert (
        runner.scoped(checks, {"npm test": ["web/**"], "dotnet test": ["api/**"]}, base, root=tmp_path)
        == checks
    )


def test_routine_change_skips_the_self_review_turn(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from darkfactory import runner
    from darkfactory.execution import writer_effort

    monkeypatch.setattr(runner, "note", runner.Transcript(None))

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args], cwd=tmp_path, check=True
        )

    git("init", "-q")
    (tmp_path / "list.ts").write_text("a\n")
    git("add", ".")
    git("commit", "-qm", "seed")
    (tmp_path / "list.ts").write_text("b\n")
    assert runner.routine_workspace(str(tmp_path))
    (tmp_path / "auth.ts").write_text("token\n")
    assert not runner.routine_workspace(str(tmp_path))
    monkeypatch.setattr(runner, "routine_workspace", lambda: True)
    assert runner.Followups({"self_review": True, "inner_checks": ["true"]}).next() is None
    # Two rejections: the writer thinks harder instead of repeating the same attempt.
    assert writer_effort("implement", SimpleNamespace(corrections=2)) == "high"
    assert writer_effort("implement", SimpleNamespace(corrections=1)) == "medium"


def test_writer_handoff_json_becomes_the_next_runs_handoff(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from darkfactory import execution

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "handoff.json").write_text(
        json.dumps({"intent": "Fix list order", "files": ["src/list.ts: sort"], "unverified": ["UI e2e"]})
    )
    summary = execution.handoff_summary(evidence)
    assert "Fix list order" in summary and "- src/list.ts: sort" in summary and "Not verified" in summary
    # A planted link never makes the worker read another file.
    (evidence / "owner-request.json").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text('{"questions": ["leak"]}')
    assert execution.owner_request(evidence, False) is None
    monkeypatch.setattr(execution, "ROOT", tmp_path)
    task = SimpleNamespace(id="t1", phase="implement", corrections=0, active_seconds=1.0)
    execution.write_handoff(task, "Готово", summary)
    handoff = (tmp_path / "artifacts" / "t1" / "handoff.md").read_text()
    assert "Fix list order" in handoff and "Run required checks" not in handoff


def test_resumed_large_codex_thread_is_compacted_first(monkeypatch, tmp_path):
    (tmp_path / "codex.json").write_text('{"session": "writer-thread", "usage": {"context": 120000}}')
    proc = Process(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"thread": {"id": "writer-thread"}}},
            {"id": 4, "result": {}},
            {"method": "item/completed", "params": {"item": {"id": "c", "type": "contextCompaction"}}},
            {"id": 5, "result": {"turn": {"id": "turn"}}},
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn", "status": "completed", "items": []}},
            },
        ]
    )
    monkeypatch.setattr(runner.subprocess, "Popen", lambda args, **kwargs: proc)
    monkeypatch.setattr(runner, "keep_sessions", lambda *args: True)
    monkeypatch.setattr(runner, "codex_totals", {"seen": {}, "base": {}})
    job = {
        "credentials": {"accessToken": "token"},
        "role": "implement",
        "prompt": "Full task prompt",
        "session_dir": str(tmp_path),
        "resume_prompt": "Close the review findings",
    }
    assert runner.codex(job)["status"] == "ok"
    sent = proc.stdin.getvalue()
    assert sent.index('"thread/compact/start"') < sent.index('"turn/start"')


def test_codex_readers_run_in_the_read_only_landlock_sandbox(monkeypatch):
    for role, sandbox in (
        ("review", "read-only"),
        ("plan", "read-only"),
        ("implement", "danger-full-access"),
    ):
        proc = Process(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"thread": {"id": "t"}}},
                {"id": 3, "result": {"turn": {"id": "turn"}}},
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn", "status": "completed", "items": []}},
                },
            ]
        )
        seen = {}

        def popen(args, proc=proc, seen=seen, **kwargs):
            seen["args"] = args
            return proc

        monkeypatch.setattr(runner.subprocess, "Popen", popen)
        runner.codex({"credentials": {"accessToken": "token"}, "role": role, "prompt": "p"})
        assert f'"sandbox": "{sandbox}"' in proc.stdin.getvalue()
        assert ("features.use_legacy_landlock=true" in seen["args"]) == (sandbox == "read-only")


def test_credential_relay_adds_the_token_and_streams_back(monkeypatch):
    import http.server
    import threading

    import httpx

    from darkfactory.relay import PLACEHOLDER, auth_relay

    seen = {}

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", "12")
            self.end_headers()
            self.wfile.write(b"data: hello\n")

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    try:
        with auth_relay(
            f"http://127.0.0.1:{server.server_address[1]}", {"Authorization": "Bearer real"}
        ) as base:
            response = httpx.post(
                base + "/v1/messages", headers={"Authorization": "Bearer " + PLACEHOLDER}, content=b"{}"
            )
    finally:
        server.shutdown()
    assert response.status_code == 200 and response.text == "data: hello\n"
    assert seen == {"auth": "Bearer real", "body": b"{}"}


def test_loopback_certificate_is_a_server_certificate_signed_by_a_separate_ca():
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    from darkfactory.relay import loopback_certificate

    ca_pem, cert_pem, key_pem = loopback_certificate()
    ca, cert = x509.load_pem_x509_certificate(ca_pem), x509.load_pem_x509_certificate(cert_pem)
    # rustls in Codex refuses a CA certificate as the server's own.
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert cert != ca and cert.issuer == ca.subject
    cert.verify_directly_issued_by(ca)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]
    usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usage) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert key_pem.startswith(b"-----BEGIN PRIVATE KEY-----")  # leak-check: allow


def test_credential_relay_serves_https_trusted_through_its_ca(monkeypatch, tmp_path):
    import http.server
    import ssl
    import threading

    import httpx

    from darkfactory.relay import auth_relay, loopback_certificate

    seen = {}

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    ca, cert, key = loopback_certificate()
    (tmp_path / "ca.pem").write_bytes(ca)
    trusted = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    try:
        upstream = f"http://127.0.0.1:{server.server_address[1]}"
        with auth_relay(upstream, {"Authorization": "Bearer real"}, tls=(cert, key)) as base:
            assert base.startswith("https://127.0.0.1:")
            response = httpx.get(base + "/backend-api/codex/models", verify=trusted)
            with pytest.raises(httpx.ConnectError):
                httpx.get(base + "/backend-api/codex/models")
    finally:
        server.shutdown()
    assert response.status_code == 200 and response.text == "ok"
    assert seen == {"auth": "Bearer real"}


def test_codex_writer_hands_over_at_the_step_cap(monkeypatch):
    monkeypatch.setenv("DARKFACTORY_CODEX_MAX_STEPS", "2")
    monkeypatch.setattr(runner, "workspace_changed", lambda root="/workspace": True)
    started = [
        {
            "method": "item/started",
            "params": {"item": {"id": str(i), "type": "commandExecution", "command": "ls"}},
        }
        for i in range(3)
    ]
    proc = Process(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"thread": {"id": "t"}}},
            {"id": 4, "result": {"turn": {"id": "turn"}}},
            *started,
            {"id": 5, "result": {}},
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn", "status": "interrupted", "items": []}},
            },
        ]
    )
    monkeypatch.setattr(runner.subprocess, "Popen", lambda args, **kwargs: proc)
    result = runner.codex(
        {
            "credentials": {"accessToken": "token"},
            "role": "implement",
            "prompt": "p",
            "inner_checks": ["false"],
        }
    )
    assert result["status"] == "ok" and "предел шагов" in result["text"]
    sent = proc.stdin.getvalue()
    assert sent.count('"turn/interrupt"') == 1 and '"turnId": "turn"' in sent


def test_step_cap_waits_for_a_first_change(monkeypatch):
    monkeypatch.setenv("DARKFACTORY_CODEX_MAX_STEPS", "2")
    monkeypatch.setattr(runner, "workspace_changed", lambda root="/workspace": False)
    started = [
        {
            "method": "item/started",
            "params": {"item": {"id": str(i), "type": "commandExecution", "command": "ls"}},
        }
        for i in range(3)
    ]
    proc = Process(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"thread": {"id": "t"}}},
            {"id": 4, "result": {"turn": {"id": "turn"}}},
            *started,
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn", "status": "completed", "items": []}},
            },
        ]
    )
    monkeypatch.setattr(runner.subprocess, "Popen", lambda args, **kwargs: proc)
    runner.codex({"credentials": {"accessToken": "token"}, "role": "implement", "prompt": "p"})
    # Three steps, cap two, nothing changed yet: no hand-over of an empty commit before 2 x cap.
    assert '"turn/interrupt"' not in proc.stdin.getvalue()


def test_large_context_is_compacted_before_a_follow_up_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "codex_totals", {"seen": {}, "base": {}, "context": 0})
    usage = {
        "method": "thread/tokenUsage/updated",
        "params": {"tokenUsage": {"last": {"inputTokens": 90000}, "total": {"inputTokens": 90000}}},
    }
    proc = Process(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"thread": {"id": "t"}}},
            {"id": 4, "result": {"turn": {"id": "turn"}}},
            usage,
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn", "status": "completed", "items": []}},
            },
            {"id": 5, "result": {}},
            {"method": "thread/compacted", "params": {"threadId": "t"}},
            {"id": 6, "result": {"turn": {"id": "turn2"}}},
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn2", "status": "completed", "items": []}},
            },
        ]
    )
    monkeypatch.setattr(runner.subprocess, "Popen", lambda args, **kwargs: proc)
    monkeypatch.setattr(runner, "routine_workspace", lambda: False)
    runner.codex(
        {"credentials": {"accessToken": "token"}, "role": "implement", "prompt": "p", "self_review": True}
    )
    sent = proc.stdin.getvalue()
    assert sent.index('"thread/compact/start"') < sent.rindex('"turn/start"')


def test_follow_up_compaction_keeps_the_saved_context_size(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "codex_totals", {"seen": {}, "base": {}, "context": 0})
    monkeypatch.setattr(runner, "keep_sessions", lambda *args: True)
    usage = {
        "method": "thread/tokenUsage/updated",
        "params": {"tokenUsage": {"last": {"inputTokens": 90000}, "total": {"inputTokens": 90000}}},
    }
    proc = Process(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"thread": {"id": "t"}}},
            {"id": 4, "result": {"turn": {"id": "turn"}}},
            usage,
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn", "status": "completed", "items": []}},
            },
            {"id": 5, "result": {}},
            {"method": "thread/compacted", "params": {"threadId": "t"}},
            {"id": 6, "result": {"turn": {"id": "turn2"}}},
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn2", "status": "completed", "items": []}},
            },
        ]
    )
    monkeypatch.setattr(runner.subprocess, "Popen", lambda args, **kwargs: proc)
    monkeypatch.setattr(runner, "routine_workspace", lambda: False)
    job = {
        "credentials": {"accessToken": "token"},
        "role": "implement",
        "prompt": "p",
        "self_review": True,
        "session_dir": str(tmp_path),
    }
    runner.codex(job)
    # The next correction must see a large thread and compact it on resume.
    assert runner.session_usage(job, "codex")["context"] > runner.RESUME_COMPACT_TOKENS


def test_provider_overload_and_gateway_block_wait_for_the_pool():
    assert (
        runner.classify('{"message": "Selected model is at capacity.", "codexErrorInfo": "serverOverloaded"}')
        == "quota"
    )
    assert runner.classify("unexpected status 403 Forbidden: <html>blocked</html>") == "quota"
    assert runner.classify("Unauthorized: invalid_api_key") == "auth"
    assert runner.classify("Failed to authenticate. API Error: 403 Request not allowed") == "quota"


def test_jvm_tools_get_the_task_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Xmx1g")
    runner.jvm_proxy("http://127.0.0.1:40123", str(tmp_path))
    options = os.environ["JAVA_TOOL_OPTIONS"]
    assert options.startswith("-Xmx1g ") and "-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=40123" in options
    settings = (tmp_path / ".m2" / "settings.xml").read_text()
    assert "<host>127.0.0.1</host><port>40123</port>" in settings
    # A project's own settings stay.
    (tmp_path / ".m2" / "settings.xml").write_text("<settings/>")
    runner.jvm_proxy("http://127.0.0.1:40124", str(tmp_path))
    assert (tmp_path / ".m2" / "settings.xml").read_text() == "<settings/>"


def test_writer_names_its_own_commit(tmp_path):
    import json

    from darkfactory import execution, worker

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    assert execution.handoff_commit(evidence) is None
    (evidence / "handoff.json").write_text(json.dumps({"commit": " Sort the list\n by date ", "intent": "x"}))
    assert execution.handoff_commit(evidence) == "Sort the list by date"
    assert worker.commit_title({"commit_title": "Sort the list by date", "text": "Готово"}) == (
        "Sort the list by date"
    )
    # Without one, the final comment says what changed; factory notes and [LATER] lines never do.
    assert worker.commit_title({"text": "\nСписок сортируется по дате.\nПроверено тестами."}) == (
        "Список сортируется по дате."
    )
    assert worker.commit_title({"text": "[darkfactory] Вывод прогонов убран"}) is None
    assert worker.commit_title({}) is None
