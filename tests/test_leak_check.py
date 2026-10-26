import subprocess
import sys
from pathlib import Path

import pytest

from darkfactory import leaks, sanitize

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "leak_check.py"
# Built at run time so this file holds no matching literal itself.
GITLAB = "glpat-" + "A1b2C3d4E5f6G7h8I9j0"
GITHUB = "ghp_" + "x" * 36


@pytest.mark.parametrize(
    "line",
    [
        f'TOKEN = "{GITLAB}"',
        f"curl -H 'Authorization: Bearer {'a' * 48}'",
        GITHUB,
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
        "https://deploy:" + "s3cretPass" + "@git.example.com/repo.git",
        'password = "' + "Zx9" * 6 + '"',
        "sk-ant-" + "k" * 40,
    ],
)
def test_credentials_are_found(line):
    assert leaks.scan_text(line)


@pytest.mark.parametrize(
    "line",
    [
        '"forge_token": "glpat-not-a-real-token"',
        'password = "example-password-value"',
        "token = os.environ['TOKEN']",
        "sha = 'a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2'",
        f'TOKEN = "{GITLAB}"  # leak-check: allow',
    ],
)
def test_fixtures_and_ordinary_code_pass(line):
    assert not leaks.scan_text(line)


def test_owner_patterns_come_from_the_environment_only():
    patterns = leaks.owner_patterns("intra.example\nsomeone@corp;  ")
    assert patterns == ["intra.example", "someone@corp"]
    assert leaks.scan_text("see https://git.INTRA.example/x", patterns) == [("diff", 1, "owner pattern #1")]


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_script_checks_added_lines_commit_messages_and_names(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text(f"OLD = '{GITLAB}'\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()

    def run(env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), base],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", **(env or {})},
        )

    # Removing a leaked value is fine.
    (tmp_path / "a.py").write_text("VALUE = 1\n")
    git(tmp_path, "commit", "-qam", "drop old value")
    assert run().returncode == 0
    # An internal host in a commit message fails, and the value itself is not printed.
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "Fix login on intra.example")
    result = run({"TEST_LEAK_PATTERNS": "intra.example"})
    assert (
        result.returncode == 1 and "commit message" in result.stdout and "intra.example" not in result.stdout
    )
    assert run().returncode == 0
    (tmp_path / "b.py").write_text(f"KEY = '{GITHUB}'\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "add b")
    result = run()
    assert result.returncode == 1 and "GitHub token in diff" in result.stdout and GITHUB not in result.stdout
    # Without a base (the preprod monitor) there is nothing to compare.
    no_base = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert no_base.returncode == 0


def test_scrub_hides_hosts_addresses_and_secrets():
    text = (
        f"git push to https://git.intra.example/team/app.git failed for ops@intra.example "
        f"from 10.1.2.3:8443 at /home/deploy/app with {GITLAB}; see build-01.corp.example.ru in Acme project"
    )
    clean = sanitize.scrub(text, patterns=["intra.example"], names=["Acme"])
    for secret in ("intra.example", "10.1.2.3", "/home/deploy", GITLAB, "corp.example", "Acme", "ops@"):
        assert secret not in clean
    assert "/workspace/src/app.py" in sanitize.scrub("error in /workspace/src/app.py")
