"""Token usage per executor run, summed per task; a token budget stops runaway retries."""

import json
import os

from . import evidence
from .db import ROOT, decrypt

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "cost_usd")
WARN_SHARE = 0.8


def run_usage(root, generation):
    try:
        data, _ = evidence.read(root, generation + "/usage.json")
        value = json.loads(data)
    except (OSError, ValueError):
        return None
    usage = {key: value[key] for key in FIELDS if isinstance(value.get(key), int | float)}
    return usage or None


def task_usage(task_id):
    """Sum per-run usage across every recorded executor run of the task."""
    root = ROOT / "artifacts" / task_id / "browser"
    total = {}
    for item in evidence.listing(root)["files"]:
        if item["name"].count("/") == 1 and item["name"].endswith("/usage.json"):
            for key, value in (run_usage(root, item["name"].split("/")[0]) or {}).items():
                total[key] = total.get(key, 0) + value
    return total or None


def spent_tokens(task_id):
    """Uncached input plus output: cached prefix reads cost a fraction and do not signal a runaway task."""
    total = task_usage(task_id) or {}
    return max(0, total.get("input_tokens", 0) - total.get("cached_input_tokens", 0)) + total.get(
        "output_tokens", 0
    )


def token_budget(project, task):
    """Project `token_budget` (0 disables) or DARKFACTORY_TASK_TOKENS; resume grants `token_credit`."""
    value = json.loads(decrypt(project.secret) or "{}").get("token_budget")
    base = int(value if value is not None else os.getenv("DARKFACTORY_TASK_TOKENS", "3000000"))
    return base + int((task.role_config or {}).get("token_credit", 0)) if base > 0 else 0
