from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from time import monotonic

from PySide6.QtCore import QTimer, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QMessageBox, QTabWidget, QVBoxLayout, QWidget

from gui.config import ConfigStore, GuiConfig, validate_connection_fields
from gui.pages.daily_page import DailyPage
from gui.pages.settings_page import SettingsPage
from gui.runner import MaaTaskExecutor, OfflineExecutor, QueueWorker
from gui.failures import FailureInfo, describe_failure
from gui.connection_status import RuntimeConnectionStatus, SessionStage
from gui.user_events import EventType
from gui.branding import WINDOW_TITLE, SOFTWARE_NAME_EN, PROJECT_DESCRIPTION, REPOSITORY_URL, GUI_LOGGER_NAME


from gui.theme import STYLE
from gui.scheduler import DailySchedule
from gui.desktop_notifications import DesktopNotifier, duration_text


class MainWindow(QMainWindow):
    def __init__(self, store: ConfigStore | None = None, executor=None, *, force_dry_run: bool = False):
        super().__init__()
        self.store = store if store is not None else ConfigStore()
        self.executor = executor
        self.force_dry_run = force_dry_run
        self.worker = None
        self._notifier = DesktopNotifier(self)
        self._queue_started_at = None
        self._notify_elapsed = None
        self._notify_enabled = False
        self._notified_tasks = set()
        self._close_pending = False
        self._last_outcome = None
        self._last_problem = None
        self._result_context = None
        self._resume_executor = None
        self._settings_dirty = False
        self._save_failed = False
        self._runtime_connection = RuntimeConnectionStatus()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(400)
        self._autosave_timer.timeout.connect(lambda: self.save_settings(automatic=True))
        warning = ""
        try:
            self.config = self.store.load()
        except (OSError, ValueError) as exc:
            self.config = GuiConfig()
            warning = f"配置读取失败，已使用默认值：{exc}"
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(880, 620)
        self.setMinimumSize(720, 520)
        self.setStyleSheet(STYLE)
        self.tabs = QTabWidget()
        self.daily_page = DailyPage(self.config.selected_entries, config=self.config)
        self.settings_page = SettingsPage(self.config)
        from gui.software_update import SoftwareUpdate
        self.updates = SoftwareUpdate(self)
        self.tabs.addTab(self.daily_page, "每日任务")
        self.tabs.addTab(self.settings_page, "设置")
        self.tabs.setCurrentIndex(0)
        shell = QWidget()
        shell.setObjectName("appShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        header = QWidget()
        header.setObjectName("appHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 12, 18, 8)
        brand = QLabel(WINDOW_TITLE)
        brand.setToolTip(f"{SOFTWARE_NAME_EN}\n{PROJECT_DESCRIPTION}\n{REPOSITORY_URL}")
        brand.setObjectName("brandTitle")
        header_layout.addWidget(brand)
        header_layout.addStretch()
        self.mode_badge = QLabel()
        self.mode_badge.setObjectName("modeBadge")
        header_layout.addWidget(self.mode_badge)
        shell_layout.addWidget(header)
        shell_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(shell)
        if self.force_dry_run:
            self.settings_page.dry_run.setChecked(True)
            self.settings_page.dry_run.setEnabled(False)
        self._show_mode()
        self.daily_page.start_requested.connect(self.start_queue)
        self.daily_page.stop_requested.connect(self.stop_queue)
        self.daily_page.retry_failed_requested.connect(self.retry_failed)
        self.daily_page.resume_requested.connect(self.resume_unfinished)
        self.daily_page.connection_settings_requested.connect(self.open_connection_settings)
        self.daily_page.reconnect_requested.connect(self.reconnect_device)
        self.daily_page.task_options_changed.connect(self.save_task_options)
        self.settings_page.save_requested.connect(self.save_settings)
        self.settings_page.settings_changed.connect(self._settings_edited)
        self.settings_page.connection_details_changed.connect(self._connection_details_edited)
        self.settings_page.connection_test_changed.connect(self._show_connection_status)
        self.daily_page.selection_changed.connect(self._settings_edited)
        self.settings_page.connection_busy_changed.connect(self._connection_busy_changed)
        self._apply_log_level()
        self._schedule = DailySchedule()
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(1000)
        self._schedule_timer.timeout.connect(self._check_schedule)
        self._schedule_timer.start()
        if warning:
            self.settings_page.feedback_label.setText(warning)
            self.daily_page.state_label.setText(warning)

    def _check_schedule(self, now=None):
        now = now or datetime.now()
        if self._close_pending or self.updates.install_pending:
            return
        # Use the current draft so disabling a timer takes effect immediately.
        draft = self.settings_page.read_config(self.daily_page.selected_entries())
        try:
            draft.validate_task_options()
            due = self._schedule.claim(draft, now)
        except ValueError as exc:
            self.settings_page.schedule_status.setText(f"定时暂停：{exc}")
            return
        if not draft.schedule_enabled:
            self.settings_page.schedule_status.setText("每日定时未启用")
            return
        if not due:
            label = self.settings_page.schedule_status
            if label.text().startswith(("每日定时未启用", "定时暂停", "按电脑", "已启用：每天")):
                label.setText(f"已启用：每天 {draft.schedule_times}（电脑本地时间）")
            return
        if self.worker is not None or self.settings_page.connection_busy:
            message = "定时任务已跳过：当前正在执行任务或检测连接"
        elif self.daily_page.reward_selection_error():
            message = "定时任务已跳过：" + self.daily_page.reward_selection_error()
        elif not draft.selected_entries:
            message = "定时任务已跳过：未勾选任务"
        else:
            message = f"{now:%H:%M} 定时任务已触发"
            self.settings_page.schedule_status.setText(message)
            self.daily_page.log_panel.append_log(now.strftime("%H:%M:%S"), "INFO", message)
            self._start_batch(["QJNN_StartAndEnterHome", *draft.selected_entries])
            return
        self.settings_page.schedule_status.setText(message)
        self.daily_page.log_panel.append_log(now.strftime("%H:%M:%S"), "WARNING", message)

    def _apply_log_level(self):
        logging.getLogger(GUI_LOGGER_NAME).setLevel(self.config.log_level)

    def _show_mode(self):
        dry_run = self.force_dry_run or self.config.dry_run
        mode = "离线演练" if dry_run else "实机执行"
        self.setWindowTitle(WINDOW_TITLE)
        self.mode_badge.setText(mode)
        self._show_connection_status()
        self.daily_page.state_label.setText(f"就绪（{mode}）")

    def _show_connection_status(self):
        dry_run = self.force_dry_run or self.config.dry_run
        draft = self.settings_page.read_config([])
        saved_key = (self.config.adb_address, self.config.adb_path, self.config.auto_connect)
        configuration = "已配置" if self.settings_page.connection_key == saved_key else "待自动保存"
        if self._save_failed and self.settings_page.connection_key != saved_key:
            configuration = "未保存 · 写入失败"
        try:
            validate_connection_fields(draft, require_address=not self.force_dry_run)
        except ValueError:
            configuration = "输入无效 · 未保存"
        runtime = self._runtime_connection
        if runtime.stage is SessionStage.IDLE and dry_run:
            runtime = RuntimeConnectionStatus(SessionStage.OFFLINE)
        self.daily_page.show_connection_details(draft.adb_address, configuration,
                                                self.settings_page.test_report, runtime)

    def _save_selection(self):
        self._autosave_timer.stop()
        candidate = self.settings_page.read_config(self.daily_page.selected_entries())
        self.daily_page.apply_task_options(candidate)
        if self.force_dry_run:
            candidate.dry_run = self.config.dry_run
        # A clean profile has no device yet; unrelated settings must still save.
        # MaaTaskExecutor requires an address before any real queue can start.
        validate_connection_fields(candidate, require_address=False)
        previous = self.config
        self.store.save(candidate)
        self.config = candidate
        self._settings_dirty = False
        self._save_failed = False
        self._apply_log_level()
        if previous.dry_run != candidate.dry_run:
            self._runtime_connection = RuntimeConnectionStatus()
        if (previous.adb_address, previous.adb_path, previous.auto_connect, previous.dry_run) != (
                candidate.adb_address, candidate.adb_path, candidate.auto_connect, candidate.dry_run):
            self._show_mode()
        else:
            self._show_connection_status()
        self.settings_page.save_button.hide()
        self.settings_page.feedback_label.setText("已自动保存")
        self.daily_page.refresh_task_options_note()

    @Slot()
    def _settings_edited(self):
        self._settings_dirty = True
        self._save_failed = False
        self.settings_page.feedback_label.setText("正在自动保存…")
        self._autosave_timer.start()

    @Slot()
    def _connection_details_edited(self):
        if self.worker is None:
            self._runtime_connection = RuntimeConnectionStatus()
            self._show_connection_status()

    def _save_error(self, exc):
        self._save_failed = isinstance(exc, OSError)
        self.settings_page.feedback_label.setText(f"未保存：{exc}。上一份有效配置已保留。" if isinstance(exc, ValueError)
                                                   else f"保存失败：{exc}。请重试保存。")
        self.settings_page.save_button.setVisible(isinstance(exc, OSError))
        self._show_connection_status()

    @Slot()
    def save_task_options(self):
        if self.worker is not None or self.settings_page.connection_busy:
            return
        try:
            self._save_selection()
        except (OSError, ValueError) as exc:
            self._settings_dirty = True
            self._save_error(exc)
            self.daily_page.refresh_task_options_note(error=str(exc))
        else:
            self.settings_page.feedback_label.setText("已自动保存")

    @Slot()
    def save_settings(self, *, automatic=False):
        if self.worker is not None or self.settings_page.connection_busy:
            return
        try:
            self._save_selection()
        except (OSError, ValueError) as exc:
            self._settings_dirty = True
            self._save_error(exc)
            return
        self.settings_page.feedback_label.setText("已自动保存" if automatic else "配置已保存")

    @Slot()
    def start_queue(self):
        if self.worker is not None or self.settings_page.connection_busy:
            return
        error = self.daily_page.reward_selection_error()
        if error:
            self.daily_page.state_label.setText(error)
            QMessageBox.information(self, "未选择奖励子项", error)
            return
        selected = self.daily_page.selected_entries()
        if not selected:
            QMessageBox.information(self, "未选择任务", "请至少勾选一项每日任务。")
            return
        self._start_batch(selected)

    @Slot()
    def retry_failed(self):
        selected = self.daily_page.entries_with_states({"failed"})
        if selected:
            self._start_batch(selected, preserve_results=True)

    @Slot()
    def resume_unfinished(self):
        selected = self.daily_page.entries_with_states({"failed", "stopped", "not_run"})
        if selected:
            self._start_batch(selected, preserve_results=True)

    def _start_batch(self, selected, *, preserve_results=False):
        if self.worker is not None or self.settings_page.connection_busy or self.updates.install_pending:
            return
        if not preserve_results and (error := self.daily_page.reward_selection_error()):
            self.daily_page.state_label.setText(error)
            return
        try:
            self._save_selection()
            if self.executor is not None:
                executor = self.executor
            elif self.force_dry_run or self.config.dry_run:
                executor = OfflineExecutor()
            else:
                executor = MaaTaskExecutor(self.config)
            real = getattr(executor, "is_real", False)
            context = (real, self.config.adb_address if real else "",
                       self.config.adb_path if real else "", self.config.connection_profile if real else "")
            if preserve_results and self._result_context is not None and self._result_context != context:
                self._last_problem = FailureInfo(
                    "context", "连接或执行模式已改变", "设备或模式已更改，请点击“开始任务”重新运行。")
                self.daily_page.show_recovery(self._last_problem)
                return
            if getattr(executor, 'supports_resume_state', False) is True:
                if not preserve_results:
                    executor.resume_state = None
                elif self._resume_executor is not None:
                    executor.resume_state = self._resume_executor.resume_state
                self._resume_executor = executor
            elif not preserve_results:
                self._resume_executor = None
            worker = QueueWorker(selected, executor, self, continue_on_failure=self.config.continue_on_failure)
        except (OSError, ValueError) as exc:
            self._save_error(exc)
            if isinstance(exc, ValueError):
                self.open_connection_settings()
            self.daily_page.state_label.setText(f"启动失败：{exc}")
            self._last_problem = describe_failure(exc, stage="prepare")
            self.daily_page.show_recovery(self._last_problem)
            return
        self.worker = worker
        self._queue_started_at = monotonic()
        self._notify_elapsed = None
        self._notify_enabled = real
        self._notified_tasks.clear()
        self._runtime_connection = RuntimeConnectionStatus(
            SessionStage.CONNECTING if real else SessionStage.OFFLINE)
        self.daily_page.log_panel.set_detailed_log_path(None)
        self._last_outcome = None
        self._last_problem = None
        self._result_context = context
        self.daily_page.set_running(True)
        self.settings_page.set_running(True)
        self.daily_page.current_task_label.setText("—")
        real = getattr(executor, "is_real", False)
        self.daily_page.begin_queue(selected, dry_run=not real, preserve_results=preserve_results)
        self._show_connection_status()
        self.daily_page.state_label.setText("正在连接 / 初始化…" if real else "运行中（离线演练）")
        worker.task_started.connect(self._task_started)
        worker.user_event.connect(self.daily_page.log_panel.append_event)
        worker.user_event.connect(self.daily_page.update_task_status)
        worker.user_event.connect(self._connection_event)
        worker.user_event.connect(self._desktop_event)
        worker.problem_reported.connect(self._remember_problem)
        worker.state_changed.connect(self.daily_page.state_label.setText)
        worker.device_changed.connect(self._runtime_device_changed)
        worker.outcome.connect(self._queue_outcome)
        worker.finished.connect(self._worker_finished)
        worker.start()

    def _current_worker_signal(self):
        sender = self.sender()
        return self.worker is not None and (sender is None or sender is self.worker)

    @Slot(str, str)
    def _runtime_device_changed(self, address, _status):
        if not self._current_worker_signal():
            return
        # Text is legacy presentation, not evidence of a state transition.
        self._runtime_connection.actual_address = address
        self._show_connection_status()

    @Slot(object)
    def _connection_event(self, event):
        if not self._current_worker_signal() or not getattr(self.worker.executor, "is_real", False):
            return
        runtime = self._runtime_connection
        if event.kind is EventType.DEVICE_CONNECTING:
            runtime.stage = SessionStage.CONNECTING
        elif event.kind is EventType.DEVICE_CONNECTED:
            runtime.stage = SessionStage.CONNECTED
            runtime.connected_at = event.time
        elif event.kind is EventType.DEVICE_FAILED:
            runtime.stage = SessionStage.FAILED
            kind = self._last_problem.kind if self._last_problem else "prepare"
            runtime.failure = ("连接失败，未进入任务执行" if kind == "connection" else
                               "初始化失败，未进入任务执行" if kind == "initialization" else
                               "连接 / 初始化未完成")
        elif event.kind is EventType.RUNTIME_RELEASED:
            if runtime.stage is not SessionStage.FAILED:
                runtime.stage = SessionStage.ENDED
        else:
            return
        self._show_connection_status()

    @Slot(object)
    def _remember_problem(self, problem):
        if isinstance(problem, FailureInfo):
            self._last_problem = problem

    @Slot()
    def open_connection_settings(self):
        if self.worker is not None or self.settings_page.connection_busy:
            return
        self.settings_page.navigation.setCurrentRow(0)
        self.tabs.setCurrentWidget(self.settings_page)

    @Slot()
    def reconnect_device(self):
        if self.worker is not None or self.settings_page.connection_busy:
            return
        self.open_connection_settings()
        if self.settings_page.dry_run.isChecked():
            self.settings_page.connection_feedback.setText("请先在运行设置中关闭“离线演练”，再连接设备。")
            return
        self.settings_page.test_connection(reconnect=True)

    @Slot(str)
    def _task_started(self, name):
        from gui.state_store import save_status
        save_status("running", name)
        self.daily_page.current_task_label.setText(name)
        if self.worker is not None and not self.worker.stop_event.is_set():
            mode = "实机执行" if getattr(self.worker.executor, "is_real", False) else "离线演练"
            self.daily_page.state_label.setText(f"运行中（{mode}）")

    @Slot()
    def stop_queue(self):
        if self.worker is None:
            return
        self.daily_page.state_label.setText("正在停止，请稍候…")
        self.daily_page.stop_button.setEnabled(False)
        self.worker.request_stop()

    @Slot(str, str)
    def _queue_outcome(self, state, message):
        from gui.state_store import save_status
        save_status(state, message)
        self._last_outcome = (state, message)
        self.daily_page.finish_queue(state)
        self.daily_page.state_label.setText(message)
        if self._notify_enabled:
            elapsed = self._notify_elapsed
            if elapsed is None:
                elapsed = monotonic() - self._queue_started_at if self._queue_started_at is not None else 0
            title = {"completed": "MMN：任务全部完成", "partial": "MMN：任务已结束（有错误）",
                     "stopped": "MMN：任务已停止"}.get(state, "MMN：任务出错，已停止")
            self._notifier.show(title, f"{message}\n总用时：{duration_text(elapsed)}",
                                error=state in {"partial", "error"})
            self._notify_enabled = False

    @Slot(object)
    def _desktop_event(self, event):
        if not self._notify_enabled:
            return
        if event.kind is EventType.QUEUE_FINISHED:
            self._notify_elapsed = event.elapsed_seconds
        elif event.kind is EventType.TASK_FAILED:
            key = event.task_entry or event.task_name
            if key in self._notified_tasks:
                return
            self._notified_tasks.add(key)
            elapsed = monotonic() - self._queue_started_at if self._queue_started_at is not None else 0
            self._notifier.show("MMN：任务出错", f"{event.task_name or '任务'}执行失败\n"
                                f"本项用时：{duration_text(event.elapsed_seconds or 0)}；"
                                f"本轮用时：{duration_text(elapsed)}", error=True)

    @Slot()
    def _worker_finished(self):
        worker = self.worker
        self.worker = None
        if worker is not None and not getattr(worker.executor, "is_real", False):
            self._runtime_connection.stage = SessionStage.OFFLINE_ENDED
        elif self._last_problem is not None and self._last_problem.kind == "cleanup":
            self._runtime_connection.stage = SessionStage.END_ERROR
        elif self._runtime_connection.stage is not SessionStage.FAILED:
            self._runtime_connection.stage = SessionStage.ENDED
        self._show_connection_status()
        self.daily_page.set_running(False)
        self.settings_page.set_running(False)
        if self.force_dry_run:
            self.settings_page.dry_run.setEnabled(False)
        if self._last_outcome is None:
            self.daily_page.finish_queue("error")
            self.daily_page.state_label.setText("执行线程异常结束")
        if worker is not None:
            self.daily_page.log_panel.set_detailed_log_path(worker.detailed_log_path)
            worker.deleteLater()
        outcome = self._last_outcome[0] if self._last_outcome else "error"
        self.daily_page.show_recovery(self._last_problem, outcome=outcome)
        if self._close_pending:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if self.settings_page.connection_busy:
            self._close_pending = True
            self.settings_page.connection_feedback.setText("等待连接检测结束后关闭…")
            event.ignore()
            return
        if self.worker is not None:
            self._close_pending = True
            self.stop_queue()
            event.ignore()
            return
        try:
            try:
                self._save_selection()
            except ValueError:
                # An unfinished invalid draft must neither overwrite the last
                # valid settings nor trap the user in the window on exit.
                self.store.save(self.daily_page.apply_task_options(replace(
                    self.config, selected_entries=self.daily_page.selected_entries())))
        except OSError as exc:
            self._close_pending = False
            if self.updates.install_pending and self.updates.process is None:
                self.updates.cancel()
            self.daily_page.state_label.setText(f"配置保存失败：{exc}")
            QMessageBox.warning(self, "配置保存失败", str(exc))
            event.ignore()
            return
        if not self.updates.close_ready():
            event.ignore()
            return
        event.accept()
        if self.updates.install_pending:
            from PySide6.QtWidgets import QApplication
            QTimer.singleShot(0, QApplication.instance().quit)

    @Slot(bool)
    def _connection_busy_changed(self, busy):
        self.daily_page.start_button.setEnabled(not busy and self.worker is None)
        self.daily_page.set_recovery_busy(busy or self.worker is not None)
        if not busy:
            if self._settings_dirty and self.worker is None:
                self.save_settings(automatic=True)
            if self.worker is None and self.settings_page.adb_address.text().strip() == self.config.adb_address:
                self._show_connection_status()
            if self.worker is None and self._last_outcome is not None:
                self.daily_page.show_recovery(self._last_problem, outcome=self._last_outcome[0])
            if self.force_dry_run:
                self.settings_page.dry_run.setEnabled(False)
            if self._close_pending:
                QTimer.singleShot(0, self.close)
