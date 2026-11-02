"""Click-through in-game pin overlay.

Windows / native X11: one PhotoImage window with all dots painted in
(cheap even at high pin counts — this is the “draw then blit” path).

Wayland (incl. XWayland): **disabled by default**. A single chroma window
becomes an opaque black slab; per-pin Toplevels thrash the compositor.
Use the F8 filter map for pins. Opt in experimentally with
``ARLO_WAYLAND_INGAME_PINS=1`` (pooled micro-windows, no X11 SHAPE).
"""

from __future__ import annotations

import logging
import os
import sys
import tkinter as tk
from dataclasses import dataclass

from PIL import Image
from PIL import ImageDraw
from PIL import ImageTk

from .clickthrough import make_click_through
from .config import get_dpi_scale
from .map_data import CATEGORY_COLORS
from .screen import phys_to_tk

logger = logging.getLogger("arc_helper")

CHROMA_RGB = (1, 4, 9)
CHROMA = "#010409"
PIN_R_PHYS = 7

_IS_WIN = sys.platform == "win32"
# Experimental: force micro-window pins on Wayland (usually a bad idea).
_WAYLAND_FORCE = os.environ.get("ARLO_WAYLAND_INGAME_PINS", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


def _is_wayland() -> bool:
    try:
        from .screen import get_backend

        return get_backend().name == "wayland"
    except Exception:  # noqa: BLE001
        return bool(os.environ.get("WAYLAND_DISPLAY"))


@dataclass
class OverlayPin:
    """One pin in viewport-local physical pixels (relative to viewport origin)."""

    x: float
    y: float
    category: str
    label: str = ""


@dataclass
class _MicroPin:
    win: tk.Toplevel
    canvas: tk.Canvas
    oval: int


class InGameMapPins:
    """Click-through pin dots over the in-game map viewport."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.dpi = get_dpi_scale()
        self._mapped = False
        self._last_pin_key: tuple | None = None
        self._micro: list[_MicroPin] = []
        self._cluster: tk.Toplevel | None = None
        self._cluster_label: tk.Label | None = None
        self._cluster_photo: ImageTk.PhotoImage | None = None
        self._cluster_click_through = False
        self._warned_skip = False
        self._wayland = _is_wayland()
        # Single-image blit: Windows always; Linux only off Wayland.
        self._use_cluster = _IS_WIN or not self._wayland

        if self._use_cluster:
            self._cluster = self._make_toplevel("ARLO Map Pins")
            self._cluster_label = tk.Label(
                self._cluster, bg=CHROMA, borderwidth=0, highlightthickness=0
            )
            self._cluster_label.pack(fill="both", expand=True)
            try:
                self._cluster.wm_attributes("-transparentcolor", CHROMA)
            except tk.TclError:
                logger.debug("transparentcolor unavailable")

        if self._wayland and not _WAYLAND_FORCE:
            logger.info(
                "In-game map pins disabled on Wayland (F8 only). "
                "Native X11/Windows use a single-image overlay; "
                "set ARLO_WAYLAND_INGAME_PINS=1 to force micro-windows."
            )

    @property
    def ingame_enabled(self) -> bool:
        """False on Wayland unless ``ARLO_WAYLAND_INGAME_PINS=1``."""
        if self._wayland:
            return _WAYLAND_FORCE
        return True

    def _make_toplevel(self, title: str) -> tk.Toplevel:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.92 if self._use_cluster else 1.0)
        win.configure(bg=CHROMA)
        win.withdraw()
        return win

    def _pin_size(self) -> tuple[int, int]:
        r = max(3, int(PIN_R_PHYS / max(self.dpi, 1.0)))
        return r, r * 2 + 2

    def hide(self) -> None:
        """Withdraw pins immediately (e.g. map started panning). Keep the pool."""
        if self._use_cluster:
            if self._cluster is not None and self._mapped:
                try:
                    self._cluster.withdraw()
                except tk.TclError:
                    pass
        else:
            for mp in self._micro:
                try:
                    mp.win.withdraw()
                except tk.TclError:
                    pass
        self._mapped = False
        self._last_pin_key = None

    def update(
        self,
        *,
        viewport_bbox: tuple[int, int, int, int],
        pins: list[OverlayPin],
    ) -> None:
        """Draw ``pins`` (viewport-local). Empty list clears the overlay."""
        if not self.ingame_enabled:
            if pins and not self._warned_skip:
                logger.info(
                    "In-game map pins skipped on Wayland — use F8. "
                    "(ARLO_WAYLAND_INGAME_PINS=1 to force)"
                )
                self._warned_skip = True
            self.hide()
            return

        if not pins:
            self.hide()
            return

        left, top, _right, _bottom = viewport_bbox
        screen_pts: list[tuple[float, float, OverlayPin]] = [
            (left + p.x, top + p.y, p) for p in pins
        ]
        pin_key = tuple(
            (round(sx), round(sy), p.category) for sx, sy, p in screen_pts
        )
        if self._mapped and pin_key == self._last_pin_key:
            return
        self._last_pin_key = pin_key

        if self._use_cluster:
            self._update_cluster(screen_pts)
        else:
            self._update_micro(screen_pts)

    def _ensure_micro_pool(self, n: int) -> None:
        _r, size = self._pin_size()
        while len(self._micro) < n:
            win = self._make_toplevel("ARLO Pin")
            canvas = tk.Canvas(
                win,
                width=size,
                height=size,
                bg=CHROMA,
                highlightthickness=0,
                borderwidth=0,
            )
            canvas.pack()
            oval = canvas.create_oval(
                1, 1, size - 1, size - 1, fill="#ffffff", outline="#111111"
            )
            self._micro.append(_MicroPin(win=win, canvas=canvas, oval=oval))
        while len(self._micro) > max(n, 8) and len(self._micro) > n:
            mp = self._micro.pop()
            try:
                mp.win.destroy()
            except tk.TclError:
                pass

    def _update_micro(
        self, screen_pts: list[tuple[float, float, OverlayPin]]
    ) -> None:
        self._ensure_micro_pool(len(screen_pts))
        _r, size = self._pin_size()
        for i, (sx, sy, pin) in enumerate(screen_pts):
            mp = self._micro[i]
            color = CATEGORY_COLORS.get(pin.category, "#ffffff")
            try:
                mp.canvas.itemconfigure(mp.oval, fill=color)
                mp.canvas.configure(bg=color)
                tk_x = phys_to_tk(sx - PIN_R_PHYS)
                tk_y = phys_to_tk(sy - PIN_R_PHYS)
                mp.win.geometry(f"{size}x{size}+{tk_x}+{tk_y}")
                if not mp.win.winfo_viewable():
                    mp.win.deiconify()
                mp.win.lift()
                mp.win.attributes("-topmost", True)
            except tk.TclError:
                continue
        for mp in self._micro[len(screen_pts) :]:
            try:
                mp.win.withdraw()
            except tk.TclError:
                pass
        self._mapped = True

    def _update_cluster(
        self, screen_pts: list[tuple[float, float, OverlayPin]]
    ) -> None:
        assert self._cluster is not None and self._cluster_label is not None
        pad = 12
        xs = [s[0] for s in screen_pts]
        ys = [s[1] for s in screen_pts]
        min_x = min(xs) - pad
        min_y = min(ys) - pad
        max_x = max(xs) + pad
        max_y = max(ys) + pad
        phys_w = max(1, int(max_x - min_x))
        phys_h = max(1, int(max_y - min_y))
        tk_x = phys_to_tk(min_x)
        tk_y = phys_to_tk(min_y)
        tk_w = max(1, phys_to_tk(phys_w))
        tk_h = max(1, phys_to_tk(phys_h))
        r = max(3, int(PIN_R_PHYS / max(self.dpi, 1.0)))

        img = Image.new("RGB", (tk_w, tk_h), CHROMA_RGB)
        draw = ImageDraw.Draw(img)
        sx_s = tk_w / max(1, phys_w)
        sy_s = tk_h / max(1, phys_h)
        for screen_x, screen_y, pin in screen_pts:
            x = (screen_x - min_x) * sx_s
            y = (screen_y - min_y) * sy_s
            color = CATEGORY_COLORS.get(pin.category, "#ffffff")
            draw.ellipse(
                (x - r, y - r, x + r, y + r), fill=color, outline="#111111"
            )

        self._cluster_photo = ImageTk.PhotoImage(img)
        try:
            self._cluster.geometry(f"{tk_w}x{tk_h}+{tk_x}+{tk_y}")
            self._cluster_label.configure(image=self._cluster_photo, bg=CHROMA)
            if not self._cluster.winfo_viewable():
                self._cluster.deiconify()
            self._cluster.lift()
            self._mapped = True
            if not self._cluster_click_through:
                try:
                    make_click_through(self._cluster)
                    self._cluster_click_through = True
                except Exception as e:  # noqa: BLE001
                    logger.warning("Map pins click-through failed: %s", e)
        except tk.TclError as e:
            logger.debug("Pin cluster update failed: %s", e)
            self._mapped = False
