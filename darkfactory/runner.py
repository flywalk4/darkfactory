"""Entrypoint inside an unprivileged task container. No database or Docker access."""

import faulthandler
import fcntl
import fnmatch
import hashlib
import json
import math
import os
import queue
import re
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import logs
from .agents import codex_mcp_config, materialize_skills
from .egress import local_proxy, local_tunnel
from .mcp_bridge import project_mcp
from .privileges import as_user, hand_over
from .relay import PLACEHOLDER as RELAY_PLACEHOLDER
from .relay import auth_relay, loopback_certificate

CHECKLIST = (
    "acceptance_met",
    "no_regression_in_touched_code",
    "no_secrets_or_unsafe_input",
    "tests_cover_change",
    "no_unrelated_changes",
)
# Unmet, these block approval; the others become backlog notes.
BLOCKING_CHECKS = {"acceptance_met", "no_regression_in_touched_code", "no_secrets_or_unsafe_input"}
REVIEW_CONFIDENCE = int(os.getenv("DARKFACTORY_REVIEW_CONFIDENCE", "80"))
# A finding that names an unmet Definition-of-Done item is the task not done, however unsure or minor the
# reviewer filed it: a feature only behind a flag or URL parameter, an API nothing calls, a report instead of
# the change, a missing production-impact section. As a [LATER] note it let such tasks pass as done.
UNDONE = re.compile(
    r"feature[- ]flag|behind (?:a |the )?(?:flag|toggle|query|url)|(?:url|query)[- ]param"
    r"|за флаг|под флаг|по флагу|параметр\w* (?:url|запроса)"
    r"|dead (?:api|code|endpoint)|no callers?\b|never called|nothing calls|not called anywhere"
    r"|мёртв\w* (?:api|код)|мертв\w* (?:api|код)|никто не вызывает|нет вызыва|не вызывается"
    r"|report[- ]only|only (?:a |the )?reports?\b|только отч[её]т"
    r"|prod(?:uction)?[- ]impact|влияни\w* на прод",
    re.IGNORECASE,
)
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "checklist": {
            "type": "object",
            "properties": {item: {"type": "boolean"} for item in CHECKLIST},
            "required": list(CHECKLIST),
            "additionalProperties": False,
        },
        "findings": {
            "type": "array",
            "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    "env": {"type": "boolean"},
                    "file": {"type": "string", "maxLength": 300},
                    "line": {"type": "integer", "minimum": 0},
                    "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
                "required": ["severity", "confidence", "env", "file", "line", "text"],
                "additionalProperties": False,
            },
        },
        "files": {
            "type": "array",
            "maxItems": 200,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "maxLength": 300},
                    "verdict": {"type": "string", "enum": ["ok", "issue"]},
                    "evidence": {"type": "string", "maxLength": 1500},
                },
                "required": ["path", "verdict", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["checklist", "findings", "files"],
    "additionalProperties": False,
}
# Evidence shorter than this is a tick, not a check: it has to name what was read and why it holds.
EVIDENCE_CHARS = 40
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string", "minLength": 1, "maxLength": 32000},
        "questions": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "subtasks": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,40}$"},
                    "title": {"type": "string", "minLength": 1, "maxLength": 300},
                    "request": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "acceptance": {"type": "string", "minLength": 1, "maxLength": 12000},
                    "agent_profile": {"type": ["string", "null"]},
                    "tier": {"type": ["string", "null"], "enum": ["light", "standard", "heavy", None]},
                    "files": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 300}},
                    "depends_on": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,40}$"},
                    },
                },
                "required": [
                    "key",
                    "title",
                    "request",
                    "acceptance",
                    "depends_on",
                    "agent_profile",
                    "tier",
                    "files",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["plan", "questions", "subtasks"],
    "additionalProperties": False,
}


