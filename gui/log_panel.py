from pathlib import Path

from PySide6.QtCore import Slot, QUrl
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMenu, QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget

MAX_LOG_LINES = 2000
COLORS = {"INFO": "#394550", "SUCCESS": "#27824a", "WARNING": "#a16a12", "ERROR": "#bb3e3e"}


class LogPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        toolbar.addWidget(QLabel("运行日志"))
        toolbar.addStretch()
        self.latest_button = QToolButton()
        self.latest_button.setText("↓")
        self.latest_button.setAccessibleName("回到最新日志")
        self.latest_button.setFixedSize(26, 28)
        self.latest_button.clicked.connect(self.jump_to_latest)
        toolbar.addWidget(self.latest_button)
        self.more_button = QToolButton()
        self.more_button.setText("...")
        self.more_button.setAccessibleName("日志操作")
        self.more_button.setToolTip("日志操作")
        self.more_button.setFixedSize(26, 28)
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self.menu = QMenu(self.more_button)
        self.copy_all_action = self.menu.addAction("复制全部日志", self.copy_all)
        self.expand_action = self.menu.addAction("展开日志", self.expand_log)
        self.open_log_action = self.menu.addAction("打开详细日志", self.open_detailed_log)
        self.more_button.setMenu(self.menu)
        self.menu.aboutToShow.connect(self._refresh_actions)
        toolbar.addWidget(self.more_button)
        self.clear_button = QPushButton("清空")
        self.clear_button.setToolTip("清空界面日志，保留详细日志文件")
        toolbar.addWidget(self.clear_button)
        layout.addLayout(toolbar)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LOG_LINES)
        self.view.setPlaceholderText("开始任务后，执行记录会显示在这里")
        self.view.setObjectName("runLog")
        layout.addWidget(self.view, 1)
        self._pending_logs = 0
        self._detailed_log_path = None
        self._appending = False
        self._expanded_dialog = None
        self.view.verticalScrollBar().valueChanged.connect(self._update_latest)
        self.view.verticalScrollBar().rangeChanged.connect(self._update_latest)
        self.view.textChanged.connect(self._refresh_actions)
        self.clear_button.clicked.connect(self.view.clear)
        self._update_latest()
        self._refresh_actions()

    def _refresh_actions(self):
        self.copy_all_action.setText("复制全部日志")
        self.copy_all_action.setEnabled(not self.view.document().isEmpty())
        self.open_log_action.setEnabled(bool(self._detailed_log_path and self._detailed_log_path.is_file()))

    def _update_latest(self, *_):
        if self._appending:
            return
        bar = self.view.verticalScrollBar()
        at_latest = bar.value() >= bar.maximum() - 2
        if at_latest:
            self._pending_logs = 0
        self.latest_button.setEnabled(not at_latest)
        self.latest_button.setToolTip(
            f"回到最新日志（新增 {self._pending_logs} 条）" if self._pending_logs
            else "已显示最新日志" if at_latest else "回到最新日志")

    @Slot()
    def jump_to_latest(self):
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._update_latest()

    @Slot()
    def copy_all(self):
        text = self.view.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self.copy_all_action.setText("已复制全部日志")

    def set_detailed_log_path(self, path):
        self._detailed_log_path = Path(path) if path else None
        self.view.setToolTip(f"详细日志：{path}" if path else "")
        self._refresh_actions()

    @Slot()
    def expand_log(self):
        if self._expanded_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("运行日志")
            dialog.resize(720, 480)
            dialog.setMinimumSize(420, 280)
            layout = QVBoxLayout(dialog)
            view = QPlainTextEdit()
            view.setReadOnly(True)
            # Share the bounded document so new messages and clearing stay in
            # sync without a second copy of the log or duplicate subscriptions.
            view.setDocument(self.view.document())
            layout.addWidget(view)
            buttons = QDialogButtonBox()
            buttons.addButton("关闭", QDialogButtonBox.RejectRole)
            buttons.rejected.connect(dialog.close)
            layout.addWidget(buttons)
            self._expanded_dialog = dialog
        self._expanded_dialog.show()
        self._expanded_dialog.raise_()
        self._expanded_dialog.activateWindow()

    @Slot()
    def open_detailed_log(self):
        if self._detailed_log_path and self._detailed_log_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._detailed_log_path.resolve())))

    @Slot(str, str, str)
    def append_log(self, timestamp: str, level: str, message: str):
        bar = self.view.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2
        old_value = bar.value()
        self._appending = True
        cursor = QTextCursor(self.view.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.view.document().isEmpty():
            cursor.insertBlock()
        format = QTextCharFormat()
        format.setForeground(QColor(COLORS.get(level, COLORS["INFO"])))
        # insertText 而不是 HTML；业务原文中的标签保持原样。
        cursor.insertText(f"{timestamp}  [{level}]  {message}", format)
        if follow:
            bar.setValue(bar.maximum())
        else:
            # 手动上滚时保留当前视图；回到底部后自动恢复跟随。
            bar.setValue(old_value)
            self._pending_logs += 1
        self._appending = False
        self._update_latest()

    @Slot(object)
    def append_event(self, event):
        from gui.user_events import UserEvent
        if isinstance(event, UserEvent):
            self.append_log(event.time, event.level, event.text)
