"""Qt orchestration; all HTTP, hashing and ZIP inspection happen off the UI thread."""
import shutil
from threading import Event

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QMessageBox

from updates.client import Cancelled, UpdateClient
from updates.installer import launch_installer
from updates.version import Version, current_version


class UpdateJob(QThread):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)  # MiB-sized packages can exceed Qt's int range: emit percent instead.

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation
        self.cancel = Event()

    def run(self):
        try:
            self.result.emit(self.operation(self.cancel, lambda done, total: self.progress.emit(
                min(100, int(done * 100 / total)), 100)))
        except Cancelled:
            self.failed.emit("已取消下载，可重新下载。")
        except Exception as exc:
            self.failed.emit(str(exc))


class SoftwareUpdate(QObject):
    def __init__(self, window, client=None):
        super().__init__(window)
        self.window, self.page = window, window.settings_page
        self.client = client or UpdateClient()
        self.job = None
        self.release = None
        self.downloaded = None
        self.install_pending = False
        self.process = None
        self.request = None
        self._ready_ticks = 0
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._automatic_check)
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._advance_install)
        self.page.check_update.clicked.connect(self.check)
        self.page.apply_update.clicked.connect(self.apply)
        self.page.cancel_update.clicked.connect(self.cancel)

    def startup(self):
        if not self.window.force_dry_run and self.page.auto_update.isChecked():
            self._startup_timer.start(2000)

    def _automatic_check(self):
        if self.page.auto_update.isChecked() and not self.window._close_pending:
            self.check()

    def _controls(self):
        busy = self.job is not None or self.install_pending
        self.page.check_update.setEnabled(not busy)
        self.page.apply_update.setEnabled(not busy and self.release is not None and
                                         Version(self.release.version) > Version(current_version()))
        self.page.cancel_update.setVisible(busy and self.process is None)
        self.page.auto_update.setEnabled(not self.install_pending)

    def _start(self, operation, on_result):
        if self.job is not None or self.install_pending:
            return
        job = UpdateJob(operation, self)
        self.job = job
        job.result.connect(on_result)
        job.failed.connect(self.page.update_status.setText)
        job.progress.connect(self._progress)
        job.finished.connect(self._finished)
        self._controls()
        job.start()

    def _progress(self, value, maximum):
        self.page.update_progress.setRange(0, maximum)
        self.page.update_progress.setValue(value)

    def _finished(self):
        job, self.job = self.job, None
        job.deleteLater()
        self.page.update_progress.hide()
        self._controls()
        if self.window._close_pending:
            QTimer.singleShot(0, self.window.close)

    def check(self):
        if self.job or self.install_pending:
            return
        self.page.update_status.setText("正在检查 GitHub 最新稳定版本…")
        self._start(lambda cancel, progress: self.client.latest(cancel), self._checked)

    def _checked(self, release):
        if self.downloaded and (not self.release or self.release.version != release.version):
            shutil.rmtree(self.downloaded[0].parent)
            self.downloaded = None
        self.release = release
        self.page.latest_version.setText(release.version)
        self.page.update_notes.setPlainText(release.notes)
        newer = Version(release.version) > Version(current_version())
        self.page.update_status.setText("发现新版本，可查看更新说明后下载。" if newer else "当前已是最新版本。")
        self.page.apply_update.setText("安装并重启" if self.downloaded else "下载更新")
        self.window.tabs.setTabText(1, "设置 · 有更新" if newer else "设置")

    def apply(self):
        if self.job or self.install_pending or not self.release:
            return
        if not self.downloaded:
            if QMessageBox.question(self.window, "下载更新", f"下载官方版本 {self.release.version}？\n下载后仍需确认安装。",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            self.page.update_status.setText("正在下载并校验更新包…")
            self.page.update_progress.setValue(0)
            self.page.update_progress.show()
            self._start(lambda cancel, progress: self.client.download(self.release, progress, cancel), self._downloaded)
            return
        import sys
        if not getattr(sys, "frozen", False):
            self.page.update_status.setText(f"开发模式不安装更新。已校验的更新包：{self.downloaded[0]}")
            return
        message = QMessageBox(self.window)
        message.setWindowTitle("安装更新")
        message.setText(f"安装 {self.release.version} 并重启 MMN？用户配置、日志和任务状态将保留。")
        wait = message.addButton("任务结束后安装", QMessageBox.AcceptRole)
        stop = message.addButton("停止任务后安装", QMessageBox.ActionRole)
        message.addButton("取消", QMessageBox.RejectRole)
        message.setDefaultButton(wait)
        message.exec()
        if message.clickedButton() not in (wait, stop):
            return
        self.install_pending = True
        self._controls()
        self.page.update_status.setText("等待任务与连接检测结束后安全退出并安装…")
        if message.clickedButton() == stop:
            self.window.stop_queue()
        self._timer.start()

    def _downloaded(self, value):
        self.downloaded = value
        self.page.apply_update.setText("安装并重启")
        self.page.update_status.setText("更新包已通过 SHA256 和结构校验，点击“安装并重启”确认安装。")

    def cancel(self):
        if self.job:
            self.job.cancel.set()
            self.page.update_status.setText("正在取消，请稍候…")
        elif self.process is None:
            self.install_pending = False
            self._timer.stop()
            self.page.update_status.setText("已取消安装，更新包已保留。")
            self._controls()

    def _advance_install(self):
        if self.window.worker is not None or self.page.connection_busy:
            return
        self.window.close()

    def finish_close(self):
        """Called after settings are saved, before accepting closeEvent."""
        try:
            if self.process is None:
                self.process, self.request = launch_installer(*self.downloaded, self.release.version)
                self._ready_ticks = 0
                self._timer.start()
                self.page.cancel_update.hide()
                return False
            self._ready_ticks += 1
            if self.process.poll() is not None or self._ready_ticks > 100:
                # The waiting helper checks this marker before touching files.
                (self.request.parent / "cancel").touch()
                raise RuntimeError("独立更新器未就绪，原版本未改动。请重试；详细信息见下载目录 result.json。")
            if (self.request.parent / "ready.json").exists():
                self._timer.stop()
                return True
            return False
        except Exception as exc:
            self.install_pending = False
            self.window._close_pending = False
            self._timer.stop()
            self.process = None
            self.page.update_status.setText(f"启动更新失败：{exc}")
            self._controls()
            return False

    def close_ready(self):
        self._startup_timer.stop()
        if self.job:
            self.job.cancel.set()
            self.window._close_pending = True
            self.page.update_status.setText("等待更新检查或下载取消后关闭…")
            return False
        return self.finish_close() if self.install_pending else True
