"""Local Windows/Qt tray messages; no network or game-controller access."""
import logging
from PySide6.QtWidgets import QSystemTrayIcon, QStyle
from gui.branding import GUI_LOGGER_NAME, WINDOW_TITLE


def duration_text(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return (f"{hours}小时{minutes}分{seconds}秒" if hours else
            f"{minutes}分{seconds}秒" if minutes else f"{seconds}秒")


class DesktopNotifier:
    def __init__(self, window):
        self.window = window
        self.tray = None

    def show(self, title, message, *, error=False):
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable() or not QSystemTrayIcon.supportsMessages():
                return False
            if self.tray is None:
                icon = self.window.windowIcon()
                if icon.isNull():
                    icon = self.window.style().standardIcon(QStyle.SP_ComputerIcon)
                self.tray = QSystemTrayIcon(icon, self.window)
                self.tray.setToolTip(WINDOW_TITLE)
                self.tray.messageClicked.connect(self.activate)
                self.tray.show()
            kind = QSystemTrayIcon.Warning if error else QSystemTrayIcon.Information
            self.tray.showMessage(title, message, kind, 10000)
            return True
        except Exception:
            logging.getLogger(GUI_LOGGER_NAME).exception("桌面通知发送失败")
            return False  # Notification availability must never affect the queue.

    def activate(self):
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
