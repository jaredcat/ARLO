"""Map filter/config window: search, categories, preview of Metaforge pins."""

from __future__ import annotations

import contextlib
import logging
import math
import tkinter as tk
from threading import Thread
from tkinter import ttk
from typing import TYPE_CHECKING

from PIL import Image
from PIL import ImageTk

from .config import DATA_DIR
from .config import get_dpi_scale
from .config import get_settings
from .map_data import CATEGORIES
from .map_data import CATEGORY_COLORS
from .map_data import MAP_DISPLAY_NAMES
from .map_data import MapCatalog
from .map_data import MapMarker
from .map_data import MapMeta
from .map_data import get_map_catalog
from .map_data import latlng_to_norm
from .map_data import marker_label
from .map_data import reference_path
from .map_filter import MapFilterState
from .map_filter import resolve_markers

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("arc_helper")

BG = "#0e1117"
PANEL = "#161b22"
FG = "#e6edf3"
FG_DIM = "#8b949e"
ACCENT = "#58a6ff"
PLAYER_COLOR = "#2dd4bf"
# Query/list caps — canvas drawing is capped separately (Tk ovals are expensive).
MAX_PINS = 2500
DRAW_PIN_CAP = 400
# Companion preview never needs the full stitched PNG (Pendola is 4k+).
DISPLAY_MAX_EDGE = 1536
SEARCH_DEBOUNCE_MS = 120
FILTER_NOTIFY_DEBOUNCE_MS = 250
ZOOM_DEBOUNCE_MS = 50


