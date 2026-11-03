"""Qt process environment and click-through helpers."""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("arc_helper")


def prepare_qt_env() -> None:
    """Call before constructing ``QApplication``.

    - Drop KDE's system ``QT_PLUGIN_PATH`` (ABI mismatch with the PySide6 wheel).
    - On Linux Wayland sessions, force the **xcb** platform so overlays can
      position themselves and stay above the game (Phase 0 chosen path).
    """
    os.environ.pop("QT_PLUGIN_PATH", None)
    if sys.platform.startswith("linux"):
        # Prefer xcb under Wayland; leave alone if the user already set a platform.
        if "QT_QPA_PLATFORM" not in os.environ:
            wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or (
                os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            )
            if wayland:
                os.environ["QT_QPA_PLATFORM"] = "xcb"
                logger.info("Qt platform: xcb (XWayland overlay path)")


def apply_overlay_flags(window, *, click_through: bool = True) -> None:
    """Frameless, always-on-top, bypass WM (override-redirect on X11)."""
    from PySide6.QtCore import Qt

    flags = (
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.Tool
        | Qt.WindowType.WindowDoesNotAcceptFocus
    )
    if sys.platform.startswith("linux"):
        flags |= Qt.WindowType.X11BypassWindowManagerHint
    if click_through:
        flags |= Qt.WindowType.WindowTransparentForInput
    window.setWindowFlags(flags)
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, on=True)
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, on=True)
    if click_through:
        window.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, on=True)


def make_click_through(window) -> None:
    """Platform click-through for a realized native window (best-effort)."""
    try:
        window.winId()  # ensure native handle
        if sys.platform == "win32":
            _windows_click_through(int(window.winId()))
        elif sys.platform.startswith("linux"):
            _x11_click_through(int(window.winId()))
    except Exception as e:  # ruff: ignore[blind-except]
        logger.warning("Could not make overlay click-through: %s", e)


def _windows_click_through(hwnd: int) -> None:
    import ctypes

    gwl_exstyle = -20
    ws_ex_layered = 0x00080000
    ws_ex_transparent = 0x00000020
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongPtrW(hwnd, gwl_exstyle)
    user32.SetWindowLongPtrW(hwnd, gwl_exstyle, style | ws_ex_layered | ws_ex_transparent)


def _x11_click_through(xid: int) -> None:
    from Xlib import X
    from Xlib import display as xlib_display
    from Xlib.ext import shape

    xdisplay = xlib_display.Display()
    try:
        if not xdisplay.has_extension("SHAPE"):
            logger.warning("X server lacks SHAPE; overlay will not be click-through")
            return
        root_id = xdisplay.screen().root.id
        xwindow = xdisplay.create_resource_object("window", xid)
        while xwindow.id != root_id:
            xwindow.shape_rectangles(
                shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0, []
            )
            parent = xwindow.query_tree().parent
            if parent is None or parent.id == root_id:
                break
            xwindow = parent
        xdisplay.sync()
    finally:
        xdisplay.close()
