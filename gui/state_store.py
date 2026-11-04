"""Persistent last queue status for diagnostics; never used to resume purchases."""
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from app_paths import DATA_ROOT

def save_status(state, message):
    temporary = None
    try:
        target = DATA_ROOT / "state/last-queue.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"state": state, "message": message, "time": datetime.now(timezone.utc).isoformat()}, stream, ensure_ascii=False)
        os.replace(temporary, target)
    except OSError:
        logging.exception("Cannot persist queue status")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
