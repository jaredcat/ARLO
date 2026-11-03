"""Status pill (Qt)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget

from arc_helper.config import get_dpi_scale
from arc_helper.overlay_format import BG_DARK
from arc_helper.ui.platform import apply_overlay_flags

if TYPE_CHECKING:
    from collections.abc import Callable


class StatusWindow(QWidget):
    """Small always-on-top status indicator."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        on_quit: Callable[[], None] | None = None,
        on_left_click: Callable[[], None] | None = None,
    ):
        super().__init__(parent)
        self.on_quit = on_quit
        self.on_left_click = on_left_click
        self.dpi_scale = get_dpi_scale()

        # Status pill accepts clicks (left = F8, right = quit).
        apply_overlay_flags(self, click_through=False)
        self.setWindowTitle("ARLO Status")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, on=False)
        self.move(10, 10)
        self._setup_ui()
        self.show()

    def _scale(self, value: int) -> int:
        return max(1, int(value / max(self.dpi_scale, 1.0)))

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            self._scale(10), self._scale(5), self._scale(10), self._scale(5)
        )
        self.setStyleSheet(f"background: {BG_DARK};")
        self.status_label = QLabel("\u25cf Scanning...")
        self.status_label.setFont(QFont("Segoe UI", self._scale(10)))
        self.status_label.setStyleSheet(f"color: #888888; background: {BG_DARK};")
        layout.addWidget(self.status_label)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # ruff: ignore[invalid-function-name]
        # Defer quit/toggle so cleanup is not nested inside the mouse handler
        # (portal join / hotkey stop can block; re-entrancy was crashing).
        if event.button() == Qt.MouseButton.RightButton and self.on_quit:
            QTimer.singleShot(0, self.on_quit)
        elif event.button() == Qt.MouseButton.LeftButton and self.on_left_click:
            QTimer.singleShot(0, self.on_left_click)
        else:
            super().mousePressEvent(event)

    def set_scanning(self) -> None:
        self.status_label.setText("\u25cf Scanning... (F8=filters)")
        self.status_label.setStyleSheet(f"color: #888888; background: {BG_DARK};")

    def set_active(self) -> None:
        self.status_label.setText("\u25cf INVENTORY")
        self.status_label.setStyleSheet(f"color: #32CD32; background: {BG_DARK};")

    def set_map(self) -> None:
        self.status_label.setText("\u25cf MAP (F8=filters)")
        self.status_label.setStyleSheet(f"color: #2DD4BF; background: {BG_DARK};")

    def set_hotkey(self) -> None:
        self.status_label.setText("\u25cf HOTKEY SCAN")
        self.status_label.setStyleSheet(f"color: #FFD700; background: {BG_DARK};")

    def set_error(self, message: str) -> None:
        self.status_label.setText(f"\u25cf {message}")
        self.status_label.setStyleSheet(f"color: #FF4444; background: {BG_DARK};")

    def set_info(self, message: str, *, fg: str = "#5AD4A0") -> None:
        self.status_label.setText(f"\u25cf {message}")
        self.status_label.setStyleSheet(f"color: {fg}; background: {BG_DARK};")

    def hide(self) -> None:
        QWidget.hide(self)

    def show(self) -> None:
        QWidget.show(self)
        self.raise_()
