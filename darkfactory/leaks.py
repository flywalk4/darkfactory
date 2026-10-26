"""Leak check for a public repository: credentials and owner-listed internal names in added lines and commit texts.

The owner's own patterns (internal domains, mail addresses, logins) come from the environment, never from the
repository: a list of internal domains committed to a public repository would itself be the leak.
"""

import os
import re
import subprocess

# Real token formats only: test fixtures such as "glpat-not-a-real-token" are too short to match.
SECRETS = {
    "GitLab token": r"glpat-[A-Za-z0-9_-]{20,}",
    "GitHub token": r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})",
    "OpenAI or Anthropic key": r"sk-(?:ant-)?[A-Za-z0-9_-]{32,}",
    "Slack token": r"xox[abpr]-[A-Za-z0-9-]{20,}",
    "private key": r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
    "JWT": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "bearer token": r"Bearer\s+[A-Za-z0-9._~+/-]{40,}",
    "AWS key": r"AKIA[0-9A-Z]{16}",
    "password in a URL": r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{6,}@",
    "assigned secret": (
        r"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)['\"]?\s*[:=]\s*"
        r"['\"](?![^'\"]*(?:example|placeholder|not-a-real|dummy|changeme|xxx|\.\.\.|…|\$\{))[^'\"\s]{16,}['\"]"
    ),
}
ALLOW = "leak-check: allow"


def owner_patterns(value=None):
    """Patterns from TEST_LEAK_PATTERNS: one per line or separated by `;`, plain text matched case-insensitively."""
    value = os.environ.get("TEST_LEAK_PATTERNS", "") if value is None else value
    return [item.strip() for item in re.split(r"[\n;]", value) if item.strip()]


def scan_text(text, extra=(), source="diff"):
    """Findings as (source, line number, rule) for every line that matches; the matched text is not repeated."""
    rules = [(name, re.compile(pattern)) for name, pattern in SECRETS.items()]
    rules += [
        (f"owner pattern #{index + 1}", re.compile(re.escape(item), re.I)) for index, item in enumerate(extra)
    ]
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW in line:
            continue
        for name, rule in rules:
            if rule.search(line):
                findings.append((source, number, name))
    return findings


def added_lines(diff):
    """Only the lines a change adds: removing a leaked value must not fail the check."""
    return "\n".join(
        line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    )


def scan_repository(base, head="HEAD", cwd=".", extra=None):
    extra = owner_patterns() if extra is None else extra

    def git(*args):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout

    diff = git("-c", "core.quotepath=off", "diff", "--no-ext-diff", "--no-textconv", base, head)
    messages = git("log", "--format=%B%n", f"{base}..{head}")
    names = git("diff", "--name-only", base, head)
    return (
        scan_text(added_lines(diff), extra, "diff")
        + scan_text(messages, extra, "commit message")
        + scan_text(names, extra, "file name")
    )
