"""Shared caching mirrors for public package registries (Compose services `npm-cache`, `pypi-cache`).

Repeated installs across tasks hit the local cache instead of a flaky upstream. A project that sets its
own default registry keeps it: mirrors only replace the public defaults.
"""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

NPMRC_REGISTRY = re.compile(r"^\s*registry\s*=", re.MULTILINE)
PIP_INDEX = re.compile(r"^\s*(index-url|--index-url|-i)\b", re.MULTILINE)


def _authority(url):
    parts = urlsplit(url)
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _read(path):
    try:
        return Path(path).read_text(errors="replace")[:200_000]
    except OSError:
        return ""


def configure(workspace):
    """Return (allowed authorities, container env) for mirrors that apply to this checkout."""
    allowed, env = set(), {}
    npm = os.getenv("DARKFACTORY_NPM_MIRROR")
    if npm and not NPMRC_REGISTRY.search(_read(Path(workspace) / ".npmrc")):
        allowed.add(_authority(npm))
        env |= {"npm_config_registry": npm, "YARN_NPM_REGISTRY_SERVER": npm, "COREPACK_NPM_REGISTRY": npm}
        # npm keeps 15 sockets by default; a cold mirror answers each miss slowly, so overlap more of them.
        env["npm_config_maxsockets"] = "50"
    pypi = os.getenv("DARKFACTORY_PYPI_MIRROR")
    own = any(PIP_INDEX.search(_read(Path(workspace) / name)) for name in ("pip.conf", "requirements.txt"))
    if pypi and not own:
        host, port = _authority(pypi)
        allowed.add((host, port))
        env |= {
            "PIP_INDEX_URL": pypi,
            "PIP_TRUSTED_HOST": host,
            "UV_DEFAULT_INDEX": pypi,
            "UV_INSECURE_HOST": host,
        }
    return allowed, env
