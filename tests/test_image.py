import re
from pathlib import Path

DOCKERFILE = (Path(__file__).parent.parent / "Dockerfile").read_text()


def stage(name):
    return DOCKERFILE.split(f" AS {name}\n", 1)[1].split("\nFROM ", 1)[0]


def test_agent_cli_update_keeps_the_chromium_layer():
    """A new Codex or Claude Code version must not download Chromium (~310 MB) again."""
    tools = stage("executor-tools")
    browser = tools.index("install-browser.cjs /tmp/install-browser.cjs")
    for name in ("CODEX_VERSION", "CLAUDE_VERSION", "@openai/codex", "@anthropic-ai/claude-code"):
        assert name not in stage("cli")
        assert tools.index(name) > browser, name


def test_codex_is_new_enough_for_current_chatgpt_models():
    # ChatGPT offers only codex-auto-review to clients older than 0.160.
    version = re.search(r"^ARG CODEX_VERSION=(\d+)\.(\d+)\.(\d+)$", DOCKERFILE, re.M)
    assert tuple(map(int, version.groups())) >= (0, 160, 0)
