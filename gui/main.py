from __future__ import annotations

import logging
import argparse
import sys
from pathlib import Path

# 同时支持 python -m gui.main 和 python gui/main.py。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.startup import configure_logging
configure_logging()

from PySide6.QtWidgets import QApplication
from gui.main_window import MainWindow
from gui.branding import PROJECT_NAME, WINDOW_TITLE, SOFTWARE_NAME_EN, PROJECT_DESCRIPTION


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{WINDOW_TITLE} · {SOFTWARE_NAME_EN}：{PROJECT_DESCRIPTION}")
    parser.add_argument("--dry-run", action="store_true", help="本次会话强制离线演练，不访问 ADB")
    parser.add_argument("--offline-check", action="store_true", help="Validate bundled resources and GUI without devices")
    parser.add_argument("--update-probe", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName(PROJECT_NAME)
    from updates.version import current_version
    app.setApplicationVersion(current_version())
    app.setApplicationDisplayName(WINDOW_TITLE)
    app.setStyle("Fusion")
    window = MainWindow(force_dry_run=args.dry_run or args.offline_check)
    if args.update_probe:
        from gui.update_probe import prepare
        prepare(window, app, args.update_probe)
    else:
        window.show()
        if not args.dry_run and not args.offline_check:
            window.updates.startup()
    if args.offline_check:
        from gui.offline_check import verify
        verify(window)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(250, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
