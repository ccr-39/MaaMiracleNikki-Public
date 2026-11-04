"""A hidden startup receipt: tasks stay disabled until the updater commits."""
import json
import os
from pathlib import Path
import time

from PySide6.QtCore import QTimer

from tools.windows_package import write_json


def prepare(window, application, stage):
    stage = Path(stage).resolve()
    expected = json.loads((stage / "probe.json").read_text(encoding="utf-8"))
    window._schedule_timer.stop()
    window.setEnabled(False)
    timer = QTimer(window)
    timer.setInterval(100)
    deadline = time.monotonic() + 45

    def check():
        if (stage / "abort").exists() or time.monotonic() > deadline:
            timer.stop()
            application.quit()
        elif (stage / "commit").exists():
            timer.stop()
            window.setEnabled(True)
            window.show()
            window._schedule_timer.start()
            window.updates.startup()

    timer.timeout.connect(check)
    timer.start()
    # The signal fires only after window construction and a live Qt event loop.
    QTimer.singleShot(0, lambda: write_json(stage / "health.json", {"token": expected["token"], "pid": os.getpid()}))