class MapWindow:
    """Filter/config UI for in-game map pins (F8). Not the primary pin view."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        catalog: MapCatalog | None = None,
        filter_state: MapFilterState | None = None,
        on_sync_request: Callable[[], None] | None = None,
        on_filter_change: Callable[[], None] | None = None,
    ):
        self.root = root
        self.settings = get_settings()
        self.dpi = get_dpi_scale()
        self.catalog = catalog or get_map_catalog()
        self.filter_state = filter_state or MapFilterState()
        self.on_sync_request = on_sync_request
        self.on_filter_change = on_filter_change

        self.window = tk.Toplevel(root)
        self.window.title("ARLO Map filters")
        self.window.configure(bg=BG)
        self.window.geometry("960x720")
        self.window.attributes("-topmost", True)  # ruff: ignore[boolean-positional-value-in-call]
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.window.withdraw()
        self._visible = False
        self._needs_fit = True

        self._map_id = tk.StringVar(value=self.filter_state.map_id)
        self._search = tk.StringVar(value=self.filter_state.search)
        self._category_vars: dict[str, tk.BooleanVar] = {
            c: tk.BooleanVar(value=c in self.filter_state.categories) for c in CATEGORIES
        }
        self._locked_only = tk.BooleanVar(value=self.filter_state.locked_only)
        self._sync_status = tk.StringVar(value="Sync: none")
        self.current_map_id = self.filter_state.map_id

        self._meta: MapMeta | None = None
        self._markers: list[MapMarker] = []
        self._photo: ImageTk.PhotoImage | None = None
        self._photo_size: tuple[int, int] | None = None
        self._base_image: Image.Image | None = None  # display-sized (fast)
        self._canvas_image_id: int | None = None

        # View transform: image coords → canvas
        self._scale = 1.0
        self._offset_x = 0.0
        self._offset_y = 0.0
        self._drag_last: tuple[int, int] | None = None

        self._player: tuple[float, float, float | None] | None = None  # lat,lng,facing
        self._item_filter: str | None = self.filter_state.item_filter
        self._suspend_filter_notify = False
        self._search_after_id: str | None = None
        self._filter_notify_after_id: str | None = None
        self._configure_after_id: str | None = None
        self._zoom_after_id: str | None = None
        # Last canvas position used for pin/player tags (for move-based pan).
        self._pins_view: tuple[float, float, float] | None = None  # ox, oy, scale

        self._build_ui()
        self._load_initial_map()

    def _scale_ui(self, v: int) -> int:
        return max(1, int(v / max(self.dpi, 1.0)))

    def _build_ui(self) -> None:
        top = tk.Frame(self.window, bg=PANEL, padx=8, pady=6)
        top.pack(fill="x")

        maps = []
        if self.catalog:
            maps = [(m.map_id, m.display_name) for m in self.catalog.list_maps()]
        if not maps:
            maps = [(mid, MAP_DISPLAY_NAMES.get(mid, mid)) for mid in MAP_DISPLAY_NAMES]

        tk.Label(top, text="Map", fg=FG_DIM, bg=PANEL).pack(side="left")
        self._map_menu = ttk.Combobox(
            top,
            textvariable=self._map_id,
            values=[m[0] for m in maps],
            width=16,
            state="readonly",
        )
        self._map_menu.pack(side="left", padx=(4, 12))
        self._map_menu.bind("<<ComboboxSelected>>", lambda _e: self.reload_map())

        tk.Label(top, text="Search", fg=FG_DIM, bg=PANEL).pack(side="left")
        search_entry = tk.Entry(
            top,
            textvariable=self._search,
            width=28,
            bg="#0d1117",
            fg=FG,
            insertbackground=FG,
            relief="flat",
        )
        search_entry.pack(side="left", padx=4)
        search_entry.bind("<Return>", lambda _e: self.refresh_markers())
        search_entry.bind("<KeyRelease>", self._on_search_key)
        # Tk on Linux uses emacs-style Ctrl+A (line start); make it Select All.
        search_entry.bind("<Control-a>", self._entry_select_all)
        search_entry.bind("<Control-A>", self._entry_select_all)

        tk.Button(
            top,
            text="Find",
            command=self.refresh_markers,
            bg="#21262d",
            fg=FG,
            relief="flat",
            padx=10,
        ).pack(side="left", padx=4)
        tk.Button(
            top,
            text="Clear",
            command=self._clear_search,
            bg="#21262d",
            fg=FG,
            relief="flat",
            padx=8,
        ).pack(side="left")

        tk.Button(
            top,
            text="Sync now",
            command=self._request_sync,
            bg="#21262d",
            fg=ACCENT,
            relief="flat",
            padx=10,
        ).pack(side="right", padx=4)
        tk.Label(
            top, textvariable=self._sync_status, fg=FG_DIM, bg=PANEL
        ).pack(side="right", padx=8)

        hint = tk.Frame(self.window, bg=PANEL, padx=8, pady=2)
        hint.pack(fill="x")
        tk.Label(
            hint,
            text="Pins draw on the in-game MAP. This window sets search/filters (F8).",
            fg=FG_DIM,
            bg=PANEL,
        ).pack(anchor="w")

        filt = tk.Frame(self.window, bg=PANEL, padx=8, pady=4)
        filt.pack(fill="x")
        for cat in CATEGORIES:
            color = CATEGORY_COLORS.get(cat, FG)
            cb = tk.Checkbutton(
                filt,
                text=cat.title(),
                variable=self._category_vars[cat],
                command=self.refresh_markers,
                bg=PANEL,
                fg=color,
                selectcolor="#0d1117",
                activebackground=PANEL,
                activeforeground=color,
            )
            cb.pack(side="left", padx=4)
        tk.Checkbutton(
            filt,
            text="Locked door",
            variable=self._locked_only,
            command=self.refresh_markers,
            bg=PANEL,
            fg=FG_DIM,
            selectcolor="#0d1117",
            activebackground=PANEL,
        ).pack(side="left", padx=8)

        body = tk.Frame(self.window, bg=BG)
        body.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(body, bg="#010409", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self.canvas.bind("<B1-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_pan_end)
        self.canvas.bind("<MouseWheel>", self._on_wheel)  # Windows
        self.canvas.bind("<Button-4>", lambda e: self._zoom_at(e.x, e.y, 1.15))
        self.canvas.bind("<Button-5>", lambda e: self._zoom_at(e.x, e.y, 1 / 1.15))

        side = tk.Frame(body, bg=PANEL, width=220)
        side.pack(side="right", fill="y")
        side.pack_propagate(flag=False)
        tk.Label(
            side, text="Results", fg=FG, bg=PANEL, font=("Segoe UI", 11, "bold")
        ).pack(anchor="w", padx=8, pady=(8, 4))
        self._results = tk.Listbox(
            side,
            bg="#0d1117",
            fg=FG,
            selectbackground="#388bfd",
            relief="flat",
            activestyle="none",
        )
        self._results.pack(fill="both", expand=True, padx=6, pady=6)
        self._results.bind("<<ListboxSelect>>", self._on_result_select)
        self._count_label = tk.Label(side, text="", fg=FG_DIM, bg=PANEL)
        self._count_label.pack(anchor="w", padx=8, pady=(0, 8))

        self.window.bind("<Escape>", lambda _e: self.hide())

    def _load_initial_map(self) -> None:
        if self.catalog:
            metas = self.catalog.list_maps()
            if metas:
                ids = {m.map_id for m in metas}
                pick = self.filter_state.map_id
                if pick not in ids:
                    pick = "blue-gate" if "blue-gate" in ids else metas[0].map_id
                self._map_id.set(pick)
                self.current_map_id = pick
                self.filter_state.map_id = pick
        self.reload_map()

    def show(self) -> None:
        self.window.deiconify()
        self.window.attributes("-topmost", True)  # ruff: ignore[boolean-positional-value-in-call]
        self.window.lift()
        with contextlib.suppress(tk.TclError):
            self.window.focus_force()
        self._visible = True
        self._needs_fit = True
        self.root.after(50, self._fit_if_needed)
        self._redraw()

    def hide(self) -> None:
        self.window.withdraw()
        self._visible = False

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def is_visible(self) -> bool:
        return self._visible

    def push_filter_state(self) -> None:
        """Copy UI widgets into the shared ``filter_state``."""
        self.filter_state.map_id = self._map_id.get().strip()
        self.filter_state.search = self._search.get().strip()
        self.filter_state.item_filter = self._item_filter
        self.filter_state.categories = {
            c for c, v in self._category_vars.items() if v.get()
        }
        self.filter_state.locked_only = bool(self._locked_only.get())
        self.current_map_id = self.filter_state.map_id

    def reload_map(self) -> None:
        map_id = self._map_id.get().strip()
        self.current_map_id = map_id
        self.filter_state.map_id = map_id
        self._meta = self.catalog.get_meta(map_id) if self.catalog else None
        self._base_image = None
        self._photo = None
        path = reference_path(map_id)
        if not path.is_file() and self._meta and self._meta.reference_rel:
            alt = DATA_DIR / self._meta.reference_rel
            if alt.is_file():
                path = alt
        self._photo = None
        self._photo_size = None
        self._canvas_image_id = None
        if path.is_file():
            try:
                raw = Image.open(path).convert("RGBA")
                self._base_image = self._for_display(raw)
            except OSError as e:
                logger.warning("Could not load map image %s: %s", path, e)
        if self._base_image is None and self._meta is not None:
            w = max(int(self._meta.ref_width or 0), 1024)
            h = max(int(self._meta.ref_height or 0), 1024)
            self._base_image = self._for_display(
                Image.new("RGBA", (w, h), (14, 17, 23, 255))
            )
            logger.info(
                "No basemap image for %s — using blank canvas for pins",
                map_id,
            )
        self._needs_fit = True
        self.refresh_markers()
        self._fit_if_needed()

    @staticmethod
    def _for_display(im: Image.Image) -> Image.Image:
        """Downscale huge basemaps once so pan/zoom/search stay snappy."""
        w, h = im.size
        edge = max(w, h)
        if edge <= DISPLAY_MAX_EDGE:
            return im
        scale = DISPLAY_MAX_EDGE / edge
        return im.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.BILINEAR,
        )

    def _on_canvas_configure(self, event: tk.Event) -> None:
        if event.width <= 80 or event.height <= 80:
            return
        if self._needs_fit:
            self._fit_view()
            self._needs_fit = False
            self._redraw(basemap=True, pins=True)
            return
        # Debounce resize storms — don't re-encode the PNG every pixel.
        if self._configure_after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._configure_after_id)
        self._configure_after_id = self.root.after(
            80, lambda: self._redraw(basemap=True, pins=True)
        )

    def _fit_if_needed(self) -> None:
        self.window.update_idletasks()
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw > 80 and ch > 80:
            self._fit_view()
            self._needs_fit = False
            self._redraw()

    def _fit_view(self) -> None:
        self.window.update_idletasks()
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        if self._base_image:
            iw, ih = self._base_image.size
            self._scale = min(cw / iw, ch / ih) * 0.98
            self._offset_x = (cw - iw * self._scale) / 2
            self._offset_y = (ch - ih * self._scale) / 2
        else:
            self._scale = 1.0
            self._offset_x = 0.0
            self._offset_y = 0.0

    def refresh_markers(self) -> None:
        self.push_filter_state()
        if not self.catalog or not self._map_id.get():
            self._markers = []
            self._fill_results()
            self._redraw(basemap=False, pins=True)
            self._schedule_filter_notify()
            return

        origin = None
        if self._player:
            origin = (self._player[0], self._player[1])

        # Same resolver as in-game (category sample when search is empty);
        # F8 just allows a higher pin cap for the preview list.
        markers = resolve_markers(
            self.catalog,
            self.filter_state,
            origin=origin,
            limit=MAX_PINS,
        )

        self._markers = markers
        self._fill_results()
        # Keep basemap; only refresh dots (no PNG re-encode).
        self._redraw(basemap=False, pins=True)
        self._schedule_filter_notify()
        # Warm Metaforge loose-spot cache off the UI thread when searching.
        needle = (self.filter_state.item_filter or self.filter_state.search or "").strip()
        if needle and self.catalog is not None and not getattr(
            self, "_loose_warm_inflight", False
        ):
            map_id = self._map_id.get().strip()
            self._loose_warm_inflight = True
            Thread(
                target=self._warm_loose_spots,
                args=(map_id, needle),
                daemon=True,
                name="loose-spots",
            ).start()

    def _warm_loose_spots(self, map_id: str, item_name: str) -> None:
        """Background: fetch Metaforge loose spots if missing, then refresh UI."""
        try:
            if not self.catalog:
                return
            changed = self.catalog.ensure_loose_spots(map_id, item_name)
            if changed:
                # Redraw only — avoid re-entering warm + filter-notify storms.
                self.root.after(0, self._apply_loose_spot_refresh)
        except Exception as e:  # ruff: ignore[blind-except]
            logger.debug("Loose-spot warm failed: %s", e)
        finally:
            self._loose_warm_inflight = False

    def _apply_loose_spot_refresh(self) -> None:
        """Reload markers after a background loose-spot fetch (no second warm)."""
        self._loose_warm_inflight = True  # block warm re-entry during this refresh
        try:
            self.push_filter_state()
            if not self.catalog or not self._map_id.get():
                return
            origin = None
            if self._player:
                origin = (self._player[0], self._player[1])
            markers = resolve_markers(
                self.catalog,
                self.filter_state,
                origin=origin,
                limit=MAX_PINS,
            )
            self._markers = markers
            self._fill_results()
            self._redraw(basemap=False, pins=True)
            self._schedule_filter_notify()
        finally:
            self._loose_warm_inflight = False

    def _schedule_filter_notify(self) -> None:
        if self._suspend_filter_notify or not self.on_filter_change:
            return
        if self._filter_notify_after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._filter_notify_after_id)
        self._filter_notify_after_id = self.root.after(
            FILTER_NOTIFY_DEBOUNCE_MS, self._emit_filter_change
        )

    def _emit_filter_change(self) -> None:
        self._filter_notify_after_id = None
        if self._suspend_filter_notify or not self.on_filter_change:
            return
        self.on_filter_change()

    def _fill_results(self) -> None:
        self._results.delete(0, tk.END)
        show_loot = bool(
            (self.filter_state.search or "").strip()
            or (self.filter_state.item_filter or "").strip()
        )
        for m in self._markers[:200]:
            label = marker_label(m, with_loot=show_loot and bool(m.loot_areas))
            dist = f"  ({m.distance:.0f})" if m.distance is not None else ""
            self._results.insert(tk.END, f"{label}{dist}")
        total = len(self._markers)
        extra = " (sampled)" if total >= MAX_PINS else ""
        if total > DRAW_PIN_CAP:
            extra = (extra + f" · drawing {DRAW_PIN_CAP}").strip()
        self._count_label.config(text=f"{total} pins{extra}")

    def _on_result_select(self, _event: object = None) -> None:
        sel = self._results.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if idx >= len(self._markers):
            return
        m = self._markers[idx]
        self.center_on_latlng(m.lat, m.lng)

    def select_map(self, map_id: str) -> None:
        """Switch the companion view to ``map_id`` and reload basemap/pins."""
        mid = (map_id or "").strip()
        if not mid or mid == self.current_map_id:
            return
        self._map_id.set(mid)
        self.reload_map()

    def find_item(self, item_name: str, *, map_id: str | None = None) -> None:
        """Set item filter for in-game pins (optionally open this config window)."""
        if map_id:
            self.select_map(map_id)
        self._item_filter = item_name.strip()
        self._search.set(item_name.strip())
        self.refresh_markers()

    def set_player_position(
        self,
        map_id: str,
        lat: float,
        lng: float,
        *,
        facing: float | None = None,
        status: str = "live",
    ) -> None:
        if map_id and map_id != self._map_id.get():
            self._map_id.set(map_id)
            self.reload_map()
        self._player = (lat, lng, facing)
        self._sync_status.set(f"Sync: {status}")
        self.refresh_markers()
        self.center_on_latlng(lat, lng)

    def set_sync_status(self, status: str) -> None:
        self._sync_status.set(f"Sync: {status}")

    def set_suspend_filter_notify(self, *, suspended: bool) -> None:
        """Suppress ``on_filter_change`` while applying live registration updates."""
        self._suspend_filter_notify = suspended

    def clear_player_position(self, *, status: str = "none") -> None:
        self._player = None
        self._sync_status.set(f"Sync: {status}")
        self.refresh_markers()

    def center_on_latlng(self, lat: float, lng: float) -> None:
        if not self._meta or not self._base_image:
            return
        nx, ny = latlng_to_norm(lat, lng, self._meta)
        iw, ih = self._base_image.size
        ix, iy = nx * iw, ny * ih
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        self._offset_x = cw / 2 - ix * self._scale
        self._offset_y = ch / 2 - iy * self._scale
        self._redraw()

    def _clear_search(self) -> None:
        self._search.set("")
        self._item_filter = None
        if self._search_after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._search_after_id)
            self._search_after_id = None
        self.refresh_markers()

    @staticmethod
    def _entry_select_all(event: object) -> str:
        widget = getattr(event, "widget", None)
        if widget is not None:
            widget.select_range(0, "end")
            widget.icursor("end")
        return "break"

    def _on_search_key(self, _event: object = None) -> None:
        if len(self._search.get().strip()) < 3:
            return
        self._item_filter = None
        if self._search_after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._search_after_id)
        self._search_after_id = self.root.after(
            SEARCH_DEBOUNCE_MS, self._run_debounced_search
        )

    def _run_debounced_search(self) -> None:
        self._search_after_id = None
        self.refresh_markers()

    def _request_sync(self) -> None:
        self._sync_status.set("Sync: capturing…")
        if self.on_sync_request:
            self.on_sync_request()

    # --- canvas transform / drawing ---

    def _img_to_canvas(self, ix: float, iy: float) -> tuple[float, float]:
        return ix * self._scale + self._offset_x, iy * self._scale + self._offset_y

    def _latlng_to_canvas(self, lat: float, lng: float) -> tuple[float, float] | None:
        if not self._meta or not self._base_image:
            return None
        nx, ny = latlng_to_norm(lat, lng, self._meta)
        iw, ih = self._base_image.size
        return self._img_to_canvas(nx * iw, ny * ih)

    def _basemap_draw_size(self) -> tuple[int, int]:
        assert self._base_image is not None
        iw, ih = self._base_image.size
        dw = max(1, int(iw * self._scale))
        dh = max(1, int(ih * self._scale))
        # Cap encoded bitmap size; view can still pan/zoom via scale.
        if dw > 2200 or dh > 2200:
            factor = min(2200 / dw, 2200 / dh)
            dw = max(1, int(dw * factor))
            dh = max(1, int(dh * factor))
        return dw, dh

    def _ensure_basemap_photo(self) -> bool:
        """Resize basemap only when the on-screen pixel size changes."""
        if self._base_image is None:
            return False
        dw, dh = self._basemap_draw_size()
        if self._photo is not None and self._photo_size == (dw, dh):
            return True
        try:
            resized = self._base_image.resize((dw, dh), Image.Resampling.BILINEAR)
            self._photo = ImageTk.PhotoImage(resized)
            self._photo_size = (dw, dh)
        except Exception as e:  # ruff: ignore[blind-except]
            logger.warning("Map basemap encode failed: %s", e)
            return False
        return True

    def _redraw(self, *, basemap: bool = True, pins: bool = True) -> None:
        if self._base_image is None:
            self.canvas.delete("all")
            self._canvas_image_id = None
            self.canvas.create_text(
                20,
                20,
                anchor="nw",
                fill=FG_DIM,
                text="No basemap image cached.\nRun: uv run python update_db.py --maps-only",
            )
            return

        if basemap:
            if not self._ensure_basemap_photo():
                return
            assert self._photo is not None
            if self._canvas_image_id is None:
                self._canvas_image_id = self.canvas.create_image(
                    self._offset_x,
                    self._offset_y,
                    anchor="nw",
                    image=self._photo,
                    tags="basemap",
                )
            else:
                self.canvas.coords(
                    self._canvas_image_id, self._offset_x, self._offset_y
                )
                self.canvas.itemconfigure(self._canvas_image_id, image=self._photo)
            self.canvas.tag_lower("basemap")

        if pins:
            self._draw_pins()

    def _fit_scale(self) -> float:
        if not self._base_image:
            return 1.0
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        iw, ih = self._base_image.size
        return min(cw / iw, ch / ih)

    def _draw_pins(self) -> None:
        """Recreate pin/player canvas items for the current view."""
        self.canvas.delete("pin")
        self.canvas.delete("player")
        r = max(2, int(3 * self._scale / max(self._fit_scale(), 0.01)))
        r = min(r, 8)
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        drawn = 0
        for m in self._markers:
            if drawn >= DRAW_PIN_CAP:
                break
            pt = self._latlng_to_canvas(m.lat, m.lng)
            if pt is None:
                continue
            x, y = pt
            if x < -20 or y < -20 or x > cw + 20 or y > ch + 20:
                continue
            color = CATEGORY_COLORS.get(m.category, "#ffffff")
            self.canvas.create_oval(
                x - r, y - r, x + r, y + r, fill=color, outline="", tags="pin"
            )
            drawn += 1

        if self._player and self._meta:
            pt = self._latlng_to_canvas(self._player[0], self._player[1])
            if pt:
                x, y = pt
                pr = max(6, int(8 * self._scale / max(self._fit_scale(), 0.01)))
                self.canvas.create_oval(
                    x - pr,
                    y - pr,
                    x + pr,
                    y + pr,
                    fill=PLAYER_COLOR,
                    outline="white",
                    width=2,
                    tags="player",
                )
                facing = self._player[2]
                if facing is not None:
                    tip = 14
                    self.canvas.create_line(
                        x,
                        y,
                        x + tip * math.cos(facing),
                        y - tip * math.sin(facing),
                        fill=PLAYER_COLOR,
                        width=2,
                        arrow=tk.LAST,
                        tags="player",
                    )
        self._pins_view = (self._offset_x, self._offset_y, self._scale)

    def _pan_canvas_items(self, dx: float, dy: float) -> None:
        """Translate basemap + pins without recreating anything (browser-like)."""
        if self._canvas_image_id is not None:
            self.canvas.move(self._canvas_image_id, dx, dy)
        self.canvas.move("pin", dx, dy)
        self.canvas.move("player", dx, dy)
        if self._pins_view is not None:
            ox, oy, sc = self._pins_view
            self._pins_view = (ox + dx, oy + dy, sc)

    def _on_pan_start(self, event: tk.Event) -> None:
        self._drag_last = (event.x, event.y)

    def _on_pan_move(self, event: tk.Event) -> None:
        if not self._drag_last:
            return
        dx = event.x - self._drag_last[0]
        dy = event.y - self._drag_last[1]
        self._offset_x += dx
        self._offset_y += dy
        self._drag_last = (event.x, event.y)
        # Pure translate — no Pillow, no oval recreate.
        if self._canvas_image_id is not None and self._pins_view is not None:
            self._pan_canvas_items(dx, dy)
        elif self._canvas_image_id is not None:
            self.canvas.coords(
                self._canvas_image_id, self._offset_x, self._offset_y
            )
            self._draw_pins()
        else:
            self._redraw(basemap=True, pins=True)

    def _on_pan_end(self, _event: tk.Event) -> None:
        self._drag_last = None

    def _on_wheel(self, event: tk.Event) -> None:
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        self._zoom_at(event.x, event.y, factor)

    def _zoom_at(self, cx: float, cy: float, factor: float) -> None:
        old = self._scale
        new = max(0.05, min(8.0, old * factor))
        if abs(new - old) < 1e-6:
            return
        ix = (cx - self._offset_x) / old
        iy = (cy - self._offset_y) / old
        self._scale = new
        self._offset_x = cx - ix * new
        self._offset_y = cy - iy * new
        # Update pin positions cheaply; debounce the expensive PNG re-encode.
        self._draw_pins()
        if self._zoom_after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._zoom_after_id)
        self._zoom_after_id = self.root.after(
            ZOOM_DEBOUNCE_MS, self._commit_zoom_basemap
        )

    def _commit_zoom_basemap(self) -> None:
        self._zoom_after_id = None
        self._redraw(basemap=True, pins=True)
