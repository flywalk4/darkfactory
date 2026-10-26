import json
import sys
import time

import pytest

from darkfactory import onboarding
from darkfactory.onboarding import LoginFlow


def wait_for(flow, statuses):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = flow.snapshot()
        if result["status"] in statuses:
            return result
        time.sleep(0.01)
    flow.cancel()
    pytest.fail("Fake login did not finish")


def fake_command(monkeypatch, program):
    monkeypatch.setattr(LoginFlow, "_command", lambda self: [sys.executable, "-u", "-c", program])


def test_codex_requires_personal_mode_and_fresh_directory(tmp_path):
    with pytest.raises(ValueError, match="personal"):
        LoginFlow("codex", tmp_path)
    (tmp_path / "auth.json").write_text("must not be read")
    flow = LoginFlow("codex", tmp_path, personal_mode=True)
    with pytest.raises(ValueError, match="empty"):
        flow.start()


def test_codex_supported_command_and_no_host_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-inherit")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "do-not-inherit")
    monkeypatch.setenv("HTTPS_PROXY", "do-not-inherit")
    flow = LoginFlow("codex", tmp_path, personal_mode=True)
    assert flow._command() == ["codex", "-c", 'cli_auth_credentials_store="file"', "login", "--device-auth"]
    env = flow._environment()
    assert env["HOME"] == str(tmp_path)
    assert env["CODEX_HOME"] == str(tmp_path / "codex")
    assert not {"OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY"} & env.keys()
    assert LoginFlow("claude", tmp_path)._command() == ["claude", "setup-token"]


def test_codex_reads_only_isolated_result_and_returns_secret_once(tmp_path, monkeypatch):
    credentials = {
        "tokens": {
            "access_token": "fake-access",
            "account_id": "fake-account",
            "refresh_token": "fake-refresh",
        }
    }
    fake_command(
        monkeypatch,
        "\n".join(
            [
                "import os,json,time,pathlib",
                "print('\\x1b[34mhttps://auth.openai.com/codex/device\\x1b[0m',flush=True)",
                "print('Enter this one-time code (expires in 15 minutes)\\n ABCD-EFGH',flush=True)",
                "time.sleep(.2)",
                f"pathlib.Path(os.environ['CODEX_HOME'],'auth.json').write_text({json.dumps(json.dumps(credentials))})",
            ]
        ),
    )
    flow = LoginFlow("codex", tmp_path, personal_mode=True)
    flow.start()
    waiting = wait_for(flow, {"waiting"})
    assert waiting["verification_url"] == "https://auth.openai.com/codex/device"
    assert waiting["user_code"] == "ABCD-EFGH"
    result = wait_for(flow, {"done", "failed"})
    assert result["status"] == "done"
    assert "fake-access" not in json.dumps(result)
    assert flow.take_credentials() == {
        "auth_method": "codex_local",
        "accessToken": "fake-access",
        "chatgptAccountId": "fake-account",
        "refreshToken": "fake-refresh",
    }
    assert flow.take_credentials() is None
    assert not (tmp_path / "codex" / "auth.json").exists()
    assert not flow._buffer


def test_claude_pty_manual_code_and_secret_redaction(tmp_path, monkeypatch):
    token = "sk-ant-oat01-" + "fake" * 20
    fake_command(
        monkeypatch,
        "\n".join(
            [
                "import sys",
                "assert sys.stdin.isatty()",
                "print('https://claude.com/cai/oauth/authorize?client_id=fake&state=fake',flush=True)",
                "assert input().strip() == 'callback-code#state'",
                f"print('Your OAuth token (valid for 1 year):\\n{token}\\n',flush=True)",
            ]
        ),
    )
    flow = LoginFlow("claude", tmp_path)
    flow.start()
    waiting = wait_for(flow, {"waiting"})
    assert "claude.com" in waiting["verification_url"]
    with pytest.raises(ValueError, match="complete"):
        flow.submit_code("callback-code\nmalicious")
    flow.submit_code("callback-code#state")
    result = wait_for(flow, {"done", "failed"})
    assert result["status"] == "done"
    assert token not in json.dumps(result)
    assert "callback-code" not in json.dumps(result)
    assert flow.take_credentials() == {"oauth_token": token}
    assert flow.take_credentials() is None
    assert not flow._buffer


def test_claude_code_and_enter_are_separate_keystrokes(tmp_path, monkeypatch):
    # Claude Code keeps a CR that arrives inside a long pasted chunk as text, never submitting.
    fake_command(
        monkeypatch,
        "\n".join(
            [
                "print('https://claude.com/cai/oauth/authorize?client_id=fake&state=fake',flush=True)",
                "import time; time.sleep(20)",
            ]
        ),
    )
    flow = LoginFlow("claude", tmp_path)
    flow.start()
    wait_for(flow, {"waiting"})
    writes = []
    monkeypatch.setattr(onboarding, "PASTE_SETTLE", 0)
    monkeypatch.setattr(onboarding.os, "write", lambda fd, data: writes.append(data) or len(data))
    code = "a" * 100 + "#" + "b" * 43
    flow.submit_code(code)
    assert writes == [code.encode(), b"\r"]
    flow.cancel()


def test_cancel_terminates_process_and_does_not_repeat_start(tmp_path, monkeypatch):
    fake_command(monkeypatch, "import time; time.sleep(20)")
    flow = LoginFlow("claude", tmp_path)
    flow.start()
    assert flow.cancel()["status"] == "cancelled"
    assert flow._process.poll() is not None
    assert flow.take_credentials() is None
    with pytest.raises(ValueError, match="already"):
        flow.start()


def test_silent_login_expires_without_exposing_output(tmp_path, monkeypatch):
    fake_command(monkeypatch, "import time; time.sleep(20)")
    monkeypatch.setattr(onboarding, "LOGIN_TIMEOUT", 0.05)
    flow = LoginFlow("claude", tmp_path)
    flow.start()
    assert wait_for(flow, {"expired"})["status"] == "expired"
    assert flow._process.poll() is not None


def test_output_overflow_fails_and_private_urls_are_not_exposed(tmp_path, monkeypatch):
    fake_command(monkeypatch, "print('https://evil.invalid/?token=secret\\n' + 'sensitive' * 10000)")
    flow = LoginFlow("claude", tmp_path)
    flow.start()
    result = wait_for(flow, {"failed"})
    assert result["verification_url"] is None
    assert "sensitive" not in json.dumps(result)
    assert "secret" not in json.dumps(result)
    assert flow.take_credentials() is None
    assert not flow._buffer


def test_symlink_login_directory_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    with pytest.raises(ValueError, match="isolated"):
        LoginFlow("claude", alias).start()


def test_vendor_url_with_token_query_is_not_public(tmp_path, monkeypatch):
    fake_command(monkeypatch, "print('https://claude.com/cai/oauth/authorize?access_token=private')")
    flow = LoginFlow("claude", tmp_path)
    flow.start()
    result = wait_for(flow, {"failed"})
    assert result["verification_url"] is None
    assert "private" not in json.dumps(result)
