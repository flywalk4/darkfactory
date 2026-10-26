"""Token and time metrics over recent factory tasks; the code lives in darkfactory/metrics.py.

docker compose exec -T worker python scripts/factory_metrics.py --last 100 [--since ISO] [--json]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from darkfactory.metrics import *  # noqa: E402,F403
from darkfactory.metrics import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
