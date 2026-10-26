"""Fail when a change adds a credential or an owner-listed internal name to the repository.

    python scripts/leak_check.py [BASE]

BASE defaults to DARKFACTORY_CHECK_BASE (the factory sets it for a task's checks). Without a base there is
no change to check, as on the preprod monitor, and the check passes. Owner patterns: TEST_LEAK_PATTERNS.
A line that must keep a matching example carries the marker `leak-check: allow`.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from darkfactory.leaks import scan_repository  # noqa: E402


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DARKFACTORY_CHECK_BASE", "")
    if not base:
        print("leak check: no base commit, nothing to compare")
        return 0
    findings = scan_repository(base)
    for source, line, rule in findings:
        # The matched value is not printed: check output lands in task logs and prompts.
        print(f"leak check: {rule} in {source}, line {line}")
    if findings:
        print("Remove the value, or mark a deliberate example with `leak-check: allow`.")
        return 1
    print("leak check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
