"""Read-only resources and installation-local persistent runtime data."""
import os
import sys
from pathlib import Path

RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
INSTALL_ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent)
# A distributed EXE always belongs to its own directory, even if launched from
# another cwd or a shell with a developer's MMN_DATA_DIR. Source-only tooling
# retains the explicit override for isolated offline tests.
DATA_ROOT = (INSTALL_ROOT / "userdata" if getattr(sys, "frozen", False) else
             Path(os.environ.get("MMN_DATA_DIR") or INSTALL_ROOT / "userdata")).expanduser().resolve()
GUI_CONFIG_PATH = DATA_ROOT / "config" / "gui.json"


def legacy_gui_candidates():
    """Only the current installation's old config is an automatic source."""
    if not getattr(sys, "frozen", False) and os.environ.get("MMN_DATA_DIR"):
        return ()
    return (INSTALL_ROOT / "config" / "gui.json",)

def ensure_data_dirs():
    for name in ("config", "cache", "logs", "state", "images/debug", "images/screenshots"):
        (DATA_ROOT / name).mkdir(parents=True, exist_ok=True)
