"""Shrink build and test logs before they reach a prompt: agents need the failures, not restore chatter."""

import os
import re

NOISE = re.compile(
    r"^\s*(Restored |Determining projects to restore|All projects are up-to-date|Collecting |Downloading |"
    r"Using cached |Requirement already satisfied|Installing collected packages|Obtaining |"
    r"Preparing metadata|Building wheel|Created wheel|Stored in directory|npm (WARN|notice|http fetch|warn deprecated)|"
    r"added \d+ packages|\d+ packages are looking for funding|run `npm fund`|Progress|\[\s*\d+%\]|"
    r"\s*\d+(\.\d+)?\s*[kMG]?B\s*/|Unpacking |Setting up |Selecting previously|Preparing to unpack|"
    r"Get:\d+ |Hit:\d+ |Fetched \d+)"
)
SIGNAL = re.compile(
    r"error|fail|exception|traceback|assert|panic|fatal|warning [A-Z]+\d+|✗|✘|×|\bE\s{2,}|exit=[1-9]",
    re.IGNORECASE,
)


def condense(text, limit=24000):
    """Drop known noise and repeats; past the limit keep the head, every signal line with context, and the tail."""
    lines, dropped, previous = [], 0, None
    for line in (text or "").splitlines():
        if NOISE.match(line) or line == previous:
            dropped += 1
            continue
        lines.append(line)
        previous = line
    if dropped:
        lines.append(f"[darkfactory: {dropped} restore/progress/duplicate lines omitted]")
    result = "\n".join(lines)
    if len(result) <= limit:
        return result
    keep = set(range(min(20, len(lines))))
    for index, line in enumerate(lines):
        if SIGNAL.search(line):
            keep.update(range(max(0, index - 2), min(len(lines), index + 4)))
    tail, size = [], 0
    for index in range(len(lines) - 1, -1, -1):
        size += len(lines[index]) + 1
        if size > limit // 3:
            break
        tail.append(index)
    keep.update(tail)
    out, budget, last = [], limit - limit // 3, -1
    for index in sorted(keep):
        if index not in tail and budget <= 0:
            continue
        if index != last + 1:
            out.append(f"[… {index - last - 1} lines …]")
        out.append(lines[index])
        if index not in tail:
            budget -= len(lines[index]) + 1
        last = index
    return "\n".join(out)[-limit:]


def checks_digest(text, limit=24000):
    """Passing checks need no output for a reviewer; failing ones keep their condensed log."""
    blocks = re.split(r"\n(?=\$ )", text or "")
    out = []
    for block in blocks:
        head, _, body = block.partition("\n")
        status, _, rest = body.partition("\n")
        if status == "exit=0":
            # The last line usually carries the test count, which shows the check really ran.
            last = next((line.strip() for line in reversed(rest.splitlines()) if line.strip()), "")
            out.append(f"{head}\n{status}" + (f"\n{last[:300]}" if last and not NOISE.match(last) else ""))
        else:
            out.append(condense(block, limit))
    return condense("\n".join(out), limit)


LOCKFILES = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|Cargo\.lock|go\.sum|"
    r"packages\.lock\.json|composer\.lock|Gemfile\.lock)$|\.(min\.js|map|snap|svg)$|"
    # Generated .NET code (designers, EF model snapshots, source generators) and build output.
    r"\.Designer\.cs$|ModelSnapshot\.cs$|\.g\.cs$|(^|/)(dist|__snapshots__)/"
)


# How a reviewer should spend a limited diff: the code that runs first, its tests next, and the writer's own
# documents, reports and scripts last. Alphabetical order put docs/ first and pushed production code out.
DOCUMENT = re.compile(r"(?:^|/)(?:docs?|reviews?|reports?)/|\.(?:md|mdx|rst|txt|adoc)$", re.IGNORECASE)
SCRIPT = re.compile(r"(?:^|/)(?:scripts?|tools?|e2e|acceptance)/", re.IGNORECASE)
TEST = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?)/|[._-](?:test|tests|spec)\.[a-z]+$|Tests?\.(?:cs|java|kt|go)$|_test\.(?:go|py)$",
    re.IGNORECASE,
)


def file_kind(path):
    """ "code", "test", "script" or "document": what the change of `path` is to a reviewer."""
    if DOCUMENT.search(path):
        return "document"
    if TEST.search(path):
        return "test"
    if SCRIPT.search(path):
        return "script"
    return "code"


def diff_files(diff):
    """Changed paths of a unified diff, in diff order."""
    return re.findall(r"(?m)^diff --git a/\S+ b/(\S+)", diff or "")


def diff_digest(diff, limit=64000):
    """The diff for a reviewer within `limit` characters: production code first, then tests, scripts and
    documents. Lockfile and generated hunks keep their file header and size only; files past the limit are
    listed by name."""
    files = [item for item in re.split(r"(?m)^(?=diff --git )", diff or "") if item]
    out = []
    for item in files:
        match = re.match(r"diff --git a/(\S+) b/(\S+)", item)
        if match and LOCKFILES.search(match.group(2)):
            changed = sum(
                1 for line in item.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")
            )
            out.append(
                f"{item.splitlines()[0]}\n[darkfactory: {changed} changed lines of a lock/generated file omitted]\n"
            )
        else:
            out.append(item)
    order = {"code": 0, "test": 1, "script": 2, "document": 3}
    out.sort(key=lambda item: order[file_kind((diff_files(item) or [""])[0])])
    result = "".join(out)
    if len(result) <= limit:
        return result
    kept, rest, size = [], [], 0
    for item in out:
        # A file that does not fit is skipped, not the end: a smaller one after it may still fit.
        if size + len(item) <= limit:
            kept.append(item)
            size += len(item)
        else:
            rest.extend(diff_files(item))
    return (
        "".join(kept)
        + f"\n[darkfactory: {len(rest)} more changed files did not fit; read each with `git diff <target> -- <path>`"
        + " in /workspace: "
        + ", ".join(rest[:80])
        + ("" if len(rest) <= 80 else ", ...")
        + "]\n"
    )


def review_budget(diff, smallest=32000, largest=160000):
    """Diff characters a reviewer gets: the whole change up to `largest`, never less than `smallest`."""
    return max(smallest, min(largest, len(diff or "")))


RISKY_PATH = re.compile(
    r"auth|login|logout|signin|signup|session|oidc|oauth|saml|sso|token|secret|password|credential|crypt|"
    r"permission|role|acl|policy|security|payment|billing|invoice|migration|\.sql$|docker|compose|"
    r"\.github/|ci\.|deploy|infra",
    re.IGNORECASE,
)
ROUTINE_LINES = int(os.getenv("DARKFACTORY_ROUTINE_REVIEW_LINES", "150"))


def routine_diff(diff):
    """A small change away from sign-in, permissions, money, migrations and deployment."""
    if ROUTINE_LINES <= 0:
        return False
    changed, files = 0, []
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            files.append(line.split(" b/", 1)[-1])
        elif line[:1] in "+-" and not line.startswith(("+++", "---")):
            changed += 1
    return bool(files) and changed <= ROUTINE_LINES and not any(RISKY_PATH.search(name) for name in files)
