"""Shared controls with consistent desktop popup placement."""
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QComboBox, QListView, QProxyStyle, QSpinBox, QStyle


class TwoDigitSpinBox(QSpinBox):
    def textFromValue(self, value):
        return f"{value:02d}"


class _DropdownStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ComboBox_Popup:
            # A list below the field, rather than a menu aligned on its value.
            return 0
        return super().styleHint(hint, option, widget, returnData)


class DropdownComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        style = _DropdownStyle("Fusion")
        style.setParent(self)
        self.setStyle(style)
        self.setView(QListView(self))
        self.setMaxVisibleItems(8)

    def showPopup(self):
        super().showPopup()
        popup = self.view().window()
        if not popup.isVisible():
            return
        field = QRect(self.mapToGlobal(QPoint(0, 0)), self.size())
        available = self.screen().availableGeometry()
        gap = 4
        below = max(0, available.bottom() - field.bottom() - gap)
        above = max(0, field.top() - available.top() - gap)
        desired = popup.height()
        place_below = desired <= below or below >= above
        height = min(desired, below if place_below else above)
        width = min(max(self.width(), popup.width()), available.width())
        x = max(available.left(), min(field.left(), available.right() - width + 1))
        y = field.bottom() + gap + 1 if place_below else field.top() - gap - height
        popup.setGeometry(x, y, width, max(1, height))
        self.view().scrollTo(self.view().currentIndex())
