"""Catalog definitions and real project-only MCP subprocesses."""

import json
import os
import select
import shlex
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from darkfactory import catalog
from darkfactory.agents import AgentSettings


@contextmanager
def server(name, workspace):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from darkfactory.catalog import serve; serve(sys.argv[1], Path(sys.argv[2]))",
            name,
            str(workspace),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    counter = 0

    def request(method, params=None):
        nonlocal counter
        counter += 1
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": counter, "method": method, "params": params or {}}) + "\n"
        )
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 3)[0], "MCP response timed out"
        line = process.stdout.readline()
        assert line, "MCP process closed output"
        value = json.loads(line)
        assert value["id"] == counter
        return value

    try:
        yield request
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def call(request, tool, arguments=None):
    return request("tools/call", {"name": tool, "arguments": arguments or {}})["result"]


def content(result):
    assert not result["isError"], result
    return json.loads(result["content"][0]["text"])


def test_every_catalog_definition_validates_together_and_has_no_setup_step():
    value = catalog.catalog()
    settings = AgentSettings.model_validate(
        {key: [item["definition"] for item in entries] for key, entries in value.items()}
        | {
            "assignments": {
                "planner": "manager",
                "writer": "developer",
                "reviewer": "reviewer",
                "director": "director",
            }
        }
    )
    assert {profile.name for profile in settings.profiles} == {
        "manager",
        "analyst",
        "developer",
        "reviewer",
        "director",
    }
    assert {profile.name for profile in settings.profiles if profile.can_delegate} == {"manager"}
    assert all(profile.provider == "codex" for profile in settings.profiles)
    assert len(settings.skills) == 7
    for skill in settings.skills:
        assert skill.content.startswith("---\nname: " + skill.name)
        assert len(skill.content) > 300
    for item in value["mcp_servers"]:
        definition = item["definition"]
        if item["id"] == "project-browser":
            assert definition["command"] == "playwright-mcp"
            assert not definition["read_only"]
            assert "--isolated" in definition["args"]
            assert "--headless" in definition["args"]
            assert "browser_navigate" in definition["enabled_tools"]
            assert "browser_evaluate" not in definition["enabled_tools"]
            continue
        if item["id"] == "jira":
            assert definition["command"] == "mcp-atlassian"
            assert definition["read_only"] is True and "--read-only" in definition["args"]
            assert definition["hosts"] == [] and "env" not in definition
            assert all(tool.startswith("jira_") for tool in definition["enabled_tools"])
            assert not {"jira_create_issue", "jira_download_attachments"} & set(definition["enabled_tools"])
            continue
        assert definition["command"] == "python"
        assert definition["args"] == ["-m", "darkfactory.catalog", "--serve", item["id"]]
        assert definition["read_only"] is True
        assert definition["enabled_tools"] == [tool["name"] for tool in catalog._tools(item["id"])]
        assert definition["env"] == {
            "PYTHONPATH": "/app",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        assert "url" not in definition
    value["profiles"][0]["definition"]["skills"].clear()
    assert catalog.catalog()["profiles"][0]["definition"]["skills"]


def test_builtin_module_cli_ignores_repository_module_shadowing(tmp_path):
    package = tmp_path / "darkfactory"
    package.mkdir()
    marker = tmp_path / "shadow-executed"
    (package / "__init__.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    result = subprocess.run(
        [sys.executable, "-m", "darkfactory.catalog", "--serve", "project-files"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        input="".join(json.dumps(message) + "\n" for message in messages),
        text=True,
        capture_output=True,
        check=True,
        timeout=3,
    )
    values = [json.loads(line) for line in result.stdout.splitlines()]
    assert values[0]["result"]["serverInfo"]["name"] == "darkfactory-project-files"
    assert len(values[1]["result"]["tools"]) == 3
    assert not marker.exists()


def test_files_server_initialization_catalog_and_bounded_working_tools(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.txt").write_text("first\nneedle second\nneedle third\n")
    (workspace / "literal.txt").write_text("literal .* text\n")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "secret").write_text("needle hidden metadata\n")
    with server("project-files", workspace) as request:
        init = request("initialize", {"protocolVersion": "2025-06-18"})["result"]
        assert init["protocolVersion"] == "2025-06-18"
        assert init["capabilities"] == {"tools": {"listChanged": False}}
        assert request("ping")["result"] == {}
        tools = request("tools/list")["result"]["tools"]
        assert {tool["name"] for tool in tools} == {"files.list", "files.read", "files.search"}
        assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
        listing = content(call(request, "files.list"))
        assert {entry["name"] for entry in listing["entries"]} == {"sample.txt", "literal.txt"}
        read = content(call(request, "files.read", {"path": "sample.txt", "max_bytes": 5}))
        assert read == {"path": "sample.txt", "text": "first", "truncated": True}
        search = content(call(request, "files.search", {"query": "needle", "limit": 1}))
        assert search["matches"] == [{"path": "sample.txt", "line": 2, "text": "needle second"}]
        assert search["truncated"] is True
        literal = content(call(request, "files.search", {"query": ".*"}))
        assert [item["path"] for item in literal["matches"]] == ["literal.txt"]
        assert "error" in request("tools/call", {"name": "git.status", "arguments": {}})
        assert "error" in request("sampling/createMessage")


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "/etc/passwd",
        ".git/config",
        "nested/../sample.txt",
        "outside-link",
        "outside-dir/outside.txt",
        "metadata-link/config",
    ],
)
def test_files_rejects_escape_git_metadata_and_symlinks(tmp_path, path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("private outside value")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("private Git metadata")
    (workspace / "outside-link").symlink_to(tmp_path / "outside.txt")
    (workspace / "outside-dir").symlink_to(tmp_path, target_is_directory=True)
    (workspace / "metadata-link").symlink_to(workspace / ".git", target_is_directory=True)
    with server("project-files", workspace) as request:
        result = call(request, "files.read", {"path": path})
        assert result["isError"] is True
        assert "private" not in result["content"][0]["text"]
        search = content(call(request, "files.search", {"query": "private"}))
        assert search["matches"] == []


def test_files_bounds_listing_large_reads_arguments_and_special_files(tmp_path):
    (tmp_path / "big.txt").write_bytes(b"x" * 100000)
    (tmp_path / "other.txt").write_text("other")
    os.mkfifo(tmp_path / "pipe")
    with server("project-files", tmp_path) as request:
        read = content(call(request, "files.read", {"path": "big.txt", "max_bytes": 65536}))
        assert len(read["text"]) == 65536 and read["truncated"]
        listing = content(call(request, "files.list", {"limit": 1}))
        assert len(listing["entries"]) == 1 and listing["truncated"]
        assert call(request, "files.read", {"path": "pipe"})["isError"]
        assert call(request, "files.read", {"path": "big.txt", "max_bytes": 65537})["isError"]
        assert "error" in request(
            "tools/call", {"name": "files.read", "arguments": {"path": "big.txt", "command": "sh"}}
        )


@pytest.fixture
def git_project(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args],
            cwd=workspace,
            env={"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    git("init", "-b", "main")
    (workspace / "value.txt").write_text("before\n")
    git("add", ".")
    git("commit", "-m", "Initial catalog fixture")
    return workspace, git


def test_git_server_reads_real_working_staged_diff_status_history_without_writing(git_project):
    workspace, git = git_project
    (workspace / "value.txt").write_text("after\n")
    before_head = git("rev-parse", "HEAD")
    index = workspace / ".git" / "index"
    before_index, before_mtime = index.read_bytes(), index.stat().st_mtime_ns
    with server("project-git", workspace) as request:
        names = [tool["name"] for tool in request("tools/list")["result"]["tools"]]
        assert names == ["git.status", "git.diff", "git.log"]
        assert " M value.txt" in content(call(request, "git.status"))["text"]
        assert "+after" in content(call(request, "git.diff"))["text"]
        assert "Initial catalog fixture" in content(call(request, "git.log", {"limit": 1}))["text"]
        assert content(call(request, "git.diff", {"staged": True}))["text"] == ""
        assert "error" in request("tools/call", {"name": "files.read", "arguments": {"path": "value.txt"}})
        assert call(request, "git.diff", {"path": "../outside"})["isError"]
    assert index.read_bytes() == before_index and index.stat().st_mtime_ns == before_mtime
    assert git("rev-parse", "HEAD") == before_head
    assert (workspace / "value.txt").read_text() == "after\n"
    git("add", "value.txt")
    with server("project-git", workspace) as request:
        assert "+after" in content(call(request, "git.diff", {"staged": True}))["text"]


def test_git_disables_external_diff_textconv_fsmonitor_and_clean_process_filters(git_project, tmp_path):
    workspace, git = git_project
    marker = tmp_path / "must not run"
    command = "touch " + shlex.quote(str(marker)) + "; cat"
    (workspace / ".gitattributes").write_text("*.txt diff=evil filter=evil\n")
    (workspace / "value.txt").write_text("after\n")
    for key in (
        "diff.external",
        "diff.evil.textconv",
        "filter.evil.clean",
        "filter.evil.process",
        "core.fsmonitor",
    ):
        git("config", key, command)
    git("config", "filter.evil.required", "true")
    with server("project-git", workspace) as request:
        assert "+after" in content(call(request, "git.diff"))["text"]
        assert "value.txt" in content(call(request, "git.status"))["text"]
    assert not marker.exists()


def test_git_pins_workspace_ignoring_repository_and_inherited_worktree_settings(
    git_project, tmp_path, monkeypatch
):
    workspace, git = git_project
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "value.txt").write_text("private outside value\n")
    (workspace / "value.txt").write_text("allowed workspace value\n")
    git("config", "core.worktree", str(outside))
    monkeypatch.setenv("GIT_WORK_TREE", str(outside))
    monkeypatch.setenv("GIT_DIR", str(outside))
    with server("project-git", workspace) as request:
        result = content(call(request, "git.diff"))["text"]
        assert "allowed workspace value" in result
        assert "private outside value" not in result


def test_git_bounds_large_diff_and_rejects_external_git_directory(git_project, tmp_path):
    workspace, _git = git_project
    (workspace / "value.txt").write_text("after" * 30000)
    with server("project-git", workspace) as request:
        result = content(call(request, "git.diff"))
        assert result["truncated"] is True
        assert len(result["text"].encode()) <= catalog.MAX_BYTES
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").symlink_to(workspace / ".git", target_is_directory=True)
    with server("project-git", other) as request:
        assert call(request, "git.status")["isError"]


def test_git_timeout_terminates_and_reaps_actual_process(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    executable = tmp_path / "git"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(100)\n")
    executable.chmod(0o700)
    monkeypatch.setattr(catalog, "GIT_EXECUTABLE", str(executable))
    monkeypatch.setattr(catalog, "GIT_TIMEOUT", 0.1)
    processes = []
    original = catalog.subprocess.Popen

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(catalog.subprocess, "Popen", capture)
    with pytest.raises(ValueError, match="время чтения Git"):
        catalog._git(tmp_path, "git.status", {})
    assert len(processes) == 1 and processes[0].poll() is not None
    assert processes[0].stdout.closed
