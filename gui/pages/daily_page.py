from __future__ import annotations

from time import monotonic

from PySide6.QtCore import Qt, Signal, Slot, QTimer
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from gui.task_registry import TASKS, REWARDS_ENTRY, REWARD_TASKS, REWARD_ENTRIES, visible_task_entries
from gui.log_panel import LogPanel
from gui.task_status import STATUS_ROLE, TaskRowStatus, TaskStatusDelegate
from gui.user_events import EventType, UserEvent
from gui.config import GuiConfig
from gui.widgets import DropdownComboBox


class StatusDetailsScroll(QScrollArea):
    """Use the content's natural height until the available space is capped."""
    def sizeHint(self):
        hint = super().sizeHint()
        if self.widget() is not None:
            layout = self.widget().layout()
            height = layout.totalHeightForWidth(max(1, self.viewport().width()))
            hint.setHeight(height if height > 0 else layout.sizeHint().height())
        return hint


class DailyPage(QWidget):
    start_requested = Signal()
    stop_requested = Signal()
    retry_failed_requested = Signal()
    resume_requested = Signal()
    connection_settings_requested = Signal()
    reconnect_requested = Signal()
    task_options_changed = Signal()
    selection_changed = Signal()

    def __init__(self, selected_entries, parent=None, *, config=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 16)
        layout.setSpacing(12)
        left = QVBoxLayout()
        self.progress_label = QLabel("每日任务")
        self.progress_label.setObjectName("panelTitle")
        left.addWidget(self.progress_label)
        self.task_list = QListWidget()
        self.task_list.setMinimumWidth(180)
        delegate = TaskStatusDelegate(self.task_list)
        delegate.settings_requested.connect(self._open_task_options)
        self.task_list.setItemDelegate(delegate)
        self._items = {}
        self._queue_active = False
        self._run_entries = set()
        self._result_entries = set()
        self._recovery_busy = False
        self._dry_run = False
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self.task_list.viewport().update)
        self._status_timer.timeout.connect(self._refresh_selected_result)
        self._options_layout_timer = QTimer(self)
        self._options_layout_timer.setSingleShot(True)
        self._options_layout_timer.timeout.connect(self._fit_task_options)
        config = config or GuiConfig()
        selected = set(selected_entries)
        reward_enabled = config.reward_enabled or bool(selected & REWARD_ENTRIES)
        selected.update(task.entry for task, checked in zip(REWARD_TASKS, (config.reward_mail, config.reward_daily)) if checked)
        if reward_enabled:
            selected.add(REWARDS_ENTRY)
        for task in (*sorted(TASKS, key=lambda task: task.order), *REWARD_TASKS):
            item = QListWidgetItem(task.name)
            item.setData(Qt.ItemDataRole.UserRole, task)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if task.entry in selected else Qt.CheckState.Unchecked)
            item.setData(STATUS_ROLE, TaskRowStatus())
            item.setToolTip(task.description)
            # Child records retain per-entry recovery state without list rows.
            if task.entry not in REWARD_ENTRIES:
                self.task_list.addItem(item)
            self._items[task.entry] = item
        self._selection_snapshot = self._checked_snapshot()
        self.task_list.itemChanged.connect(self._selection_edited)
        left.addWidget(self.task_list, 1)
        buttons = QHBoxLayout()
        self.select_all_button = QPushButton("全选")
        self.clear_button = QPushButton("清空")
        buttons.addWidget(self.select_all_button)
        buttons.addWidget(self.clear_button)
        buttons.addStretch()
        left.addLayout(buttons)
        controls = QHBoxLayout()
        self.start_button = QPushButton("开始任务")
        self.start_button.setObjectName("startButton")
        self.start_button.setMinimumHeight(40)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        controls.addWidget(self.start_button, 2)
        controls.addWidget(self.stop_button, 1)
        left.addLayout(controls)
        tasks_panel = QFrame()
        tasks_panel.setObjectName("tasksPanel")
        tasks_panel.setLayout(left)
        left.setContentsMargins(12, 12, 12, 12)
        tasks_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        layout.addWidget(tasks_panel, 1)

        self.parameters_panel = QFrame()
        self.parameters_panel.setObjectName("parametersPanel")
        self.parameters_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        middle = QVBoxLayout(self.parameters_panel)
        middle.setContentsMargins(10, 12, 10, 12)
        self.parameters_title = QLabel("任务设置")
        self.parameters_title.setObjectName("panelTitle")
        middle.addWidget(self.parameters_title)
        self.description_label = QLabel()
        self.description_label.setObjectName("secondaryText")
        self.description_label.setWordWrap(True)
        self.description_label.setTextFormat(Qt.PlainText)
        middle.addWidget(self.description_label)
        self.selected_result_label = QLabel()
        self.selected_result_label.setObjectName("taskResult")
        self.selected_result_label.setWordWrap(True)
        self.selected_result_label.setTextFormat(Qt.PlainText)
        self.selected_result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        middle.addWidget(self.selected_result_label)
        self.editing_hint = QLabel("勾选任务，设置自动保存。")
        self.editing_hint.setObjectName("secondaryText")
        self.editing_hint.setWordWrap(True)
        middle.addWidget(self.editing_hint)
        layout.addWidget(self.parameters_panel, 1)

        status_panel = QFrame()
        status_panel.setObjectName("statusPanel")
        status_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        right = QVBoxLayout(status_panel)
        right.setContentsMargins(12, 12, 12, 12)
        self.status_details_scroll = StatusDetailsScroll()
        self.status_details_scroll.setObjectName("statusDetailsScroll")
        self.status_details_scroll.setFrameShape(QFrame.NoFrame)
        self.status_details_scroll.setWidgetResizable(True)
        self.status_details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.status_details_scroll.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        details = QWidget()
        details.setObjectName("statusDetails")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(8)
        self.status_details_scroll.setWidget(details)
        right.addWidget(self.status_details_scroll)
        title = QLabel("运行状态")
        title.setObjectName("panelTitle")
        right.insertWidget(0, title)
        form = QFormLayout()
        form.setVerticalSpacing(8)
        self.device_label = QLabel()
        self.connection_test_label = QLabel()
        self.runtime_connection_label = QLabel()
        self.current_task_label = QLabel("—")
        self.state_label = QLabel("就绪（离线演练）")
        for label in (self.device_label, self.connection_test_label, self.runtime_connection_label,
                      self.current_task_label, self.state_label):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("目标设备", self.device_label)
        form.addRow("截图测试", self.connection_test_label)
        form.addRow("本次连接", self.runtime_connection_label)
        form.addRow("执行任务", self.current_task_label)
        form.addRow("运行状态", self.state_label)
        details_layout.addLayout(form)
        config = config or GuiConfig()
        self.task_options_panel = QFrame()
        self.task_options_panel.setObjectName("taskOptionsPanel")
        self.task_options_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        options_layout = QVBoxLayout(self.task_options_panel)
        options_layout.setContentsMargins(10, 8, 10, 8)
        options_layout.setSpacing(5)
        self.task_options_stack = QStackedWidget()
        self.task_options_stack.setFixedHeight(78)
        competition_page = QWidget()
        competition_form = QFormLayout(competition_page)
        competition_form.setContentsMargins(0, 0, 0, 0)
        competition_form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.competition_votes = QSpinBox()
        self.competition_votes.setRange(1, 30)
        self.competition_votes.setSuffix(" 票")
        self.competition_votes.setValue(config.competition_votes)
        self.competition_votes.setMaximumWidth(110)
        # Hide native arrows through their subcontrols; NoButtons gives the
        # editor a zero-width rectangle with the Windows stylesheet engine.
        self.competition_votes.setStyleSheet("QSpinBox::up-button, QSpinBox::down-button { width: 0px; }")
        self.competition_votes.setMinimumHeight(32)
        self.vote_minus = QPushButton("−")
        self.vote_plus = QPushButton("+")
        for button in (self.vote_minus, self.vote_plus):
            button.setFixedWidth(30)
            button.setMinimumHeight(32)
            button.setStyleSheet("padding: 0px; font-size: 18px;")
            button.setAutoRepeat(True)
        self.vote_minus.setAccessibleName("减少投票次数")
        self.vote_plus.setAccessibleName("增加投票次数")
        self.vote_minus.clicked.connect(self.competition_votes.stepDown)
        self.vote_plus.clicked.connect(self.competition_votes.stepUp)
        vote_controls = QHBoxLayout()
        vote_controls.setSpacing(4)
        vote_controls.addWidget(self.vote_minus)
        vote_controls.addWidget(self.competition_votes)
        vote_controls.addWidget(self.vote_plus)
        vote_row = QWidget()
        vote_row.setLayout(vote_controls)
        vote_controls.setContentsMargins(0, 0, 0, 0)
        competition_form.addRow("本次投票次数", vote_row)
        self.competition_enter = QCheckBox("我要参赛")
        self.competition_enter.setToolTip("默认使用第一个套装。")
        self.competition_enter.setChecked(config.competition_enter)
        competition_form.addRow(self.competition_enter)
        self.task_options_stack.addWidget(competition_page)
        mystery_page = QWidget()
        mystery_form = QFormLayout(mystery_page)
        mystery_form.setContentsMargins(0, 0, 0, 0)
        mystery_form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.mystery_draw_mode = DropdownComboBox()
        self.mystery_draw_mode.addItem("仅免费抽取", False)
        self.mystery_draw_mode.addItem("免费 + 金币", True)
        self.mystery_draw_mode.setCurrentIndex(1 if config.mystery_allow_coin_draws else 0)
        mystery_form.addRow("谜之屋抽取方式", self.mystery_draw_mode)
        self.mystery_coin_limit = DropdownComboBox()
        self.mystery_coin_limit.addItem("1 次 · 3000 金币", 1)
        self.mystery_coin_limit.addItem("2 次 · 6000 金币", 2)
        self.mystery_coin_limit.setCurrentIndex(config.mystery_max_coin_draws - 1)
        mystery_form.addRow("当日金币抽取上限", self.mystery_coin_limit)
        self.task_options_stack.addWidget(mystery_page)
        stamina_page = QWidget()
        stamina_form = QFormLayout(stamina_page)
        stamina_form.setContentsMargins(0, 0, 0, 0)
        stamina_form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.stamina_max_items = QSpinBox()
        self.stamina_max_items.setRange(1, 999)
        self.stamina_max_items.setValue(config.stamina_max_items)
        self.stamina_max_items.setSuffix(" 件")
        self.stamina_max_items.setMinimumHeight(32)
        self.stamina_max_items.setStyleSheet("QSpinBox::up-button, QSpinBox::down-button { width: 0px; }")
        self.stamina_minus = QPushButton("−")
        self.stamina_plus = QPushButton("+")
        stamina_controls = QHBoxLayout()
        stamina_controls.setContentsMargins(0, 0, 0, 0)
        stamina_controls.setSpacing(4)
        for button, name in ((self.stamina_minus, "减少处理件数"), (self.stamina_plus, "增加处理件数")):
            button.setFixedSize(30, 32)
            button.setStyleSheet("padding: 0px; font-size: 18px;")
            button.setAccessibleName(name)
            button.setAutoRepeat(True)
        self.stamina_minus.clicked.connect(self.stamina_max_items.stepDown)
        self.stamina_plus.clicked.connect(self.stamina_max_items.stepUp)
        stamina_controls.addWidget(self.stamina_minus)
        stamina_controls.addWidget(self.stamina_max_items, 1)
        stamina_controls.addWidget(self.stamina_plus)
        stamina_form.addRow("本次处理件数", stamina_controls)
        self.task_options_stack.addWidget(stamina_page)
        signin_page = QWidget()
        signin_layout = QVBoxLayout(signin_page)
        self.signin_makeup_once = QCheckBox("补签")
        self.signin_makeup_once.setChecked(config.signin_makeup_once)
        signin_layout.addWidget(self.signin_makeup_once)
        signin_tiers = QHBoxLayout()
        signin_tiers.setContentsMargins(20, 0, 0, 0)
        self.signin_makeup_tiers = {}
        for fee, label in ((0, "免费"), (50, "50钻"), (100, "100钻")):
            checkbox = QCheckBox(label)
            checkbox.setChecked(fee in config.signin_makeup_tiers)
            self.signin_makeup_tiers[fee] = checkbox
            signin_tiers.addWidget(checkbox)
        signin_tiers.addStretch()
        signin_layout.addLayout(signin_tiers)
        self.signin_limited_event = QCheckBox("限时活动签到")
        self.signin_limited_event.setChecked(config.signin_limited_event)
        signin_layout.addWidget(self.signin_limited_event)
        self.task_options_stack.addWidget(signin_page)
        rewards_page = QWidget()
        rewards_layout = QVBoxLayout(rewards_page)
        rewards_layout.setContentsMargins(0, 0, 0, 0)
        self.reward_mail = QCheckBox("邮箱")
        self.reward_mail.setChecked("QJNN_Mail" in selected)
        self.reward_daily = QCheckBox("每日任务")
        self.reward_daily.setChecked("QJNN_DailyTaskClaim" in selected)
        rewards_layout.addWidget(self.reward_mail)
        self.mail_claim_friends = QCheckBox("领取好友邮件")
        self.mail_claim_friends.setChecked(config.mail_claim_friends)
        friend_row = QHBoxLayout()
        friend_row.setContentsMargins(20, 0, 0, 0)
        friend_row.addWidget(self.mail_claim_friends)
        rewards_layout.addLayout(friend_row)
        rewards_layout.addWidget(self.reward_daily)
        self.task_options_stack.addWidget(rewards_page)
        library_page = QWidget()
        library_layout = QVBoxLayout(library_page)
        self.alliance_library_three = QCheckBox("整理资料3次")
        self.alliance_library_three.setChecked(config.alliance_library_three)
        library_layout.addWidget(self.alliance_library_three)
        self.task_options_stack.addWidget(library_page)
        options_layout.addWidget(self.task_options_stack)
        self.task_options_note = QLabel()
        self.task_options_note.setObjectName("secondaryText")
        self.task_options_note.setWordWrap(True)
        self.task_options_note.setTextFormat(Qt.PlainText)
        options_layout.addWidget(self.task_options_note)
        middle.addWidget(self.task_options_panel)
        self.no_options_label = QLabel("无需设置。")
        self.no_options_label.setObjectName("secondaryText")
        self.no_options_label.setWordWrap(True)
        middle.addWidget(self.no_options_label)
        middle.addStretch()
        self.task_options_panel.hide()
        self.competition_votes.valueChanged.connect(self._task_options_edited)
        self.competition_enter.toggled.connect(self._task_options_edited)
        self.mystery_draw_mode.currentIndexChanged.connect(self._task_options_edited)
        self.mystery_coin_limit.currentIndexChanged.connect(self._task_options_edited)
        self.stamina_max_items.valueChanged.connect(self._task_options_edited)
        self.signin_makeup_once.toggled.connect(self._task_options_edited)
        self.signin_limited_event.toggled.connect(self._task_options_edited)
        for checkbox in self.signin_makeup_tiers.values():
            checkbox.toggled.connect(self._task_options_edited)
        self.mail_claim_friends.toggled.connect(self._task_options_edited)
        self.reward_mail.toggled.connect(self._task_options_edited)
        self.reward_daily.toggled.connect(self._task_options_edited)
        self.alliance_library_three.toggled.connect(self._task_options_edited)
        self.problem_panel = QFrame()
        self.problem_panel.setObjectName("problemPanel")
        problem_layout = QVBoxLayout(self.problem_panel)
        problem_layout.setContentsMargins(12, 10, 12, 10)
        problem_layout.setSpacing(6)
        self.problem_title = QLabel()
        self.problem_advice = QLabel()
        for label in (self.problem_title, self.problem_advice):
            label.setWordWrap(True)
            label.setTextFormat(Qt.PlainText)
        problem_layout.addWidget(self.problem_title)
        problem_layout.addWidget(self.problem_advice)
        actions = QGridLayout()
        self.retry_failed_button = QPushButton("重试失败项")
        self.resume_button = QPushButton("继续未完成")
        self.reconnect_button = QPushButton("重新连接")
        self.connection_settings_button = QPushButton("连接设置")
        self.retry_failed_button.setToolTip("只重试失败的任务。")
        self.resume_button.setToolTip("继续未完成的任务。")
        for index, button in enumerate((self.retry_failed_button, self.resume_button,
                                         self.reconnect_button, self.connection_settings_button)):
            button.setMinimumHeight(32)
            actions.addWidget(button, index, 0)
        actions.setColumnStretch(0, 1)
        problem_layout.addLayout(actions)
        self.retry_failed_button.clicked.connect(self.retry_failed_requested.emit)
        self.resume_button.clicked.connect(self.resume_requested.emit)
        self.connection_settings_button.clicked.connect(self.connection_settings_requested.emit)
        self.reconnect_button.clicked.connect(self.reconnect_requested.emit)
        self.problem_panel.hide()
        details_layout.addWidget(self.problem_panel)
        self.log_panel = LogPanel()
        right.addWidget(self.log_panel, 1)
        layout.addWidget(status_panel, 1)

        self.select_all_button.clicked.connect(lambda: self.set_all_checked(True))
        self.clear_button.clicked.connect(lambda: self.set_all_checked(False))
        self.start_button.clicked.connect(self.start_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.task_list.currentItemChanged.connect(self._describe)
        self.set_recovery_busy(False)
        self.task_list.setCurrentRow(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.status_details_scroll.setMaximumHeight(max(120, int((self.height() - 56) * 0.54)))
        self._options_layout_timer.start(0)

    def _fit_task_options(self):
        if self.task_options_panel.isHidden():
            return
        width = max(80, self.task_options_panel.width() - 20)
        note_height = (max(24, self.task_options_note.heightForWidth(width))
                       if self.task_options_note.isVisible() else 0)
        self.task_options_panel.setFixedHeight(self.task_options_stack.height() + note_height + 21)

    def _describe(self, item, previous=None):
        if item:
            task = item.data(Qt.ItemDataRole.UserRole)
            self.description_label.setText(task.description)
            self.parameters_title.setText(task.name)
            self._refresh_selected_result()
            pages = {"QJNN_CompetitionDaily": 0, "QJNN_MysteryHouseDaily": 1, "QJNN_StaminaDaily": 2,
                     "QJNN_Signin": 3, REWARDS_ENTRY: 4, "QJNN_AllianceDaily": 5}
            self.task_options_panel.setVisible(task.entry in pages)
            self.no_options_label.setVisible(task.entry not in pages)
            if task.entry in pages:
                self.task_options_stack.setCurrentIndex(pages[task.entry])
                self.task_options_stack.setFixedHeight(110 if pages[task.entry] == 3 else
                    130 if pages[task.entry] in {0, 1, 4} else 78)
                self.refresh_task_options_note()
            else:
                self.no_options_label.setText("无需设置。")

    def _refresh_selected_result(self):
        item = self.task_list.currentItem()
        if item is None:
            self.selected_result_label.hide()
            return
        result = item.data(STATUS_ROLE)
        self.selected_result_label.setVisible(bool(result.state))
        if result.state:
            text = result.text()
            detail = result.skip_reason or result.detail
            if detail:
                text += "\n" + detail
            self.selected_result_label.setText(text)

    def _open_task_options(self, index):
        self.task_list.setCurrentIndex(index)
        self._describe(self.task_list.currentItem())
        if self.task_options_panel.isHidden():
            return
        control = {0: self.competition_votes, 1: self.mystery_draw_mode,
                   2: self.stamina_max_items, 3: self.signin_makeup_once, 4: self.reward_mail, 5: self.alliance_library_three}[self.task_options_stack.currentIndex()]
        control.setFocus()

    def refresh_task_options_note(self, error=None):
        if error:
            self.task_options_note.setText(f"未保存：{error}")
        elif self.task_options_stack.currentIndex() == 0:
            self.task_options_note.setText("周二、周六02:00–10:00暂停（北京时间）。")
        elif self.task_options_stack.currentIndex() == 1:
            limit = self.mystery_coin_limit.currentData()
            policy = (f"金币抽取每天最多{limit}次。" if self.mystery_draw_mode.currentData()
                      else "仅免费抽取。")
            self.task_options_note.setText(policy)
        elif self.task_options_stack.currentIndex() == 2:
            self.task_options_note.setText("不购买体力。")
        elif self.task_options_stack.currentIndex() == 3:
            self.task_options_note.clear()
        elif self.task_options_stack.currentIndex() == 4:
            self.task_options_note.setText(self.reward_selection_error() or "")
        elif self.task_options_stack.currentIndex() == 5:
            self.task_options_note.setText("第2、3次整理会消耗金币。")
        self.task_options_note.setVisible(bool(self.task_options_note.text()))
        self._options_layout_timer.start(0)

    def _task_options_edited(self, *_):
        self.set_recovery_busy(self._recovery_busy)
        self.refresh_task_options_note()
        self.task_options_changed.emit()

    def apply_task_options(self, config):
        config.competition_enter = self.competition_enter.isChecked()
        config.alliance_library_three = self.alliance_library_three.isChecked()
        config.reward_enabled = self._items[REWARDS_ENTRY].checkState() == Qt.CheckState.Checked
        config.reward_mail = self.reward_mail.isChecked()
        config.reward_daily = self.reward_daily.isChecked()
        config.mail_claim_friends = self.mail_claim_friends.isChecked()
        config.signin_makeup_once = self.signin_makeup_once.isChecked()
        config.signin_limited_event = self.signin_limited_event.isChecked()
        config.signin_makeup_tiers = [fee for fee, checkbox in self.signin_makeup_tiers.items() if checkbox.isChecked()]
        config.competition_votes = self.competition_votes.value()
        config.mystery_allow_coin_draws = self.mystery_draw_mode.currentData()
        config.mystery_max_coin_draws = self.mystery_coin_limit.currentData()
        config.stamina_max_items = self.stamina_max_items.value()
        return config

    def _checked_snapshot(self):
        return tuple(entry for entry, item in self._items.items()
                     if entry not in REWARD_ENTRIES and item.checkState() == Qt.CheckState.Checked)

    def selected_entries(self) -> list[str]:
        checked = self._checked_snapshot()
        selected = [entry for entry in checked if entry != REWARDS_ENTRY]
        if REWARDS_ENTRY in checked:
            selected.extend(task.entry for task, control in zip(REWARD_TASKS, (self.reward_mail, self.reward_daily))
                            if control.isChecked())
        return selected

    def reward_selection_error(self):
        if (self._items[REWARDS_ENTRY].checkState() == Qt.CheckState.Checked
                and not (self.reward_mail.isChecked() or self.reward_daily.isChecked())):
            return "请至少选择邮箱或每日任务。"
        return None

    def _selection_edited(self, item):
        selected = self._checked_snapshot()
        if selected != self._selection_snapshot:
            self._selection_snapshot = selected
            self.selection_changed.emit()
            self.refresh_task_options_note()

    def set_all_checked(self, checked: bool):
        if self._queue_active or self._recovery_busy:
            return
        for row in range(self.task_list.count()):
            item = self.task_list.item(row)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def set_running(self, running: bool):
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.select_all_button.setEnabled(not running)
        self.clear_button.setEnabled(not running)
        # Keep selection and scrolling available while the queue runs.
        # Removing only the checkable flag locks both mouse and keyboard edits.
        for item in self._items.values():
            flags = item.flags()
            item.setFlags(flags & ~Qt.ItemIsUserCheckable if running
                          else flags | Qt.ItemIsUserCheckable)
        self.editing_hint.setText("运行中，设置不可修改。" if running
                                  else "勾选任务，设置自动保存。")
        self.set_recovery_busy(running)

    def begin_queue(self, selected_entries, *, dry_run=False, preserve_results=False):
        self._run_entries = set(selected_entries)
        self._result_entries = (self._result_entries | self._run_entries
                                if preserve_results else set(self._run_entries))
        self._queue_active = True
        self._dry_run = dry_run
        self.problem_panel.hide()
        self.status_details_scroll.updateGeometry()
        for entry, item in self._items.items():
            if preserve_results and entry not in self._run_entries:
                continue
            item.setData(STATUS_ROLE, TaskRowStatus(
                state="waiting" if entry in self._run_entries else "", dry_run=dry_run,
            ))
            self._refresh_tooltip(item)
        self._update_progress()
        self._refresh_selected_result()
        self._status_timer.start()

    def _refresh_tooltip(self, item):
        task = item.data(Qt.ItemDataRole.UserRole)
        result = item.data(STATUS_ROLE)
        parts = [task.name, task.description]
        if result.state:
            parts.append(result.text())
        if result.detail:
            parts.append(result.detail)
        if result.dry_run and result.state:
            parts.append("离线演练不会操作游戏。")
        item.setToolTip("\n".join(parts))

    def _update_progress(self):
        self._refresh_reward_status()
        visible_entries = visible_task_entries(self._result_entries)
        finished = sum(self._items[entry].data(STATUS_ROLE).state in {"completed", "skipped"}
                       for entry in visible_entries)
        mode = "离线演练" if self._dry_run else "本次进度"
        self.progress_label.setText(f"{mode} · {finished}/{len(visible_entries)}")

    def _refresh_reward_status(self):
        results = [(task, self._items[task.entry].data(STATUS_ROLE)) for task in REWARD_TASKS
                   if task.entry in self._result_entries]
        result = TaskRowStatus(dry_run=self._dry_run)
        if results:
            states = {child.state for _, child in results}
            result.state = next((state for state in ("failed", "running", "stopped", "not_run", "waiting")
                                 if state in states), "completed")
            result.detail = "\n".join(f"{task.name}：{child.text()}" + (f" · {child.detail}" if child.detail else "")
                                      for task, child in results)
        item = self._items[REWARDS_ENTRY]
        item.setData(STATUS_ROLE, result)
        self._refresh_tooltip(item)

    def entries_with_states(self, states):
        return [entry for entry, item in self._items.items()
                if entry in self._result_entries and item.data(STATUS_ROLE).state in states]

    def set_recovery_busy(self, busy):
        self._recovery_busy = busy
        enabled = not busy and not self._queue_active
        self.competition_votes.setEnabled(enabled)
        self.competition_enter.setEnabled(enabled)
        self.signin_makeup_once.setEnabled(enabled)
        self.signin_limited_event.setEnabled(enabled)
        for checkbox in self.signin_makeup_tiers.values():
            checkbox.setEnabled(enabled and self.signin_makeup_once.isChecked())
        self.reward_mail.setEnabled(enabled)
        self.reward_daily.setEnabled(enabled)
        self.mail_claim_friends.setEnabled(enabled and self.reward_mail.isChecked())
        self.alliance_library_three.setEnabled(enabled)
        self.vote_minus.setEnabled(enabled and self.competition_votes.value() > self.competition_votes.minimum())
        self.vote_plus.setEnabled(enabled and self.competition_votes.value() < self.competition_votes.maximum())
        self.mystery_draw_mode.setEnabled(enabled)
        self.mystery_coin_limit.setEnabled(enabled and self.mystery_draw_mode.currentData())
        self.stamina_max_items.setEnabled(enabled)
        self.stamina_minus.setEnabled(enabled and self.stamina_max_items.value() > 1)
        self.stamina_plus.setEnabled(enabled and self.stamina_max_items.value() < 999)
        for button in (self.retry_failed_button, self.resume_button,
                       self.reconnect_button, self.connection_settings_button):
            button.setEnabled(enabled)

    def show_recovery(self, problem=None, *, outcome="error"):
        failed = self.entries_with_states({"failed"})
        unfinished = self.entries_with_states({"failed", "stopped", "not_run"})
        if problem is None and not unfinished:
            self.problem_panel.hide()
            self.status_details_scroll.updateGeometry()
            return
        title = problem.title if problem else ("仍有任务失败" if failed else
                                               "任务已停止" if outcome == "stopped" else "还有任务未执行")
        advice = problem.advice if problem else "请先回到游戏主页，再继续未完成任务。已完成的不会重跑。"
        failed_count, unfinished_count = len(visible_task_entries(failed)), len(visible_task_entries(unfinished))
        counts = f"失败 {failed_count} 项 · 未完成 {unfinished_count} 项" if unfinished else ""
        self.problem_title.setText(f"{title}　{counts}".strip())
        self.problem_advice.setText(advice)
        self.retry_failed_button.setText(f"重试失败项（{failed_count}）")
        self.resume_button.setText(f"继续未完成（{unfinished_count}）")
        self.retry_failed_button.setVisible(bool(failed))
        self.resume_button.setVisible(bool(unfinished))
        kind = problem.kind if problem else "task"
        self.reconnect_button.setVisible(kind in {"connection", "prepare"})
        self.connection_settings_button.setVisible(kind in {"connection", "prepare", "initialization"})
        self.problem_panel.show()
        self.status_details_scroll.updateGeometry()
        self.set_recovery_busy(self._recovery_busy)
        if kind in {"cleanup", "context"}:
            self.retry_failed_button.setEnabled(False)
            self.resume_button.setEnabled(False)

    @Slot(object)
    def update_task_status(self, event):
        if not self._queue_active or not isinstance(event, UserEvent):
            return
        entry = event.task_entry
        if entry is None:
            entry = next((entry for entry, item in self._items.items()
                          if item.data(Qt.ItemDataRole.UserRole).name == event.task_name), None)
        if entry not in self._run_entries:
            return
        item = self._items[entry]
        result = item.data(STATUS_ROLE)
        if event.kind is EventType.TASK_STARTED and result.state == "waiting":
            result.state = "running"
            result.started_at = monotonic()
            result.detail = event.text
        elif event.kind is EventType.TASK_SKIPPED and result.state == "running":
            # A skip notification can precede a later failure. Only successful
            # task completion confirms the skipped result for the whole task.
            result.skip_reason = event.text
            result.detail = event.text
        elif result.state == "running" and event.kind in {
            EventType.TASK_COMPLETED, EventType.TASK_FAILED, EventType.TASK_STOPPED,
        }:
            result.state = {
                EventType.TASK_COMPLETED: "skipped" if result.skip_reason else "completed",
                EventType.TASK_FAILED: "failed", EventType.TASK_STOPPED: "stopped",
            }[event.kind]
            result.elapsed = event.elapsed_seconds
            if result.elapsed is None and result.started_at is not None:
                result.elapsed = max(0, monotonic() - result.started_at)
            result.detail = result.skip_reason if result.state == "skipped" else event.text
        else:
            return
        item.setData(STATUS_ROLE, result)
        self._refresh_tooltip(item)
        self._update_progress()
        self._refresh_selected_result()

    def finish_queue(self, outcome):
        if not self._queue_active:
            return
        self._queue_active = False
        self._status_timer.stop()
        for entry in self._run_entries:
            item = self._items[entry]
            result = item.data(STATUS_ROLE)
            if result.state == "waiting":
                result.state = "not_run"
                result.detail = "本次运行已结束，此任务未执行。"
            elif result.state == "running":
                result.state = "stopped" if outcome == "stopped" else "failed"
                result.elapsed = max(0, monotonic() - result.started_at)
                result.detail = "任务中断，原因见运行日志。"
            item.setData(STATUS_ROLE, result)
            self._refresh_tooltip(item)
        self._update_progress()
        self._refresh_selected_result()

    @Slot(str, str)
    def show_device(self, address: str, status: str = "未连接"):
        self.device_label.setText(f"{address or '未配置地址'}\n{status}")

    def show_connection_details(self, address, configuration, test_report, runtime):
        self.device_label.setText(f"{address or '未填写地址'}\n{configuration}")
        self.connection_test_label.setText(test_report.text)
        self.connection_test_label.setToolTip(test_report.details or test_report.text)
        self.runtime_connection_label.setText(runtime.text)
        details = runtime.text
        if runtime.actual_address:
            details += f"\n本次设备：{runtime.actual_address}"
        if runtime.connected_at:
            details += f"\n确认连接时间：{runtime.connected_at}"
        self.runtime_connection_label.setToolTip(details)
        for label, tone in ((self.connection_test_label, test_report.tone),
                            (self.runtime_connection_label, runtime.tone)):
            if label.property("connectionTone") != tone:
                label.setProperty("connectionTone", tone)
                label.style().unpolish(label)
                label.style().polish(label)
        self.status_details_scroll.updateGeometry()
