"""Offline Qt tests with injected HTTP and installer boundaries."""
import os
from pathlib import Path
import sys
import time
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from gui.config import ConfigStore, GuiConfig
from gui.main_window import MainWindow
from gui.runner import OfflineExecutor
from gui.task_registry import TASKS
from updates.client import Release, UpdateError


@pytest.fixture
def window(tmp_path):
    app = QApplication.instance() or QApplication([])
    store = ConfigStore(tmp_path / "config/gui.json")
    store.save(GuiConfig(dry_run=True))
    w = MainWindow(store, OfflineExecutor(duration=0.1))
    w.show()
    yield w
    w.updates.cancel()
    deadline = time.monotonic() + 5
    while w.updates.job is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    w.close()
    deadline = time.monotonic() + 5
    while w.worker is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def wait(predicate):
    end = time.monotonic() + 5
    while not predicate():
        QApplication.processEvents()
        assert time.monotonic() < end
        time.sleep(.005)


def test_async_check_keeps_gui_responsive_and_renders_plain_notes(window):
    def latest(cancel):
        cancel.wait(.2)
        return Release("9.0.0", "<script>中文更新说明</script>", None, None)
    window.updates.client = Mock(latest=latest)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    window.updates.check()
    wait(lambda: window.updates.job is None)
    timer.stop()
    assert len(ticks) >= 3
    assert window.settings_page.latest_version.text() == "9.0.0"
    assert window.settings_page.update_notes.toPlainText() == "<script>中文更新说明</script>"
    assert window.settings_page.apply_update.isEnabled()
    assert len(TASKS) == 14  # Mail and daily rewards now share one visible group.


def test_no_update_and_quiet_network_failure(window):
    window.updates.client = Mock()
    window.updates.client.latest.return_value = Release("0.0.1", "旧版本", None, None)
    window.updates.check()
    wait(lambda: window.updates.job is None)
    assert not window.settings_page.apply_update.isEnabled()
    window.updates.client.latest.side_effect = UpdateError("GitHub 超时，请稍后重试")
    with patch.object(QMessageBox, "warning") as warning:
        window.updates.check()
        wait(lambda: window.updates.job is None)
    warning.assert_not_called()
    assert "超时" in window.settings_page.update_status.text()
    assert window.daily_page.start_button.isEnabled()


def test_auto_check_preference_survives_reopen(window):
    window.settings_page.auto_update.setChecked(False)
    window.save_settings()
    assert not window.store.load().auto_check_updates
    with patch.object(window.updates, "check") as check:
        window.updates._automatic_check()
        check.assert_not_called()
        window.settings_page.auto_update.setChecked(True)
        window.updates._automatic_check()
        check.assert_called_once()


def test_download_failure_and_development_install_guard(window, tmp_path):
    window.updates.release = Release("9.0.0", "说明", None, None)
    window.updates.client = Mock()
    window.updates.client.download.side_effect = UpdateError("SHA256 校验失败")
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        window.updates.apply()
        wait(lambda: window.updates.job is None)
    assert "SHA256" in window.settings_page.update_status.text()
    window.updates.downloaded = (tmp_path / "fixture.zip", "a" * 64)
    with patch("gui.software_update.launch_installer") as launch:
        window.updates.apply()
    launch.assert_not_called()
    assert "开发模式" in window.settings_page.update_status.text()


def test_pending_update_blocks_schedules_and_new_tasks_until_safe_idle(window):
    window.updates.install_pending = True
    window._start_batch([TASKS[0].entry])
    assert window.worker is None
    with patch.object(window, "close") as close:
        window.worker = Mock()
        window.updates._advance_install()
        close.assert_not_called()
        window.worker = None
        window.updates._advance_install()
        close.assert_called_once()
    window.updates.cancel()
    assert not window.updates.install_pending


def test_close_cancels_background_work_without_destroying_thread(window):
    def latest(cancel):
        cancel.wait(5)
        raise UpdateError("已取消")
    window.updates.client = Mock(latest=latest)
    window.updates.check()
    window.close()
    assert window._close_pending and window.isVisible()
    wait(lambda: window.updates.job is None and not window.isVisible())


def test_install_handshake_does_not_exit_until_updater_is_ready(window, tmp_path):
    w = window.updates
    w.install_pending = True
    w.release = Release("9.0.0", "", None, None)
    w.downloaded = (tmp_path / "file.zip", "a" * 64)
    process = Mock()
    process.poll.return_value = None
    request = tmp_path / "request.json"
    with patch("gui.software_update.launch_installer", return_value=(process, request)):
        assert not w.finish_close()
        assert not w.finish_close()
        (tmp_path / "ready.json").write_text('{"ready":true}')
        assert w.finish_close()
    w.install_pending = False
    w.process = None


def test_hidden_probe_gates_tasks_until_commit(window, tmp_path):
    from gui.update_probe import prepare
    (tmp_path / "probe.json").write_text('{"token":"offline-test"}')
    window.hide()
    application = QApplication.instance()
    with patch.object(window.updates, "startup") as startup:
        prepare(window, application, tmp_path)
        assert not window._schedule_timer.isActive() and not window.isEnabled()
        wait(lambda: (tmp_path / "health.json").exists())
        assert not window.isVisible()
        (tmp_path / "commit").touch()
        wait(window.isVisible)
        assert window.isEnabled() and window._schedule_timer.isActive()
        startup.assert_called_once()


def test_save_failure_cancels_install_wait_instead_of_repeatedly_closing(window):
    window.updates.install_pending = True
    window.updates._timer.start()
    with patch.object(window.store, "save", side_effect=OSError("disk full")), \
            patch.object(QMessageBox, "warning"), patch("gui.software_update.launch_installer") as launch:
        window.close()
    assert not window.updates.install_pending and not window.updates._timer.isActive()
    assert window.isVisible()
    launch.assert_not_called()
