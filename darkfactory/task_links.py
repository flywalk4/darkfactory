"""Safe human-facing links; Git API URLs and credentials never reach messages."""

import os
import re
from urllib.parse import quote, urlencode, urlsplit, urlunsplit


def _web_base(value):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
            or any(ord(char) <= 32 or char == "\\" for char in value)
        ):
            return None
        return parsed
    except ValueError:
        return None


def task_url(task, *, commit=None):
    configured = os.getenv("DARKFACTORY_PUBLIC_URL", "http://127.0.0.1:8080")
    base = _web_base(configured)
    # A misconfigured display URL must never undo an already completed Git merge.
    root = urlunsplit(base).rstrip("/") if base else "http://127.0.0.1:8080"
    query = {"task": task.id}
    if commit:
        query["commit"] = commit
    return root + "/?" + urlencode(query)


def public_asset(name):
    """A static file's URL for chat avatars, only when the factory has a configured public address."""
    base = _web_base(os.getenv("DARKFACTORY_PUBLIC_URL", ""))
    # A loopback address (the Compose default) is unreachable from chat users' browsers.
    if base and base.hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return None
    return f"{urlunsplit(base).rstrip('/')}/static/{name}" if base else None


def commit_url(project, task):
    sha = task.result_sha
    if not sha or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        return None
    forge, base, repo = project.forge, None, project.forge_repo
    if forge in {"github", "gitlab"}:
        base = _web_base(project.forge_url or "")
        if base:
            if forge == "github" and base.hostname == "api.github.com":
                base = urlsplit("https://github.com")
            else:
                suffix = "/api/v3" if forge == "github" else "/api/v4"
                base = base._replace(path=base.path.removesuffix("/").removesuffix(suffix))
    else:
        remote = project.remote
        # Recognise public hosts only; generic/corporate Git uses the factory's diff view.
        if remote.startswith("git@") and ":" in remote:
            remote = "https://" + remote[4:].replace(":", "/", 1)
        elif remote.startswith("ssh://git@"):
            remote = "https://" + remote[len("ssh://git@") :]
        parsed = _web_base(remote)
        if parsed and parsed.hostname in {"github.com", "gitlab.com"}:
            forge = "github" if parsed.hostname == "github.com" else "gitlab"
            repo = parsed.path.strip("/").removesuffix(".git")
            base = parsed._replace(path="")
    if base and repo:
        parts = repo.split("/")
        if len(parts) >= 2 and all(part and part not in {".", ".."} for part in parts):
            path = "/".join(quote(part, safe="") for part in parts)
            marker = "/commit/" if forge == "github" else "/-/commit/"
            return urlunsplit(base).rstrip("/") + "/" + path + marker + sha
    return task_url(task, commit=sha)