def plan_object(text, allow_subtasks=True):
    value = json.loads(text)
    if (
        not isinstance(value, dict)
        or set(value) not in ({"plan", "questions"}, {"plan", "questions", "subtasks"})
        or not isinstance(value["plan"], str)
        or not value["plan"].strip()
        or len(value["plan"]) > 32000
        or not isinstance(value["questions"], list)
        or len(value["questions"]) > 20
        or not all(
            isinstance(question, str) and question.strip() and len(question) <= 2000
            for question in value["questions"]
        )
    ):
        raise ValueError("Planner did not return a valid structured plan")
    subtasks = value.setdefault("subtasks", [])
    if not isinstance(subtasks, list) or len(subtasks) > 8 or (subtasks and not allow_subtasks):
        raise ValueError("Planner subtasks exceed the permitted decomposition")
    graph = {}
    for subtask in subtasks:
        if (
            not isinstance(subtask, dict)
            or set(subtask) - {"agent_profile", "tier", "files"}
            != {"key", "title", "request", "acceptance", "depends_on"}
            or not isinstance(subtask.get("files", []), list)
            or len(subtask.get("files", [])) > 20
            or not all(isinstance(path, str) and 0 < len(path) <= 300 for path in subtask.get("files", []))
            or subtask.get("tier") not in (None, "light", "standard", "heavy")
            or (
                subtask.get("agent_profile") is not None
                and (
                    not isinstance(subtask["agent_profile"], str)
                    or re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,49}", subtask["agent_profile"]) is None
                )
            )
            or not isinstance(subtask["key"], str)
            or re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", subtask["key"]) is None
            or any(
                not isinstance(subtask[field], str)
                or not subtask[field].strip()
                or len(subtask[field]) > maximum
                for field, maximum in (("title", 300), ("request", 20000), ("acceptance", 12000))
            )
            or not isinstance(subtask["depends_on"], list)
            or len(subtask["depends_on"]) > 8
            or not all(isinstance(key, str) for key in subtask["depends_on"])
            or len(set(subtask["depends_on"])) != len(subtask["depends_on"])
            or subtask["key"] in graph
        ):
            raise ValueError("Planner returned an invalid subtask")
        graph[subtask["key"]] = subtask["depends_on"]
    if any(dependency not in graph for dependencies in graph.values() for dependency in dependencies):
        raise ValueError("Planner referenced an unknown subtask")
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            raise ValueError("Planner subtask dependencies contain a cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in graph[key]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in graph:
        visit(key)
    return value


def codex_quota(snapshot):
    """Read documented RateLimitSnapshot fields without inferring unknown limits."""
    if not isinstance(snapshot, dict):
        return {}
    windows = [
        window
        for key in ("primary", "secondary")
        if isinstance(window := snapshot.get(key), dict) and type(window.get("usedPercent")) is int
    ]
    if not windows:
        return {}
    remaining = min(max(0, min(100, 100 - window["usedPercent"])) for window in windows)
    limiting = [window for window in windows if max(0, min(100, 100 - window["usedPercent"])) == remaining]
    resets = [
        window["resetsAt"]
        for window in limiting
        if type(window.get("resetsAt")) is int and window["resetsAt"] > 0
    ]
    quota = {"remaining_percent": remaining, "source": "codex.account/rateLimits/updated"}
    if resets:
        quota["reset_at"] = max(resets)
    return quota


def classify(value):
    text = str(value).lower()
    if any(
        word in text
        for word in ("usagelimitexceeded", "rate_limit", "rate limit", "quota", "429", "usage limit")
    ):
        return "quota"
    if any(
        word in text
        for word in (
            "serveroverloaded",
            "at capacity",
            "overloaded",
            "503 service unavailable",
            "403 forbidden",
            # Claude subscriptions intermittently answer "Failed to authenticate. API Error: 403 Request not
            # allowed" and work again minutes later; marking them "auth" would park them until a re-login.
            "403 request not allowed",
        )
    ):
        # Provider overload or a gateway's block page: the account is fine and the next attempt usually
        # works. Treated like quota, the task waits for the pool instead of stopping for a human.
        return "quota"
    if any(
        word in text
        for word in (
            "unauthorized",
            "401",
            "invalid_api_key",
            "authentication",
            "not logged in",
            "oauth token",
        )
    ):
        return "auth"
    return "error"


def json_object(text):
    """The review verdict as {"approved", "findings": [str]}. A checklist verdict is decided here, by the
    factory: confident blocker/major findings, findings that name an unmet Definition-of-Done item and unmet
    blocking checks reject; everything else becomes a [LATER] note, and sandbox gaps an [ENV] one."""
    value = json.loads(text)
    if isinstance(value, dict) and {"checklist", "findings"} <= set(value) <= {
        "checklist",
        "findings",
        "files",
    }:
        return checklist_verdict(value)
    if (
        not isinstance(value, dict)
        or set(value) != {"approved", "findings"}
        or type(value["approved"]) is not bool
        or not isinstance(value["findings"], list)
        or not all(isinstance(item, str) for item in value["findings"])
    ):
        raise ValueError("Reviewer did not return a valid structured verdict")
    return value


def uncovered(value, required):
    """Changed code files the reviewer gave no real evidence for."""
    try:
        files = (value if isinstance(value, dict) else json.loads(value or "{}")).get("files") or []
    except (ValueError, AttributeError):
        return list(required or [])
    checked = {
        str(item.get("path", "")).lstrip("./")
        for item in files
        if isinstance(item, dict) and len(str(item.get("evidence") or "").strip()) >= EVIDENCE_CHARS
    }
    return [path for path in required or [] if path.lstrip("./") not in checked]


def checklist_verdict(value):
    checklist, findings = value["checklist"], value["findings"]
    if (
        not isinstance(checklist, dict)
        or set(checklist) != set(CHECKLIST)
        or not all(type(item) is bool for item in checklist.values())
        or not isinstance(findings, list)
    ):
        raise ValueError("Reviewer did not return a valid structured verdict")
    unmet = [item for item in CHECKLIST if not checklist[item]]
    blocked, notes = [], []
    for item in findings:
        if (
            not isinstance(item, dict)
            or item.get("severity") not in {"blocker", "major", "minor"}
            or type(item.get("confidence")) is not int
            or not isinstance(item.get("text"), str)
            or not item["text"].strip()
        ):
            raise ValueError("Reviewer did not return a valid structured verdict")
        where = f"{item.get('file')}:{item.get('line')} — " if item.get("file") else ""
        text = where + item["text"].strip()
        confident = item["severity"] != "minor" and item["confidence"] >= REVIEW_CONFIDENCE
        if item.get("env"):
            # A sandbox gap blocks only while the requested behaviour is not shown to work at all.
            (blocked if confident and "acceptance_met" in unmet else notes).append("[ENV] " + text)
        elif confident:
            blocked.append(text)
        elif UNDONE.search(item["text"]):
            blocked.append("Не сделано (критерий готовности): " + text)
        else:
            notes.append("[LATER] " + text)
    for item in unmet:
        if item in BLOCKING_CHECKS and not any(not entry.startswith("[ENV]") for entry in blocked):
            # The reviewer said the check fails but filed its reasons as minor: those reasons are the
            # rejection. Without them the writer got a bare "acceptance_met" and nothing to act on.
            reasons = [entry.removeprefix("[LATER] ") for entry in notes if entry.startswith("[LATER] ")]
            reasons += [
                f"{entry.get('path')}: {entry.get('evidence')}"
                for entry in value.get("files") or []
                if isinstance(entry, dict) and entry.get("verdict") == "issue" and entry.get("evidence")
            ]
            if blocked and not reasons:
                # The sandbox gaps are the reason: a checklist line would make the rejection look like
                # code work and send it back to a writer who cannot close it, cycle after cycle.
                continue
            notes = [entry for entry in notes if not entry.startswith("[LATER] ")]
            blocked.append(f"Чеклист ревью: не выполнено {item}")
            blocked.extend(
                reasons or [f"Ревьюер не указал причину невыполненного {item}; сверь задачу с кодом"]
            )
        elif item not in BLOCKING_CHECKS:
            notes.append(f"[LATER] Чеклист ревью: {item}")
    return {"approved": not blocked, "findings": blocked + notes}


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
    elif isinstance(value, dict):
        value = {key: redact(item, secrets) for key, item in value.items()}
    elif isinstance(value, list):
        value = [redact(item, secrets) for item in value]
    return value


class Transcript:
    """Append-only, redacted agent activity that the task page shows while a run is live."""

    LIMIT = 4 * 1024 * 1024
    ENTRY = 6000

    def __init__(self, path=None, secrets=()):
        self.path, self.secrets, self.written, self.pulsed = path, secrets, 0, 0.0

    def pulse(self):
        """Mark the agent alive without logging; the control plane reads the mtime for stall detection."""
        if not self.path or time.monotonic() - self.pulsed < 10:
            return
        self.pulsed = time.monotonic()
        try:
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644))
            os.utime(self.path)
        except OSError:
            pass

    def __call__(self, kind, text):
        if not self.path or not text or self.written >= self.LIMIT:
            return
        text = redact(str(text), self.secrets)
        if len(text) > self.ENTRY:
            text = text[: self.ENTRY] + "\n…"
        line = json.dumps({"at": time.time(), "kind": kind, "text": text}, ensure_ascii=False) + "\n"
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
            try:
                self.written += os.write(fd, line.encode())
            finally:
                os.close(fd)
        except OSError:
            self.path = None

    def usage(self, **values):
        """Replace the run's token/cost summary beside the transcript; the task page shows it per run."""
        if not self.path:
            return
        path = os.path.join(os.path.dirname(self.path), "usage.json")
        try:
            with open(path + ".tmp", "w") as file:
                json.dump({key: value for key, value in values.items() if value is not None}, file)
            os.replace(path + ".tmp", path)
        except OSError:
            pass


note = Transcript()

# Files whose change means dependencies must be installed again.
MANIFESTS = re.compile(
    r"(?:^|/)(?:package(?:-lock)?\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|[^/]+\.(?:cs|fs|vb)proj|"
    r"packages\.lock\.json|Directory\.(?:Packages|Build)\.props|global\.json|[Nn]u[Gg]et\.[Cc]onfig|"
    r"pyproject\.toml|requirements[^/]*\.txt|uv\.lock|poetry\.lock|Pipfile\.lock|go\.(?:mod|sum)|"
    r"Cargo\.(?:toml|lock)|Gemfile(?:\.lock)?|composer\.(?:json|lock)|pom\.xml|[^/]+\.gradle(?:\.kts)?)$"
)


INSTALLED = re.compile(r"(?:^|/)(?:node_modules|bin|obj|\.venv|venv)/")


