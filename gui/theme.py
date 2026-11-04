"""Shared light theme, including vector indicators for consistent Qt controls."""
from pathlib import Path

_ICONS = (Path(__file__).parent / "icons").as_posix()

STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif; font-size: 13px; color: #293449; }
QMainWindow, QWidget#appShell { background: #f4f6fa; }
QWidget#appHeader { background: transparent; }
QLabel#brandTitle { font-size: 19px; font-weight: 600; color: #18253c; }
QLabel#brandCaption { font-size: 11px; color: #8892a4; }
QLabel#modeBadge { background: #e8efff; color: #4267cd; border-radius: 10px; padding: 5px 10px; font-size: 11px; }
QTabWidget::pane { border: 0; background: transparent; }
QTabWidget::tab-bar { left: 14px; }
QTabBar::tab { margin: 0 5px 4px 0; padding: 8px 20px; border: 0; border-radius: 9px; background: transparent; color: #7a8599; }
QTabBar::tab:selected { background: #e6edff; color: #4267cd; font-weight: 600; }
QTabBar::tab:hover:!selected { background: #ebeff5; }
QFrame#tasksPanel, QFrame#parametersPanel, QFrame#statusPanel { background: white; border: 1px solid #e9edf4; border-radius: 13px; }
QLabel#panelTitle { font-size: 15px; font-weight: 600; color: #202e44; padding-bottom: 5px; }
QLabel#secondaryText { color: #69778e; font-size: 12px; }
QLabel[connectionTone="success"] { color: #27824a; }
QLabel[connectionTone="error"] { color: #bb3e3e; }
QLabel[connectionTone="muted"] { color: #69778e; }
QLabel#taskResult { background: #f0f4fc; color: #526783; border-radius: 8px; padding: 8px; }
QListWidget { border: 0; background: transparent; outline: 0; }
QListWidget::item { padding: 7px 9px; margin: 2px 0; border-radius: 7px; color: #465167; }
QListWidget::item:selected { background: #eaf0ff; color: #4267cd; }
QListWidget::item:hover:!selected { background: #f4f6fa; }
QListWidget::indicator, QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #cfd7e5; border-radius: 5px; background: white; }
QListWidget::indicator:checked, QCheckBox::indicator:checked { background: #5579e5; border-color: #5579e5; image: url(__ICONS__/check.svg); }
QCheckBox { spacing: 8px; }
QCheckBox::indicator:disabled { border-color: #e0e5ed; background: #f0f3f8; }
QCheckBox::indicator:checked:disabled { background: #a1b3e8; border-color: #a1b3e8; }
QPushButton { background: #f0f3f9; border: 1px solid transparent; border-radius: 8px; padding: 7px 12px; color: #53617a; }
QPushButton:hover { background: #e6ecf8; color: #4267cd; }
QPushButton:pressed { background: #dce5f8; }
QPushButton:focus { border-color: #8ba5ef; }
QPushButton#startButton, QPushButton#saveButton { background: #5579e5; color: white; font-weight: 600; }
QPushButton#startButton { font-size: 14px; }
QPushButton#startButton:hover, QPushButton#saveButton:hover { background: #4567d0; }
QPushButton:disabled { background: #f0f2f6; color: #a5adbb; border-color: transparent; }
QPushButton#startButton:disabled, QPushButton#saveButton:disabled { background: #dde4f3; color: #9aa9c7; }
QToolButton { border: 0; border-radius: 6px; background: #f0f3f9; color: #6f7e97; padding: 0; }
QToolButton:hover { background: #e5ecfb; color: #4267cd; }
QToolButton:disabled { color: #bdc5d2; background: #f7f8fb; }
QLineEdit, QComboBox, QSpinBox, QTimeEdit { border: 1px solid #e3e8f1; border-radius: 8px; padding: 7px 9px; background: #f9fafe; selection-background-color: #5579e5; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTimeEdit:focus { border-color: #8ba5ef; background: white; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QTimeEdit:disabled { background: #f2f4f8; color: #a1aabc; }
QComboBox { padding-right: 25px; }
QComboBox::drop-down { border: 0; width: 24px; }
QComboBox::down-arrow { image: url(__ICONS__/chevron.svg); width: 16px; height: 16px; }
QComboBox QAbstractItemView { background: white; border: 1px solid #e3e8f1; selection-background-color: #eaf0ff; selection-color: #4267cd; outline: 0; padding: 4px; }
QComboBox QAbstractItemView::item { min-height: 26px; padding: 3px 10px; }
QFrame#taskOptionsPanel { background: #f7f9fd; border: 0; border-radius: 10px; }
QFrame#problemPanel { background: #fff7ed; border: 0; border-radius: 10px; }
QScrollArea#statusDetailsScroll, QWidget#statusDetails { background: transparent; border: 0; }
QPlainTextEdit#runLog { background: #f7f9fc; border: 0; border-radius: 9px; padding: 9px; selection-background-color: #dce6ff; selection-color: #293449; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 3px 0; }
QScrollBar::handle:vertical { background: #d4dce8; border-radius: 3px; min-height: 26px; }
QScrollBar::handle:vertical:hover { background: #aebcd2; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QListWidget#settingsNavigation { background: #ebeff6; border-radius: 12px; padding: 7px; }
QListWidget#settingsNavigation::item { padding: 11px 12px; margin: 3px 0; }
QListWidget#settingsNavigation::item:selected { background: white; color: #4267cd; font-weight: 600; }
QStackedWidget#settingsSections, QScrollArea#settingsScroll, QWidget#settingsContent { background: transparent; border: 0; }
QGroupBox { background: white; border: 1px solid #e9edf4; border-radius: 12px; margin-top: 12px; padding: 24px 16px 16px; }
QGroupBox::title { subcontrol-origin: margin; left: 16px; padding: 0 5px; color: #4267cd; font-weight: 600; }
QMenu { background: white; border: 1px solid #e3e8f1; border-radius: 8px; padding: 5px; }
QMenu::item { padding: 7px 16px; border-radius: 5px; }
QMenu::item:selected { background: #eaf0ff; color: #4267cd; }
QMenu::item:disabled { color: #a5adbb; }
QToolTip { background: #293449; color: white; border: 0; padding: 6px 9px; }
""".replace("__ICONS__", _ICONS)
