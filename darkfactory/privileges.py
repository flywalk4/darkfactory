"""Who runs what inside a task container.

The runner starts as root with only CAP_SETUID and CAP_SETGID. It alone reads the job file (credentials,
test secrets, MCP headers) and owns the container's stdout, where the result line goes. The agent, its
tool commands, setup and checks run as AGENT_UID; stdio MCP servers run as MCP_UID, so their secrets stay
out of the agent's /proc. A process of another uid can neither read root's memory or file descriptors nor
signal it, so the agent cannot forge or suppress the result.
"""

import os
from pathlib import Path

AGENT_UID = 1000
MCP_UID = 1001


def privileged():
    return os.geteuid() == 0


def as_user(uid=AGENT_UID):
    """Popen keyword arguments that run the child as `uid`; none when the runner is not root."""
    return {"user": uid, "group": uid, "extra_groups": []} if privileged() else {}


def hand_over(*paths, uid=AGENT_UID):
    """Give directories the runner created to the agent's uid, recursively, without following links."""
    if not privileged():
        return
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        os.chown(path, uid, uid, follow_symlinks=False)
        if path.is_dir() and not path.is_symlink():
            for root, dirs, files in os.walk(path, followlinks=False):
                for name in dirs + files:
                    os.chown(os.path.join(root, name), uid, uid, follow_symlinks=False)
