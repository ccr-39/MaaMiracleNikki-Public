"""Compact task-result rendering; task names and checkbox data stay unchanged."""
from dataclasses import dataclass
from time import monotonic
from math import cos, sin, pi

from PySide6.QtCore import Qt, QRect, Signal, QEvent, QPointF
from PySide6.QtGui import QColor, QFontMetrics, QPen, QPainter, QPolygonF
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem


STATUS_ROLE = Qt.ItemDataRole.UserRole + 1
LABELS = {"waiting": "等待", "running": "执行中", "completed": "完成",
          "skipped": "跳过", "failed": "失败", "stopped": "已停止", "not_run": "未执行"}
COLORS = {"waiting": "#7d8893", "running": "#1686c7", "completed": "#27824a",
          "skipped": "#a16a12", "failed": "#bb3e3e", "stopped": "#a16a12", "not_run": "#7d8893"}


@dataclass
class TaskRowStatus:
    state: str = ""
    elapsed: float | None = None
    started_at: float | None = None
    detail: str = ""
    skip_reason: str = ""
    dry_run: bool = False

    def text(self):
        if not self.state:
            return ""
        label = LABELS[self.state]
        if self.dry_run:
            label = {"waiting": "待演练", "running": "演练中", "completed": "演练完成",
                     "failed": "演练失败"}.get(self.state, label)
        if self.state == "running" and self.started_at is not None:
            return f"{label} {max(0, monotonic() - self.started_at):.0f}秒"
        if self.elapsed is not None:
            return f"{label} {self.elapsed:.1f}秒"
        return label


class TaskStatusDelegate(QStyledItemDelegate):
    settings_requested = Signal(object)
    def has_settings(self, index):
        return bool(getattr(index.data(Qt.UserRole), "entry", None))

    @staticmethod
    def gear_rect(rect):
        return QRect(rect.right() - 27, rect.y(), 26, rect.height())

    def paint(self, painter, option, index):
        if not self.has_settings(index):
            return self._paint_content(painter, option, index)
        self._paint_content(painter, option, index)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        selected = bool(option.state & QStyle.State_Selected)
        painter.translate(self.gear_rect(option.rect).center())
        painter.setPen(QPen(QColor("#4267cd" if selected else "#9aa6ba"), 1.3))
        outline = QPolygonF()
        for tooth in range(8):
            for offset, radius in ((0, 6), (0.15, 8), (0.55, 8), (0.7, 6)):
                angle = (tooth + offset) * pi / 4
                outline.append(QPointF(radius * cos(angle), radius * sin(angle)))
        painter.drawPolygon(outline)
        painter.drawEllipse(QRect(-2, -2, 4, 4))
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if (self.has_settings(index) and event.type() == QEvent.MouseButtonRelease
                and event.button() == Qt.LeftButton
                and self.gear_rect(option.rect).contains(event.position().toPoint())):
            self.settings_requested.emit(index)
            return True
        return super().editorEvent(event, model, option, index)

    def _paint_content(self, painter, option, index):
        result = index.data(STATUS_ROLE)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        name = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        painter.save()
        painter.setClipRect(opt.rect)
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        area = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget)
        if self.has_settings(index):
            area.setRight(min(area.right(), self.gear_rect(opt.rect).left() - 6))
        status_font = painter.font()
        status_font.setPixelSize(12)
        status = result.text() if isinstance(result, TaskRowStatus) else ""
        status_width = QFontMetrics(status_font).horizontalAdvance(status) + 4 if status else 0
        name_rect = QRect(area.x(), area.y(), max(0, area.width() - status_width - 8), area.height())
        painter.setFont(opt.font)
        painter.setPen(QColor("#4267cd" if opt.state & QStyle.State_Selected else "#465167"))
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         opt.fontMetrics.elidedText(name, Qt.ElideRight, name_rect.width()))
        if status:
            painter.setFont(status_font)
            painter.setPen(QColor(COLORS[result.state]))
            painter.drawText(area, Qt.AlignRight | Qt.AlignVCenter, status)
        painter.restore()
