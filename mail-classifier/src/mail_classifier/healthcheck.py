from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    heartbeat_path = Path(
        os.getenv("CLASSIFIER_HEARTBEAT_PATH", "/tmp/mail-classifier.heartbeat")
    )
    max_age_seconds = int(os.getenv("CLASSIFIER_HEALTHCHECK_MAX_AGE_SECONDS", "120"))

    if not heartbeat_path.exists():
        return 1

    age = time.time() - heartbeat_path.stat().st_mtime
    return 0 if age <= max_age_seconds else 1


if __name__ == "__main__":
    sys.exit(main())

