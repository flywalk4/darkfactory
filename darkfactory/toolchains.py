"""Prepare an executor image for whatever stack a repository uses.

The base executor carries Python, Node, git, browsers and the agent CLIs. During `prepare` the worker reads the
workspace, detects the remaining toolchains and their versions from the project's own files, and builds a cached
image layered on the base. Package feeds the repository declares (nuget.config, .npmrc, pip/uv/poetry indexes,
Maven repositories) are opened in the task egress, so a corporate registry works without manual allowlisting.
"""

import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .db import ROOT

SKIP = {".git", "node_modules", "bin", "obj", "target", "dist", "build", ".venv", "vendor", ".gradle"}
MAX_DEPTH = 5
MAX_FILES = 20000
MAX_READ = 256 * 1024
BUILD_SECONDS = 1800
# The executor image without the factory's own code: toolchains build on it, so a factory deploy only
# rebuilds a thin layer with the runner instead of every project's SDKs.
TOOLS_IMAGE = os.getenv("EXECUTOR_TOOLS_IMAGE", "darkfactory-executor-tools:local")
RUNNER_PATH = "/app/darkfactory"

INSTALL = {
    "dotnet": (
        "curl -fsSL --retry 5 --retry-all-errors https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh"
        " && for channel in {versions}; do bash /tmp/dotnet-install.sh --channel $channel"
        " --install-dir /usr/share/dotnet; done"
        " && ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet && rm /tmp/dotnet-install.sh",
        {"DOTNET_ROOT": "/usr/share/dotnet", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"},
        ["libicu-dev"],
    ),
    "go": (
        "arch=$(dpkg --print-architecture) && curl -fsSL --retry 5 https://go.dev/dl/go{version}.linux-$arch.tar.gz"
        " | tar -xz -C /usr/local",
        {"PATH": "/usr/local/go/bin:$PATH", "GOTOOLCHAIN": "local"},
        [],
    ),
    "java": (
        "arch=$(dpkg --print-architecture | sed 's/amd64/x64/;s/arm64/aarch64/') && mkdir -p /opt/jdk"
        " && curl -fsSL --retry 5 https://api.adoptium.net/v3/binary/latest/{version}/ga/linux/$arch/jdk/hotspot/"
        "normal/eclipse | tar -xz -C /opt/jdk --strip-components=1",
        {"JAVA_HOME": "/opt/jdk", "PATH": "/opt/jdk/bin:$PATH"},
        [],
    ),
    "maven": (
        "mkdir -p /opt/maven && curl -fsSL --retry 5 https://archive.apache.org/dist/maven/maven-3/{version}/"
        "binaries/apache-maven-{version}-bin.tar.gz | tar -xz -C /opt/maven --strip-components=1"
        " && ln -sf /opt/maven/bin/mvn /usr/local/bin/mvn",
        {},
        [],
    ),
    "rust": (
        "curl -fsSL --retry 5 https://sh.rustup.rs | RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo"
        " sh -s -- -y --no-modify-path --profile minimal --component clippy,rustfmt --default-toolchain {version}"
        " && chmod -R a+rX /usr/local/rustup /usr/local/cargo",
        {"RUSTUP_HOME": "/usr/local/rustup", "PATH": "/usr/local/cargo/bin:$PATH"},
        ["build-essential"],
    ),
    "node": (
        "arch=$(dpkg --print-architecture | sed 's/amd64/x64/') && mkdir -p /opt/node"
        " && name=$(curl -fsSL --retry 5 https://nodejs.org/dist/latest-v{version}.x/SHASUMS256.txt"
        " | awk '{{print $2}}' | grep \"linux-$arch.tar.xz$\") && curl -fsSL --retry 5"
        " https://nodejs.org/dist/latest-v{version}.x/$name | tar -xJ -C /opt/node --strip-components=1",
        {"PATH": "/opt/node/bin:$PATH"},
        ["xz-utils"],
    ),
    "ruby": ("true", {}, ["ruby-full", "ruby-bundler", "build-essential"]),
    "php": (
        "curl -fsSL --retry 5 https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin"
        " --filename=composer",
        {},
        ["php-cli", "php-xml", "php-mbstring", "php-curl", "php-zip", "unzip"],
    ),
}
# Per file, only the settings that name a package feed: comments, docs and license links stay closed.
FEED_PATTERNS = {
    "nuget.config": r"<add\b[^>]*\bvalue=\"(https?://[^\"]+)\"",
    ".npmrc": r"^\s*(?:@[\w.-]+:)?registry\s*=\s*(https?://\S+)",
    ".yarnrc.yml": r"npmRegistryServer:\s*[\"']?(https?://[^\s\"']+)",
    "pip.conf": r"(?:extra-)?index-url\s*=\s*(https?://\S+)",
    "requirements.txt": r"--(?:extra-)?index-url[\s=]+(https?://\S+)",
    "pyproject.toml": r"^\s*(?:url|index-url)\s*=\s*\"(https?://[^\"]+)\"",
    "uv.toml": r"^\s*(?:url|index-url)\s*=\s*\"(https?://[^\"]+)\"",
    "pom.xml": r"<(?:repository|pluginRepository|mirror)>.*?<url>\s*(https?://[^<\s]+)",
    "settings.xml": r"<(?:repository|pluginRepository|mirror)>.*?<url>\s*(https?://[^<\s]+)",
    "build.gradle": r"maven\s*(?:\{[^}]*?|\(\s*)(?:url\s*=?\s*(?:uri\()?)?[\"'](https?://[^\"']+)",
}
FEED_PATTERNS["build.gradle.kts"] = FEED_PATTERNS["settings.gradle"] = FEED_PATTERNS[
    "settings.gradle.kts"
] = FEED_PATTERNS["build.gradle"]


def _files(root):
    root = Path(root)
    seen = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        depth = len(Path(current).relative_to(root).parts)
        dirs[:] = (
            [] if depth >= MAX_DEPTH else sorted(d for d in dirs if d not in SKIP and not d.startswith("."))
        )
        for name in sorted(files):
            seen += 1
            if seen > MAX_FILES:
                return
            yield Path(current) / name


def _read(path):
    try:
        if path.is_symlink() or path.stat().st_size > MAX_READ:
            return ""
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _version_key(value):
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


def detect(root):
    """Toolchains the repository needs beyond the base image, as {name: version}."""
    root = Path(root)
    found, dotnet, java = {}, set(), set()
    for path in _files(root):
        name = path.name
        if name == "global.json":
            match = re.search(r'"version"\s*:\s*"(\d+)\.(\d+)', _read(path))
            if match:
                dotnet.add(f"{match[1]}.{match[2]}")
        elif name.endswith((".csproj", ".fsproj", ".vbproj")) or name in {
            "Directory.Build.props",
            "Directory.Packages.props",
        }:
            text = _read(path)
            dotnet.update(f"{major}.{minor}" for major, minor in re.findall(r"\bnet(\d{1,2})\.(\d)\b", text))
            if name.endswith("proj") and not dotnet:
                dotnet.add("LTS")
        elif name.endswith((".sln", ".slnx")):
            dotnet.add("LTS")
        elif name == "go.mod" and "go" not in found:
            text = _read(path)
            match = re.search(r"^toolchain\s+go(\d+\.\d+(?:\.\d+)?)", text, re.M) or re.search(
                r"^go\s+(\d+\.\d+(?:\.\d+)?)", text, re.M
            )
            version = match[1] if match else "1.23.0"
            found["go"] = version if version.count(".") == 2 else version + ".0"
        elif name in {"pom.xml", "build.gradle", "build.gradle.kts"}:
            text = _read(path)
            java.update(
                re.findall(
                    r"<(?:maven\.compiler\.release|maven\.compiler\.source|java\.version|release)>(\d+)<",
                    text,
                )
                + re.findall(r"languageVersion(?:\.set)?\s*[(=]\s*JavaLanguageVersion\.of\((\d+)\)", text)
                + re.findall(
                    r"(?:sourceCompatibility|targetCompatibility)\s*=\s*(?:JavaVersion\.VERSION_)?['\"]?(\d+)",
                    text,
                )
            )
            java.add("")
            if name == "pom.xml" and not (path.parent / "mvnw").exists():
                found["maven"] = "3.9.9"
        elif name == ".java-version":
            java.update(re.findall(r"^(\d+)", _read(path)))
        elif name in {"Cargo.toml", "rust-toolchain", "rust-toolchain.toml"} and found.get("rust") in (
            None,
            "stable",
        ):
            match = (
                re.search(r"""channel\s*=\s*["']([\w.-]+)["']""", _read(path))
                if name != "Cargo.toml"
                else None
            )
            if name == "rust-toolchain":
                match = re.match(r"\s*([\w.-]+)", _read(path))
            found["rust"] = match[1] if match else "stable"
        elif name in {".nvmrc", ".node-version"} and "node" not in found:
            match = re.match(r"\s*v?(\d+)", _read(path))
            if match and match[1] != "22" and int(match[1]) >= 18:
                found["node"] = match[1]
        elif name == "Gemfile":
            found["ruby"] = "system"
        elif name == "composer.json":
            found["php"] = "system"
    if dotnet:
        exact = sorted((v for v in dotnet if v != "LTS"), key=_version_key)
        found["dotnet"] = " ".join(exact) if exact else "LTS"
    if java:
        versions = [int(v) for v in java if v and 8 <= int(v) <= 30]
        found["java"] = str(max(versions) if versions else 21)
    return dict(sorted(found.items()))


def feeds(root):
    """Package feed hosts the repository itself configures, as ["host:port"]."""
    hosts = set()
    for path in _files(root):
        pattern = FEED_PATTERNS.get(path.name.lower() if path.name.lower() == "nuget.config" else path.name)
        if not pattern:
            continue
        for url in re.findall(pattern, _read(path), re.M | re.S | re.I):
            parts = urlsplit(url)
            if parts.hostname and "." in parts.hostname:
                hosts.add(
                    f"{parts.hostname.lower()}:{parts.port or (443 if parts.scheme == 'https' else 80)}"
                )
    return sorted(hosts)[:30]


def check_paths(commands, root):
    """Path scopes for checks that name parts of the repository, as {command: ["top/level/**"]}.

    A check that names `src/frontend` (an npm --prefix, a tsconfig, a project path) only needs to rerun when
    something under that part changed. The scope is the first two path levels, wide enough to cover sibling
    libraries a part imports. Checks that run a repository script, or name no existing path, always run."""
    root, scopes = Path(root), {}
    for command in commands:
        try:
            words = shlex.split(command)
        except ValueError:
            continue
        if not words or words[0] in {"sh", "bash", "make", "just"} or words[0].endswith(".sh"):
            continue
        parts = set()
        for word in words:
            value = word.split("=", 1)[-1].strip("'\"")
            if not value or value.startswith(("-", "/", ".")) or ".." in value.split("/"):
                continue
            path = Path(root, value)
            if "/" in value and path.exists():
                parts.add("/".join(Path(value).parts[:2]))
        if parts:
            scopes[command] = sorted(part + "/**" for part in parts)
    return scopes


DOTNET_CHANNEL = re.compile(r"^\d+\.\d+$")


def dockerfile(base, spec, apt=()):
    packages = sorted({package for name in spec for package in INSTALL[name][2]} | set(apt))
    lines = [f"FROM {base}", "USER root"]
    lines.append(
        "RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates "
        + " ".join(packages)
        + " && rm -rf /var/lib/apt/lists/*"
    )
    env = {}
    for name, version in spec.items():
        command, variables, _ = INSTALL[name]
        channels = version.split()
        if name == "dotnet" and channels and all(DOTNET_CHANNEL.match(item) for item in channels):
            # Content-addressed SDK images: pulled once, cached by the daemon, no install script, and one
            # layer per channel instead of a multi-GB RUN that the snapshotter has to commit.
            lines += [
                f"COPY --from=mcr.microsoft.com/dotnet/sdk:{item} /usr/share/dotnet /usr/share/dotnet"
                for item in channels
            ]
            lines.append("RUN ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet")
        else:
            lines.append("RUN " + command.format(version=version, versions=version))
        for key, value in variables.items():
            env[key] = value.replace("$PATH", env.get(key, "$PATH")) if key == "PATH" else value
    if env:
        lines.append("ENV " + " ".join(f'{key}="{value}"' for key, value in env.items()))
        # Setup and checks run in login shells (`sh -lc`), where /etc/profile resets PATH: without this
        # file every toolchain outside /usr/local/bin (go, java, cargo, node) is "not found" there.
        profile = "\\n".join(f'export {key}="{value}"' for key, value in env.items())
        lines.append(f"RUN printf '{profile}\\n' > /etc/profile.d/darkfactory-toolchain.sh")
    lines += ["USER agent", "WORKDIR /workspace"]
    return "\n".join(lines) + "\n"


def state_path(project_id):
    return ROOT / "system" / "toolchains" / f"{project_id}.json"


def state(project_id):
    if not project_id:
        return {}
    try:
        return json.loads(state_path(project_id).read_text())
    except (OSError, ValueError):
        return {}


def _save(project_id, value):
    path = state_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A per-writer name: the worker and a task's prepare may save at once, and a shared .tmp vanished
    # under the second rename ("No such file or directory: …tmp").
    temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.replace(path)


@contextmanager
def gc_lock(exclusive=False, wait=True):
    """Builds hold it shared; pruning images or build cache holds it exclusively. Yields False when an
    exclusive holder would have to wait and wait=False: the prune then skips this round."""
    path = ROOT / "system" / "toolchains" / "gc.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(stream, flags | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def building():
    """True while any worker builds a toolchain image; pruning the build cache then breaks the build."""
    for path in (ROOT / "system" / "toolchains").glob("*.lock"):
        try:
            with path.open("a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            continue
    return False


def prune(client):
    """Drop toolchain images no project points to; Docker refuses ones a container still uses."""
    keep = set()
    for path in (ROOT / "system" / "toolchains").glob("*.json"):
        try:
            value = json.loads(path.read_text())
            # The SDK layer outlives factory deploys; only the runner layer on top of it is rebuilt.
            keep.update((value.get("image"), value.get("heavy")))
        except (OSError, ValueError):
            continue
    removed = []
    for picture in client.images.list(name="darkfactory-toolchain"):
        # By tag: an image id can carry a kept tag too, and removing the id would take it along.
        for tag in picture.tags:
            if tag in keep:
                continue
            try:
                client.images.remove(tag)
                removed.append(tag)
            except Exception:
                continue
    return removed


def invalidate(project_id):
    """Make the next run rebuild the project's toolchain image (a no-op build when it is intact)."""
    current = state(project_id)
    if current.get("spec") or current.get("apt"):
        _save(
            project_id,
            {
                **current,
                "status": "failed",
                "error": "checks failed on the environment",
                "failed_at": 0,
                "attempts": 0,
            },
        )


def image(project_id, client=None):
    """The project's built toolchain image; None when it is not ready or no longer exists (base image then)."""
    current = state(project_id)
    tag = current.get("image") if current.get("status") == "ready" else None
    if tag and client is not None:
        try:
            client.images.get(tag)
        except Exception:
            return None
    return tag


# Files the toolchain is read from: version pins, project files, feed configs and the declared spec.
SPEC_FILES = re.compile(
    r"(?:^|/)(?:global\.json|[^/]+\.(?:cs|fs|vb)proj|Directory\.(?:Build|Packages)\.props|[^/]+\.slnx?|go\.mod|"
    r"pom\.xml|build\.gradle(?:\.kts)?|settings\.gradle(?:\.kts)?|settings\.xml|\.java-version|Cargo\.toml|"
    r"rust-toolchain(?:\.toml)?|\.nvmrc|\.node-version|Gemfile|composer\.json|[Nn]u[Gg]et\.[Cc]onfig|\.npmrc|"
    r"\.yarnrc\.yml|pip\.conf|requirements\.txt|pyproject\.toml|uv\.toml|\.darkfactory/environment\.json)$"
)


@contextmanager
def snapshot(mirror, ref):
    """The spec files of `ref` in a temporary tree: the toolchain follows preprod, never a task branch
    whose agent may have edited its environment."""
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix="toolchain-") as directory:
        names = subprocess.run(
            ["git", "--git-dir", str(mirror), "ls-tree", "-r", "--name-only", ref],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout.splitlines()
        for name in names:
            if name.count("/") >= MAX_DEPTH or not SPEC_FILES.search(name):
                continue
            blob = subprocess.run(
                ["git", "--git-dir", str(mirror), "show", f"{ref}:{name}"],
                capture_output=True,
                check=False,
                timeout=60,
            )
            if blob.returncode == 0 and len(blob.stdout) <= MAX_READ:
                target = Path(directory, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(blob.stdout)
        yield Path(directory)


@contextmanager
def _tree(root):
    if callable(root):
        with root() as path:
            yield path
    else:
        yield root


def fresh(client, project_id, root, base, heartbeat=lambda: True):
    """The project's toolchain image built on the current base; a rebuilt executor image rebuilds it.
    `root` is a tree to read the spec from, or a context-manager factory yielding one when a build is due."""
    if callable(root):
        # Snapshot preprod only when this call may provision; the common case is an intact image.
        current = state(project_id)
        if client is None:
            return image(project_id, client)
        if current.get("status") == "ready" and not stale(current, client):
            try:
                base_id = client.images.get(base).id
            except Exception:
                return image(project_id, client)
            intact = current.get("base_id") == base_id and image(project_id, client)
            if intact or (current.get("heavy") and _exists(client, current["heavy"])):
                return fresh(client, project_id, None, base, heartbeat)
        elif (current.get("status") != "failed" or backing_off(current)) and not (
            current.get("status") == "ready" and stale(current, client)
        ):
            return image(project_id, client)
        with _tree(root) as path:
            return fresh(client, project_id, path, base, heartbeat)
    current = state(project_id)
    if (
        current.get("status") == "failed"
        and client is not None
        and (current.get("spec") or current.get("apt"))
        and not backing_off(current)
    ):
        # A failed build (often a transient Docker error) must not leave the project on a base image
        # without its tools: every run would fail with "command not found". Retries back off, so a
        # persistent failure costs one attempt per window instead of a build at every container start.
        current = provision(client, project_id, root, base, heartbeat)
    if (
        current.get("status") == "ready"
        and client is not None
        and root is not None
        and stale(current, client)
    ):
        # The factory changed how it installs this toolchain (a missing component, a PATH fix).
        current = provision(client, project_id, root, base, heartbeat)
    if current.get("status") != "ready" or client is None:
        return image(project_id, client)
    try:
        base_id = client.images.get(base).id
    except Exception:
        return image(project_id, client)
    if current.get("base_id") == base_id and image(project_id, client):
        return current["image"]
    # The image carries the runner: a new executor image needs a new runner layer. With the SDK layer
    # still present that is a seconds-long rebuild of the thin layer, not the whole toolchain.
    if current.get("heavy") and _exists(client, current["heavy"]):
        try:
            with gc_lock():
                current.update(image=_runner_layer(client, current["heavy"], base, base_id, heartbeat))
            current["base_id"] = base_id
            _save(project_id, current)
            return current["image"]
        except Exception:  # noqa: BLE001 - fall back to a full provision
            pass
    return provision(client, project_id, root, base, heartbeat).get("image")


def recipe(spec, apt=()):
    """Identity of how the factory installs this spec: a changed install recipe makes old images stale."""
    return hashlib.sha256(dockerfile("base", spec or {}, apt or ()).encode()).hexdigest()[:16]


def stale(current, client=None):
    """The image no longer matches how the factory builds it: a changed recipe, or a rebuilt tools image
    (a browser or CLI the SDK layer was built on top of)."""
    if not (current.get("spec") or current.get("apt")):
        return False
    if current.get("recipe") != recipe(current.get("spec"), current.get("apt")):
        return True
    if client is not None and current.get("heavy"):
        # Images built before tools_id was recorded rebuild once.
        try:
            return client.images.get(TOOLS_IMAGE).id != current.get("tools_id")
        except Exception:
            return False
    return False


def backing_off(current):
    """True while a failed build waits before its next attempt: 2, 4, 8 ... up to 60 minutes."""
    attempts, failed_at = current.get("attempts") or 0, current.get("failed_at") or 0
    return bool(attempts and failed_at) and time.time() - failed_at < min(3600, 120 * 2 ** (attempts - 1))


def _exists(client, tag):
    try:
        client.images.get(tag)
        return True
    except Exception:
        return False


def provision(client, project_id, root, base, heartbeat=lambda: True):
    """Detect, build if missing, remember. Returns the state; never raises for a failed build."""
    from . import environment

    declared = environment.read(Path(root)) or {}
    # The agent-written spec wins over detection; detection covers projects not prepared yet.
    spec = declared.get("toolchains") if declared else detect(root)
    apt = declared.get("apt", [])
    hosts = sorted(set(feeds(root)) | set(declared.get("feeds", [])))
    current = {
        "spec": spec,
        "apt": apt,
        "feeds": hosts,
        "setup": declared.get("setup", []),
        "check_paths": declared.get("check_paths", {}),
        "services": declared.get("services", []),
        "env": declared.get("env", {}),
        "declared": bool(declared),
        "image": None,
        "status": "base",
        "error": None,
    }
    previous = state(project_id)
    same = all(previous.get(key) == current[key] for key in ("spec", "apt"))
    if previous.get("status") == "failed" and same and backing_off(previous):
        # The same spec failed moments ago: report it again instead of rebuilding on every prepare.
        current.update({key: previous.get(key) for key in ("status", "error", "attempts", "failed_at")})
        _save(project_id, current)
        return current
    try:
        if (spec or apt) and client is not None:
            _build(client, base, spec, current, heartbeat, apt)
    except Exception as exc:  # noqa: BLE001 - the task continues on the base image; the owner sees why
        current.update(status="failed", error=str(exc)[-2000:], image=None)
    if current["status"] == "failed":
        current["attempts"] = (previous.get("attempts") or 0) + 1 if same else 1
        current["failed_at"] = time.time()
    _save(project_id, current)
    return current


def _build(client, base, spec, current, heartbeat, apt=()):
    """Build the SDK layer on the tools image (cached across factory deploys), then the thin runner layer."""
    base_id = client.images.get(base).id
    tools = TOOLS_IMAGE if _exists(client, TOOLS_IMAGE) else None
    origin = tools or base
    text = dockerfile(origin, spec, apt)
    heavy = (
        "darkfactory-toolchain:"
        + hashlib.sha256((client.images.get(origin).id + text).encode()).hexdigest()[:16]
    )
    with gc_lock():
        _image(client, heavy, text, spec, heartbeat)
        tag = _runner_layer(client, heavy, base, base_id, heartbeat) if tools else heavy
    current.update(
        image=tag,
        heavy=heavy if tools else None,
        status="ready",
        base_id=base_id,
        recipe=recipe(spec, apt),
        tools_id=client.images.get(origin).id if tools else None,
    )


def _runner_layer(client, heavy, base, base_id, heartbeat):
    """The SDK image plus the current runner code from the executor image."""
    heavy_id = client.images.get(heavy).id
    text = f"FROM {heavy}\nCOPY --from={base} {RUNNER_PATH} {RUNNER_PATH}\n"
    tag = "darkfactory-toolchain:" + hashlib.sha256((heavy_id + base_id).encode()).hexdigest()[:16]
    _image(client, tag, text, {}, heartbeat)
    return tag


def _image(client, tag, text, spec, heartbeat):
    """Build `tag` from a Dockerfile text unless it exists. The per-tag lock is held until the build has
    really ended; a timed-out or cancelled build is stopped by closing its connection, never left running
    under a released lock where a prune or a second build would race it."""
    lock = ROOT / "system" / "toolchains" / (tag.split(":")[1] + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if _exists(client, tag):
            return
        if shutil.which("docker"):
            # BuildKit: the classic builder that docker-py drives fails on Docker Desktop's containerd
            # image store with "failed to export layer: CreateDiff ... no such file or directory".
            _buildkit(tag, text, spec, heartbeat)
            return
        api = _build_client(client)
        outcome, done = {}, threading.Event()

        def build():
            try:
                for chunk in api.build(
                    fileobj=io.BytesIO(text.encode()),
                    tag=tag,
                    rm=True,
                    pull=False,
                    forcerm=True,
                    decode=True,
                    labels={"darkfactory.toolchain": json.dumps(spec)},
                ):
                    if isinstance(chunk, dict) and chunk.get("error"):
                        outcome["error"] = str(chunk["error"])[-2000:]
            except Exception as exc:  # noqa: BLE001 - recorded for the owner, the task continues on base
                outcome.setdefault("error", str(exc)[-2000:])
            finally:
                done.set()

        worker = threading.Thread(target=build, daemon=True)
        worker.start()
        started = time.monotonic()
        while not done.wait(5):
            if not heartbeat() or time.monotonic() - started > BUILD_SECONDS:
                outcome["error"] = "Сборка окружения прервана или превысила лимит времени"
                # The daemon cancels a build whose client went away.
                api.close()
                done.wait(120)
                break
        if outcome.get("error"):
            raise RuntimeError(outcome["error"])


def _buildkit(tag, text, spec, heartbeat):
    """`docker buildx build --load` of a Dockerfile text, killed for real on timeout or cancellation."""
    import subprocess
    import tempfile

    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen(
            [
                "docker",
                "buildx",
                "build",
                "--load",
                "--progress",
                "plain",
                "--tag",
                tag,
                "--label",
                "darkfactory.toolchain=" + json.dumps(spec),
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        proc.stdin.write(text.encode())
        proc.stdin.close()
        started, code = time.monotonic(), None
        while code is None:
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if not heartbeat() or time.monotonic() - started > BUILD_SECONDS:
                    proc.kill()
                    proc.wait()
                    raise RuntimeError("Сборка окружения прервана или превысила лимит времени") from None
        if code:
            log.seek(max(0, log.seek(0, 2) - 4000))
            raise RuntimeError(log.read().decode(errors="replace")[-2000:])


def _build_client(client):
    """A connection of its own for one build, so cancelling it never closes the shared client."""
    import docker

    try:
        return docker.APIClient(base_url=client.api.base_url, version=client.api.api_version, timeout=None)
    except Exception:  # noqa: BLE001 - stub clients in tests and unusual transports
        return client.api