def setup_stamp(commands, root="/workspace"):
    """Digest of setup commands and dependency manifests, or None when the tree cannot be listed."""
    try:
        names = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return None
    digest = hashlib.sha256(json.dumps(commands).encode())
    # Installed trees (a node_modules without a .gitignore entry) hold thousands of their own manifests.
    for name in sorted(n for n in names if MANIFESTS.search(n) and not INSTALLED.search(n)):
        digest.update(name.encode() + b"\0")
        try:
            digest.update(Path(root, name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()


SETUP_SECONDS = 900
OUTPUT_TAIL = 65536


def shell(command, cwd=None, limit=None):
    """Run a command with live pulses; (exit code or None on timeout, output tail).

    Output goes to a file, not a pipe: build servers (MSBuild nodes, VBCSCompiler) and other daemons
    inherit the pipe and keep it open after the command exits, which hangs a reader until the run alarm.
    A long install or test suite is work, not a stalled agent, so the transcript keeps pulsing."""
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(
            ["sh", "-lc", command],
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,
            **as_user(),
        )
        started, code = time.monotonic(), None
        while code is None:
            note.pulse()
            try:
                code = proc.wait(timeout=min(10, limit) if limit else 10)
            except subprocess.TimeoutExpired:
                if limit and time.monotonic() - started > limit:
                    break
        try:
            # Whatever the command left behind would keep running into the next check.
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        if code is None:
            proc.wait()
        out.seek(max(0, out.seek(0, 2) - OUTPUT_TAIL))
        return code, out.read().decode(errors="replace")


def keep_sessions(job, provider, store):
    """Point the agent's own session store at the task's persistent session directory."""
    if not job.get("session_dir"):
        return False
    target = Path(job["session_dir"]) / provider
    target.mkdir(parents=True, exist_ok=True)
    hand_over(target)
    if not store.exists() and not store.is_symlink():
        store.parent.mkdir(parents=True, exist_ok=True)
        store.symlink_to(target)
    return True


def session_state(job, provider):
    """The session to continue, when the factory asked for it and this provider left one."""
    if not job.get("session_dir") or not job.get("resume_prompt"):
        return None
    try:
        return json.loads((Path(job["session_dir"]) / f"{provider}.json").read_text()).get("session") or None
    except (OSError, ValueError, AttributeError):
        return None


def save_session(job, provider, session, usage=None):
    if job.get("session_dir") and session:
        (Path(job["session_dir"]) / f"{provider}.json").write_text(
            json.dumps({"session": session, **({"usage": usage} if usage else {})})
        )


def session_usage(job, provider):
    """Thread token totals saved at the end of the provider's previous run, or {}."""
    try:
        value = json.loads((Path(job["session_dir"]) / f"{provider}.json").read_text()).get("usage")
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def extension(path):
    """Seconds left of a deadline the control plane moved; 0 when it did not."""
    try:
        return float(Path(path).read_text()) - time.time()
    except (OSError, ValueError):
        return 0


# Set when setup was skipped on a matching stamp: a stamp only hashes the manifests, so a workspace
# that lost what setup made (a donor copied without its virtualenv) still looks installed.
setup_skipped = {}


def checks_after_setup(commands, scopes=None, base=None):
    """Run the checks; when they fail after setup was skipped on its stamp, reinstall once and rerun."""
    passed, text = run_checks(commands, scopes, base)
    if passed or not setup_skipped.get("by"):
        return passed, text
    setup_commands, marker, root = setup_skipped.pop("by")
    note("status", "Проверка упала после пропуска setup — переустанавливаю зависимости и повторяю проверки")
    Path(marker).unlink(missing_ok=True)
    setup(setup_commands, marker, root)
    setup_skipped.clear()
    return run_checks(commands, scopes, base)


def setup(commands, marker=None, root="/workspace"):
    """Install dependencies the environment spec declares; skipped when nothing they depend on changed.

    Returns the commands that failed, as "command (exit N)" lines; the writer is told about them."""
    if not commands:
        return []
    stamp = setup_stamp(commands, root) if marker else None
    marker = Path(marker) if marker else None
    try:
        if stamp and marker.read_text() == stamp:
            note("status", "Зависимости не менялись с прошлой установки — setup пропущен")
            setup_skipped["by"] = (commands, str(marker), root)
            return []
    except OSError:
        pass
    try:
        # A workspace copied from this one mid-install must not inherit a stamp for a half-built tree.
        if marker:
            marker.unlink(missing_ok=True)
    except OSError:
        pass
    failed = []
    for command in commands:
        # Failures are the agent's to see, not fatal.
        note("tool", "$ " + command)
        code, output = shell(command, cwd=root, limit=SETUP_SECONDS)
        if code is None:
            note("output", "timeout")
            failed.append(f"{command} (timeout)")
        else:
            note("output", f"exit={code}\n" + output[-4000:])
            if code:
                failed.append(f"{command} (exit {code})")
    try:
        # Stamped after the install: a restore that writes a lock file must not miss the stamp forever.
        if marker and not failed and (stamp := setup_stamp(commands, root)):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(stamp)
    except OSError:
        pass
    return failed


# Writers re-ran `npm ci` and `dotnet restore` despite the prompt: npm ci deletes node_modules under running
# tools and each costs minutes. Once the factory installed everything, these wrappers answer for exactly the
# installs setup ran. Anything else, such as the restore of a project the writer adds, reaches the real tool.
SHIM = """#!{python} -I
import os, sys
REAL, TOOL, COVERED = {real!r}, {tool!r}, {covered!r}
args = sys.argv[1:]

def target(words, flags):
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
            continue
        if word in flags:
            skip_next = True
            continue
        if not word.startswith("-"):
            return os.path.normpath(word)
    return "."

covered = False
if TOOL == "dotnet" and args[:1] == ["restore"]:
    covered = target(args[1:], {{"-s", "--source", "-r", "--runtime", "--packages", "--configfile", "-v", "--verbosity"}}) in COVERED
elif TOOL == "npm":
    prefix, words, flag = ".", [], False
    for word in args:
        if flag:
            prefix, flag = os.path.normpath(word), False
        elif word in ("--prefix", "-C"):
            flag = True
        elif word.startswith("--prefix="):
            prefix = os.path.normpath(word.split("=", 1)[1])
        elif not word.startswith("-"):
            words.append(word)
    command = words[:1]
    covered = prefix in COVERED and (
        command in (["ci"], ["clean-install"], ["install-clean"]) or (command in (["install"], ["i"]) and len(words) == 1)
    )
if covered and not os.environ.get("DARKFACTORY_REAL_INSTALL"):
    print("darkfactory: " + TOOL + " " + " ".join(args) + " skipped: the factory already ran this install for the "
          "workspace. Set DARKFACTORY_REAL_INSTALL=1 only after changing that project's manifest.")
    sys.exit(0)
os.execv(REAL, [REAL, *args])
"""


def setup_targets(commands):
    """What each installer of the setup covered: dotnet restore targets and npm prefixes, normalized."""

    covered = {"dotnet": set(), "npm": set()}
    for command in commands:
        for part in command.split("&&"):
            try:
                words = shlex.split(part)
            except ValueError:
                continue
            if words[:2] == ["dotnet", "restore"]:
                rest = [word for word in words[2:] if not word.startswith("-")]
                covered["dotnet"].add(os.path.normpath(rest[0]) if rest else ".")
            elif words[:1] == ["npm"] and any(
                word in ("ci", "install", "i", "clean-install") for word in words
            ):
                prefix = "."
                for index, word in enumerate(words):
                    if word in ("--prefix", "-C") and index + 1 < len(words):
                        prefix = os.path.normpath(words[index + 1])
                    elif word.startswith("--prefix="):
                        prefix = os.path.normpath(word.split("=", 1)[1])
                covered["npm"].add(prefix)
    return covered


MAVEN_SETTINGS = """<settings>
  <proxies>
    <proxy><id>factory-https</id><active>true</active><protocol>https</protocol><host>{host}</host><port>{port}</port><nonProxyHosts>localhost|127.0.0.1</nonProxyHosts></proxy>
    <proxy><id>factory-http</id><active>true</active><protocol>http</protocol><host>{host}</host><port>{port}</port><nonProxyHosts>localhost|127.0.0.1</nonProxyHosts></proxy>
  </proxies>
</settings>
"""


def jvm_proxy(proxy, home):
    """The JVM ignores HTTPS_PROXY: Maven, Gradle and plain java resolved hosts themselves and failed with
    "Unknown host" in the no-network sandbox. System properties cover the JVM, settings.xml covers Maven."""
    endpoint = urlsplit(proxy)
    host, port = endpoint.hostname, endpoint.port
    properties = " ".join(
        f"-D{scheme}.proxyHost={host} -D{scheme}.proxyPort={port}" for scheme in ("http", "https")
    )
    os.environ["JAVA_TOOL_OPTIONS"] = " ".join(
        filter(
            None,
            [os.environ.get("JAVA_TOOL_OPTIONS"), properties, "-Dhttp.nonProxyHosts=localhost|127.0.0.1"],
        )
    )
    settings = Path(home, ".m2", "settings.xml")
    if not settings.exists():
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(MAVEN_SETTINGS.format(host=host, port=port))
        hand_over(settings.parent)


def install_shims(setup, directory="/tmp/df-bin"):
    """Put installer wrappers first on PATH for the installs `setup` ran; DARKFACTORY_REAL_INSTALL=1 runs
    the real installer anyway."""
    made = False
    for name, covered in setup_targets(setup).items():
        real = shutil.which(name)
        if not real or not covered:
            continue
        Path(directory).mkdir(parents=True, exist_ok=True)
        path = Path(directory, name)
        path.write_text(SHIM.format(python=sys.executable, real=real, tool=name, covered=sorted(covered)))
        path.chmod(0o755)
        made = True
    if made:
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")


class Hints:
    """Operator messages the control plane appends for the live run (read-only mount)."""

    def __init__(self, path=None):
        self.path, self.seen, self.checked = path, 0, 0.0

    def take(self, force=False):
        if not self.path or (not force and time.monotonic() - self.checked < 2):
            return []
        self.checked = time.monotonic()
        try:
            lines = Path(self.path).read_text().splitlines()
        except OSError:
            return []
        fresh, self.seen = lines[self.seen :], len(lines)
        texts = []
        for line in fresh:
            try:
                text = str(json.loads(line)["text"])[:4000]
            except (ValueError, KeyError, TypeError):
                continue
            note("hint", text)
            texts.append(text)
        return texts


hints = Hints()


FIX_ATTEMPTS = 2
SELF_REVIEW = (
    "Before you finish, review your own change as a strict reviewer would, in a few minutes: start with "
    "`git status` and `git diff --stat HEAD`, then read the diff of the files that matter. Look for bugs, unhandled cases, callers "
    "and tests your change breaks, security mistakes around sign-in, permissions and data, leftover debug "
    "code and edits outside the task. Every behaviour you fixed or added needs a regression test that "
    "fails without the change; add a missing one. Do not re-run broad builds or test suites: the factory "
    "runs the required checks right after this step. Fix what you find, then end with the short Russian "
    "comment as before."
)


def scoped(commands, scopes=None, base=None, root="/workspace"):
    """Checks that cover what the task changed since `base`; every check when that is unknown."""
    if not (scopes and base):
        return list(commands)
    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only", base], cwd=root, capture_output=True, text=True, check=True
        ).stdout.split("\n")
        changed += subprocess.run(
            ["git", "ls-files", "-o", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\n")
    except (OSError, subprocess.CalledProcessError):
        return list(commands)
    changed = [path for path in changed if path]
    if any("/" not in path for path in changed):
        # Root files (lock files, build props, CI and tool configs) can affect every part.
        return list(commands)
    kept = [
        command
        for command in commands
        if command not in scopes
        or any(fnmatch.fnmatch(path, pattern) for path in changed for pattern in scopes[command])
    ]
    if skipped := [command for command in commands if command not in kept]:
        note("status", "Не затронуто задачей, пропущено: " + "; ".join(skipped))
    return kept


def run_checks(commands, scopes=None, base=None):
    """Run required checks in order, stopping at the first failure."""
    # The task's base commit, for checks that look only at the change (scripts/leak_check.py).
    if base:
        os.environ["DARKFACTORY_CHECK_BASE"] = base
    results = []
    for command in scoped(commands, scopes, base):
        note("tool", "$ " + command)
        # The run alarm bounds checks; a fixed per-command timeout would ignore deadline extensions.
        code, output = shell(command)
        results.append(f"$ {command}\nexit={code}\n" + output[-18000:])
        note("output", f"exit={code}\n" + output[-4000:])
        if code:
            return False, "\n\n".join(results)
    return True, "\n\n".join(results)


def routine_workspace(root="/workspace"):
    """The writer's uncommitted change is small and away from risky areas (see logs.routine_diff)."""
    try:
        diff = subprocess.run(
            ["git", "diff", "HEAD"], cwd=root, capture_output=True, text=True, check=True, timeout=60
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "-o", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return False
    if len(untracked) > 5 or any(logs.RISKY_PATH.search(name) for name in untracked):
        return False
    return logs.routine_diff(diff) or (not diff.strip() and bool(untracked))


class Followups:
    """What the factory asks a writer after its turn, in the same session: its own review, then the
    required checks until they pass, as an engineer runs CI before handing work over."""

    def __init__(self, job):
        self.review = bool(job.get("self_review"))
        # Install steps already ran as this session's setup (or are unchanged since); re-running them as
        # checks would only reinstall dependencies.
        installed = set(job.get("setup") or [])
        self.checks = [command for command in job.get("inner_checks") or [] if command not in installed]
        self.scopes, self.base = job.get("check_scopes"), job.get("check_base")
        self.fixes, self.result = 0, None
        # A reviewer that skipped changed code files is asked once more, in the same session, for them.
        self.review_files, self.coverage_asked = job.get("review_files") or [], False

    def coverage(self, output):
        if not self.review_files or self.coverage_asked or not output:
            return None
        missing = uncovered(output, self.review_files)
        if not missing:
            return None
        self.coverage_asked = True
        note("message", "[darkfactory] Ревью без проверки файлов кода: " + ", ".join(missing[:20]))
        return (
            "Your verdict gives no evidence for these changed code files: "
            + ", ".join(missing)
            + ". Read the diff of each (`git diff <target> -- <path>`) and the code that calls it, then return "
            "the complete verdict again: every changed code file in `files` with what you checked (functions, "
            "lines, callers) and why it holds or what is wrong. A regression in touched code is a blocker."
        )

    def next(self, output=None):
        if (ask := self.coverage(output)) is not None:
            return ask
        evidence = os.environ.get("DARKFACTORY_EVIDENCE_DIR")
        if evidence and (Path(evidence) / "owner-request.json").exists():
            # The writer stopped to ask the owner or to split the work; nothing is ready to check.
            return None
        if self.review:
            self.review = False
            if routine_workspace():
                # A small change away from risky areas gets the independent review anyway; a second
                # full-context turn over the same diff costs more than it finds.
                note("status", "Небольшое изменение — самопроверка пропущена")
            else:
                note("message", "[darkfactory] Самопроверка изменений")
                return SELF_REVIEW
        if not self.checks:
            return None
        note("message", "[darkfactory] Обязательные проверки")
        passed, text = checks_after_setup(self.checks, self.scopes, self.base)
        self.result = {"passed": passed, "text": text}
        if passed or self.fixes >= FIX_ATTEMPTS:
            return None
        self.fixes += 1
        return (
            "The factory ran the required checks on your change and one failed:\n"
            + logs.checks_digest(text, 12000)
            + "\nFix the cause in the code (never weaken, skip or delete a test or check), run that "
            "check yourself, then end with the short Russian comment as before."
        )

    def attach(self, result):
        if self.result is not None:
            result["inner_checks"] = self.result
        return result


def hint_prompt(texts):
    return "Operator note sent while you are working; follow it:\n\n" + "\n\n".join(texts)


def note_codex(message):
    method, item = message.get("method"), (message.get("params") or {}).get("item") or {}
    kind = item.get("type")
    if method == "mcpServer/startupStatus/updated":
        # A server that never became ready leaves the agent without its tools and no other trace.
        params = message.get("params") or {}
        if params.get("status") != "starting":
            note(
                "status",
                f"MCP {params.get('name')}: {params.get('status')} {params.get('error') or ''}".strip(),
            )
        return
    if method == "item/started" and kind == "commandExecution":
        note("tool", "$ " + str(item.get("command", "")))
    elif method == "item/started" and kind == "mcpToolCall":
        note("tool", f"MCP {item.get('server')}.{item.get('tool')}")
    elif method != "item/completed":
        return
    elif kind == "agentMessage":
        note("message", item.get("text"))
    elif kind == "reasoning":
        note("reasoning", "\n".join(map(str, item.get("summary") or [])))
    elif kind == "commandExecution":
        note("output", f"exit={item.get('exitCode')}\n" + str(item.get("aggregatedOutput") or "")[-4000:])
    elif kind == "fileChange":
        note("files", "\n".join(str(c.get("path")) for c in item.get("changes") or []))


def data_url(image):
    return f"data:{image['media_type']};base64,{image['data']}"


CODEX_USAGE_KEYS = ("inputTokens", "cachedInputTokens", "outputTokens")
# Thread totals: `seen` is the latest, `base` what a resumed thread had used before this run.
codex_totals = {"seen": {}, "base": {}}


def codex_usage(message):
    """Codex reports cumulative thread usage; record this run's share of it. A resumed thread starts from
    the earlier runs' total, which would otherwise be counted again in every later run."""
    if message.get("method") != "thread/tokenUsage/updated":
        return
    total = ((message.get("params") or {}).get("tokenUsage") or {}).get("total") or {}
    last = ((message.get("params") or {}).get("tokenUsage") or {}).get("last") or {}
    if last.get("inputTokens"):
        # The latest request's input is the thread's current context size.
        codex_totals["context"] = int(last["inputTokens"])
    if total:
        codex_totals["seen"] = {key: int(total.get(key) or 0) for key in CODEX_USAGE_KEYS}
        run = {
            key: max(0, codex_totals["seen"][key] - codex_totals["base"].get(key, 0))
            for key in CODEX_USAGE_KEYS
        }
        note.usage(
            input_tokens=run["inputTokens"],
            cached_input_tokens=run["cachedInputTokens"],
            output_tokens=run["outputTokens"],
        )


def claude_usage(result):
    usage = result.get("usage") or {}
    note.usage(
        input_tokens=sum(
            int(usage.get(key) or 0)
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        ),
        cached_input_tokens=usage.get("cache_read_input_tokens"),
        output_tokens=usage.get("output_tokens"),
        # Subscription runs report the API-equivalent price, not an actual charge.
        cost_usd=result.get("total_cost_usd"),
    )


EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}


def note_claude(message):
    for block in (message.get("message") or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if message.get("type") == "assistant" and block.get("type") == "text":
            note("message", block.get("text"))
        elif message.get("type") == "assistant" and block.get("type") == "thinking":
            note("reasoning", block.get("thinking"))
        elif message.get("type") == "assistant" and block.get("type") == "tool_use":
            note("tool", f"{block.get('name')} {json.dumps(block.get('input'), ensure_ascii=False)}")
            path = (block.get("input") or {}).get("file_path") or (block.get("input") or {}).get(
                "notebook_path"
            )
            if block.get("name") in EDIT_TOOLS and path:
                note("files", str(path))
        elif block.get("type") == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            note("output", str(content or "")[-4000:])


RESUME_COMPACT_TOKENS = int(os.getenv("DARKFACTORY_RESUME_COMPACT_TOKENS", "60000"))


def compacted(incoming):
    """Wait until the App Server reports the thread compacted (or the compaction turn ended)."""
    for line in incoming():
        message = json.loads(line)
        note.pulse()
        codex_usage(message)
        method, item = message.get("method"), (message.get("params") or {}).get("item") or {}
        if method in {"thread/compacted", "turn/completed"} or (
            method == "item/completed" and item.get("type") == "contextCompaction"
        ):
            return True
    return False


READ_ONLY_ROLES = {"review", "plan", "locate"}
OPENAI_API = "https://api.openai.com"
ANTHROPIC_API = "https://api.anthropic.com"


CHATGPT_ORIGIN = "https://chatgpt.com"


def void_signature(token):
    """The token's claims (plan, account, expiry) with a signature no server accepts: enough for the CLI
    to log in, useless to anyone who reads it from the agent's memory."""
    parts = token.split(".")
    return ".".join(parts[:2] + ["ZGFya2ZhY3RvcnktcmVsYXk"]) if len(parts) == 3 else RELAY_PLACEHOLDER


def workspace_changed(root="/workspace"):
    """The writer has changed or added a file since the task's last commit."""
    try:
        return bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        return True


def codex_sandbox(role):
    """Readers run every command in Codex's read-only sandbox: whatever the repository talks them into,
    they cannot write a file. The writer's sandbox is the task container itself."""
    return "read-only" if role in READ_ONLY_ROLES else "danger-full-access"


def codex(job, gateway_url=None):
    credentials = job["credentials"]
    personal = credentials.get("auth_method") == "codex_local"
    if personal and not job.get("personal_mode"):
        return {"status": "auth", "text": "Local Codex authentication requires personal deployment mode"}
    env = {**os.environ, "CODEX_HOME": "/tmp/codex"}
    # Codex requires an existing explicit CODEX_HOME before loading configuration.
    Path(env["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
    hand_over(env["CODEX_HOME"])
    keep_sessions(job, "codex", Path(env["CODEX_HOME"]) / "sessions")
    for key in ("ACCESS_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(key, None)
    args = ["codex", "app-server", "--listen", "stdio://"]
    relay = ExitStack()
    if not personal:
        # The CLI gets a placeholder; the runner's relay adds the real token, so the agent never holds it.
        env["ACCESS_TOKEN"] = RELAY_PLACEHOLDER
        base = relay.enter_context(
            auth_relay(OPENAI_API, {"Authorization": "Bearer " + credentials["accessToken"]})
        )
        for config in (
            'model_provider="openai_chatgpt_plan"',
            'model_providers.openai_chatgpt_plan.name="ChatGPT plan"',
            "model_providers.openai_chatgpt_plan.base_url=" + json.dumps(base + "/v1"),
            'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
            'model_providers.openai_chatgpt_plan.wire_api="responses"',
            "model_providers.openai_chatgpt_plan.requires_openai_auth=false",
            "model_providers.openai_chatgpt_plan.supports_websockets=false",
        ):
            args.extend(["-c", config])
    else:
        # Personal login: the CLI logs in with a token whose signature is void, and both the ChatGPT
        # backend and the gateway are reached through relays that add the real one.
        personal_headers = {
            "Authorization": "Bearer " + credentials["accessToken"],
            "ChatGPT-Account-Id": credentials["chatgptAccountId"],
        }
        # Codex reaches the ChatGPT backend only over HTTPS: the relay serves a certificate from a one-off
        # CA that only this Codex trusts. The CA's key is dropped here; the server key never touches disk.
        ca, cert, key = loopback_certificate()
        ca_path = Path(env["CODEX_HOME"]) / "relay-ca.pem"
        ca_path.write_bytes(ca)
        ca_path.chmod(0o644)
        env["CODEX_CA_CERTIFICATE"] = str(ca_path)
        backend = relay.enter_context(auth_relay(CHATGPT_ORIGIN, personal_headers, tls=(cert, key)))
        args.extend(["-c", "chatgpt_base_url=" + json.dumps(backend + "/backend-api/")])
    if personal and gateway_url:
        split = urlsplit(gateway_url)
        origin = f"{split.scheme}://{split.netloc}"
        gateway = relay.enter_context(auth_relay(origin, personal_headers)) + gateway_url[len(origin) :]
        for config in (
            'model_provider="personal_gateway"',
            'model_providers.personal_gateway.name="Personal Codex gateway"',
            "model_providers.personal_gateway.base_url=" + json.dumps(gateway),
            'model_providers.personal_gateway.wire_api="responses"',
            "model_providers.personal_gateway.requires_openai_auth=true",
            "model_providers.personal_gateway.supports_websockets=false",
        ):
            args.extend(["-c", config])
    elif personal:
        # Native login uses the built-in provider; Codex forbids overriding its ID. With a ChatGPT login it
        # sends model requests straight to chatgpt.com, not to chatgpt_base_url, so they too go to the relay.
        args.extend(["-c", 'model_provider="openai"'])
        args.extend(["-c", "openai_base_url=" + json.dumps(backend + "/backend-api/codex")])
    # Every turn re-sends the whole thread: cap tool output and compact before the context window fills.
    args.extend(
        ["-c", "tool_output_token_limit=" + str(int(os.getenv("DARKFACTORY_TOOL_OUTPUT_TOKENS", "1500")))]
    )
    # Plugin and ChatGPT app sync reaches github.com on every start (refused by egress) and adds tools nobody uses.
    args.extend(["-c", "features.plugins=false", "-c", "features.apps=false"])
    if codex_sandbox(job["role"]) == "read-only":
        # bwrap needs user namespaces, which the task container forbids; Landlock works inside it.
        args.extend(["-c", "features.use_legacy_landlock=true"])
    if compact := int(os.getenv("DARKFACTORY_CODEX_COMPACT_TOKENS", "60000")):
        args.extend(["-c", f"model_auto_compact_token_limit={compact}"])
    args.extend(codex_mcp_config((job.get("agent_profile") or {}).get("mcp_definitions", [])))
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        **as_user(),
    )
    counter = 0
    messages = {}
    quota = {}
    thread = None
    # A dedicated reader keeps App Server output drained, so a slow write from this side can never
    # leave both processes blocked on full pipes while the model keeps working unseen.
    inbox = queue.Queue()

    def pump():
        for line in proc.stdout:
            inbox.put(line)
        inbox.put(None)

    threading.Thread(target=pump, daemon=True).start()

    def incoming():
        while (line := inbox.get()) is not None:
            yield line

    def capture(message):
        nonlocal quota
        note.pulse()
        note_codex(message)
        codex_usage(message)
        if message.get("method") == "account/rateLimits/updated":
            quota = codex_quota(message.get("params", {}).get("rateLimits"))
        if message.get("method") == "item/completed":
            item = message.get("params", {}).get("item", {})
            if item.get("type") == "agentMessage":
                phase = item.get("phase") or messages.get(item["id"], ("", None))[1]
                messages[item["id"]] = (item.get("text", ""), phase)

    def send(method, params=None, notification=False):
        nonlocal counter
        counter += 1
        msg = {"method": method, "params": params or {}}
        if not notification:
            msg["id"] = counter
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        return counter

    def receive(request_id):
        for line in incoming():
            message = json.loads(line)
            capture(message)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"]))
                return message.get("result", {})
            if "id" in message and "method" in message:
                # Container is the sandbox; interactive approvals must never deadlock a run.
                proc.stdin.write(
                    json.dumps(
                        {
                            "id": message["id"],
                            "error": {"code": -32601, "message": "Interactive request unsupported"},
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
        raise RuntimeError("Codex App Server closed before replying")

    try:
        receive(
            send(
                "initialize",
                {
                    "clientInfo": {"name": "darkfactory", "title": "Darkfactory", "version": "0.1.0"},
                    **({"capabilities": {"experimentalApi": True}} if personal else {}),
                },
            )
        )
        send("initialized", notification=True)
        if personal:
            receive(
                send(
                    "account/login/start",
                    {
                        "type": "chatgptAuthTokens",
                        "accessToken": void_signature(credentials["accessToken"]),
                        "chatgptAccountId": credentials["chatgptAccountId"],
                    },
                )
            )
            if "quota_policy" in job:
                try:
                    snapshot = receive(send("account/rateLimits/read"))
                    quota = codex_quota(snapshot.get("rateLimits"))
                    if quota:
                        quota["source"] = "codex.account/rateLimits/read"
                except RuntimeError:
                    quota = {}
                policy = job["quota_policy"]
                low = quota and quota["remaining_percent"] < policy.get("min_remaining_percent", 10)
                if low or (not quota and policy.get("require_known_quota", False)):
                    return {
                        "status": "quota",
                        "quota": quota,
                        "text": "Subscription quota preflight: safety reserve or unknown available quota",
                    }
        params = {"cwd": "/workspace", "approvalPolicy": "never", "sandbox": codex_sandbox(job["role"])}
        if job.get("model"):
            params["model"] = job["model"]
        followups = Followups(job)
        prompt, images, thread = job["prompt"], job.get("images", []), None
        if saved := session_state(job, "codex"):
            # The writer continues its own thread: it already knows the code and what it changed.
            try:
                thread = receive(send("thread/resume", {**params, "threadId": saved}))["thread"]["id"]
                prompt, images = job["resume_prompt"], []
                codex_totals["base"] = session_usage(job, "codex")
                if codex_totals["base"].get("context", 0) > RESUME_COMPACT_TOKENS:
                    # Every turn of the correction would re-send the whole earlier run; one summary call
                    # costs less than carrying it.
                    note("status", "Сжимаю контекст прежней сессии")
                    receive(send("thread/compact/start", {"threadId": thread}))
                    compacted(incoming)
                note("message", "[darkfactory] Продолжаю прежнюю сессию агента")
            except (RuntimeError, KeyError, TypeError):
                thread = None
        if not thread:
            thread = receive(send("thread/start", {**params, "ephemeral": not job.get("session_dir")}))[
                "thread"
            ]["id"]
        save_session(job, "codex", thread)
        turn_params = {
            "threadId": thread,
            "input": [{"type": "text", "text": prompt, "text_elements": []}]
            + [{"type": "image", "url": data_url(image)} for image in images],
        }
        if job.get("effort"):
            turn_params["effort"] = job["effort"]
        if job.get("output_schema") or job["role"] in {"review", "plan"}:
            turn_params["outputSchema"] = job.get("output_schema") or (
                REVIEW_SCHEMA if job["role"] == "review" else PLAN_SCHEMA
            )
        turn_id = receive(send("turn/start", turn_params)).get("turn", {}).get("id")
        queued, steering = [], {}
        # Past the step cap the writer hands over: checks and review judge what it has, and a correction
        # resumes the compacted thread instead of re-sending an ever longer context on every step.
        steps, capped = 0, False
        cap = (
            int(job.get("max_steps") or os.getenv("DARKFACTORY_CODEX_MAX_STEPS", "45"))
            if job["role"] == "implement"
            else 0
        )
        for line in incoming():
            message = json.loads(line)
            capture(message)
            item = (message.get("params") or {}).get("item") or {}
            if message.get("method") == "item/started" and item.get("type") in {
                "commandExecution",
                "fileChange",
                "mcpToolCall",
            }:
                steps += 1
                # No edits yet means nothing to review: an empty commit only wastes a review cycle. The
                # cap then waits for the first change, up to twice its size.
                if (
                    cap
                    and steps >= cap
                    and not capped
                    and turn_id
                    and (steps >= 2 * cap or workspace_changed())
                ):
                    capped = True
                    note("status", f"Достигнут предел шагов ({cap}); работа передаётся на проверку")
                    send("turn/interrupt", {"threadId": thread, "turnId": turn_id})
            for text in hints.take():
                if turn_id:
                    steer = {
                        "threadId": thread,
                        "input": [{"type": "text", "text": hint_prompt([text]), "text_elements": []}],
                        "expectedTurnId": turn_id,
                    }
                    steering[send("turn/steer", steer)] = text
                else:
                    queued.append(text)
            if message.get("id") in steering and "method" not in message:
                text = steering.pop(message["id"])
                if "error" in message:
                    # Older App Servers lack mid-turn steering; deliver as a follow-up turn.
                    queued.append(text)
            if "id" in message and "method" in message:
                proc.stdin.write(
                    json.dumps(
                        {
                            "id": message["id"],
                            "error": {"code": -32601, "message": "Interactive request unsupported"},
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
            if message.get("method") == "turn/completed":
                turn = message["params"]["turn"]
                if capped and turn["status"] != "failed":
                    return {
                        "status": "ok",
                        "text": "[darkfactory] Достигнут предел шагов агента; работа передана на проверку.",
                        "session": thread,
                        **({"quota": quota} if quota else {}),
                    }
                if turn["status"] != "completed":
                    error = json.dumps(turn.get("error") or turn)
                    return {
                        "status": classify(error),
                        "text": error,
                        "session": thread,
                        **({"quota": quota} if quota else {}),
                    }
                for item in turn.get("items", []):
                    if item.get("type") == "agentMessage":
                        phase = item.get("phase") or messages.get(item["id"], ("", None))[1]
                        messages[item["id"]] = (item.get("text", ""), phase)
                queued += hints.take(force=True)
                final = [text for text, phase in messages.values() if phase == "final_answer"]
                latest = (final or [text for text, phase in messages.values() if phase is None] or [""])[-1]
                follow = hint_prompt(queued) if queued else followups.next(latest)
                if follow:
                    if codex_totals.get("context", 0) > RESUME_COMPACT_TOKENS:
                        # Self-review and check fixes need the diff and the failure, not every file read
                        # on the way: each follow-up turn would otherwise re-send the whole first pass.
                        note("status", "Сжимаю контекст перед продолжением")
                        receive(send("thread/compact/start", {"threadId": thread}))
                        compacted(incoming)
                        # No reset of the context size here: it is saved with the session, and a zero made the
                        # next correction resume the full thread uncompacted (task c8688e76: 2.42M). The next
                        # token report replaces it with the compacted size.
                    turn_params["input"] = [{"type": "text", "text": follow, "text_elements": []}]
                    queued, steering = [], {}
                    turn_id = receive(send("turn/start", turn_params)).get("turn", {}).get("id")
                    continue
                final = [text for text, phase in messages.values() if phase == "final_answer"]
                output = (final or [text for text, phase in messages.values() if phase is None] or [""])[-1]
                return followups.attach(
                    {
                        "status": "ok",
                        "text": output,
                        "session": thread,
                        **({"quota": quota} if quota else {}),
                    }
                )
        raise RuntimeError("Codex App Server exited without turn/completed")
    except TimeoutError:
        # The run alarm: main() reports it as a deadline, not as an error to classify.
        raise
    except Exception as exc:
        return {
            "status": classify(str(exc)),
            "text": str(exc),
            "session": thread,
            **({"quota": quota} if quota else {}),
        }
    finally:
        relay.close()
        if thread and codex_totals["seen"]:
            # The next run resumes this thread; it subtracts these totals from what the thread reports.
            try:
                save_session(
                    job, "codex", thread, {**codex_totals["seen"], "context": codex_totals.get("context", 0)}
                )
            except OSError:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def claude(job, upstream=None):
    servers = {
        server["name"]: {"type": "http", "url": server["url"]}
        for server in (job.get("agent_profile") or {}).get("mcp_definitions", [])
    }
    args = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--input-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "",
        "--dangerously-skip-permissions",
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps({"mcpServers": servers}),
    ]
    if job.get("model"):
        args += ["--model", job["model"]]
    # Past ~50 steps an agent rarely solves what it has not; the cap turns a runaway into a handoff.
    turns = os.getenv("DARKFACTORY_CLAUDE_MAX_TURNS", "80" if job["role"] in {"implement", "qa"} else "40")
    if int(turns) > 0:
        args += ["--max-turns", turns]
    if job.get("output_schema") or job["role"] in {"review", "plan"}:
        schema = job.get("output_schema") or (REVIEW_SCHEMA if job["role"] == "review" else PLAN_SCHEMA)
        # The stand tester reads its sign-in from the environment with a shell; its workspace is read-only.
        tools = "Read,Glob,Grep,Bash" if job["role"] == "qa" else "Read,Glob,Grep"
        args += ["--json-schema", json.dumps(schema), "--tools", tools]
    Path("/tmp/claude").mkdir(parents=True, exist_ok=True)
    hand_over("/tmp/claude")
    prompt, images = job["prompt"], job.get("images", [])
    if not keep_sessions(job, "claude", Path("/tmp/claude/projects")):
        args.append("--no-session-persistence")
    elif (saved := session_state(job, "claude")) and any(
        Path("/tmp/claude/projects").glob(f"*/{saved}.jsonl")
    ):
        args += ["--resume", saved]
        prompt, images = job["resume_prompt"], []
        note("message", "[darkfactory] Продолжаю прежнюю сессию агента")
    relay = ExitStack()
    # The CLI gets a placeholder; the runner's relay adds the real token, so the agent never holds it.
    base = relay.enter_context(
        auth_relay(
            upstream or ANTHROPIC_API, {"Authorization": "Bearer " + job["credentials"]["oauth_token"]}
        )
    )
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": "/tmp/claude",
        "CLAUDE_CODE_OAUTH_TOKEN": RELAY_PLACEHOLDER,
        "ANTHROPIC_BASE_URL": base,
        "CLAUDE_CODE_PROXY_RESOLVES_HOSTS": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # Project MCP servers start inside a busy task container; the default 30s can cut a Python server off.
        "MCP_TIMEOUT": "60000",
        # Every turn re-sends the whole context: trim tool output and compact before it grows huge.
        "BASH_MAX_OUTPUT_LENGTH": os.getenv("DARKFACTORY_TOOL_OUTPUT_CHARS", "12000"),
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": os.getenv("DARKFACTORY_CLAUDE_COMPACT_PERCENT", "60"),
    }
    # --bare would ignore subscription OAuth credentials.
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        **as_user(),
    )

    def say(text, images=()):
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]}}
            for i in images
        ] + [{"type": "text", "text": text}]
        content = content if images else text
        proc.stdin.write(
            json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False)
            + "\n"
        )
        proc.stdin.flush()

    followups = Followups(job)
    try:
        say(prompt, images)
        result = None
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            note.pulse()
            note_claude(message)
            if (
                message.get("type") == "system"
                and message.get("subtype") == "init"
                and message.get("session_id")
            ):
                # Saved at the start: a run stopped by the deadline or a stall still leaves its session.
                try:
                    save_session(job, "claude", message["session_id"])
                except OSError:
                    pass
            if message.get("type") == "result":
                result = message
                claude_usage(result)
                # One user message yields one result; hints wait for the turn boundary.
                pending = [] if result.get("is_error") else hints.take(force=True)
                follow = (
                    None
                    if result.get("is_error")
                    else hint_prompt(pending)
                    if pending
                    else followups.next(
                        json.dumps(result.get("structured_output"))
                        if result.get("structured_output")
                        else None
                    )
                )
                if follow:
                    say(follow)
                else:
                    proc.stdin.close()
        proc.wait()
        if not result:
            return {"status": "error", "text": f"Claude exited without result (exit {proc.returncode})"}
        text = (
            json.dumps(result["structured_output"])
            if result.get("structured_output")
            else str(result.get("result", result.get("errors", "")))
        )
        save_session(job, "claude", result.get("session_id"))
        if result.get("subtype") == "error_max_turns":
            # The step cap is a handoff, not a failure: checks and review judge what the run produced.
            return followups.attach(
                {
                    "status": "ok",
                    "text": "[darkfactory] Достигнут предел шагов агента; работа передана на проверку.",
                    "session": result.get("session_id"),
                }
            )
        return followups.attach(
            {
                "status": classify(text) if result.get("is_error") or proc.returncode else "ok",
                "text": text,
                "session": result.get("session_id"),
            }
        )
    finally:
        relay.close()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Run a shell command in the task workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]


def api_agent(job):
    credentials = job["credentials"]
    rates = [float(credentials.get(key, -1)) for key in ("input_usd_per_million", "output_usd_per_million")]
    if not all(math.isfinite(rate) and rate >= 0 for rate in rates) or not job.get("model"):
        return {
            "status": "error",
            "text": "API model and explicit token prices required for budget enforcement",
        }
    messages = [
        {
            "role": "system",
            "content": "You are a coding agent in an isolated workspace. "
            "Use shell tools to inspect and edit files. Never commit or push. "
            "For review, do not modify files and return JSON with approved:boolean, findings:string[].",
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": job["prompt"]}]
            + [
                {"type": "image_url", "image_url": {"url": data_url(image)}}
                for image in job.get("images", [])
            ]
            if job.get("images")
            else job["prompt"],
        },
    ]
    spent, tokens = 0.0, [0, 0]
    verify = ssl.create_default_context(cadata=credentials["ca_cert"]) if credentials.get("ca_cert") else True
    with httpx.Client(timeout=120, verify=verify) as client:
        for _ in range(20):
            # Reserve a conservative input bound and full possible output before each call.
            input_bound = len(json.dumps(messages).encode()) + len(json.dumps(TOOLS).encode()) + 4096
            ceiling = input_bound * rates[0] / 1e6 + 4096 * rates[1] / 1e6
            if spent + ceiling > job.get("budget_usd", 0):
                return {"status": "budget", "text": "API cost ceiling reached", "cost_usd": spent}
            response = client.post(
                credentials["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + credentials["api_key"]},
                json={"model": job["model"], "messages": messages, "tools": TOOLS, "max_tokens": 4096},
            )
            if response.status_code >= 400:
                return {
                    "status": classify(response.text),
                    "text": f"API HTTP {response.status_code}",
                    "cost_usd": spent,
                }
            data = response.json()
            usage = data.get("usage")
            if not usage or "prompt_tokens" not in usage or "completion_tokens" not in usage:
                return {
                    "status": "budget",
                    "text": "Provider omitted usage; stopping with reserved cost",
                    "cost_usd": spent + ceiling,
                }
            spent += (usage["prompt_tokens"] * rates[0] + usage["completion_tokens"] * rates[1]) / 1e6
            tokens[0] += usage["prompt_tokens"]
            tokens[1] += usage["completion_tokens"]
            note.usage(input_tokens=tokens[0], output_tokens=tokens[1], cost_usd=round(spent, 6))
            message = data["choices"][0]["message"]
            messages.append(message)
            note("message", message.get("content"))
            pending = hints.take(force=True)
            if not message.get("tool_calls") and not pending:
                return {"status": "ok", "text": message.get("content", ""), "cost_usd": spent}
            for call in message.get("tool_calls") or []:
                try:
                    command = json.loads(call["function"]["arguments"])["command"]
                    note("tool", "$ " + command)
                    proc = subprocess.run(
                        ["sh", "-lc", command], capture_output=True, text=True, timeout=60, **as_user()
                    )
                    output = f"exit={proc.returncode}\n" + (proc.stdout + proc.stderr)[-18000:]
                except Exception as exc:
                    output = str(exc)
                note("output", output[-4000:])
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
            if pending:
                messages.append({"role": "user", "content": hint_prompt(pending)})
    return {"status": "error", "text": "API tool-call limit reached", "cost_usd": spent}


def main():
    # The worker sends SIGUSR1 when a run goes silent; the stacks land in the container log.
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    except (AttributeError, ValueError, OSError):
        pass
    # The agent shares this uid: `kill -INT 1` after printing a forged result line would leave that line as
    # the last one. The runner ignores such signals and always prints its own result last; the worker stops
    # containers with SIGKILL. A Python handler, unlike SIG_IGN, is not inherited by the agent's processes.
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(number, lambda *_: None)
    job = json.loads(Path("/run/job/input.json").read_text())
    secrets = [*job.get("credentials", {}).values(), *job.get("test_env", {}).values()]
    for server in (job.get("agent_profile") or {}).get("mcp_definitions", []):
        secrets.extend((server.get("headers") or {}).values())
        secrets.extend((server.get("env") or {}).values())
    secrets = sorted(
        {value for value in secrets if isinstance(value, str) and len(value) >= 8}, key=len, reverse=True
    )
    global note, hints
    evidence = os.environ.get("DARKFACTORY_EVIDENCE_DIR")
    note = Transcript(evidence and os.path.join(evidence, "transcript.jsonl"), secrets)
    if job["role"] != "checks":
        hints = Hints(f"/run/task-lock/hints-{job['generation']}.jsonl")
    note("status", f"Запуск: {job['role']} · {job.get('provider', 'checks')}")

    def deadline(signum, frame):
        # The control plane extends a run that is making progress; honour that before stopping.
        if (later := extension(f"/run/task-lock/deadline-{job['generation']}")) > 1:
            signal.setitimer(signal.ITIMER_REAL, later)
            return
        raise TimeoutError("Active-time limit reached inside executor")

    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, job["timeout"])
    stack = ExitStack()
    try:
        # Inside the try: a held lock or a stale generation still reports a result, not a bare exit.
        lock = stack.enter_context(open("/run/task-lock/lock", "rb"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if Path("/run/task-lock/generation").read_text() != job["generation"]:
            raise RuntimeError("Stale execution generation")
        Path(os.environ.get("HOME", "/tmp/home")).mkdir(parents=True, exist_ok=True)
        materialize_skills(job.get("agent_profile"), os.environ.get("HOME", "/tmp/home"))
        hand_over(os.environ.get("HOME", "/tmp/home"))
        os.environ.update(job.get("test_env", {}))
        if Path("/run/job/egress.sock").exists():
            proxy = stack.enter_context(local_proxy("/run/job/egress.sock"))
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                os.environ[key] = proxy
            os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
            jvm_proxy(proxy, os.environ.get("HOME", "/tmp/home"))
        failed = setup(job.get("setup", []), job.get("setup_marker"))
        if job["role"] == "implement" and job.get("setup"):
            if failed:
                job["prompt"] = (
                    "The factory's dependency setup failed: "
                    + "; ".join(failed)
                    + ". Fix or re-run what you need before building.\n\n"
                    + job["prompt"]
                )
            else:
                install_shims(job["setup"])
        if job["role"] == "checks":
            succeeded, text = checks_after_setup(
                job["checks"], job.get("check_scopes"), job.get("check_base")
            )
            result = {"status": "ok" if succeeded else "checks_failed", "text": text}
        else:
            profile = job.get("agent_profile")
            if profile and profile.get("mcp_definitions"):
                if job["provider"] == "api":
                    raise ValueError("Project MCP requires a native Codex or Claude executor")
                job["agent_profile"] = {
                    **profile,
                    "mcp_definitions": stack.enter_context(project_mcp(profile["mcp_definitions"])),
                }
            if (
                job["provider"] == "claude"
                and job.get("personal_mode")
                and job["credentials"].get("base_url")
            ):
                endpoint = urlsplit(job["credentials"]["base_url"])
                gateway = stack.enter_context(
                    local_tunnel("/run/job/egress.sock", endpoint.hostname, endpoint.port or 80)
                )
                result = claude(job, gateway + endpoint.path.rstrip("/"))
            elif (
                job["provider"] == "codex"
                and job.get("personal_mode")
                and job["credentials"].get("auth_method") == "codex_local"
                and job["credentials"].get("base_url")
            ):
                endpoint = urlsplit(job["credentials"]["base_url"])
                gateway = stack.enter_context(
                    local_tunnel("/run/job/egress.sock", endpoint.hostname, endpoint.port or 80)
                )
                result = codex(job, gateway + endpoint.path.rstrip("/"))
            else:
                result = {"codex": codex, "claude": claude, "api": api_agent}[job["provider"]](job)
            if result["status"] == "ok" and job["role"] == "review":
                result["review"] = json_object(result["text"])
                raw = json.loads(result["text"])
                if "checklist" in raw:
                    # The owner's view of the review: every check, finding and per-file verdict as given.
                    result["review"]["verdict"] = raw
                if missing := uncovered(result["text"], job.get("review_files")):
                    result["review"]["uncovered"] = missing
                result["text"] = json.dumps(
                    {key: result["review"][key] for key in ("approved", "findings")}, ensure_ascii=False
                )
            if result["status"] == "ok" and job["role"] == "plan":
                result["plan"] = plan_object(result["text"], allow_subtasks=job.get("allow_subtasks", True))
    except Exception as exc:
        result = {
            "status": "deadline" if isinstance(exc, TimeoutError) else classify(str(exc)),
            "text": str(exc),
        }
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            stack.close()
        except Exception:
            # A finished implement or review stays valid when an MCP bridge or proxy fails to close.
            note("status", "Executor cleanup failed")

    note("status", f"Завершено: {result['status']}")
    # Never emit credentials or raw protocol traces into control-plane logs.
    print("DARKFACTORY_RESULT:" + json.dumps(redact(result, secrets), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
