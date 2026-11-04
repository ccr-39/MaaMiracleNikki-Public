from PySide6.QtCore import Qt, Signal, Slot
from datetime import datetime
from dataclasses import replace
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)
from gui.config import GuiConfig, LOG_LEVELS, validate_connection_fields
from gui.connection import ConnectionWorker
from gui.widgets import DropdownComboBox, TwoDigitSpinBox
from gui.connection_status import ConnectionTestReport, TestStage
from gui.scheduler import parse_times


class SettingsPage(QWidget):
    save_requested = Signal()
    connection_busy_changed = Signal(bool)
    settings_changed = Signal()
    connection_details_changed = Signal()
    connection_test_changed = Signal()

    def __init__(self, config: GuiConfig, parent=None):
        super().__init__(parent)
        self._running = False
        self._connection_worker = None
        self.test_report = ConnectionTestReport()
        self._probe_context = None
        self._probe_is_test = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(12)
        columns = QHBoxLayout()
        columns.setSpacing(20)
        self.navigation = QListWidget()
        self.navigation.setObjectName("settingsNavigation")
        self.navigation.setFixedWidth(148)
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sections = QStackedWidget()
        self.sections.setObjectName("settingsSections")
        self.navigation.currentRowChanged.connect(self.sections.setCurrentIndex)
        columns.addWidget(self.navigation)
        columns.addWidget(self.sections, 1)
        layout.addLayout(columns, 1)
        body = self._add_section("连接设置")
        connection = QGroupBox("连接设置")
        form = QFormLayout(connection)
        form.setVerticalSpacing(8)
        self.connection_profile = QLabel("通用 ADB（支持 MuMu / 雷电 / 蓝叠 / 夜神等）")
        self.connection_profile.setWordWrap(True)
        self.adb_address = QLineEdit(config.adb_address)
        self.adb_address.setPlaceholderText("例如 127.0.0.1:16384 或 emulator-5554")
        self.adb_address.setClearButtonEnabled(True)
        self.adb_path = QLineEdit(config.adb_path)
        self.adb_path.setPlaceholderText("留空自动查找，或选择 adb.exe")
        self.adb_path.setToolTip("也支持模拟器自带的 HD-adb.exe、nox_adb.exe，以及相对路径。")
        self.browse_adb = QPushButton("选择…")
        self.browse_adb.clicked.connect(self._browse_adb)
        path_row = QHBoxLayout()
        path_row.addWidget(self.adb_path, 1)
        path_row.addWidget(self.browse_adb)
        self.detected_devices = DropdownComboBox()
        self.detected_devices.addItem("检测后可选择设备", "")
        self.detected_devices.activated.connect(self._select_device)
        self.detect_button = QPushButton("检测设备")
        self.detect_button.clicked.connect(lambda: self._inspect(test=False))
        device_row = QHBoxLayout()
        device_row.addWidget(self.detected_devices, 1)
        device_row.addWidget(self.detect_button)
        self.auto_connect = QCheckBox("自动连接指定地址")
        self.auto_connect.setChecked(config.auto_connect)
        self.auto_connect.setToolTip("运行前连接所填地址。关闭后，只使用已连接的设备；USB 设备无需开启。")
        self.test_button = QPushButton("连接与截图测试")
        self.test_button.clicked.connect(lambda: self._inspect(test=True))
        self.connection_feedback = QLabel("填写连接地址，或点击“检测设备”选择。")
        self.connection_feedback.setWordWrap(True)
        self.screenshot_preview = QLabel()
        self.screenshot_preview.setAlignment(Qt.AlignLeft)
        self.screenshot_preview.hide()
        form.addRow("连接配置", self.connection_profile)
        form.addRow("连接地址", self.adb_address)
        form.addRow("ADB 路径", path_row)
        form.addRow("已发现设备", device_row)
        form.addRow("", self.auto_connect)
        form.addRow("", self.test_button)
        form.addRow("", self.connection_feedback)
        form.addRow("", self.screenshot_preview)
        body.addWidget(connection)
        note = QLabel("先开启模拟器的 ADB 调试。MuMu 的端口可在多开器设置中查看。\n"
                      "多开时注意选对设备。连接测试不会操作游戏。")
        note.setWordWrap(True)
        body.addWidget(note)
        body.addStretch()
        body = self._add_section("运行设置")
        execution = QGroupBox("运行设置")
        execution_form = QFormLayout(execution)
        execution_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        execution_form.setVerticalSpacing(14)
        self.dry_run = QCheckBox("离线演练（不连接设备）")
        self.dry_run.setToolTip("只演示任务顺序和运行日志，不操作游戏。")
        self.dry_run.setChecked(config.dry_run)
        self.dry_run.toggled.connect(self._update_enabled)
        self.remember_selection = QCheckBox("保存上次勾选")
        self.remember_selection.setChecked(config.remember_selection)
        self.continue_on_failure = QCheckBox("任务失败后继续下一项")
        self.continue_on_failure.setChecked(config.continue_on_failure)
        self.continue_on_failure.setToolTip(
            "先返回主页，再执行下一项，不自动重试失败项。断连或无法返回主页时停止。")
        self.close_emulator_after_completion = QCheckBox("全部任务完成后关闭模拟器")
        self.close_emulator_after_completion.setChecked(config.close_emulator_after_completion)
        self.close_emulator_after_completion.setToolTip("仅全部任务成功后关闭当前 MuMu 实例，保留小助手窗口。请在定时与启动中选择 MuMu 启动程序。")
        execution_form.addRow("完成后", self.close_emulator_after_completion)
        execution_form.addRow("执行模式", self.dry_run)
        execution_form.addRow("", self.remember_selection)
        execution_form.addRow("失败处理", self.continue_on_failure)
        body.addWidget(execution)
        run_note = QLabel("失败后继续：返回主页后执行下一项，断连或无法返回主页时停止。\n"
                          "点击“停止”会结束本次运行。")
        run_note.setWordWrap(True)
        body.addWidget(run_note)
        body.addStretch()
        body = self._add_section("日志设置")
        log_group = QGroupBox("日志设置")
        log_form = QFormLayout(log_group)
        self.log_level = DropdownComboBox()
        self.log_level.addItems(LOG_LEVELS)
        self.log_level.setCurrentText(config.log_level)
        log_form.addRow("日志等级", self.log_level)
        body.addWidget(log_group)
        log_note = QLabel("日志等级只影响详细日志，日常使用保持 INFO 即可。\n"
                          "每日任务页的日志菜单可打开详细日志，文件保存在 images/debug/gui。")
        log_note.setWordWrap(True)
        body.addWidget(log_note)
        body.addStretch()
        body = self._add_section("定时与启动")
        schedule = QGroupBox("每日定时")
        schedule_form = QFormLayout(schedule)
        self.schedule_enabled = QCheckBox("启用每日定时执行")
        self.schedule_enabled.setChecked(config.schedule_enabled)
        hour, minute = map(int, parse_times(config.schedule_times)[0].split(":"))
        self.schedule_hour = TwoDigitSpinBox()
        self.schedule_minute = TwoDigitSpinBox()
        time_row = QHBoxLayout()
        time_row.setSpacing(8)
        for editor, maximum, value, name in ((self.schedule_hour, 23, hour, "小时"),
                                             (self.schedule_minute, 59, minute, "分钟")):
            editor.setRange(0, maximum)
            editor.setValue(value)
            editor.setFixedSize(58, 36)
            editor.setStyleSheet("QSpinBox::up-button, QSpinBox::down-button { width: 0px; }")
            editor.setAlignment(Qt.AlignCenter)
            editor.setAccessibleName(name)
            editor.setToolTip(f"请输入{name}，范围 00–{maximum:02d}")
            if editor is self.schedule_minute:
                self.schedule_separator = QLabel("：")
                self.schedule_separator.setAlignment(Qt.AlignCenter)
                time_row.addWidget(self.schedule_separator)
            time_row.addWidget(editor)
        time_row.addStretch()
        schedule_form.addRow("", self.schedule_enabled)
        schedule_form.addRow("执行时间", time_row)
        self.schedule_status = QLabel("按电脑本地时间每天执行一次，请分别输入小时和分钟。")
        self.schedule_status.setWordWrap(True)
        schedule_form.addRow("", self.schedule_status)
        body.addWidget(schedule)
        emulator = QGroupBox("自动启动模拟器")
        emulator_form = QFormLayout(emulator)
        self.emulator_auto_start = QCheckBox("设备未就绪时自动启动模拟器")
        self.emulator_auto_start.setChecked(config.emulator_auto_start)
        self.emulator_path = QLineEdit(config.emulator_path)
        self.emulator_path.setPlaceholderText("选择模拟器的 exe 启动程序")
        self.emulator_path.setToolTip("点击“选择…”定位模拟器启动文件；MuMu 通常为 nx_main 目录中的 MuMuNxMain.exe。")
        self.browse_emulator = QPushButton("选择…")
        self.browse_emulator.clicked.connect(self._browse_emulator)
        emulator_row = QHBoxLayout()
        emulator_row.addWidget(self.emulator_path, 1)
        emulator_row.addWidget(self.browse_emulator)
        self.emulator_arguments = QLineEdit(config.emulator_arguments)
        self.emulator_arguments.setPlaceholderText("MuMu 留空按连接地址自动选择实例；其他模拟器按需填写")
        emulator_form.addRow("", self.emulator_auto_start)
        emulator_form.addRow("启动程序", emulator_row)
        emulator_form.addRow("启动参数", self.emulator_arguments)
        body.addWidget(emulator)
        note = QLabel("请保持电脑开机、小助手运行，可最小化。关闭窗口会退出定时。\n"
                      "到点运行当前勾选任务，并自动补上启动游戏；忙碌时跳过，不补跑。\n"
                      "手动执行也会使用自动启动模拟器设置。需要登录或验证码时仍需手动处理。")
        note.setWordWrap(True)
        body.addWidget(note)
        body.addStretch()
        body = self._add_section("软件更新")
        update_group = QGroupBox("软件更新")
        update_form = QFormLayout(update_group)
        from updates.version import current_version
        self.current_version = QLabel(current_version())
        self.latest_version = QLabel("尚未检查")
        self.auto_update = QCheckBox("启动后自动检查更新")
        self.auto_update.setChecked(config.auto_check_updates)
        self.check_update = QPushButton("检查更新")
        self.apply_update = QPushButton("下载更新")
        self.apply_update.setEnabled(False)
        self.cancel_update = QPushButton("取消")
        self.cancel_update.hide()
        actions = QHBoxLayout()
        for button in (self.check_update, self.apply_update, self.cancel_update):
            actions.addWidget(button)
        self.update_status = QLabel("仅检查官方 GitHub 稳定版，安装前需要确认。")
        self.update_status.setWordWrap(True)
        self.update_status.setTextFormat(Qt.PlainText)
        self.update_progress = QProgressBar()
        self.update_progress.hide()
        self.update_notes = QPlainTextEdit()
        self.update_notes.setReadOnly(True)
        self.update_notes.setPlaceholderText("检查更新后显示发行说明")
        self.update_notes.setMinimumHeight(150)
        update_form.addRow("当前版本", self.current_version)
        update_form.addRow("最新稳定版", self.latest_version)
        update_form.addRow("", self.auto_update)
        update_form.addRow("", actions)
        update_form.addRow("状态", self.update_status)
        update_form.addRow("", self.update_progress)
        update_form.addRow("更新说明", self.update_notes)
        body.addWidget(update_group)
        body.addStretch()
        self.save_button = QPushButton("重试保存")
        self.save_button.setObjectName("saveButton")
        self.save_button.setMaximumWidth(160)
        self.save_button.clicked.connect(self.save_requested.emit)
        self.save_button.hide()
        self.feedback_label = QLabel("设置自动保存，下次运行生效。")
        self.feedback_label.setWordWrap(True)
        footer = QHBoxLayout()
        footer.setContentsMargins(168, 0, 0, 0)
        footer.addWidget(self.save_button)
        footer.addWidget(self.feedback_label, 1)
        layout.addLayout(footer)
        self.navigation.setCurrentRow(0)
        self._update_enabled()
        for control in (self.adb_address, self.adb_path):
            control.textChanged.connect(self._connection_edited)
        self.auto_connect.toggled.connect(self._connection_edited)
        for control in (self.dry_run, self.remember_selection, self.continue_on_failure, self.close_emulator_after_completion):
            control.toggled.connect(self._settings_edited)
        self.log_level.currentTextChanged.connect(self._settings_edited)
        self.emulator_auto_start.toggled.connect(self._settings_edited)
        self.auto_update.toggled.connect(self._settings_edited)
        self.schedule_enabled.toggled.connect(self._schedule_edited)
        self.schedule_hour.valueChanged.connect(self._schedule_edited)
        self.schedule_minute.valueChanged.connect(self._schedule_edited)
        for control in (self.emulator_path, self.emulator_arguments):
            control.textChanged.connect(self._settings_edited)

    def _settings_edited(self, *_):
        self.settings_changed.emit()

    def _schedule_edited(self, *_):
        time = f"{self.schedule_hour.value():02d}:{self.schedule_minute.value():02d}"
        self.schedule_status.setText(f"已启用：每天 {time}（电脑本地时间）"
                                     if self.schedule_enabled.isChecked() else "每日定时未启用")
        self.settings_changed.emit()

    def _connection_edited(self, *_):
        self._set_test_report(ConnectionTestReport())
        self.screenshot_preview.clear()
        self.screenshot_preview.hide()
        self.connection_feedback.setText("地址或连接选项已修改，请重新测试。")
        self.connection_details_changed.emit()
        self.settings_changed.emit()

    @property
    def last_test_time(self):
        return self.test_report.time if self.test_report.stage is TestStage.PASSED else None

    @property
    def connection_key(self):
        return (self.adb_address.text().strip(), self.adb_path.text().strip(), self.auto_connect.isChecked())

    def _set_test_report(self, report):
        self.test_report = report
        self.connection_test_changed.emit()

    def _add_section(self, title):
        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        content.setObjectName("settingsContent")
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 8)
        body.setSpacing(12)
        scroll.setWidget(content)
        self.sections.addWidget(scroll)
        self.navigation.addItem(title)
        return body

    @property
    def connection_busy(self):
        return self._connection_worker is not None

    def _browse_adb(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 ADB 可执行文件", self.adb_path.text(),
                                             "可执行文件 (*.exe);;所有文件 (*)")
        if path:
            self.adb_path.setText(path)

    def _browse_emulator(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择模拟器启动程序", self.emulator_path.text(),
                                             "可执行文件 (*.exe)")
        if path:
            self.emulator_path.setText(path)

    def _select_device(self, index):
        address = self.detected_devices.itemData(index)
        if address:
            self.adb_address.setText(address)

    def _inspect(self, *, test, force_connect=False):
        if self._running or self.connection_busy or self.dry_run.isChecked():
            return
        candidate = self.read_config([])
        try:
            validate_connection_fields(candidate if test else replace(candidate, adb_address=""), require_address=test)
        except ValueError as exc:
            self._set_test_report(ConnectionTestReport(TestStage.INVALID, details=str(exc)))
            self.screenshot_preview.clear()
            self.screenshot_preview.hide()
            self.connection_feedback.setText(str(exc))
            return
        self._probe_context = self.connection_key
        self._probe_is_test = test
        self._set_test_report(ConnectionTestReport(TestStage.TESTING if test else TestStage.DETECTING))
        self.screenshot_preview.clear()
        self.screenshot_preview.hide()
        self.connection_feedback.setText("正在连接并读取截图…" if test else "正在检测 ADB 设备…")
        if force_connect:
            candidate.auto_connect = True
        worker = ConnectionWorker(candidate, test=test, parent=self)
        self._connection_worker = worker
        worker.result_ready.connect(self._connection_result)
        worker.failed.connect(self._connection_failed)
        worker.finished.connect(self._connection_finished)
        self._update_enabled()
        self.connection_busy_changed.emit(True)
        worker.start()

    def test_connection(self, *, reconnect=False):
        self._inspect(test=True, force_connect=reconnect)

    @Slot(object)
    def _connection_result(self, result):
        if self._probe_context is not None and self.connection_key != self._probe_context:
            self.connection_feedback.setText("连接设置已更改，请重新测试。")
            self._set_test_report(ConnectionTestReport())
            return
        if not self.adb_path.text().strip():
            self.adb_path.setText(result["adb_path"])
        current = self.adb_address.text().strip()
        self.detected_devices.clear()
        self.detected_devices.addItem("请选择设备", "")
        online = []
        for device in result["devices"]:
            status = {"device": "在线", "offline": "离线", "unauthorized": "未授权"}.get(device.state, "状态未知")
            self.detected_devices.addItem(f"{device.serial} · {device.model or '未知型号'} · {status}",
                                          device.serial if device.state == "device" else "")
            if device.state == "device":
                online.append(device.serial)
        if not current and len(online) == 1:
            self.adb_address.setText(online[0])
        if self._probe_context is not None:
            # Resolved ADB paths / a detected sole target are edits made by
            # this result itself, not a stale user change during the probe.
            self._probe_context = self.connection_key
        if "png" in result:
            pixmap = QPixmap()
            if not pixmap.loadFromData(result["png"], "PNG"):
                self._connection_failed("截图无法显示，请重新测试连接。")
                return
            self.screenshot_preview.setPixmap(pixmap.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.screenshot_preview.show()
            checked_at = datetime.now().strftime("%H:%M:%S")
            self._set_test_report(ConnectionTestReport(TestStage.PASSED, checked_at,
                f"{result['address']} · {result['model']} · Android {result['android']}\n"
                f"截图：{result['width']} × {result['height']}；开始任务时会重新检查连接。"))
            self.connection_feedback.setText(
                f"测试通过（{self.last_test_time}）：{result['address']} · {result['model']} · Android {result['android']}\n"
                f"截图：{result['width']} × {result['height']}。开始任务时会重新检查连接。")
        else:
            self._set_test_report(ConnectionTestReport(TestStage.DETECTED,
                details=f"发现 {len(online)} 台在线设备，还未测试截图。"))
            self.connection_feedback.setText(
                f"找到 {len(online)} 台在线设备。选好设备后，点击“连接与截图测试”。" if online else
                "未找到在线设备。请开启模拟器的 ADB 调试，填写地址并勾选“自动连接指定地址”后测试。")

    @Slot(str)
    def _connection_failed(self, message):
        if self._probe_context is not None and self.connection_key != self._probe_context:
            self._set_test_report(ConnectionTestReport())
            self.connection_feedback.setText("连接设置已更改，请重新测试。")
            return
        self.screenshot_preview.clear()
        self.screenshot_preview.hide()
        self._set_test_report(ConnectionTestReport(TestStage.FAILED, datetime.now().strftime("%H:%M:%S"), message))
        operation = "连接与截图测试" if self._probe_is_test else "设备检测"
        self.connection_feedback.setText(f"{operation}失败：{message}")

    @Slot()
    def _connection_finished(self):
        worker = self._connection_worker
        self._connection_worker = None
        self._probe_context = None
        if worker is not None:
            worker.deleteLater()
        self._update_enabled()
        self.connection_busy_changed.emit(False)

    def read_config(self, selected_entries) -> GuiConfig:
        return GuiConfig(
            adb_address=self.adb_address.text().strip(), auto_connect=self.auto_connect.isChecked(),
            remember_selection=self.remember_selection.isChecked(), log_level=self.log_level.currentText(),
            selected_entries=list(selected_entries), dry_run=self.dry_run.isChecked(),
            adb_path=self.adb_path.text().strip(), connection_profile="General",
            continue_on_failure=self.continue_on_failure.isChecked(),
            close_emulator_after_completion=self.close_emulator_after_completion.isChecked(),
            schedule_enabled=self.schedule_enabled.isChecked(),
            schedule_times=f"{self.schedule_hour.value():02d}:{self.schedule_minute.value():02d}",
            emulator_auto_start=self.emulator_auto_start.isChecked(), emulator_path=self.emulator_path.text().strip(),
            emulator_arguments=self.emulator_arguments.text().strip(),
            auto_check_updates=self.auto_update.isChecked(),
        )

    def _update_enabled(self, *_):
        enabled = not self._running and not self.connection_busy
        for control in (self.adb_address, self.adb_path, self.connection_profile, self.browse_adb,
                        self.detected_devices, self.auto_connect, self.dry_run,
                        self.remember_selection, self.continue_on_failure, self.close_emulator_after_completion, self.log_level, self.save_button):
            control.setEnabled(enabled)
        for control in (self.schedule_enabled, self.schedule_hour, self.schedule_minute, self.emulator_auto_start,
                        self.emulator_path, self.emulator_arguments, self.browse_emulator):
            control.setEnabled(enabled)
        for control in (self.detect_button, self.test_button):
            control.setEnabled(enabled and not self.dry_run.isChecked())

    def set_running(self, running: bool):
        self._running = running
        self._update_enabled()
