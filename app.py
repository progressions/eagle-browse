#!/usr/bin/env python3
"""Eagle Browse — keyboard-first read-only Eagle.cool library picker for Omarchy."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk  # noqa: E402

from filters import (  # noqa: E402
    RATING_OP_EQ,
    RATING_OP_GTE,
    RATING_OP_LTE,
    RATING_OP_SYMBOLS,
    ViewFilters,
    format_filter_date,
    item_matches_view_filters,
    parse_filter_date,
    rating_chip_label,
)
from config import DEFAULT_LIBRARY, inbox_path  # noqa: E402
from pixbuf_io import pixbuf_from_path as _pixbuf_from_path  # noqa: E402
from library import (  # noqa: E402
    EagleLibrary,
    Item,
    SmartFolder,
    eval_smart_conditions,
)
from sets import (  # noqa: E402
    SET_PREFIX,
    is_set_tag,
    mint_set_tag,
    set_tag_of,
    set_tags_of,
)
from sounds import mark_gui_running, mark_gui_stopped, play_sound  # noqa: E402
from upscale_queue import (  # noqa: E402
    UpscaleResult,
    already_reason,
    post_upscale,
)
from write import INBOX_SIGNAL_FILENAME  # noqa: E402

APP_ID = "cool.eagle.Browse"
THUMB_SIZE_DEFAULT = 160  # square cell edge (Eagle-style uniform tiles)
THUMB_SIZE_MIN = 72
THUMB_SIZE_MAX = 360
THUMB_SIZE_STEP = 24
VIEWER_ZOOM_STEP = 1.08  # 8% of the current on-screen size per notch
VIEWER_ZOOM_MAX = 8.0  # cap vs native pixels
VIEWER_ZOOM_COOLDOWN_S = 0.12
PAGE_CHUNK = 500  # first page + each infinite-scroll increment
BULK_EDIT_CONFIRM = 100  # confirm tag/folder writes above this many items
SEARCH_DEBOUNCE_MS = 150
G_PREFIX_TIMEOUT_MS = 800
# Staging handoff (copy out of library — never writes into .library)
DEFAULT_STAGE_DIR = Path.home() / "Dropbox/ISAAC/GENNIE/Eunbi/outbox"
# Sidebar expand/selection — survives close / crash
_UI_STATE_PATH = Path.home() / ".config" / "eagle-browse" / "ui-state.json"
# Grid sort options: (id, label) — applied after folder/smart/search filters
SORT_OPTIONS: list[tuple[str, str]] = [
    ("added_desc", "Added · newest"),
    ("added_asc", "Added · oldest"),
    ("mtime_desc", "Modified · newest"),
    ("mtime_asc", "Modified · oldest"),
    ("name_asc", "Name · A–Z"),
    ("name_desc", "Name · Z–A"),
    ("size_desc", "Size · largest"),
    ("size_asc", "Size · smallest"),
    ("rating_desc", "Rating · high"),
    ("rating_asc", "Rating · low"),
    ("duration_desc", "Duration · long"),
    ("duration_asc", "Duration · short"),
]
SORT_IDS = [s[0] for s in SORT_OPTIONS]
SORT_LABELS = [s[1] for s in SORT_OPTIONS]
NAV_HISTORY_MAX = 80
_OMARCHY_COLORS_PATH = (
    Path.home() / ".local" / "state" / "omarchy" / "current" / "theme" / "colors.toml"
)


def _omarchy_colors() -> dict[str, str]:
    """Selection colors from the active Omarchy theme, with GTK-safe fallbacks."""
    fallback = {
        "accent": "#3584e4",
        "selection": "#1c71d8",
        "background": "#1e1e1e",
        "bright_foreground": "#ffffff",
    }
    try:
        loaded = tomllib.loads(_OMARCHY_COLORS_PATH.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return fallback
    for key in fallback:
        value = loaded.get(key)
        if (
            isinstance(value, str)
            and len(value) in (4, 7)
            and value.startswith("#")
            and all(c in "0123456789abcdefABCDEF" for c in value[1:])
        ):
            fallback[key] = value
    return fallback


class _ViewLoc(NamedTuple):
    """One place in the app: sidebar scope plus optional inline viewer item."""

    smart_id: str | None
    folder_id: str | None
    special: str | None
    set_tag: str | None
    descendants: bool
    viewer_id: str | None


def _cell_w(thumb: int) -> int:
    return thumb + 12


def _cell_h(thumb: int) -> int:
    return thumb + 36


def _spawn_detached(cmd: list[str], env: dict[str, str] | None = None) -> bool:
    """Start a helper process that is not a child of this app.

    Popen without start_new_session leaves zombies (mpv after a UI sound or
    an external player) because this process never wait()s.
    """
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        return True
    except (FileNotFoundError, OSError):
        return False

# Decode thumbs off the UI thread; textures are applied on the main loop.
_thumb_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="eagle-thumb")
# cache_key → Gdk.Texture (main thread writes; lock for inflight + cache)
_thumb_textures: dict[str, Gdk.Texture] = {}
_THUMB_CACHE_MAX = 400
# Keys currently decoding — skip a second submit for the same key.
_thumb_inflight: set[str] = set()
# Waiters for an in-flight key: (list_item, gen)
_thumb_waiters: dict[str, list[tuple[object, int]]] = {}
_thumb_lock = threading.Lock()


class ItemObject(GObject.Object):
    def __init__(self, item: Item):
        super().__init__()
        self.item = item


def _center_crop_square(pixbuf: GdkPixbuf.Pixbuf, size: int) -> GdkPixbuf.Pixbuf:
    """Center-crop to square, then scale to size×size."""
    w, h = pixbuf.get_width(), pixbuf.get_height()
    if w <= 0 or h <= 0:
        return pixbuf
    side = min(w, h)
    x = (w - side) // 2
    y = (h - side) // 2
    if side != w or side != h:
        pixbuf = pixbuf.new_subpixbuf(x, y, side, side)
    if side != size:
        pixbuf = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
    return pixbuf


def _decode_square_pixbuf(path: str, size: int = THUMB_SIZE_DEFAULT) -> GdkPixbuf.Pixbuf | None:
    """Worker-thread: decode + crop. Do not touch GTK widgets here."""
    pixbuf = _pixbuf_from_path(path, size * 2, size * 2)
    if pixbuf is None:
        return None
    return _center_crop_square(pixbuf, size)


def _thumb_cache_key(path: str, size: int) -> str:
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = 0.0
    return f"{path}@{size}@{mt:.3f}"


def _fmt_grid_duration(seconds: float) -> str:
    """Compact clock for a thumb overlay: 0:12, 3:05, 1:02:05."""
    total = int(round(max(0.0, float(seconds))))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _type_badge(item: Item) -> str:
    dur = ""
    if (item.is_video or item.is_audio) and item.duration:
        dur = _fmt_grid_duration(item.duration)
    if item.is_video:
        return f"▶ {dur}" if dur else "▶"
    if item.is_audio:
        return f"♪ {dur}" if dur else "♪"
    if not item.is_image:
        return item.ext.upper()[:4] if item.ext else "·"
    return ""


def _thumb_path_for(item: Item) -> str | None:
    # Paths resolved at library load — don't stat() on every bind (Dropbox latency)
    if item.thumb is not None:
        return str(item.thumb)
    if item.is_image:
        return str(item.path)
    return None


class EagleBrowseWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, library: EagleLibrary):
        # Default under a typical Omarchy scrolling-layout column (~1230px
        # on Ginger at 1.5x). A larger default plus GridView min_columns=4
        # made the window wider than the column and clipped the inspector.
        super().__init__(application=app, title="Eagle Browse", default_width=1200, default_height=800)
        self.set_size_request(720, 400)
        mark_gui_running()
        self.library = library
        self.current_folder_id: str | None = None
        self.current_smart_folder_id: str | None = None
        # Virtual views: None | "untagged" | "uncategorized" | "set"
        self._special_view: str | None = None
        self._set_view_tag: str | None = None
        self._set_counts: dict[str, int] = {}
        self._set_counts_ready = False
        self._nav_back: list[_ViewLoc] = []
        self._nav_forward: list[_ViewLoc] = []
        self._nav_restoring = False
        self.include_descendants = True
        self.selected_item: Item | None = None
        self._items: list[Item] = []  # prefix currently in the GridView store
        self._all_items: list[Item] = []  # full sorted query result
        self._loading_more = False
        self._restoring_scroll = False
        self._scroll_restore_gen = 0
        self._scroll_restore_source = 0
        self._saved_grid_scroll: dict[str, Any] | None = None
        self._filter_text = ""
        # View filters: tags/folders/types include+exclude, dimensions, duration, stars
        self._view_filters = ViewFilters()
        # Multi-selection (item ids) — Shift range / Ctrl add / Space toggle
        # Used for path copy, tags, folders, rate, stage
        self._marked: set[str] = set()
        self._sel_anchor: int = 0  # index for Shift-range selection
        # Empty-grid click cleared the selection; don't restore it on focus
        self._keep_grid_unselected = False
        self._stage_dir = Path(
            os.environ.get("EAGLE_STAGE_DIR", str(DEFAULT_STAGE_DIR))
        ).expanduser()
        self._inbox_dir = inbox_path()
        # Smart-folder ids that are expanded. Empty = all top levels collapsed.
        self._smart_expanded: set[str] = set()
        # Sidebar section headers (Smart folders / Folders)
        self._folders_section_expanded = False  # collapsible; start closed
        self._left_sidebar_open = True
        self._right_sidebar_open = True
        self._grid_has_focus = False
        self._last_focus_idx = 0
        # Forced grid column count (min_columns == max_columns). Arrow-down
        # must step by exactly this many items or selection drifts diagonally.
        # Start at 1 so the window can shrink into a scrolling-layout column;
        # _sync_columns raises this to whatever actually fits.
        self._cols = 1
        self._left_pane_w = 280
        self._insp_pane_w = 260
        self._query_gen = 0  # bump to cancel stale background queries
        self._search_timeout_id = 0
        self._cols_sync_timeout_id = 0
        self._inbox_importing = False
        self._scope_text = "all"
        self._thumb_size = THUMB_SIZE_DEFAULT
        # Sort key id from SORT_OPTIONS (default: newest added first)
        self._sort_key = "added_desc"
        # Smart-folder id → last known item count for sidebar "(N)" labels
        self._smart_counts: dict[str, int] = {}
        # "untagged" / "uncategorized" → last known item count for sidebar
        self._special_counts: dict[str, int] = {}
        # While tag/folder/type pickers are open, ignore main-window hotkeys
        self._picker_blocking = False
        # Non-modal dialogs often do not get keyboard focus on Hyprland, so
        # Esc is handled here against this reference.
        self._open_dialog: Gtk.Window | None = None
        self._handling_escape = False
        # Vim-style lowercase-g prefix (gg / gs / gr). The saved context
        # prevents a pending prefix from leaking across focus or view changes.
        self._g_prefix_source = 0
        self._g_prefix_context: tuple[Any, ...] | None = None
        # Ignore the row-selected that follows a viewer-open sidebar click
        self._sidebar_nav_lock = False
        # Soft-delete undo: each entry is a batch of item ids (newest last)
        self._delete_undo_stack: list[list[str]] = []
        self._sf_menu: Gtk.Popover | None = None
        self._sf_editor = None
        self._sf_drop_row: Gtk.ListBoxRow | None = None
        self._frame_saving = False
        self._trim_exporting = False
        self._viewer_in: float | None = None
        self._viewer_out: float | None = None
        # Inbox signal: watcher writes this after each new import
        self._inbox_signal_path = self.library.root / INBOX_SIGNAL_FILENAME
        self._inbox_signal_ts = 0.0
        # id → (tries, next_retry_unix). Incomplete Dropbox .info folders retry
        # with backoff instead of every 1s forever.
        self._pending_imports: dict[str, tuple[int, float]] = {}
        self._inbox_poll_id = 0
        self._inbox_monitor: Gio.FileMonitor | None = None
        self._inbox_images_mtime = 0.0
        self._images_scan_running = False
        self._images_scan_again = False
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)
        self._library_ready = False
        # Super+W (killactive) / window close must quit the process, not leave
        # a headless instance with a ThreadPoolExecutor alive.
        self.connect("close-request", self._on_window_close_request)

        self._build_ui()
        self._install_keybinds()
        self._populate_sidebar()
        # Do NOT start an inbox poller here. Auto-import is eagle-inbox-watch
        # only (one machine). Opening the GUI on multiple hosts must not race
        # the watcher or each other over the intake folder. Manual import: key `i`.
        self._start_library_load()


    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._toast_overlay.set_child(root)

        header = Adw.HeaderBar()
        root.append(header)

        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav.add_css_class("linked")
        self.nav_back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self.nav_back_btn.set_tooltip_text("Back (Alt+←)")
        self.nav_back_btn.set_sensitive(False)
        self.nav_back_btn.connect("clicked", lambda *_: self.nav_back())
        self.nav_fwd_btn = Gtk.Button(icon_name="go-next-symbolic")
        self.nav_fwd_btn.set_tooltip_text("Forward (Alt+→)")
        self.nav_fwd_btn.set_sensitive(False)
        self.nav_fwd_btn.connect("clicked", lambda *_: self.nav_forward())
        nav.append(self.nav_back_btn)
        nav.append(self.nav_fwd_btn)
        header.pack_start(nav)

        self.search = Gtk.SearchEntry(placeholder_text="Search name, tags, folders, id…  (/)")
        self.search.set_hexpand(True)
        self.search.set_sensitive(False)
        self.search.connect("search-changed", self._on_search_changed)
        # SearchEntry binds Esc to stop-search and can swallow it before
        # window key-pressed. Close the viewer from that signal too.
        self.search.connect("stop-search", self._on_search_escape)
        header.set_title_widget(self.search)

        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload library (r)")
        reload_btn.connect("clicked", lambda *_: self.reload_library())
        header.pack_end(reload_btn)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_vexpand(True)
        root.append(body)

        # Left sidebar: folders (collapsible pane).
        # Pin width on a Box wrapper — GTK4 ScrolledWindow ignores max-width CSS,
        # so a bare scrolled window can grow or shrink and clip children on HiDPI.
        LEFT_W = self._left_pane_w
        self.left_sidebar = Gtk.ScrolledWindow()
        self.left_sidebar.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.left_sidebar.set_hexpand(True)
        self.left_sidebar.set_vexpand(True)
        self.left_sidebar.add_css_class("sidebar")
        self.left_sidebar.add_css_class("left-sidebar")

        self.folder_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.folder_list.add_css_class("navigation-sidebar")
        self.folder_list.connect("row-selected", self._on_sidebar_selected)
        self._install_sidebar_dnd_css()
        sidebar_click = Gtk.GestureClick()
        sidebar_click.set_button(1)
        sidebar_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        sidebar_click.connect("pressed", self._on_sidebar_pressed)
        self.folder_list.add_controller(sidebar_click)
        self.left_sidebar.set_child(self.folder_list)

        left_wrap = self._fixed_width_pane(LEFT_W, "left-sidebar-wrap")
        left_wrap.append(self.left_sidebar)
        body.append(left_wrap)

        # Main: filter bar + grid + status
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main.set_hexpand(True)
        main.set_vexpand(True)
        main.add_css_class("eagle-main")
        body.append(main)

        # ── View filter bar ───────────────────────────────────────────
        filter_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        filter_bar.add_css_class("toolbar")
        filter_bar.set_margin_start(8)
        filter_bar.set_margin_end(8)
        filter_bar.set_margin_top(6)
        filter_bar.set_margin_bottom(2)

        filter_btns = Gtk.FlowBox()
        filter_btns.set_selection_mode(Gtk.SelectionMode.NONE)
        filter_btns.set_homogeneous(False)
        filter_btns.set_hexpand(True)
        filter_btns.set_halign(Gtk.Align.FILL)
        filter_btns.set_valign(Gtk.Align.START)
        filter_btns.set_min_children_per_line(1)
        filter_btns.set_max_children_per_line(20)
        filter_btns.set_column_spacing(6)
        filter_btns.set_row_spacing(2)
        filter_btns.set_can_focus(False)
        filter_btns.add_css_class("eagle-filter-btns")

        for label, handler in (
            ("Tags", self.open_view_tag_filter),
            ("Folders", self.open_view_folder_filter),
            ("Type", self.open_view_type_filter),
            ("Stars", self.open_star_filter),
            ("Size", self.open_dimension_filter),
            ("Duration", self.open_duration_filter),
            ("Dates", self.open_date_filter),
        ):
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b, h=handler: h())
            filter_btns.append(btn)

        clear_btn = Gtk.Button(label="Clear filters")
        clear_btn.add_css_class("flat")
        clear_btn.connect("clicked", lambda *_: self.clear_view_filters())
        filter_btns.append(clear_btn)

        # Sort dropdown (top bar)
        sort_lbl = Gtk.Label(label="Sort")
        sort_lbl.add_css_class("dim-label")
        sort_lbl.set_margin_start(8)
        filter_btns.append(sort_lbl)
        sort_model = Gtk.StringList.new(SORT_LABELS)
        self.sort_dropdown = Gtk.DropDown(model=sort_model)
        self.sort_dropdown.set_selected(0)
        self.sort_dropdown.set_tooltip_text("Sort items in the current view")
        self.sort_dropdown.connect("notify::selected", self._on_sort_changed)
        filter_btns.append(self.sort_dropdown)
        filter_bar.append(filter_btns)

        self.filter_chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.filter_chips.set_hexpand(True)
        chip_scroll = Gtk.ScrolledWindow()
        try:
            chip_h = Gtk.PolicyType.EXTERNAL
        except AttributeError:
            chip_h = Gtk.PolicyType.NEVER
        chip_scroll.set_policy(chip_h, Gtk.PolicyType.NEVER)
        chip_scroll.set_child(self.filter_chips)
        chip_scroll.set_size_request(-1, 28)
        try:
            chip_scroll.set_propagate_natural_width(False)
        except AttributeError:
            pass
        filter_bar.append(chip_scroll)
        main.append(filter_bar)

        mid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        mid.set_vexpand(True)
        mid.set_hexpand(True)
        main.append(mid)

        # Center column: grid OR inline image viewer (fills space between sidebars)
        grid_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        grid_col.set_hexpand(True)
        grid_col.set_vexpand(True)
        mid.append(grid_col)

        self.center_stack = Gtk.Stack()
        self.center_stack.set_hexpand(True)
        self.center_stack.set_vexpand(True)
        self.center_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.center_stack.set_transition_duration(120)
        grid_col.append(self.center_stack)

        self.grid_scroll = Gtk.ScrolledWindow()
        self.grid_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.grid_scroll.set_vexpand(True)
        self.grid_scroll.set_hexpand(True)
        self.center_stack.add_named(self.grid_scroll, "grid")

        # Inline viewer (Eagle-style detail pane) — stills + in-frame video.
        # Audio still opens in mpv (no playhead we need). Video stays in-process
        # so Space/p can read the current timestamp.
        self._viewer_open = False
        self._viewer_item_id: str | None = None
        self._viewer_audio_proc: subprocess.Popen[bytes] | None = None
        self._viewer_mute_tries = 0
        self._viewer_playing_handler = 0
        self._viewer_playing_stream = None
        self._viewer_audio_ignore_playing = False
        self._viewer_mode: str = "image"  # "image" | "video" | "compare"
        self._viewer_fit = True  # True = contain; False = scaled
        self._viewer_scale: float | None = None  # None = fit pane; else × native
        self._viewer_zoom_steps = 0  # 0 = fit; each notch is +1
        self._viewer_pane: tuple[int, int] | None = None
        self._viewer_src_pixbuf: GdkPixbuf.Pixbuf | None = None
        self._compare_a_id: str | None = None
        self._compare_b_id: str | None = None
        self._compare_a_pixbuf: GdkPixbuf.Pixbuf | None = None
        self._compare_b_pixbuf: GdkPixbuf.Pixbuf | None = None
        self._compare_split = 0.5
        self._compare_dragging = False
        self._compare_drag_x0 = 0.0
        self._viewer_zoom_last = 0.0
        self._viewer_drag_h0 = 0.0
        self._viewer_drag_v0 = 0.0
        self._viewer_lock_center = False
        self._viewer_center_wh: tuple[int, int] | None = None
        self._viewer_setting_adj = False
        viewer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        viewer.set_hexpand(True)
        viewer.set_vexpand(True)
        viewer.add_css_class("view")

        vbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        vbar.add_css_class("toolbar")
        vbar.set_margin_start(8)
        vbar.set_margin_end(8)
        vbar.set_margin_top(4)
        vbar.set_margin_bottom(4)
        self.viewer_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.viewer_title.add_css_class("heading")
        vbar.append(self.viewer_title)
        self.viewer_hint = Gtk.Label(label="")
        self.viewer_hint.add_css_class("dim-label")
        self.viewer_hint.add_css_class("caption")
        self.viewer_hint.set_ellipsize(3)
        vbar.append(self.viewer_hint)

        def _tool_icon(icon: str, tooltip: str, handler) -> Gtk.Button:
            btn = Gtk.Button(icon_name=icon)
            btn.add_css_class("flat")
            btn.set_tooltip_text(tooltip)
            btn.connect("clicked", lambda *_: handler())
            return btn

        self.crop_btn = _tool_icon(
            "image-crop-symbolic", "Crop (x)", self.open_crop_dialog
        )
        self.crop_btn.set_sensitive(False)
        self.upscale_btn = _tool_icon(
            "view-fullscreen-symbolic", "Upscale", self.queue_upscale
        )
        self.upscale_btn.set_sensitive(False)
        self.viewer_zoom_out_btn = _tool_icon(
            "zoom-out-symbolic", "Zoom out (scroll / −)", lambda: self.viewer_toggle_zoom(larger=False)
        )
        self.viewer_zoom_in_btn = _tool_icon(
            "zoom-in-symbolic", "Zoom in (scroll / +)", lambda: self.viewer_toggle_zoom(larger=True)
        )
        self.viewer_save_frame_btn = _tool_icon(
            "camera-photo-symbolic",
            "Save current frame (p)",
            self.save_viewer_frame,
        )
        self.viewer_save_frame_btn.set_visible(False)
        self.viewer_trim_btn = _tool_icon(
            "edit-cut-symbolic",
            "Cut marked range (x)",
            self._export_viewer_trim,
        )
        self.viewer_trim_btn.set_visible(False)
        self.crop_916_btn = Gtk.Button(label="9:16")
        self.crop_916_btn.add_css_class("flat")
        self.crop_916_btn.set_tooltip_text(
            "Center-crop this video to 9:16 as a new item"
        )
        self.crop_916_btn.set_sensitive(False)
        self.crop_916_btn.set_visible(False)
        self.crop_916_btn.connect("clicked", lambda *_: self.crop_selected_videos_916())
        self.viewer_prev_btn = _tool_icon(
            "go-previous-symbolic", "Previous (←)", lambda: self.viewer_navigate(-1)
        )
        self.viewer_next_btn = _tool_icon(
            "go-next-symbolic", "Next (→)", lambda: self.viewer_navigate(1)
        )
        self.viewer_close_btn = _tool_icon(
            "window-close-symbolic", "Close (Esc)", self.close_inline_viewer
        )
        for b in (
            self.crop_btn,
            self.upscale_btn,
            self.viewer_zoom_out_btn,
            self.viewer_zoom_in_btn,
            self.viewer_save_frame_btn,
            self.viewer_trim_btn,
            self.crop_916_btn,
            self.viewer_prev_btn,
            self.viewer_next_btn,
            self.viewer_close_btn,
        ):
            vbar.append(b)
        viewer.append(vbar)

        # Stack: still image (DrawingArea) vs video (Gtk.Video with controls)
        self.viewer_body = Gtk.Stack()
        self.viewer_body.set_hexpand(True)
        self.viewer_body.set_vexpand(True)
        self.viewer_body.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.viewer_body.set_transition_duration(80)

        self.viewer_scroll = Gtk.ScrolledWindow()
        self.viewer_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.viewer_scroll.set_hexpand(True)
        self.viewer_scroll.set_vexpand(True)
        try:
            self.viewer_scroll.set_propagate_natural_width(False)
            self.viewer_scroll.set_propagate_natural_height(False)
        except AttributeError:
            pass
        # DrawingArea reports the zoom size as its natural size, so the
        # ScrolledWindow can pan. One source pixbuf is drawn scaled — no
        # replace-paintable / delayed-recenter jump each notch.
        self.viewer_picture = Gtk.DrawingArea()
        self.viewer_picture.set_hexpand(True)
        self.viewer_picture.set_vexpand(True)
        self.viewer_picture.set_halign(Gtk.Align.CENTER)
        self.viewer_picture.set_valign(Gtk.Align.CENTER)
        self.viewer_picture.set_can_focus(True)
        self.viewer_picture.set_draw_func(self._on_viewer_draw, None)
        self.viewer_scroll.set_child(self.viewer_picture)
        hadj = self.viewer_scroll.get_hadjustment()
        vadj = self.viewer_scroll.get_vadjustment()
        if hadj is not None:
            hadj.connect("changed", self._on_viewer_adj_changed)
            hadj.connect("value-changed", self._on_viewer_adj_value)
        if vadj is not None:
            vadj.connect("changed", self._on_viewer_adj_changed)
            vadj.connect("value-changed", self._on_viewer_adj_value)
        self.viewer_scroll.connect("notify::width", self._on_viewer_scroll_resized)
        self.viewer_scroll.connect("notify::height", self._on_viewer_scroll_resized)
        # Wheel zooms the still; capture so the ScrolledWindow does not pan instead
        for host in (self.viewer_scroll, self.viewer_picture):
            vz = Gtk.EventControllerScroll()
            vz.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
            vz.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            vz.connect("scroll", self._on_viewer_scroll)
            host.add_controller(vz)
            vdrag = Gtk.GestureDrag()
            vdrag.set_button(1)
            vdrag.connect("drag-begin", self._on_viewer_drag_begin)
            vdrag.connect("drag-update", self._on_viewer_drag_update)
            vdrag.connect("drag-end", self._on_viewer_drag_end)
            host.add_controller(vdrag)

        self.viewer_body.add_named(self.viewer_scroll, "image")

        self.viewer_video = Gtk.Video()
        self.viewer_video.set_autoplay(True)
        self.viewer_video.set_loop(False)
        self.viewer_video.set_hexpand(True)
        self.viewer_video.set_vexpand(True)
        self.viewer_video.set_halign(Gtk.Align.FILL)
        self.viewer_video.set_valign(Gtk.Align.FILL)
        self.viewer_body.add_named(self.viewer_video, "video")
        viewer.append(self.viewer_body)
        # Gtk.Video (and its seek bar) can eat Esc before the window handler.
        for host in (viewer, self.viewer_picture, self.viewer_video):
            v_esc = Gtk.EventControllerKey()
            v_esc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            v_esc.connect("key-pressed", self._on_viewer_escape)
            host.add_controller(v_esc)

        # Double-click still image to close (video uses its own controls)
        vclick = Gtk.GestureClick()
        vclick.set_button(1)

        def on_viewer_click(_g, n_press: int, _x, _y) -> None:
            if n_press == 2 and self._viewer_mode in ("image", "compare"):
                self.close_inline_viewer()

        vclick.connect("pressed", on_viewer_click)
        self.viewer_picture.add_controller(vclick)

        self.center_stack.add_named(viewer, "viewer")

        load_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        load_box.set_halign(Gtk.Align.CENTER)
        load_box.set_valign(Gtk.Align.CENTER)
        load_box.set_hexpand(True)
        load_box.set_vexpand(True)
        load_spin = Gtk.Spinner()
        load_spin.set_spinning(True)
        load_spin.set_size_request(36, 36)
        load_title = Gtk.Label(label="Loading library…")
        load_title.add_css_class("title-4")
        load_hint = Gtk.Label(label="Reading items from disk")
        load_hint.add_css_class("dim-label")
        load_box.append(load_spin)
        load_box.append(load_title)
        load_box.append(load_hint)
        self.center_stack.add_named(load_box, "loading")
        self.center_stack.set_visible_child_name("loading")

        self.store = Gio.ListStore(item_type=ItemObject)
        self.selection = Gtk.SingleSelection(model=self.store)
        self.selection.set_can_unselect(True)
        self.selection.set_autoselect(False)
        self.selection.connect("notify::selected-item", self._on_grid_selection)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)
        factory.connect("unbind", self._on_factory_unbind)
        # Bound list-item widgets (mark overlays without store rebuild)
        self._live_list_items: set[Gtk.ListItem] = set()

        self.grid = Gtk.GridView(
            model=self.selection,
            factory=factory,
            max_columns=16,
            min_columns=1,
            single_click_activate=False,
            enable_rubberband=False,
        )
        self.grid.set_vexpand(True)
        self.grid.set_hexpand(True)
        self.grid.connect("activate", self._on_grid_activate)
        self.grid_scroll.set_child(self.grid)
        vadj = self.grid_scroll.get_vadjustment()
        if vadj is not None:
            vadj.connect("value-changed", self._on_grid_scrolled)
            vadj.connect("changed", self._on_grid_scrolled)
        # Debounced column sync only on real resize — never on every arrow key.
        # Hyprland tile/fullscreen changes the Gdk.Surface size; Gtk.Window
        # default-width often does not notify, so thumbs stay at the old
        # column count and look crushed or sparse.
        self.grid.connect("map", lambda *_: self._schedule_column_sync())
        self.grid_scroll.connect("notify::width", lambda *_: self._schedule_column_sync())
        self.connect("notify::default-width", lambda *_: self._schedule_column_sync())
        self.connect("notify::width", lambda *_: self._schedule_column_sync())
        self.connect("realize", self._hook_surface_resize)
        if self.get_realized():
            self._hook_surface_resize()
        GLib.idle_add(self._sync_columns)
        self._install_shrink_css()

        # Only show blue selection highlight when the grid actually has focus
        grid_focus = Gtk.EventControllerFocus()
        grid_focus.connect("enter", lambda *_: self._set_grid_focus(True))
        grid_focus.connect("leave", lambda *_: self._set_grid_focus(False))
        self.grid.add_controller(grid_focus)
        # Also track clicks that focus the grid
        self.grid.connect("notify::has-focus", self._on_grid_has_focus_notify)

        # Click empty grid space (no thumb) → drop the current selection.
        # On the scrolled window as well: when the last row doesn't fill the
        # viewport, that leftover area is the viewport, not the GridView.
        grid_bg_click = Gtk.GestureClick()
        grid_bg_click.set_button(1)
        grid_bg_click.connect("pressed", self._on_grid_background_pressed)
        self.grid.add_controller(grid_bg_click)
        scroll_bg_click = Gtk.GestureClick()
        scroll_bg_click.set_button(1)
        scroll_bg_click.connect("pressed", self._on_grid_background_pressed)
        self.grid_scroll.add_controller(scroll_bg_click)

        # Right: inspector (thumbnail, rating, tags, folders)
        self.inspector_sidebar = self._build_inspector()
        mid.append(self.inspector_sidebar)

        # Status + hints span full window width (under both sidebars)
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        status.add_css_class("toolbar")
        status.set_hexpand(True)
        status.set_margin_start(10)
        status.set_margin_end(10)
        status.set_margin_top(4)
        status.set_margin_bottom(2)

        self.status_left = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.status_left.add_css_class("dim-label")
        status.append(self.status_left)

        self.status_path = Gtk.Label(xalign=1, ellipsize=3)
        self.status_path.add_css_class("dim-label")
        self.status_path.set_selectable(True)
        status.append(self.status_path)
        root.append(status)

        hints = Gtk.Label(
            label=(
                "Enter open (image inline · video/audio mpv) · Esc close viewer · "
                "i/o video marks · x cut · p save frame · Shift+E add to editor · Ctrl+Shift+E new editor project · t tags · f folders · g group · G ungroup · Ctrl+A all · Del · Ctrl+Z · Super+W"
            ),
            xalign=0,
        )
        hints.add_css_class("dim-label")
        hints.set_margin_start(10)
        hints.set_margin_end(10)
        hints.set_margin_bottom(8)
        hints.set_wrap(True)
        hints.set_hexpand(True)
        root.append(hints)
        self.status_left.set_text("Loading library…")

    def _start_library_load(self) -> None:
        """Scan the library off the UI thread so the window can appear first."""

        def work() -> None:
            try:
                self.library.load()
                err = None
            except Exception as exc:  # noqa: BLE001
                err = exc

            def apply() -> bool:
                if err is not None:
                    self._on_library_load_failed(err)
                    return False
                self._library_ready = True
                self.search.set_sensitive(True)
                if self.center_stack.get_visible_child_name() == "loading":
                    self.center_stack.set_visible_child_name("grid")
                self._load_sidebar_state()
                self._rebuild_set_counts(force=True)
                self._populate_sidebar(select_current=True)
                self.refresh_items()
                self._start_inbox_watch()
                self._start_duration_backfill()
                return False

            GLib.idle_add(apply)

        threading.Thread(target=work, name="eagle-library-load", daemon=True).start()

    def _start_duration_backfill(self) -> None:
        """ffprobe audio/video that Eagle stored without duration; then refresh."""

        def work() -> None:
            try:
                written = self.library.backfill_missing_durations()
            except Exception:  # noqa: BLE001
                written = []

            def apply() -> bool:
                if written:
                    self.refresh_items(reset_selection=False, scroll_to_top=False)
                return False

            GLib.idle_add(apply)

        threading.Thread(
            target=work, name="eagle-duration-fill", daemon=True
        ).start()

    def _on_library_load_failed(self, exc: BaseException) -> None:
        self.status_left.set_text("Library failed to load")
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Could not open Eagle library",
            body=str(exc),
        )
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.connect("response", lambda d, *_: d.close())
        dialog.present()

    def _fixed_width_pane(self, width: int, css_class: str) -> Gtk.Box:
        """Box with a hard width. GTK4 ScrolledWindow does not honor max-width."""
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrap.set_hexpand(False)
        wrap.set_vexpand(True)
        wrap.set_size_request(width, -1)
        wrap.add_css_class(css_class)
        try:
            wrap.set_overflow(Gtk.Overflow.HIDDEN)
        except (AttributeError, TypeError):
            pass
        css = Gtk.CssProvider()
        # GTK4 theme parser on this stack rejects max-width; pin via size_request.
        css.load_from_data(
            f"""
            box.{css_class} {{
                min-width: {width}px;
            }}
            """.encode()
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        return wrap

    def _install_shrink_css(self) -> None:
        """Let the center column shrink so a scrolling-layout tile can clip us.

        Without min-width: 0, the filter button row + GridView min_columns
        report a natural width wider than the column and Hyprland cuts the
        inspector off the right edge.
        """
        colors = _omarchy_colors()
        css = Gtk.CssProvider()
        css.load_from_data(
            f"""
            box.eagle-main {{
                min-width: 0;
            }}
            flowbox.eagle-filter-btns {{
                min-width: 0;
            }}
            flowbox.eagle-filter-btns > flowboxchild {{
                padding: 0;
            }}
            gridview {{
                min-width: 0;
            }}
            box.asset-selected {{
                background-color: {colors["selection"]};
                border-radius: 9px;
                box-shadow: 0 0 0 3px {colors["accent"]};
            }}
            box.asset-selection-outline {{
                border: 4px solid {colors["bright_foreground"]};
                border-radius: 7px;
                box-shadow: inset 0 0 0 1px alpha({colors["background"]}, 0.75),
                            0 0 5px 1px alpha({colors["accent"]}, 0.95);
            }}
            label.asset-selection-mark {{
                min-width: 28px;
                min-height: 28px;
                padding: 0;
                border-radius: 999px;
                color: {colors["background"]};
                background-color: {colors["bright_foreground"]};
                box-shadow: 0 1px 5px alpha(black, 0.80);
            }}
            """
            .encode()
        )
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _install_sidebar_dnd_css(self) -> None:
        css = Gtk.CssProvider()
        css.load_from_data(
            b"""
            row.drop-before {
                box-shadow: inset 0 2px 0 @accent_bg_color;
            }
            row.drop-after {
                box-shadow: inset 0 -2px 0 @accent_bg_color;
            }
            """
        )
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _build_inspector(self) -> Gtk.Widget:
        """Right sidebar: preview + rating + tags + folders for selection."""
        # Hard-pin the pane width on a Box (ScrolledWindow cannot take max-width).
        # Keep the preview under the pane width so HiDPI / fractional-scale clip
        # on the window's right edge cannot hide the thumbnail.
        # Compact edit controls sit next to section titles (left side), not as
        # a second "Edit" row and not flush against the clipped right edge.
        INSPECTOR_WIDTH = self._insp_pane_w
        SIDE_PAD = 12
        PREVIEW = 180
        self._insp_preview_px = PREVIEW

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        try:
            scroll.set_overlay_scrolling(True)
        except AttributeError:
            pass
        try:
            scroll.set_propagate_natural_width(False)
        except AttributeError:
            pass
        scroll.add_css_class("inspector-sidebar")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(SIDE_PAD)
        box.set_margin_end(SIDE_PAD)
        box.set_hexpand(True)
        box.set_halign(Gtk.Align.FILL)
        scroll.set_child(box)

        def _narrow_label(lbl: Gtk.Label, *, chars: int = 20) -> Gtk.Label:
            lbl.set_wrap(True)
            lbl.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            lbl.set_max_width_chars(chars)
            lbl.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
            lbl.set_hexpand(True)
            lbl.set_xalign(0.0)
            return lbl

        def _icon_btn(
            *,
            tooltip: str,
            on_click,
            icon_name: str = "document-edit-symbolic",
            fallback: str = "✎",
        ) -> Gtk.Button:
            """Compact flat icon button next to a section title (left-aligned)."""
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("circular")
            btn.add_css_class("insp-icon-btn")
            btn.set_tooltip_text(tooltip)
            btn.set_hexpand(False)
            btn.set_vexpand(False)
            btn.set_valign(Gtk.Align.CENTER)
            try:
                btn.set_icon_name(icon_name)
            except (AttributeError, TypeError):
                btn.set_label(fallback)
            btn.connect("clicked", lambda *_: on_click())
            return btn

        def _section_head(title: str, on_edit, *, tooltip: str = "Edit") -> Gtk.Box:
            """Section title with a compact pencil control beside it (same row)."""
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            row.set_hexpand(True)
            row.set_halign(Gtk.Align.START)
            lbl = Gtk.Label(label=title, xalign=0)
            lbl.add_css_class("insp-section-title")
            lbl.set_hexpand(False)
            row.append(lbl)
            row.append(_icon_btn(tooltip=tooltip, on_click=on_edit))
            return row

        def _chip_box() -> Gtk.Box:
            # Vertical box — avoid Gtk.FlowBox here. On this stack FlowBox inside
            # a fixed-width ScrolledWindow can spin the main thread in layout and
            # freeze the window (close/killactive stop responding).
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            col.set_halign(Gtk.Align.START)
            col.set_hexpand(True)
            col.set_valign(Gtk.Align.START)
            return col

        def _clickable(widget: Gtk.Widget, on_click) -> None:
            click = Gtk.GestureClick()
            click.set_button(1)

            def _pressed(*_a) -> None:
                on_click()

            click.connect("pressed", _pressed)
            widget.add_controller(click)

        # ── Title row: name + rename ───────────────────────────────────
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title_row.set_hexpand(True)
        self.insp_title = _narrow_label(Gtk.Label(xalign=0), chars=20)
        self.insp_title.add_css_class("insp-title")
        title_row.append(self.insp_title)
        self.insp_rename_btn = _icon_btn(
            tooltip="Rename file (F2)",
            on_click=self.open_rename_dialog,
            icon_name="document-edit-symbolic",
        )
        self.insp_rename_btn.set_sensitive(False)
        title_row.append(self.insp_rename_btn)
        box.append(title_row)

        # Dimensions — large and readable
        self.insp_dims = _narrow_label(Gtk.Label(xalign=0), chars=18)
        self.insp_dims.add_css_class("insp-dims")
        box.append(self.insp_dims)

        self.insp_subtitle = _narrow_label(Gtk.Label(xalign=0), chars=24)
        self.insp_subtitle.add_css_class("dim-label")
        self.insp_subtitle.add_css_class("caption")
        box.append(self.insp_subtitle)

        # Preview
        self.insp_picture = Gtk.Picture()
        self.insp_picture.set_size_request(PREVIEW, PREVIEW)
        self.insp_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.insp_picture.set_can_shrink(True)
        self.insp_picture.set_hexpand(False)
        self.insp_picture.set_vexpand(False)
        self.insp_picture.set_halign(Gtk.Align.START)
        self.insp_picture.set_valign(Gtk.Align.START)
        self.insp_preview_frame = Gtk.Box()
        self.insp_preview_frame.add_css_class("card")
        self.insp_preview_frame.add_css_class("insp-preview")
        # Left-align: Hyprland fractional scale clips the window's right edge.
        self.insp_preview_frame.set_halign(Gtk.Align.START)
        self.insp_preview_frame.set_hexpand(False)
        self.insp_preview_frame.set_vexpand(False)
        self.insp_preview_frame.set_size_request(PREVIEW, PREVIEW)
        try:
            self.insp_preview_frame.set_overflow(Gtk.Overflow.HIDDEN)
        except (AttributeError, TypeError):
            pass
        self.insp_preview_frame.append(self.insp_picture)
        box.append(self.insp_preview_frame)

        # ── Rating: stars + Clear on one row ──────────────────────────
        rate_lbl = Gtk.Label(label="Rating", xalign=0)
        rate_lbl.add_css_class("insp-section-title")
        box.append(rate_lbl)

        rate_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        rate_row.set_halign(Gtk.Align.START)
        self.insp_stars_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.insp_stars_box.set_halign(Gtk.Align.START)
        self.insp_stars_box.set_hexpand(False)
        self.insp_star_buttons: list[Gtk.Button] = []
        for n in range(1, 6):
            btn = Gtk.Button(label="☆")
            btn.add_css_class("flat")
            btn.add_css_class("circular")
            btn.add_css_class("insp-star-btn")
            btn.add_css_class("insp-star-off")
            btn.set_tooltip_text(f"Set {n} star(s)")
            btn.set_size_request(32, 32)
            btn.set_hexpand(False)
            btn.connect("clicked", lambda _b, s=n: self.set_rating(s))
            self.insp_star_buttons.append(btn)
            self.insp_stars_box.append(btn)
        rate_row.append(self.insp_stars_box)
        clear_r = Gtk.Button(label="Clear")
        clear_r.add_css_class("flat")
        clear_r.add_css_class("insp-quiet-btn")
        clear_r.set_valign(Gtk.Align.CENTER)
        clear_r.set_tooltip_text("Clear rating (0)")
        clear_r.connect("clicked", lambda *_: self.set_rating(0))
        rate_row.append(clear_r)
        box.append(rate_row)
        # Only shown when multi-select ratings differ
        self.insp_rating_note = _narrow_label(Gtk.Label(xalign=0), chars=28)
        self.insp_rating_note.add_css_class("dim-label")
        self.insp_rating_note.add_css_class("caption")
        self.insp_rating_note.set_visible(False)
        box.append(self.insp_rating_note)

        # ── Tags ──────────────────────────────────────────────────────
        box.append(
            _section_head("Tags", self.edit_tags_dialog, tooltip="Edit tags (T)")
        )
        self.insp_tags = _chip_box()
        self.insp_tags.add_css_class("insp-chip-box")
        _clickable(self.insp_tags, self.edit_tags_dialog)
        box.append(self.insp_tags)

        # ── Folders ───────────────────────────────────────────────────
        box.append(
            _section_head(
                "Folders", self.edit_folders_dialog, tooltip="Edit folders (F)"
            )
        )
        self.insp_folders = _chip_box()
        self.insp_folders.add_css_class("insp-chip-box")
        _clickable(self.insp_folders, self.edit_folders_dialog)
        box.append(self.insp_folders)

        # ── Notes (Eagle annotation) — truncated; click opens edit ────
        box.append(
            _section_head("Notes", self.edit_notes_dialog, tooltip="Edit note")
        )
        self.insp_notes = _narrow_label(Gtk.Label(xalign=0), chars=26)
        self.insp_notes.add_css_class("caption")
        self.insp_notes.add_css_class("insp-notes")
        try:
            self.insp_notes.set_lines(3)
        except (AttributeError, TypeError):
            pass
        self.insp_notes_btn = Gtk.Button()
        self.insp_notes_btn.add_css_class("flat")
        self.insp_notes_btn.add_css_class("insp-notes-btn")
        self.insp_notes_btn.set_halign(Gtk.Align.FILL)
        self.insp_notes_btn.set_hexpand(True)
        self.insp_notes_btn.set_child(self.insp_notes)
        self.insp_notes_btn.set_tooltip_text("Click to view or edit note")
        self.insp_notes_btn.set_sensitive(False)
        self.insp_notes_btn.connect("clicked", lambda *_: self.edit_notes_dialog())
        box.append(self.insp_notes_btn)

        # Path — no heavy heading; dim selectable line at the bottom
        self.insp_path = _narrow_label(
            Gtk.Label(xalign=0, selectable=True), chars=28
        )
        self.insp_path.add_css_class("caption")
        self.insp_path.add_css_class("dim-label")
        self.insp_path.add_css_class("insp-path")
        box.append(self.insp_path)

        # ── Set (family joined by a set: tag) ─────────────────────────
        self.insp_set_title = Gtk.Label(label="Set", xalign=0)
        self.insp_set_title.add_css_class("insp-section-title")
        box.append(self.insp_set_title)
        self.insp_set_thumbs = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=4
        )
        self.insp_set_thumbs.set_halign(Gtk.Align.START)
        self.insp_set_thumbs.set_hexpand(False)
        self.insp_set_thumbs.set_vexpand(False)
        box.append(self.insp_set_thumbs)
        set_act = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.insp_set_open = Gtk.Button(label="Open set")
        self.insp_set_open.set_tooltip_text("Open the focused asset's set (Ctrl+G)")
        self.insp_set_open.add_css_class("suggested-action")
        self.insp_set_open.set_sensitive(False)
        self.insp_set_open.connect("clicked", lambda *_: self.open_focused_set())
        self.insp_set_group = Gtk.Button(label="Group")
        self.insp_set_group.add_css_class("flat")
        self.insp_set_group.add_css_class("insp-quiet-btn")
        self.insp_set_group.set_tooltip_text("Group selection into a set (gs)")
        self.insp_set_group.set_sensitive(False)
        self.insp_set_group.connect("clicked", lambda *_: self.group_selection_into_set())
        self.insp_set_remove = Gtk.Button(label="Remove")
        self.insp_set_remove.add_css_class("flat")
        self.insp_set_remove.add_css_class("insp-quiet-btn")
        self.insp_set_remove.set_tooltip_text("Remove selection from set (gr)")
        self.insp_set_remove.set_sensitive(False)
        self.insp_set_remove.connect("clicked", lambda *_: self.remove_selection_from_set())
        set_act.append(self.insp_set_open)
        set_act.append(self.insp_set_group)
        set_act.append(self.insp_set_remove)
        box.append(set_act)

        # ── Compare slots (A / B stills, then open the slider) ────────
        cmp_lbl = Gtk.Label(label="Compare", xalign=0)
        cmp_lbl.add_css_class("insp-section-title")
        box.append(cmp_lbl)

        def _slot_btn(letter: str) -> tuple[Gtk.Button, Gtk.Picture, Gtk.Label]:
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("insp-cmp-slot")
            btn.set_halign(Gtk.Align.FILL)
            btn.set_hexpand(True)
            btn.set_tooltip_text(f"Park the current still as {letter}")
            btn.connect("clicked", lambda *_ , s=letter.lower(): self._compare_set_slot(s))
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            pic = Gtk.Picture()
            pic.set_size_request(36, 36)
            pic.set_content_fit(Gtk.ContentFit.COVER)
            pic.add_css_class("insp-cmp-thumb")
            tag = Gtk.Label(label=letter, xalign=0)
            tag.add_css_class("heading")
            name = Gtk.Label(label="(empty)", xalign=0)
            name.add_css_class("caption")
            name.add_css_class("dim-label")
            name.set_ellipsize(3)
            name.set_hexpand(True)
            name.set_max_width_chars(16)
            row.append(tag)
            row.append(pic)
            row.append(name)
            btn.set_child(row)
            box.append(btn)
            return btn, pic, name

        self.insp_cmp_a_btn, self.insp_cmp_a_pic, self.insp_cmp_a_name = _slot_btn("A")
        self.insp_cmp_b_btn, self.insp_cmp_b_pic, self.insp_cmp_b_name = _slot_btn("B")

        act_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.insp_cmp_go = Gtk.Button(label="Compare")
        self.insp_cmp_go.add_css_class("suggested-action")
        self.insp_cmp_go.set_sensitive(False)
        self.insp_cmp_go.connect("clicked", lambda *_: self.open_compare_viewer())
        self.insp_cmp_swap = Gtk.Button(label="Swap")
        self.insp_cmp_swap.add_css_class("flat")
        self.insp_cmp_swap.add_css_class("insp-quiet-btn")
        self.insp_cmp_swap.set_sensitive(False)
        self.insp_cmp_swap.connect("clicked", lambda *_: self._compare_swap_slots())
        self.insp_cmp_clear = Gtk.Button(label="Clear")
        self.insp_cmp_clear.add_css_class("flat")
        self.insp_cmp_clear.add_css_class("insp-quiet-btn")
        self.insp_cmp_clear.set_sensitive(False)
        self.insp_cmp_clear.connect("clicked", lambda *_: self._compare_clear_slots())
        act_row.append(self.insp_cmp_go)
        act_row.append(self.insp_cmp_swap)
        act_row.append(self.insp_cmp_clear)
        box.append(act_row)

        # ── Inspector CSS (one provider) ──────────────────────────────
        css = Gtk.CssProvider()
        css.load_from_data(
            b"""
            scrolledwindow.inspector-sidebar button {
                min-width: 0;
                min-height: 0;
            }
            scrolledwindow.inspector-sidebar button.insp-icon-btn {
                padding: 2px;
                min-width: 24px;
                min-height: 24px;
            }
            scrolledwindow.inspector-sidebar button.insp-star-btn {
                padding: 0;
                font-size: 18px;
            }
            scrolledwindow.inspector-sidebar button.insp-star-btn.insp-star-off {
                opacity: 0.32;
            }
            scrolledwindow.inspector-sidebar button.insp-star-btn.insp-star-on {
                opacity: 1;
            }
            scrolledwindow.inspector-sidebar button.insp-star-btn:hover {
                opacity: 1;
                background-color: alpha(@theme_fg_color, 0.12);
            }
            scrolledwindow.inspector-sidebar button.insp-star-btn.insp-star-off:hover {
                opacity: 0.72;
            }
            scrolledwindow.inspector-sidebar button.insp-quiet-btn {
                padding: 2px 6px;
                font-size: 0.85em;
                opacity: 0.75;
            }
            label.insp-title {
                font-weight: 600;
                font-size: 1.05em;
            }
            label.insp-section-title {
                font-size: 0.72em;
                font-weight: 600;
                letter-spacing: 0.06em;
                text-transform: uppercase;
                opacity: 0.72;
                margin-top: 2px;
            }
            label.insp-dims {
                font-size: 18px;
                font-weight: 600;
                letter-spacing: 0.02em;
            }
            label.insp-chip {
                font-size: 0.82em;
                padding: 2px 8px;
                border-radius: 999px;
                background-color: alpha(@theme_fg_color, 0.10);
            }
            label.insp-chip.insp-chip-dim {
                opacity: 0.55;
            }
            label.insp-chip.insp-chip-empty {
                opacity: 0.45;
                font-style: italic;
                background-color: transparent;
                padding-left: 0;
            }
            button.insp-notes-btn {
                padding: 8px 10px;
                min-height: 0;
                border-radius: 8px;
                background-color: alpha(@theme_fg_color, 0.06);
            }
            button.insp-notes-btn:hover {
                background-color: alpha(@theme_fg_color, 0.10);
            }
            button.insp-notes-btn label {
                font-weight: normal;
            }
            button.insp-notes-btn:disabled {
                opacity: 0.5;
            }
            label.insp-path {
                margin-top: 4px;
                opacity: 0.65;
            }
            box.insp-preview {
                border-radius: 8px;
            }
            picture.insp-cmp-thumb {
                min-width: 36px;
                min-height: 36px;
            }
            button.insp-cmp-slot {
                padding: 4px 2px;
                min-height: 0;
            }
            button.insp-cmp-slot:hover {
                background-color: alpha(@theme_fg_color, 0.10);
            }
            button.insp-set-thumb {
                padding: 0;
                min-width: 36px;
                max-width: 36px;
                min-height: 36px;
                max-height: 36px;
            }
            label.insp-set-more {
                font-weight: 700;
                font-size: 0.9em;
                min-width: 36px;
                min-height: 36px;
                background-color: alpha(@window_bg_color, 0.7);
            }
            label.grid-duration {
                font-weight: 700;
                font-size: 0.85em;
                padding: 2px 6px;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._inspector_empty()

        wrap = self._fixed_width_pane(INSPECTOR_WIDTH, "inspector-wrap")
        wrap.append(scroll)
        self.inspector_sidebar = wrap
        return wrap

    def _set_inspector_preview(self, path: str | None) -> None:
        """Load inspector thumbnail scaled to the fixed preview box.

        Full-resolution textures make Gtk.Picture report a huge natural size,
        which expands the inspector and gets clipped on Hyprland fractional
        scale (surface wider than the visible client).
        """
        if not path:
            self.insp_picture.set_paintable(None)
            return
        size = int(getattr(self, "_insp_preview_px", 180) or 180)
        pix = _pixbuf_from_path(path, size, size)
        if pix is None:
            self.insp_picture.set_paintable(None)
            return
        self.insp_picture.set_paintable(Gdk.Texture.new_for_pixbuf(pix))

    @staticmethod
    def _fmt_size(n: int) -> str:
        """Human-readable byte size for the inspector subtitle."""
        try:
            n = int(n)
        except (TypeError, ValueError):
            return ""
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.0f} KB"
        if n < 1024 * 1024 * 1024:
            return f"{n / (1024 * 1024):.1f} MB"
        return f"{n / (1024 * 1024 * 1024):.2f} GB"

    def _inspector_empty(self) -> None:
        self.insp_title.set_text("No selection")
        self.insp_dims.set_text("")
        self.insp_subtitle.set_text("Select an asset in the grid")
        if hasattr(self, "crop_btn"):
            self.crop_btn.set_sensitive(False)
        if hasattr(self, "upscale_btn"):
            self.upscale_btn.set_sensitive(False)
        if hasattr(self, "insp_rename_btn"):
            self.insp_rename_btn.set_sensitive(False)
        if hasattr(self, "crop_916_btn"):
            self.crop_916_btn.set_sensitive(False)
        self.insp_picture.set_paintable(None)
        self.insp_rating_note.set_text("")
        self.insp_rating_note.set_visible(False)
        self._paint_insp_stars(0)
        self._clear_box(self.insp_tags)
        self._clear_box(self.insp_folders)
        if hasattr(self, "insp_notes"):
            self.insp_notes.set_text("")
        if hasattr(self, "insp_notes_btn"):
            self.insp_notes_btn.set_sensitive(False)
            self.insp_notes_btn.set_tooltip_text("Select an asset to add a note")
        self.insp_path.set_text("")
        if hasattr(self, "insp_cmp_a_btn"):
            self.insp_cmp_a_btn.set_sensitive(False)
            self.insp_cmp_b_btn.set_sensitive(False)
            self._sync_compare_ui()
        self._sync_set_ui([])

    @staticmethod
    def _clear_box(box: Gtk.Widget) -> None:
        while (c := box.get_first_child()) is not None:
            box.remove(c)

    def _add_chip_label(
        self,
        box: Gtk.Widget,
        text: str,
        *,
        dim: bool = False,
        empty: bool = False,
        tooltip: str | None = None,
    ) -> None:
        """Pill chip for tags/folders."""
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.set_halign(Gtk.Align.START)
        lbl.set_hexpand(False)
        lbl.set_ellipsize(3)  # END
        lbl.set_max_width_chars(24)
        lbl.add_css_class("insp-chip")
        if empty:
            lbl.add_css_class("insp-chip-empty")
        elif dim:
            lbl.add_css_class("insp-chip-dim")
        if tooltip:
            lbl.set_tooltip_text(tooltip)
        box.append(lbl)

    def update_inspector(self) -> None:
        """Refresh right sidebar for current multi-selection / focus."""
        items = self._effective_hand_off_items()
        if not items:
            self._inspector_empty()
            return

        n = len(items)
        if n == 1:
            it = items[0]
            self.insp_title.set_text(it.display_name)
            if it.width and it.height:
                self.insp_dims.set_text(f"{it.width} × {it.height}")
            else:
                self.insp_dims.set_text("")
            bits = [it.ext_lower or "?"]
            if it.duration:
                bits.append(f"{it.duration:.1f}s")
            if it.size:
                bits.append(self._fmt_size(it.size))
            self.insp_subtitle.set_text(" · ".join(bits))
            self.insp_path.set_text(str(it.path))
            self._set_inspector_preview(_thumb_path_for(it))
            if hasattr(self, "crop_btn"):
                self.crop_btn.set_sensitive(
                    bool((it.is_image or it.is_audio) and it.path.is_file())
                )
            if hasattr(self, "upscale_btn"):
                self.upscale_btn.set_sensitive(
                    bool((it.is_image or it.is_video) and it.path.is_file())
                )
            can_rename = bool(it.item_dir and it.path.is_file())
            if hasattr(self, "insp_rename_btn"):
                self.insp_rename_btn.set_sensitive(can_rename)
            if hasattr(self, "crop_916_btn"):
                self.crop_916_btn.set_sensitive(
                    bool(it.is_video and it.path.is_file())
                )
        else:
            self.insp_title.set_text(f"{n} assets selected")
            # Show size range when multi-selected and dimensions differ
            dims = {(it.width, it.height) for it in items if it.width and it.height}
            if len(dims) == 1:
                w, h = next(iter(dims))
                self.insp_dims.set_text(f"{w} × {h}")
            elif dims:
                self.insp_dims.set_text("Mixed sizes")
            else:
                self.insp_dims.set_text("")
            self.insp_subtitle.set_text("Showing values shared by all")
            self.insp_path.set_text("")
            self._set_inspector_preview(_thumb_path_for(items[0]))
            if hasattr(self, "crop_btn"):
                self.crop_btn.set_sensitive(False)
            if hasattr(self, "upscale_btn"):
                self.upscale_btn.set_sensitive(False)
            if hasattr(self, "insp_rename_btn"):
                self.insp_rename_btn.set_sensitive(False)
            if hasattr(self, "crop_916_btn"):
                vids = [it for it in items if it.is_video and it.path.is_file()]
                self.crop_916_btn.set_sensitive(bool(vids))

        # Rating commonality — stars carry the value; note only for mixed
        stars = {it.star for it in items}
        if len(stars) == 1:
            s = next(iter(stars))
            rating = s if s else 0
            self.insp_rating_note.set_text("")
            self.insp_rating_note.set_visible(False)
            self._paint_insp_stars(rating)
        else:
            self.insp_rating_note.set_text("Mixed ratings")
            self.insp_rating_note.set_visible(True)
            self._paint_insp_stars(0)

        # Tags: intersection (common) and partial — pill chips
        tag_sets = [{t for t in it.tags if not is_set_tag(t)} for it in items]
        common_tags = set.intersection(*tag_sets) if tag_sets else set()
        union_tags = set.union(*tag_sets) if tag_sets else set()
        partial_tags = union_tags - common_tags
        self._clear_box(self.insp_tags)
        if common_tags:
            for t in sorted(common_tags, key=str.lower):
                self._add_chip_label(self.insp_tags, t)
        if partial_tags and n > 1:
            for t in sorted(partial_tags, key=str.lower):
                self._add_chip_label(self.insp_tags, f"± {t}", dim=True)
        if not common_tags and not partial_tags:
            self._add_chip_label(self.insp_tags, "None — click to add", empty=True)

        # Folders commonality
        folder_sets = [set(it.folders) for it in items]
        common_f = set.intersection(*folder_sets) if folder_sets else set()
        union_f = set.union(*folder_sets) if folder_sets else set()
        partial_f = union_f - common_f
        self._clear_box(self.insp_folders)
        if common_f:
            for fid in sorted(common_f):
                name = self.library.folder_paths.get(fid, fid)
                # Prefer leaf name for chips; full path in tooltip
                leaf = name.rsplit("/", 1)[-1] if name else fid
                self._add_chip_label(
                    self.insp_folders,
                    leaf,
                    tooltip=name if name != leaf else None,
                )
        if partial_f and n > 1:
            for fid in sorted(partial_f):
                name = self.library.folder_paths.get(fid, fid)
                leaf = name.rsplit("/", 1)[-1] if name else fid
                self._add_chip_label(
                    self.insp_folders,
                    f"± {leaf}",
                    dim=True,
                    tooltip=name if name != leaf else None,
                )
        if not common_f and not partial_f:
            self._add_chip_label(self.insp_folders, "None — click to add", empty=True)

        # Notes (annotation) commonality — truncated preview in the card
        notes = [(it.annotation or "").strip() for it in items]
        unique_notes = set(notes)
        if hasattr(self, "insp_notes_btn"):
            self.insp_notes_btn.set_sensitive(True)
            self.insp_notes_btn.set_tooltip_text("Click to view or edit note")
        if len(unique_notes) == 1:
            note = next(iter(unique_notes))
            if note:
                preview = " ".join(note.split())
                self.insp_notes.set_text(preview)
            else:
                self.insp_notes.set_text("Add a note…")
        else:
            self.insp_notes.set_text("Mixed notes — click to set for all")

        still = self._compare_current_still()
        if hasattr(self, "insp_cmp_a_btn"):
            self.insp_cmp_a_btn.set_sensitive(still is not None)
            self.insp_cmp_b_btn.set_sensitive(still is not None)
            self._sync_compare_ui()
        self._sync_set_ui(items)

    def _paint_insp_stars(self, rating: int) -> None:
        """Filled stars full-opacity ★; empty stars faded ☆."""
        for i, b in enumerate(self.insp_star_buttons, start=1):
            on = bool(rating) and i <= rating
            b.set_label("★" if on else "☆")
            if on:
                b.add_css_class("insp-star-on")
                b.remove_css_class("insp-star-off")
            else:
                b.add_css_class("insp-star-off")
                b.remove_css_class("insp-star-on")

    def _install_keybinds(self) -> None:
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key)
        self.add_controller(controller)
        # App-wide Esc: SearchEntry and Gtk.Video otherwise keep the key.
        esc = Gtk.ShortcutController()
        esc.set_scope(Gtk.ShortcutScope.GLOBAL)
        esc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def _esc_action(_widget, _args) -> bool:
            return self._handle_escape()

        esc.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.KeyvalTrigger.new(Gdk.KEY_Escape, Gdk.ModifierType(0)),
                Gtk.CallbackAction.new(_esc_action),
            )
        )
        self.add_controller(esc)

    def _remember_dialog(self, win: Gtk.Window) -> None:
        """Track a non-modal picker so Esc on this window can close it."""
        prev = self._open_dialog
        if prev is not None and prev is not win:
            try:
                prev.close()
            except Exception:  # noqa: BLE001
                pass
        self._open_dialog = win
        self._picker_blocking = True

        def _clear(*_a) -> None:
            if self._open_dialog is win:
                self._open_dialog = None
            if self._open_dialog is None:
                self._picker_blocking = False

        win.connect("destroy", _clear)

    def _close_open_dialog(self) -> bool:
        win = self._open_dialog
        if win is None:
            self._picker_blocking = False
            return False
        try:
            visible = bool(win.get_visible())
        except Exception:  # noqa: BLE001
            visible = False
        if not visible:
            self._open_dialog = None
            self._picker_blocking = False
            return False
        try:
            win.close()
        except Exception:  # noqa: BLE001
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                self._open_dialog = None
                self._picker_blocking = False
                return False
        if self._open_dialog is win:
            self._open_dialog = None
        self._picker_blocking = False
        return True

    def _shutdown_background(self) -> None:
        """Stop timers / workers so the process can actually exit."""
        mark_gui_stopped()
        if self._search_timeout_id:
            try:
                GLib.source_remove(self._search_timeout_id)
            except Exception:  # noqa: BLE001
                pass
            self._search_timeout_id = 0
        if self._cols_sync_timeout_id:
            try:
                GLib.source_remove(self._cols_sync_timeout_id)
            except Exception:  # noqa: BLE001
                pass
            self._cols_sync_timeout_id = 0
        if self._inbox_poll_id:
            try:
                GLib.source_remove(self._inbox_poll_id)
            except Exception:  # noqa: BLE001
                pass
            self._inbox_poll_id = 0
        if self._inbox_monitor is not None:
            try:
                self._inbox_monitor.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._inbox_monitor = None
        if self._scroll_restore_source:
            try:
                GLib.source_remove(self._scroll_restore_source)
            except Exception:  # noqa: BLE001
                pass
            self._scroll_restore_source = 0
        try:
            _thumb_executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python < 3.9 has no cancel_futures
            try:
                _thumb_executor.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def _on_window_close_request(self, *_args) -> bool:
        """Window closed (including Hyprland Super+W killactive)."""
        self._save_sidebar_state()
        self._shutdown_background()
        app = self.get_application()
        if app is not None:
            # Quit after this close finishes so D-Bus single-instance releases.
            GLib.idle_add(app.quit)
        return False  # allow destroy

    def _save_sidebar_state(self) -> None:
        """Write expand + current-view ids so the next launch matches."""
        if not getattr(self, "_library_ready", False):
            return
        data = {
            "smart_expanded": sorted(self._smart_expanded),
            "folders_section_expanded": bool(self._folders_section_expanded),
            "current_smart_folder_id": self.current_smart_folder_id,
            "current_folder_id": self.current_folder_id,
            "special_view": self._special_view,
        }
        try:
            _UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _UI_STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            tmp.replace(_UI_STATE_PATH)
        except OSError:
            pass

    def _load_sidebar_state(self) -> None:
        """Restore expand + current view after the library has loaded."""
        try:
            data = json.loads(_UI_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return
        valid_smart = set(self.library.smart_folders_by_id)
        valid_folder = set(self.library.folders_by_id)
        raw_exp = data.get("smart_expanded") or []
        if isinstance(raw_exp, list):
            self._smart_expanded = {str(i) for i in raw_exp if str(i) in valid_smart}
        self._folders_section_expanded = bool(data.get("folders_section_expanded"))
        sid = data.get("current_smart_folder_id")
        fid = data.get("current_folder_id")
        special = data.get("special_view")
        self.current_smart_folder_id = (
            str(sid) if sid and str(sid) in valid_smart else None
        )
        self.current_folder_id = (
            str(fid) if fid and str(fid) in valid_folder else None
        )
        self._special_view = (
            special if special in ("untagged", "uncategorized") else None
        )
        if self.current_smart_folder_id:
            self.current_folder_id = None
            self._special_view = None
            self._ensure_smart_expanded_path(self.current_smart_folder_id)
        elif self.current_folder_id:
            self._special_view = None
            self._folders_section_expanded = True

    # ── Sidebar ───────────────────────────────────────────────────────

    def _make_header_row(
        self,
        title: str,
        *,
        collapsible: bool = False,
        section_id: str | None = None,
        expanded: bool = True,
        on_add=None,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.row_kind = "section" if collapsible else "header"  # type: ignore[attr-defined]
        row.section_id = section_id  # type: ignore[attr-defined]
        row.has_children = collapsible  # type: ignore[attr-defined]
        row.expanded = expanded  # type: ignore[attr-defined]
        if collapsible:
            # Selectable so Enter / ←→ work like other sidebar rows
            row.set_selectable(True)
            row.set_activatable(True)
        else:
            row.set_selectable(False)
            row.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_margin_start(4)
        box.set_margin_end(8)
        box.set_margin_top(8)
        box.set_margin_bottom(2)

        if collapsible:
            twisty = Gtk.Button()
            twisty.add_css_class("flat")
            twisty.add_css_class("circular")
            twisty.set_valign(Gtk.Align.CENTER)
            twisty.set_focus_on_click(False)
            twisty.set_icon_name("pan-down-symbolic" if expanded else "pan-end-symbolic")
            twisty.set_tooltip_text("Collapse" if expanded else "Expand")
            sid = section_id or ""

            def on_twisty(_btn: Gtk.Button, sec: str = sid) -> None:
                self._toggle_section(sec)

            twisty.connect("clicked", on_twisty)
            box.append(twisty)
        else:
            spacer = Gtk.Box()
            spacer.set_size_request(28, 1)
            box.append(spacer)

        label = Gtk.Label(label=title, xalign=0, hexpand=True)
        label.add_css_class("heading")
        box.append(label)
        if on_add is not None:
            add_btn = Gtk.Button()
            add_btn.add_css_class("flat")
            add_btn.add_css_class("circular")
            add_btn.set_icon_name("list-add-symbolic")
            add_btn.set_tooltip_text("New smart folder")
            add_btn.set_valign(Gtk.Align.CENTER)
            add_btn.set_focus_on_click(False)
            add_btn.connect("clicked", lambda *_: on_add())
            box.append(add_btn)
        row.set_child(box)
        if section_id == "smart":
            click = Gtk.GestureClick()
            click.set_button(3)

            def on_header_right(
                _g: Gtk.GestureClick, _n: int, x: float, y: float
            ) -> None:
                self._open_smart_header_menu(row, x, y)

            click.connect("pressed", on_header_right)
            row.add_controller(click)
            row.set_tooltip_text("Right-click · new smart folder · drop here to move to top")
            self._attach_smart_drop_target(row, dest_id=None, first=True)
        return row

    def _toggle_section(self, section_id: str) -> None:
        if section_id == "folders":
            self._folders_section_expanded = not self._folders_section_expanded
            # If collapsing while a regular folder is selected, keep selection
            # but hide the tree; re-expand when focusing that folder again.
            self._repopulate_sidebar_keep_selection()
        elif section_id == "smart":
            # Reserved if we make Smart folders a section later
            pass

    def _make_nav_row(
        self,
        *,
        label: str,
        depth: int = 0,
        kind: str,
        folder_id: str | None = None,
        smart_folder_id: str | None = None,
        special_view: str | None = None,
        dim: bool = False,
        has_children: bool = False,
        expanded: bool = False,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.row_kind = kind  # type: ignore[attr-defined]
        row.folder_id = folder_id  # type: ignore[attr-defined]
        row.smart_folder_id = smart_folder_id  # type: ignore[attr-defined]
        row.special_view = special_view  # type: ignore[attr-defined]
        row.has_children = has_children  # type: ignore[attr-defined]
        row.expanded = expanded  # type: ignore[attr-defined]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_margin_start(4 + depth * 12)
        box.set_margin_end(8)
        box.set_margin_top(2)
        box.set_margin_bottom(2)

        # Disclosure triangle for collapsible smart folders
        if kind == "smart" and has_children:
            twisty = Gtk.Button()
            twisty.add_css_class("flat")
            twisty.add_css_class("circular")
            twisty.set_valign(Gtk.Align.CENTER)
            twisty.set_focus_on_click(False)
            icon = "pan-down-symbolic" if expanded else "pan-end-symbolic"
            twisty.set_icon_name(icon)
            twisty.set_tooltip_text("Collapse" if expanded else "Expand")
            sid = smart_folder_id

            def on_twisty(_btn: Gtk.Button, folder_sid: str = sid or "") -> None:
                self._toggle_smart_expand(folder_sid)

            twisty.connect("clicked", on_twisty)
            box.append(twisty)
            row.twisty = twisty  # type: ignore[attr-defined]
        else:
            # Spacer so labels line up with expandable rows
            spacer = Gtk.Box()
            spacer.set_size_request(28 if kind == "smart" else 0, 1)
            box.append(spacer)

        text = Gtk.Label(label=label, xalign=0, hexpand=True, ellipsize=3)
        if dim:
            text.add_css_class("dim-label")
        box.append(text)
        row.name_label = text  # type: ignore[attr-defined]

        # Folder auto-tags (Eagle "Auto tagging")
        if kind == "folder" and folder_id:
            folder = self.library.folders_by_id.get(folder_id)
            auto = list(folder.tags) if folder else []
            if auto:
                badge = Gtk.Label(label="🏷")
                badge.add_css_class("caption")
                badge.set_tooltip_text("Auto-tags: " + ", ".join(auto))
                box.append(badge)
            tip = f"{label}"
            if auto:
                tip += f"\nAuto-tags: {', '.join(auto)}"
            tip += "\nRight-click or press A · edit auto-tags"
            row.set_tooltip_text(tip)

            click = Gtk.GestureClick()
            click.set_button(3)

            def on_right(
                _g: Gtk.GestureClick,
                _n: int,
                _x: float,
                _y: float,
                fid: str = folder_id,
            ) -> None:
                self.edit_folder_auto_tags(fid)

            click.connect("pressed", on_right)
            row.add_controller(click)

        if kind == "smart" and smart_folder_id:
            row.set_tooltip_text(
                f"{label}\nDrag to reorder · right-click · e edits · Shift+↑↓"
            )
            sf_click = Gtk.GestureClick()
            sf_click.set_button(3)

            def on_sf_right(
                _g: Gtk.GestureClick,
                _n: int,
                x: float,
                y: float,
                sid: str = smart_folder_id,
            ) -> None:
                self._open_smart_folder_menu(row, x, y, sid)

            sf_click.connect("pressed", on_sf_right)
            row.add_controller(sf_click)
            self._attach_smart_drag_source(row, smart_folder_id)
            self._attach_smart_drop_target(row, dest_id=smart_folder_id)

        row.set_child(box)
        return row

    def _attach_smart_drag_source(self, row: Gtk.ListBoxRow, smart_id: str) -> None:
        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.MOVE)

        def prepare(_src: Gtk.DragSource, _x: float, _y: float) -> Gdk.ContentProvider:
            return Gdk.ContentProvider.new_for_value(
                GObject.Value(GObject.TYPE_STRING, smart_id)
            )

        def on_end(_src: Gtk.DragSource, _drag: Gdk.Drag, _delete: bool) -> None:
            self._set_sf_drop_hint(None, None)

        source.connect("prepare", prepare)
        source.connect("drag-end", on_end)
        row.add_controller(source)

    def _attach_smart_drop_target(
        self,
        row: Gtk.ListBoxRow,
        *,
        dest_id: str | None,
        first: bool = False,
    ) -> None:
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)

        def place_for_y(y: float) -> str:
            if first:
                return "first"
            h = row.get_height() or 1
            return "before" if y < h / 2 else "after"

        def on_motion(_t: Gtk.DropTarget, _x: float, y: float) -> Gdk.DragAction:
            self._set_sf_drop_hint(row, place_for_y(y))
            return Gdk.DragAction.MOVE

        def on_leave(_t: Gtk.DropTarget) -> None:
            self._set_sf_drop_hint(None, None)

        def on_drop(_t: Gtk.DropTarget, value: object, _x: float, y: float) -> bool:
            src_id = str(value or "")
            self._set_sf_drop_hint(None, None)
            if not src_id:
                return False
            if first:
                self._move_smart_folder(src_id, None, "first")
                return True
            if not dest_id or src_id == dest_id:
                return False
            self._move_smart_folder(src_id, dest_id, place_for_y(y))
            return True

        target.connect("motion", on_motion)
        target.connect("leave", on_leave)
        target.connect("drop", on_drop)
        row.add_controller(target)

    def _set_sf_drop_hint(
        self, row: Gtk.ListBoxRow | None, place: str | None
    ) -> None:
        prev = self._sf_drop_row
        if prev is not None and prev is not row:
            prev.remove_css_class("drop-before")
            prev.remove_css_class("drop-after")
        if row is None or place is None:
            if prev is not None:
                prev.remove_css_class("drop-before")
                prev.remove_css_class("drop-after")
            self._sf_drop_row = None
            return
        row.remove_css_class("drop-before")
        row.remove_css_class("drop-after")
        if place == "after":
            row.add_css_class("drop-after")
        else:
            # first / before share the top-edge marker
            row.add_css_class("drop-before")
        self._sf_drop_row = row

    def _smart_siblings(self, smart_id: str) -> list[str]:
        sf = self.library.smart_folders_by_id.get(smart_id)
        if sf is None:
            return []
        if sf.parent_id:
            parent = self.library.smart_folders_by_id.get(sf.parent_id)
            kids = parent.children if parent else []
        else:
            kids = self.library.smart_folders
        return [c.id for c in kids]

    def _nudge_smart_folder(self, smart_id: str, delta: int) -> None:
        siblings = self._smart_siblings(smart_id)
        try:
            idx = siblings.index(smart_id)
        except ValueError:
            return
        dest_idx = idx + delta
        if dest_idx < 0 or dest_idx >= len(siblings):
            return
        dest = siblings[dest_idx]
        place = "before" if delta < 0 else "after"
        self._move_smart_folder(smart_id, dest, place)

    def _move_smart_folder(
        self, src_id: str, dest_id: str | None, place: str
    ) -> None:
        from write import WriteError, move_smart_folder_node, write_session

        before = self.library.smart_folders_by_id.get(src_id)
        old_parent = before.parent_id if before else None
        try:
            with write_session(self.library.root):
                move_smart_folder_node(
                    self.library.root,
                    src_id,
                    target_id=dest_id,
                    place=place,
                )
            self.library.reload_metadata_trees()
        except WriteError as exc:
            self._toast(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._toast(str(exc))
            return
        after = self.library.smart_folders_by_id.get(src_id)
        new_parent = after.parent_id if after else None
        if old_parent != new_parent:
            self._smart_counts.clear()
        if src_id in self.library.smart_folders_by_id:
            self.current_smart_folder_id = src_id
            self.current_folder_id = None
            self._special_view = None
            self._ensure_smart_expanded_path(src_id)
        self._populate_sidebar(select_current=True)
        if old_parent != new_parent:
            self.refresh_items()

    def _smart_ancestors(self, smart_id: str) -> list[str]:
        """Parent chain from root → immediate parent (not including smart_id)."""
        chain: list[str] = []
        sf = self.library.smart_folders_by_id.get(smart_id)
        while sf and sf.parent_id:
            chain.append(sf.parent_id)
            sf = self.library.smart_folders_by_id.get(sf.parent_id)
        chain.reverse()
        return chain

    def _ensure_smart_expanded_path(self, smart_id: str | None) -> None:
        """Expand ancestors so a nested smart folder is visible in the sidebar."""
        if not smart_id:
            return
        for parent_id in self._smart_ancestors(smart_id):
            self._smart_expanded.add(parent_id)

    def open_location_picker(self) -> None:
        """Open the fuzzy smart-folder / folder switcher."""
        from picker import Choice, ChoicePicker

        choices = [
            Choice(
                "special:uncategorized",
                "Intake",
                "Special view",
                ("uncategorized", "uncat"),
            ),
            Choice("special:untagged", "Untagged", "Special view"),
        ]
        choices.extend(
            Choice(f"smart:{smart_id}", path, "Smart folder")
            for smart_id, path in self.library.smart_folder_paths.items()
        )
        choices.extend(
            Choice(f"folder:{folder_id}", path, "Folder")
            for folder_id, path in self.library.folder_paths.items()
        )

        def choose(choice: Choice) -> None:
            kind, location_id = choice.key.split(":", 1)
            before = self._view_loc()
            if self.is_viewer_open():
                self.close_inline_viewer(restore_scroll=False)
            self._set_view_tag = None
            if kind == "smart":
                self._special_view = None
                self.current_smart_folder_id = location_id
                self.current_folder_id = None
                self._ensure_smart_expanded_path(location_id)
            elif kind == "folder":
                self._special_view = None
                self.current_smart_folder_id = None
                self.current_folder_id = location_id
                self._folders_section_expanded = True
            else:
                self.open_special_view(location_id)
                return
            self._populate_sidebar(select_current=True)
            self.refresh_items(reset_selection=True, scroll_to_top=True)
            self._save_sidebar_state()
            self._record_view_change(before)
            self.focus_grid()

        picker = ChoicePicker(
            self,
            title="Go to folder",
            subtitle="Search special views, smart folders, and library folder paths.",
            choices=choices,
            on_choose=choose,
        )
        picker.present()

    def open_special_view(self, view: str) -> None:
        """Open a virtual library view and keep navigation state in sync."""
        before = self._view_loc()
        if self.is_viewer_open():
            self.close_inline_viewer(restore_scroll=False)
        self._set_view_tag = None
        self._special_view = view
        self.current_smart_folder_id = None
        self.current_folder_id = None
        self._populate_sidebar(select_current=True)
        self.refresh_items(reset_selection=True, scroll_to_top=True)
        self._save_sidebar_state()
        self._record_view_change(before)
        self.focus_grid()

    def open_keyboard_help(self) -> None:
        """Show the keyboard command reference."""
        win = Gtk.Window(
            title="Keyboard commands",
            transient_for=self,
            # A compositor can occasionally place a modal transient behind its
            # parent, making the entire app appear frozen. Dialog tracking still
            # blocks app hotkeys without disabling the parent window.
            modal=False,
            default_width=620,
            default_height=680,
        )
        self._remember_dialog(win)

        def restore_parent_focus(*_args) -> None:
            def apply() -> bool:
                try:
                    self.present()
                    self.focus_grid()
                except Exception:  # noqa: BLE001
                    pass
                return False

            GLib.idle_add(apply)

        win.connect("destroy", restore_parent_focus)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        win.set_child(root)
        header = Gtk.HeaderBar()
        header.set_title_widget(Gtk.Label(label="Keyboard commands"))
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_args: win.close())
        header.pack_end(close)
        root.append(header)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(scroll)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_margin_top(16)
        content.set_margin_bottom(20)
        content.set_margin_start(20)
        content.set_margin_end(20)
        scroll.set_child(content)

        groups = (
            ("Navigate", (
                ("V", "Go to a special view, smart folder, or folder"),
                ("I", "Set video In marker; otherwise open Intake"),
                ("/  or  Ctrl+F", "Search assets"),
                ("Arrow keys  or  h j k l", "Move through the grid"),
                ("gg / G", "Jump to the first / last asset in the view"),
                ("b", "Focus the sidebar"),
                ("Alt+← / Alt+→", "Back / forward through views"),
                ("Enter  or  o", "Open or close the focused asset"),
                ("Esc", "Close the current dialog, viewer, or filter"),
            )),
            ("Select and organize", (
                ("Space", "Mark or unmark the focused asset"),
                ("Ctrl+A", "Select all assets in the current view"),
                ("t", "Edit tags"),
                ("f", "Edit folders"),
                ("n", "Edit notes"),
                ("Shift+N  or  F2", "Rename file"),
                ("Ctrl+G", "Open the focused asset's set"),
                ("gs / gr", "Create a set / remove from set"),
                ("Delete", "Move selection to Eagle trash"),
                ("Ctrl+Z", "Undo the last delete"),
                ("1–5 / 0", "Set or clear star rating"),
            )),
            ("Use assets", (
                ("e", "Reveal in Files"),
                ("Shift+E", "Add to the current clip-editor project"),
                ("Ctrl+Shift+E", "Create a clip-editor project"),
                ("y", "Copy Eagle ID"),
                ("Shift+Y  or  c", "Copy file path"),
                ("s", "Stage marked assets"),
                ("x", "Crop the focused image"),
                ("r", "Reload the library"),
            )),
            ("Video viewer", (
                ("Space", "Play or pause"),
                ("o", "Mark out (use the toolbar for mark in)"),
                ("x", "Cut the marked range"),
                ("p", "Save the current frame"),
                ("+ / −", "Zoom in / out"),
            )),
        )
        for title, commands in groups:
            heading = Gtk.Label(label=title, xalign=0)
            heading.add_css_class("heading")
            heading.set_margin_top(8)
            content.append(heading)
            grid = Gtk.Grid(column_spacing=20, row_spacing=8)
            grid.set_margin_bottom(8)
            for row_index, (keys, action) in enumerate(commands):
                key_label = Gtk.Label(label=keys, xalign=1, valign=Gtk.Align.START)
                key_label.add_css_class("monospace")
                key_label.add_css_class("accent")
                action_label = Gtk.Label(label=action, xalign=0, wrap=True, hexpand=True)
                grid.attach(key_label, 0, row_index, 1, 1)
                grid.attach(action_label, 1, row_index, 1, 1)
            content.append(grid)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def on_key(_controller, keyval, _keycode, _state) -> bool:
            if keyval in (Gdk.KEY_Escape, Gdk.KEY_question):
                win.close()
                return True
            return False

        keys.connect("key-pressed", on_key)
        win.add_controller(keys)
        win.present()

    def _smart_is_under(self, smart_id: str | None, ancestor_id: str) -> bool:
        """True if *smart_id* is *ancestor_id* or a descendant of it."""
        if not smart_id or not ancestor_id:
            return False
        if smart_id == ancestor_id:
            return True
        return ancestor_id in self._smart_ancestors(smart_id)

    def _toggle_smart_expand(self, smart_id: str) -> None:
        if not smart_id:
            return
        if smart_id in self._smart_expanded:
            # A selected child would force this parent open again. Move
            # the view to the parent, then collapse.
            moved = False
            if self._smart_is_under(self.current_smart_folder_id, smart_id):
                self.current_smart_folder_id = smart_id
                self.current_folder_id = None
                self._special_view = None
                moved = True
            self._smart_expanded.discard(smart_id)
            to_drop = {
                sid
                for sid in self._smart_expanded
                if smart_id in self._smart_ancestors(sid)
            }
            self._smart_expanded -= to_drop
            self._repopulate_sidebar_keep_selection()
            if moved:
                self.refresh_items(reset_selection=True, scroll_to_top=True)
            return
        self._smart_expanded.add(smart_id)
        self._repopulate_sidebar_keep_selection()

    def _repopulate_sidebar_keep_selection(self) -> None:
        # Keep current selection visible if it's nested
        self._ensure_smart_expanded_path(self.current_smart_folder_id)
        if self.current_folder_id:
            self._folders_section_expanded = True
        self._populate_sidebar(select_current=True)
        self._save_sidebar_state()

    def _smart_label(self, sf: SmartFolder) -> str:
        """Sidebar label with live count, e.g. 'images (12)'."""
        n = self._smart_counts.get(sf.id)
        if n is None:
            # Prefer cached query; first open of a large tree is still O(n) once
            try:
                n = self.library.count_smart_folder(sf.id, fresh=False)
            except Exception:  # noqa: BLE001
                n = 0
            self._smart_counts[sf.id] = n
        return f"{sf.name} ({n})"

    def _special_label(self, view: str, base: str) -> str:
        """Sidebar label for Untagged / Intake, e.g. 'Untagged (42)'."""
        n = self._special_counts.get(view)
        if n is None:
            try:
                n = self.library.count_special_view(view)
            except Exception:  # noqa: BLE001
                n = 0
            self._special_counts[view] = n
        return f"{base} ({n})"

    def _update_smart_count_label(self, smart_id: str, count: int) -> None:
        """Patch the sidebar row label for one smart folder without full rebuild."""
        self._smart_counts[smart_id] = count
        sf = self.library.smart_folders_by_id.get(smart_id)
        if not sf:
            return
        label_text = f"{sf.name} ({count})"
        row = self.folder_list.get_first_child()
        while row is not None:
            if (
                isinstance(row, Gtk.ListBoxRow)
                and getattr(row, "row_kind", None) == "smart"
                and getattr(row, "smart_folder_id", None) == smart_id
            ):
                name_lbl = getattr(row, "name_label", None)
                if name_lbl is not None:
                    name_lbl.set_text(label_text)
                break
            row = row.get_next_sibling()

    def _update_special_count_label(self, view: str, count: int) -> None:
        """Patch Untagged / Intake sidebar label without full rebuild."""
        self._special_counts[view] = count
        base = "Untagged" if view == "untagged" else "Intake"
        label_text = f"{base} ({count})"
        row = self.folder_list.get_first_child()
        while row is not None:
            if (
                isinstance(row, Gtk.ListBoxRow)
                and getattr(row, "row_kind", None) == "special"
                and getattr(row, "special_view", None) == view
            ):
                name_lbl = getattr(row, "name_label", None)
                if name_lbl is not None:
                    name_lbl.set_text(label_text)
                break
            row = row.get_next_sibling()

    def _refresh_special_counts(self) -> None:
        """Recount Untagged / Intake in the background and patch labels."""

        def work() -> None:
            try:
                counts = {
                    "untagged": self.library.count_special_view("untagged"),
                    "uncategorized": self.library.count_special_view(
                        "uncategorized"
                    ),
                }
            except Exception:  # noqa: BLE001
                return

            def apply() -> bool:
                for view, n in counts.items():
                    self._update_special_count_label(view, n)
                return False

            GLib.idle_add(apply)

        threading.Thread(
            target=work, name="eagle-special-count", daemon=True
        ).start()

    def _append_smart_tree(self, nodes: list[SmartFolder], depth: int = 0) -> None:
        for sf in nodes:
            has_children = bool(sf.children)
            expanded = sf.id in self._smart_expanded
            self.folder_list.append(
                self._make_nav_row(
                    label=self._smart_label(sf),
                    depth=depth,
                    kind="smart",
                    smart_folder_id=sf.id,
                    has_children=has_children,
                    expanded=expanded,
                )
            )
            if has_children and expanded:
                self._append_smart_tree(sf.children, depth + 1)

    def _populate_sidebar(self, *, select_current: bool = False) -> None:
        while (child := self.folder_list.get_first_child()) is not None:
            self.folder_list.remove(child)

        all_row = self._make_nav_row(label="All items", kind="all")
        self.folder_list.append(all_row)
        self.folder_list.append(
            self._make_nav_row(
                label=self._special_label("untagged", "Untagged"),
                kind="special",
                special_view="untagged",
            )
        )
        self.folder_list.append(
            self._make_nav_row(
                label=self._special_label("uncategorized", "Intake"),
                kind="special",
                special_view="uncategorized",
            )
        )

        # Smart folders first — primary navigation; top levels collapsed by default
        self.folder_list.append(
            self._make_header_row(
                "Smart folders",
                section_id="smart",
                on_add=lambda: self.open_smart_folder_editor(),
            )
        )
        if self.library.smart_folders:
            self._append_smart_tree(self.library.smart_folders, 0)

        if self.library.folders:
            self.folder_list.append(
                self._make_header_row(
                    "Folders",
                    collapsible=True,
                    section_id="folders",
                    expanded=self._folders_section_expanded,
                )
            )
            if self._folders_section_expanded:
                for folder, depth in self.library.flatten_folders():
                    self.folder_list.append(
                        self._make_nav_row(
                            label=folder.name,
                            depth=depth,
                            kind="folder",
                            folder_id=folder.id,
                        )
                    )

        if select_current:
            self._restore_sidebar_selection()
        else:
            self.folder_list.select_row(all_row)

    def _on_sidebar_pressed(
        self, _gesture: Gtk.GestureClick, n_press: int, x: float, y: float
    ) -> None:
        """Force a view switch when row-selected would not fire.

        ``row-selected`` does not fire when the clicked row is already selected.
        That happens with the inline viewer, and with a set view that left the
        previous Uncategorized / smart-folder row highlighted.
        """
        if n_press != 1:
            return
        if not self.is_viewer_open() and self._special_view != "set":
            return
        widget = self.folder_list.pick(x, y, Gtk.PickFlags.DEFAULT)
        w = widget
        while w is not None and w is not self.folder_list:
            if isinstance(w, Gtk.Button):
                return
            w = w.get_parent()
        row = self.folder_list.get_row_at_y(int(y))
        if row is None:
            return
        kind = getattr(row, "row_kind", None)
        if kind in (None, "header", "section"):
            return
        self._sidebar_nav_lock = True
        if self.folder_list.get_selected_row() is not row:
            self.folder_list.select_row(row)
        self._apply_sidebar_navigation(row)
        GLib.idle_add(self._unlock_sidebar_nav)

    def _unlock_sidebar_nav(self) -> bool:
        self._sidebar_nav_lock = False
        return False

    def _on_sidebar_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None or self._sidebar_nav_lock:
            return
        self._apply_sidebar_navigation(row)

    def _apply_sidebar_navigation(self, row: Gtk.ListBoxRow) -> None:
        kind = getattr(row, "row_kind", None)
        if kind in ("header", "section"):
            # Section headers don't change the grid filter
            return
        new_special: str | None = None
        if kind == "smart":
            new_smart = getattr(row, "smart_folder_id", None)
            new_folder = None
        elif kind == "folder":
            new_smart = None
            new_folder = getattr(row, "folder_id", None)
        elif kind == "special":
            new_smart = None
            new_folder = None
            new_special = getattr(row, "special_view", None)
        else:  # all
            new_smart = None
            new_folder = None

        same_place = (
            self._special_view != "set"
            and new_smart == self.current_smart_folder_id
            and new_folder == self.current_folder_id
            and new_special == self._special_view
        )

        # Smart folders: always recompute count on click so the "(N)" is fresh
        if kind == "smart" and new_smart:
            def recount(sid: str = new_smart) -> None:
                try:
                    n = self.library.count_smart_folder(sid, fresh=True)
                except Exception:  # noqa: BLE001
                    return

                def apply() -> bool:
                    self._update_smart_count_label(sid, n)
                    return False

                GLib.idle_add(apply)

            # Count in background so large smart folders don't freeze the UI
            threading.Thread(
                target=recount, name="eagle-smart-count", daemon=True
            ).start()

        # Untagged / Uncategorized: same — refresh the badge on click
        if kind == "special" and new_special in ("untagged", "uncategorized"):
            def recount_special(view: str = new_special) -> None:
                try:
                    n = self.library.count_special_view(view)
                except Exception:  # noqa: BLE001
                    return

                def apply() -> bool:
                    self._update_special_count_label(view, n)
                    return False

                GLib.idle_add(apply)

            threading.Thread(
                target=recount_special, name="eagle-special-count", daemon=True
            ).start()

        # Leaving an inline preview via the sidebar: close it and drop selection
        # so the previously previewed asset is not still selected/marked.
        was_viewing = self.is_viewer_open()
        before = self._view_loc()
        if was_viewing:
            self.close_inline_viewer(restore_scroll=False)
            self._marked.clear()
            self.selected_item = None
            self._viewer_item_id = None
            try:
                self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
            except Exception:  # noqa: BLE001
                pass
            self.update_inspector()
            self._update_path_label()
            self._refresh_status()

        # Same place as already shown (e.g. returning from grid via ←) — don't re-query
        # Except smart folders: re-click still refreshes the grid after a count refresh
        # And except when we just closed a preview — still refresh so selection resets
        if same_place and kind != "smart" and not was_viewing:
            return

        self.current_smart_folder_id = new_smart
        self.current_folder_id = new_folder
        self._special_view = new_special
        if new_special != "set":
            self._set_view_tag = None
        # Reset selection when changing scope, or after closing a preview from the sidebar
        # Sidebar click always starts at the top. Dialogs still pass
        # reset_selection=False so tag/folder pickers keep their offset.
        self.refresh_items(
            reset_selection=not same_place or was_viewing,
            scroll_to_top=True,
        )
        self._save_sidebar_state()
        self._record_view_change(before)

    def _restore_sidebar_selection(self) -> None:
        target_smart = self.current_smart_folder_id
        target_folder = self.current_folder_id
        target_special = self._special_view
        if target_special == "set":
            locked = self._sidebar_nav_lock
            self._sidebar_nav_lock = True
            try:
                self.folder_list.unselect_all()
            except Exception:  # noqa: BLE001
                pass
            if not locked:
                GLib.idle_add(self._unlock_sidebar_nav)
            return
        # Expand ancestors / Folders section, rebuild if the tree shape must change
        before_smart = set(self._smart_expanded)
        before_folders = self._folders_section_expanded
        self._ensure_smart_expanded_path(target_smart)
        if target_folder:
            self._folders_section_expanded = True
        if self._smart_expanded != before_smart or self._folders_section_expanded != before_folders:
            self._populate_sidebar(select_current=False)

        row = self.folder_list.get_first_child()
        match: Gtk.ListBoxRow | None = None
        first_selectable: Gtk.ListBoxRow | None = None
        while row is not None:
            if isinstance(row, Gtk.ListBoxRow) and row.get_selectable():
                if first_selectable is None:
                    first_selectable = row
                kind = getattr(row, "row_kind", None)
                if (
                    target_smart
                    and kind == "smart"
                    and getattr(row, "smart_folder_id", None) == target_smart
                ):
                    match = row
                    break
                if (
                    target_folder
                    and kind == "folder"
                    and getattr(row, "folder_id", None) == target_folder
                ):
                    match = row
                    break
                if (
                    target_special
                    and kind == "special"
                    and getattr(row, "special_view", None) == target_special
                ):
                    match = row
                    break
                if (
                    not target_smart
                    and not target_folder
                    and not target_special
                    and kind == "all"
                ):
                    match = row
                    break
            row = row.get_next_sibling()
        self.folder_list.select_row(match or first_selectable)

    def _sidebar_expand_selected(self) -> bool:
        """Right-arrow in sidebar: expand section or smart folder with children."""
        row = self.folder_list.get_selected_row()
        if row is None:
            return False
        kind = getattr(row, "row_kind", None)
        if kind == "section":
            sid = getattr(row, "section_id", None)
            if sid == "folders" and not self._folders_section_expanded:
                self._folders_section_expanded = True
                self._repopulate_sidebar_keep_selection()
                return True
            return False
        if kind != "smart":
            return False
        if not getattr(row, "has_children", False):
            return False
        sid = getattr(row, "smart_folder_id", None)
        if not sid or sid in self._smart_expanded:
            return False
        self._smart_expanded.add(sid)
        self._repopulate_sidebar_keep_selection()
        return True

    def _sidebar_collapse_selected(self) -> bool:
        """Left-arrow in sidebar: collapse section/smart folder, or jump to parent."""
        row = self.folder_list.get_selected_row()
        if row is None:
            return False
        kind = getattr(row, "row_kind", None)
        if kind == "section":
            sid = getattr(row, "section_id", None)
            if sid == "folders" and self._folders_section_expanded:
                self._folders_section_expanded = False
                self._repopulate_sidebar_keep_selection()
                return True
            return False
        if kind == "folder":
            # Collapse Folders section; keep current grid filter, select header
            if self._folders_section_expanded:
                self._folders_section_expanded = False
                self._populate_sidebar(select_current=False)
                r = self.folder_list.get_first_child()
                while r is not None:
                    if (
                        isinstance(r, Gtk.ListBoxRow)
                        and getattr(r, "row_kind", None) == "section"
                        and getattr(r, "section_id", None) == "folders"
                    ):
                        self.folder_list.select_row(r)
                        r.grab_focus()
                        break
                    r = r.get_next_sibling()
                self._save_sidebar_state()
                return True
            return False
        if kind != "smart":
            return False
        sid = getattr(row, "smart_folder_id", None)
        if not sid:
            return False
        if getattr(row, "has_children", False) and sid in self._smart_expanded:
            self._toggle_smart_expand(sid)
            return True
        # Collapse parent path: select parent if any
        sf = self.library.smart_folders_by_id.get(sid)
        if sf and sf.parent_id:
            before = self._view_loc()
            self.current_smart_folder_id = sf.parent_id
            self.current_folder_id = None
            # Ensure parent visible; collapse is optional
            self._repopulate_sidebar_keep_selection()
            self.refresh_items()
            self._record_view_change(before)
            return True
        return False

    def _sidebar_toggle_selected(self) -> bool:
        """Enter in sidebar: toggle expand/collapse on section or smart folder."""
        row = self.folder_list.get_selected_row()
        if row is None:
            return False
        kind = getattr(row, "row_kind", None)
        if kind == "section":
            sid = getattr(row, "section_id", None)
            if sid:
                self._toggle_section(sid)
                return True
            return False
        if kind != "smart":
            return False
        if not getattr(row, "has_children", False):
            return False
        sid = getattr(row, "smart_folder_id", None)
        if not sid:
            return False
        self._toggle_smart_expand(sid)
        return True

    # ── Grid ──────────────────────────────────────────────────────────

    def _scope_label(self) -> str:
        if self._special_view == "set":
            tag = self._set_view_tag or "set"
            n = self._set_counts.get(tag, 0)
            return f"Set · {n}" if n else "Set"
        if self._special_view == "untagged":
            return "Untagged"
        if self._special_view == "uncategorized":
            return "Intake"
        if self.current_smart_folder_id:
            return "⚡ " + self.library.smart_folder_paths.get(
                self.current_smart_folder_id, self.current_smart_folder_id
            )
        if self.current_folder_id:
            scope = self.library.folder_paths.get(
                self.current_folder_id, self.current_folder_id
            )
            if self.include_descendants:
                scope += " (+sub)"
            return scope
        return "all"

    def toggle_left_sidebar(self) -> None:
        self._left_sidebar_open = not self._left_sidebar_open
        self.left_sidebar.set_visible(self._left_sidebar_open)
        self._schedule_column_sync()

    def toggle_right_sidebar(self) -> None:
        self._right_sidebar_open = not self._right_sidebar_open
        self.inspector_sidebar.set_visible(self._right_sidebar_open)
        self._schedule_column_sync()

    def _grid_scroll_value(self) -> float:
        adj = self.grid_scroll.get_vadjustment()
        return float(adj.get_value()) if adj is not None else 0.0

    def _set_grid_scroll_value(self, value: float) -> None:
        adj = self.grid_scroll.get_vadjustment()
        if adj is None:
            return
        upper = max(0.0, adj.get_upper() - adj.get_page_size())
        adj.set_value(min(max(0.0, value), upper))

    def _cancel_scroll_restore(self) -> None:
        self._scroll_restore_gen += 1
        if self._scroll_restore_source:
            try:
                GLib.source_remove(self._scroll_restore_source)
            except Exception:  # noqa: BLE001
                pass
            self._scroll_restore_source = 0

    def _restore_grid_scroll(self, value: float) -> None:
        """Re-apply a pixel offset after a store rebuild or the grid remaps."""
        self._cancel_scroll_restore()
        gen = self._scroll_restore_gen
        attempts = {"n": 0}

        def apply() -> bool:
            if gen != self._scroll_restore_gen:
                self._scroll_restore_source = 0
                return False
            adj = self.grid_scroll.get_vadjustment()
            if adj is None:
                self._scroll_restore_source = 0
                return False
            attempts["n"] += 1
            upper = adj.get_upper()
            page = adj.get_page_size()
            # Store rebuild / stack remap: upper grows after layout. Wait until
            # the saved offset is representable, or give up after ~200ms.
            max_val = max(0.0, upper - page)
            if value > 0 and max_val + 1.0 < value and attempts["n"] < 20:
                return True
            self._restoring_scroll = True
            try:
                self._set_grid_scroll_value(value)
            finally:
                self._restoring_scroll = False
            self._scroll_restore_source = 0
            # set_value was ignored by the scroll handler while restoring
            self._maybe_load_more()
            return False

        self._scroll_restore_source = GLib.timeout_add(10, apply)

    def _scroll_grid_to_top(self) -> None:
        """Pin the grid at 0 after a view change. One set_value is lost on rebuild."""
        self._cancel_scroll_restore()
        gen = self._scroll_restore_gen
        attempts = {"n": 0}

        def apply() -> bool:
            if gen != self._scroll_restore_gen:
                self._scroll_restore_source = 0
                return False
            attempts["n"] += 1
            self._restoring_scroll = True
            try:
                self._set_grid_scroll_value(0.0)
                if self.store.get_n_items() > 0:
                    try:
                        self.grid.scroll_to(0, Gtk.ListScrollFlags.NONE, None)
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                self._restoring_scroll = False
            adj = self.grid_scroll.get_vadjustment()
            at_top = adj is None or float(adj.get_value()) <= 1.0
            if at_top or attempts["n"] >= 20:
                self._scroll_restore_source = 0
                return False
            return True

        self._set_grid_scroll_value(0.0)
        self._scroll_restore_source = GLib.timeout_add(10, apply)

    def _on_grid_scrolled(self, *_args: object) -> None:
        if self._loading_more or self._restoring_scroll:
            return
        self._maybe_load_more()

    def _maybe_load_more(self) -> None:
        if self._loading_more or self._restoring_scroll:
            return
        if len(self._items) >= len(self._all_items):
            return
        adj = self.grid_scroll.get_vadjustment()
        if adj is None:
            return
        page = adj.get_page_size() or 0.0
        remaining = adj.get_upper() - adj.get_value() - page
        if remaining > max(page * 1.5, 400.0):
            return
        if self._load_more_items():
            GLib.idle_add(lambda: (self._maybe_load_more() or False))

    def _load_more_items(self, extra: int = PAGE_CHUNK) -> bool:
        """Append the next chunk of `_all_items` to the grid. True if anything added."""
        have = len(self._items)
        want = min(have + max(1, extra), len(self._all_items))
        if want <= have:
            return False
        self._loading_more = True
        try:
            for it in self._all_items[have:want]:
                self._items.append(it)
                self.store.append(ItemObject(it))
        finally:
            self._loading_more = False
        self._rebuild_scope_text()
        self._refresh_status()
        return True

    def _ensure_loaded(self, count: int) -> None:
        """Make sure at least `count` items (or the whole result) are in the store."""
        want = min(max(0, count), len(self._all_items))
        have = len(self._items)
        if want > have:
            self._load_more_items(want - have)

    def _rebuild_scope_text(self) -> None:
        total = len(self._all_items)
        shown = len(self._items)
        scope = self._scope_label()
        note = f" · showing {shown} of {total}" if shown < total else ""
        page_ids = {it.id for it in self._items}
        in_view = len(self._marked & page_ids) if self._marked else 0
        out_view = (len(self._marked) - in_view) if self._marked else 0
        self._scope_text = f"{total} items · {scope}{note}"
        if out_view > 0:
            self._scope_text += (
                f" · ✓ {len(self._marked)} selected ({out_view} off-view)"
            )

    def _set_grid_focus(self, focused: bool) -> None:
        self._grid_has_focus = focused
        # Clearing / restoring SingleSelection can jump GridView to the top.
        scroll = self._grid_scroll_value()
        if focused:
            # Restore blue highlight on last focused asset
            n = self.store.get_n_items()
            if n == 0 or self._keep_grid_unselected:
                self._set_grid_scroll_value(scroll)
                return
            idx = self._last_focus_idx if 0 <= self._last_focus_idx < n else 0
            cur = self.selection.get_selected()
            if cur == Gtk.INVALID_LIST_POSITION:
                self.selection.set_selected(idx)
                obj = self.selection.get_selected_item()
                self.selected_item = obj.item if obj else None
                if self.selected_item and not self._marked:
                    self._marked = {self.selected_item.id}
                self._update_path_label()
                self.update_inspector()
        else:
            # Hide blue selection when focus is in search / sidebars
            idx = self.selection.get_selected()
            if idx != Gtk.INVALID_LIST_POSITION:
                self._last_focus_idx = int(idx)
            try:
                self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
            except Exception:
                pass
        self._set_grid_scroll_value(scroll)

    def _on_grid_has_focus_notify(self, *_args) -> None:
        self._set_grid_focus(self.grid.has_focus())

    def refresh_items(
        self, *, reset_selection: bool = False, scroll_to_top: bool | None = None
    ) -> None:
        """
        Kick off a background query so the UI never freezes on smart folders.

        By default, multi-selection (_marked) is preserved across refresh so
        tagging/categorizing on Untagged/Uncategorized can remove items from
        the view without clearing the selection. Pass reset_selection=True
        when changing sidebar scope. scroll_to_top defaults to that same flag;
        sidebar clicks pass True even when the folder is already selected.
        """
        self._query_gen += 1
        gen = self._query_gen
        folder_id = self.current_folder_id
        smart_id = self.current_smart_folder_id
        special = self._special_view
        set_tag = self._set_view_tag if special == "set" else None
        descendants = self.include_descendants
        search = self._filter_text
        # Snapshot filters for the worker thread
        vf = self._view_filters
        scope = self._scope_label()
        self.status_left.set_text(f"Loading… · {scope}")
        # Capture selection for restore after re-query
        keep_marks = set(self._marked) if not reset_selection else set()
        keep_focus_id = (
            self.selected_item.id
            if (not reset_selection and self.selected_item is not None)
            else None
        )
        if scroll_to_top is None:
            scroll_to_top = reset_selection
        keep_scroll = 0.0 if scroll_to_top else self._grid_scroll_value()
        keep_loaded = 0 if reset_selection else len(self._items)
        if scroll_to_top:
            self._cancel_scroll_restore()

        def work() -> None:
            # Always drop query cache so tag/star/folder edits re-evaluate
            # smart folders. Scan disk only when changing sidebar scope.
            if reset_selection:
                try:
                    self.library.scan_new_items()
                except Exception:  # noqa: BLE001
                    pass
            self.library._invalidate_caches()  # noqa: SLF001
            if special == "set":
                items = self.library.query(
                    search=search,
                    include_deleted=False,
                )
                if set_tag:
                    items = [it for it in items if set_tag in it.tag_set]
                else:
                    items = []
            elif special in ("untagged", "uncategorized"):
                items = self.library.query(
                    search=search,
                    include_deleted=False,
                )
                if special == "untagged":
                    # Only assets with zero tags
                    items = [it for it in items if not it.tags]
                else:
                    # Only assets with zero folders/categories
                    items = [it for it in items if not it.folders]
            else:
                items = self.library.query(
                    folder_id=folder_id,
                    smart_folder_id=smart_id,
                    include_descendants=descendants,
                    search=search,
                    include_deleted=False,
                )
            if vf.active():
                items = [it for it in items if item_matches_view_filters(it, vf)]
            items = self._sort_items(items)
            total = len(items)
            # Keep smart-folder / special sidebar counts in sync with this view
            # (no search or type filters — those shrink the grid but not the badge)
            if smart_id and not search and not vf.active() and special is None:
                self._smart_counts[smart_id] = total
            if (
                special in ("untagged", "uncategorized")
                and not search
                and not vf.active()
            ):
                self._special_counts[special] = total
            load_n = PAGE_CHUNK if reset_selection else max(PAGE_CHUNK, keep_loaded)
            if keep_focus_id:
                for i, it in enumerate(items):
                    if it.id == keep_focus_id:
                        load_n = max(load_n, i + 1)
                        break
            load_n = min(load_n, total)
            page = items[:load_n]

            def apply() -> bool:
                if gen != self._query_gen:
                    return False  # stale
                self._rebuild_set_counts()
                if smart_id and not search and not vf.active() and special is None:
                    self._update_smart_count_label(smart_id, total)
                if (
                    special in ("untagged", "uncategorized")
                    and not search
                    and not vf.active()
                ):
                    self._update_special_count_label(special, total)
                self._all_items = items
                self._items = page
                self.store.remove_all()
                for item in page:
                    self.store.append(ItemObject(item))

                id_to_idx = {it.id: i for i, it in enumerate(page)}

                if reset_selection:
                    self._keep_grid_unselected = False

                if self._keep_grid_unselected:
                    # Empty-click unselect survives incidental refresh
                    self._marked.clear()
                    self.selected_item = None
                    self._sel_anchor = 0
                    try:
                        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
                    except Exception:  # noqa: BLE001
                        pass
                elif reset_selection or not keep_marks:
                    if page:
                        self._sel_anchor = 0
                        self._last_focus_idx = 0
                        self._marked = {page[0].id}
                        self.selected_item = page[0]
                        if self._grid_has_focus:
                            self.selection.set_selected(0)
                        else:
                            try:
                                self.selection.set_selected(
                                    Gtk.INVALID_LIST_POSITION
                                )
                            except Exception:
                                self.selection.set_selected(0)
                    else:
                        self.selected_item = None
                        self._marked.clear()
                        self._sel_anchor = 0
                        try:
                            self.selection.set_selected(
                                Gtk.INVALID_LIST_POSITION
                            )
                        except Exception:
                            pass
                else:
                    # Keep marks even for items that left this view (e.g. just
                    # tagged while on Untagged). Inspector / stage / delete
                    # still see them via _marked_items().
                    self._marked = set(keep_marks)
                    focus_idx = 0
                    if page:
                        if keep_focus_id and keep_focus_id in id_to_idx:
                            focus_idx = id_to_idx[keep_focus_id]
                        else:
                            # Prefer first still-visible marked item
                            for mid in keep_marks:
                                if mid in id_to_idx:
                                    focus_idx = id_to_idx[mid]
                                    break
                        self.selected_item = page[focus_idx]
                        self._last_focus_idx = focus_idx
                        self._sel_anchor = focus_idx
                        if self._grid_has_focus:
                            self.selection.set_selected(focus_idx)
                        else:
                            try:
                                self.selection.set_selected(
                                    Gtk.INVALID_LIST_POSITION
                                )
                            except Exception:
                                self.selection.set_selected(focus_idx)
                        # Ensure at least the focused row is marked if nothing
                        # was marked (shouldn't happen with keep_marks)
                        if not self._marked:
                            self._marked = {page[focus_idx].id}
                    else:
                        # View empty (all selected items left this filter)
                        self.selected_item = None
                        try:
                            self.selection.set_selected(
                                Gtk.INVALID_LIST_POSITION
                            )
                        except Exception:
                            pass
                        # keep_marks retained so handoff/delete still works

                self._rebuild_scope_text()
                self._refresh_status()
                self._update_path_label()
                self._rebuild_filter_chips()
                if scroll_to_top:
                    self._scroll_grid_to_top()
                elif not reset_selection:
                    self._restore_grid_scroll(keep_scroll)
                else:
                    self._set_grid_scroll_value(0.0)
                return False

            GLib.idle_add(apply)

        threading.Thread(target=work, name="eagle-query", daemon=True).start()

    def _on_sort_changed(self, dropdown: Gtk.DropDown, *_args: object) -> None:
        idx = int(dropdown.get_selected())
        if idx < 0 or idx >= len(SORT_IDS):
            return
        new_key = SORT_IDS[idx]
        if new_key == self._sort_key:
            return
        self._sort_key = new_key
        self.refresh_items()

    @staticmethod
    def _added_sort_key(it: Item) -> int:
        """Library-add time. Ignore missing or future file-birth values."""
        t = int(it.btime or 0)
        now_ms = int(time.time() * 1000)
        if t <= 0 or t > now_ms + 60_000:
            t = int(it.modification_time or 0)
        return t

    def _sort_items(self, items: list[Item]) -> list[Item]:
        """Sort a query result list by the current top-bar sort option."""
        key = self._sort_key

        def name_key(it: Item) -> str:
            return it.display_name.lower()

        def rating_key(it: Item) -> int:
            return int(it.star or 0)

        def duration_key(it: Item) -> float:
            return float(it.duration or 0.0)

        def added_key(it: Item) -> int:
            return self._added_sort_key(it)

        if key == "added_desc":
            return sorted(items, key=added_key, reverse=True)
        if key == "added_asc":
            return sorted(items, key=added_key)
        if key == "mtime_desc":
            return sorted(items, key=lambda it: it.modification_time, reverse=True)
        if key == "mtime_asc":
            return sorted(items, key=lambda it: it.modification_time)
        if key == "name_asc":
            return sorted(items, key=name_key)
        if key == "name_desc":
            return sorted(items, key=name_key, reverse=True)
        if key == "size_desc":
            return sorted(items, key=lambda it: it.size, reverse=True)
        if key == "size_asc":
            return sorted(items, key=lambda it: it.size)
        if key == "rating_desc":
            return sorted(items, key=rating_key, reverse=True)
        if key == "rating_asc":
            return sorted(items, key=rating_key)
        if key == "duration_desc":
            return sorted(items, key=duration_key, reverse=True)
        if key == "duration_asc":
            return sorted(items, key=duration_key)
        return items

    def _on_factory_setup(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        # Card sizes applied in bind from self._thumb_size (+/- zoom)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.set_hexpand(False)
        card.set_vexpand(False)
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.START)
        card.set_margin_start(4)
        card.set_margin_end(4)
        card.set_margin_top(4)
        card.set_margin_bottom(4)

        # Clip host so nothing paints outside the square
        tile = Gtk.Overlay()
        tile.set_hexpand(False)
        tile.set_vexpand(False)
        tile.set_overflow(Gtk.Overflow.HIDDEN)
        tile.add_css_class("card")
        tile.add_css_class("frame")

        picture = Gtk.Picture()
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_can_shrink(True)
        picture.set_halign(Gtk.Align.FILL)
        picture.set_valign(Gtk.Align.FILL)
        tile.set_child(picture)

        # This is an overlay (rather than a border on the tile itself), so the
        # selected outline is always painted above bright or dark thumbnails.
        selection_outline = Gtk.Box()
        selection_outline.add_css_class("asset-selection-outline")
        selection_outline.set_halign(Gtk.Align.FILL)
        selection_outline.set_valign(Gtk.Align.FILL)
        selection_outline.set_can_target(False)
        selection_outline.set_visible(False)
        tile.add_overlay(selection_outline)

        # Multi-select mark (top-left)
        mark = Gtk.Label(label="✓")
        mark.add_css_class("osd")
        mark.add_css_class("heading")
        mark.add_css_class("asset-selection-mark")
        mark.set_halign(Gtk.Align.START)
        mark.set_valign(Gtk.Align.START)
        mark.set_margin_start(6)
        mark.set_margin_top(4)
        mark.set_visible(False)
        tile.add_overlay(mark)

        # Star rating (top-right)
        stars = Gtk.Label(xalign=1.0)
        stars.add_css_class("osd")
        stars.add_css_class("caption")
        stars.set_halign(Gtk.Align.END)
        stars.set_valign(Gtk.Align.START)
        stars.set_margin_end(4)
        stars.set_margin_top(4)
        stars.set_visible(False)
        tile.add_overlay(stars)

        # Type / duration badge (video / audio / other)
        badge = Gtk.Label(xalign=1.0)
        badge.add_css_class("osd")
        badge.add_css_class("grid-duration")
        badge.set_halign(Gtk.Align.END)
        badge.set_valign(Gtk.Align.END)
        badge.set_margin_end(4)
        badge.set_margin_bottom(4)
        tile.add_overlay(badge)

        # Set member count (bottom-left)
        set_badge = Gtk.Label(xalign=0.0)
        set_badge.add_css_class("osd")
        set_badge.add_css_class("caption")
        set_badge.set_halign(Gtk.Align.START)
        set_badge.set_valign(Gtk.Align.END)
        set_badge.set_margin_start(4)
        set_badge.set_margin_bottom(2)
        set_badge.set_visible(False)
        tile.add_overlay(set_badge)

        # Fallback icon when no thumb decodes
        icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        icon.set_pixel_size(48)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        icon.set_visible(False)
        tile.add_overlay(icon)

        card.append(tile)

        cap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        cap.set_hexpand(False)
        label = Gtk.Label(xalign=0.0, ellipsize=3, hexpand=True)
        label.add_css_class("caption")
        dur_lbl = Gtk.Label(xalign=1.0)
        dur_lbl.add_css_class("caption")
        dur_lbl.add_css_class("dim-label")
        dur_lbl.set_visible(False)
        cap.append(label)
        cap.append(dur_lbl)
        card.append(cap)

        list_item.set_child(card)
        list_item.picture = picture  # type: ignore[attr-defined]
        list_item.label = label  # type: ignore[attr-defined]
        list_item.dur_lbl = dur_lbl  # type: ignore[attr-defined]
        list_item.caption_row = cap  # type: ignore[attr-defined]
        list_item.badge = badge  # type: ignore[attr-defined]
        list_item.set_badge = set_badge  # type: ignore[attr-defined]
        list_item.icon = icon  # type: ignore[attr-defined]
        list_item.mark = mark  # type: ignore[attr-defined]
        list_item.selection_outline = selection_outline  # type: ignore[attr-defined]
        list_item.stars = stars  # type: ignore[attr-defined]
        list_item.card = card  # type: ignore[attr-defined]
        list_item.tile = tile  # type: ignore[attr-defined]

        # Click multi-select: plain / Shift-range / Ctrl-toggle
        # Do not rebuild the ListStore on click — that tears down widgets mid
        # gesture and prevents GridView "activate" (double-click) from firing.
        click = Gtk.GestureClick()
        click.set_button(1)

        def on_click(
            gesture: Gtk.GestureClick,
            n_press: int,
            _x: float,
            _y: float,
            li: Gtk.ListItem = list_item,
        ) -> None:
            pos = li.get_position()
            if pos == Gtk.INVALID_LIST_POSITION:
                return
            state = gesture.get_current_event_state()
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            self._select_index(int(pos), ctrl=ctrl, shift=shift, from_click=True)
            # Open on double-click here. GridView "activate" is intentionally a
            # no-op — both used to fire and spawn two viewers.
            if n_press == 2 and not ctrl and not shift:
                self.open_selected()

        click.connect("pressed", on_click)
        card.add_controller(click)

        set_click = Gtk.GestureClick()
        set_click.set_button(1)

        def on_set_badge(
            gesture: Gtk.GestureClick,
            _n: int,
            _x: float,
            _y: float,
            li: Gtk.ListItem = list_item,
        ) -> None:
            obj = li.get_item()
            if obj is None:
                return
            tag = set_tag_of(obj.item)
            if not tag:
                return
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.open_set_view(tag, keep_id=obj.item.id)

        set_click.connect("pressed", on_set_badge)
        set_badge.add_controller(set_click)

    def _on_factory_unbind(
        self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem
    ) -> None:
        self._live_list_items.discard(list_item)

    def _apply_thumb_geometry(self, list_item: Gtk.ListItem) -> int:
        """Size card/tile/picture for current zoom level. Returns edge length."""
        size = self._thumb_size
        card: Gtk.Box = list_item.card  # type: ignore[attr-defined]
        tile: Gtk.Overlay = list_item.tile  # type: ignore[attr-defined]
        picture: Gtk.Picture = list_item.picture  # type: ignore[attr-defined]
        label: Gtk.Label = list_item.label  # type: ignore[attr-defined]
        card.set_size_request(_cell_w(size), _cell_h(size))
        tile.set_size_request(size, size)
        picture.set_size_request(size, size)
        cap = getattr(list_item, "caption_row", None)
        if cap is not None:
            cap.set_size_request(size, -1)
        else:
            label.set_size_request(size, -1)
        return size

    def _on_factory_bind(self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        obj = list_item.get_item()
        if obj is None:
            return
        self._live_list_items.add(list_item)
        item: Item = obj.item
        size = self._apply_thumb_geometry(list_item)
        list_item.label.set_text(item.display_name)  # type: ignore[attr-defined]
        picture: Gtk.Picture = list_item.picture  # type: ignore[attr-defined]
        badge: Gtk.Label = list_item.badge  # type: ignore[attr-defined]
        icon: Gtk.Image = list_item.icon  # type: ignore[attr-defined]
        mark: Gtk.Label = list_item.mark  # type: ignore[attr-defined]
        stars_lbl: Gtk.Label = list_item.stars  # type: ignore[attr-defined]
        card: Gtk.Box = list_item.card  # type: ignore[attr-defined]

        marked = item.id in self._marked
        mark.set_visible(marked)
        list_item.selection_outline.set_visible(marked)  # type: ignore[attr-defined]
        if marked:
            card.add_css_class("asset-selected")
        else:
            card.remove_css_class("asset-selected")

        if item.star and 1 <= item.star <= 5:
            stars_lbl.set_text("★" * item.star)
            stars_lbl.set_visible(True)
        else:
            stars_lbl.set_text("")
            stars_lbl.set_visible(False)

        badge_text = _type_badge(item)
        badge.set_text(badge_text)
        badge.set_visible(bool(badge_text))
        dur_lbl = getattr(list_item, "dur_lbl", None)
        if (item.is_video or item.is_audio) and item.duration:
            clock = _fmt_grid_duration(item.duration)
            badge.set_tooltip_text(f"{item.duration:.1f}s")
            if dur_lbl is not None:
                dur_lbl.set_text(clock)
                dur_lbl.set_visible(True)
        else:
            badge.set_tooltip_text("")
            if dur_lbl is not None:
                dur_lbl.set_text("")
                dur_lbl.set_visible(False)

        set_badge: Gtk.Label = list_item.set_badge  # type: ignore[attr-defined]
        stag = set_tag_of(item)
        scount = self._set_counts.get(stag, 0) if stag else 0
        if stag and scount:
            set_badge.set_text(str(scount))
            set_badge.set_visible(True)
            set_badge.set_tooltip_text(f"{stag} · {scount}")
        else:
            set_badge.set_text("")
            set_badge.set_visible(False)
            set_badge.set_tooltip_text("")

        # Generation token so recycled list items don't get the wrong late-arriving thumb
        list_item._thumb_gen = getattr(list_item, "_thumb_gen", 0) + 1  # type: ignore[attr-defined]
        gen = list_item._thumb_gen  # type: ignore[attr-defined]

        path = _thumb_path_for(item)
        if path is None:
            picture.set_paintable(None)
            picture.set_visible(False)
            if item.is_video:
                icon.set_from_icon_name("video-x-generic-symbolic")
            elif item.is_audio:
                icon.set_from_icon_name("audio-x-generic-symbolic")
            else:
                icon.set_from_icon_name("folder-documents-symbolic")
            icon.set_visible(True)
            return

        cache_key = _thumb_cache_key(path, size)
        with _thumb_lock:
            cached = _thumb_textures.get(cache_key)
        if cached is not None:
            picture.set_paintable(cached)
            picture.set_visible(True)
            icon.set_visible(False)
            return

        # Placeholder while decoding off-thread
        picture.set_paintable(None)
        picture.set_visible(False)
        if item.is_video:
            icon.set_from_icon_name("video-x-generic-symbolic")
        elif item.is_audio:
            icon.set_from_icon_name("audio-x-generic-symbolic")
        else:
            icon.set_from_icon_name("image-x-generic-symbolic")
        icon.set_visible(True)

        already_loading = False
        with _thumb_lock:
            _thumb_waiters.setdefault(cache_key, []).append((list_item, gen))
            if cache_key in _thumb_inflight:
                already_loading = True
            else:
                _thumb_inflight.add(cache_key)
        if already_loading:
            return

        def work() -> None:
            pixbuf = _decode_square_pixbuf(path, size)

            def apply() -> bool:
                texture = None
                if pixbuf is not None:
                    texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                with _thumb_lock:
                    _thumb_inflight.discard(cache_key)
                    waiters = _thumb_waiters.pop(cache_key, [])
                    if texture is not None:
                        if len(_thumb_textures) >= _THUMB_CACHE_MAX:
                            for _ in range(40):
                                try:
                                    _thumb_textures.pop(next(iter(_thumb_textures)))
                                except StopIteration:
                                    break
                        _thumb_textures[cache_key] = texture
                if texture is None:
                    return False
                for li, waiter_gen in waiters:
                    if getattr(li, "_thumb_gen", None) != waiter_gen:
                        continue
                    pic = getattr(li, "picture", None)
                    icn = getattr(li, "icon", None)
                    if pic is None:
                        continue
                    pic.set_paintable(texture)
                    pic.set_visible(True)
                    if icn is not None:
                        icn.set_visible(False)
                return False

            GLib.idle_add(apply)

        try:
            _thumb_executor.submit(work)
        except RuntimeError:
            # Pool shut down on window close
            with _thumb_lock:
                _thumb_inflight.discard(cache_key)
                _thumb_waiters.pop(cache_key, None)

    def _on_grid_selection(self, selection: Gtk.SingleSelection, _pspec) -> None:
        obj = selection.get_selected_item()
        self.selected_item = obj.item if obj else None
        self._update_path_label()
        self.update_inspector()

    def _on_grid_activate(self, _grid: Gtk.GridView, _position: int) -> None:
        self.open_selected()

    def _picked_is_grid_item(self, widget: Gtk.Widget | None) -> bool:
        """True if pick() landed on a thumb cell, not the grid background."""
        if widget is None:
            return False
        w: Gtk.Widget | None = widget
        while w is not None:
            if w is self.grid:
                return widget is not self.grid
            if w is self.grid_scroll:
                return False
            w = w.get_parent()
        return False

    @staticmethod
    def _picked_is_scrollbar(widget: Gtk.Widget | None) -> bool:
        w: Gtk.Widget | None = widget
        while w is not None:
            if isinstance(w, Gtk.Scrollbar):
                return True
            w = w.get_parent()
        return False

    def _on_grid_background_pressed(
        self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float
    ) -> None:
        """Unselect when the click is on empty grid space, not a thumb."""
        if n_press != 1:
            return
        if self.is_viewer_open():
            return
        origin = gesture.get_widget()
        if origin is None:
            return
        picked = origin.pick(
            x,
            y,
            Gtk.PickFlags.INSENSITIVE | Gtk.PickFlags.NON_TARGETABLE,
        )
        if self._picked_is_scrollbar(picked):
            return
        if self._picked_is_grid_item(picked):
            return
        self._clear_grid_selection()

    def _clear_grid_selection(self) -> None:
        """Drop focus + multi-selection so the inspector shows nothing."""
        already_clear = (
            not self._marked
            and self.selected_item is None
            and self.selection.get_selected() == Gtk.INVALID_LIST_POSITION
        )
        self._keep_grid_unselected = True
        if already_clear:
            return
        self._marked.clear()
        self.selected_item = None
        try:
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        except Exception:  # noqa: BLE001
            pass
        self._sync_mark_overlays()
        self._update_path_label()
        self._refresh_status()

    def _update_path_label(self) -> None:
        if self.selected_item:
            item = self.selected_item
            star = f" · {'★' * item.star}" if item.star else ""
            tags = f" · [{', '.join(item.tags[:6])}]" if item.tags else ""
            self.status_path.set_text(f"{item.display_name}{star}{tags}  ·  {item.path}")
        else:
            self.status_path.set_text("")

    def _refresh_status(self) -> None:
        marks = len(self._marked)
        mark_bit = f" · ✓ {marks} selected" if marks > 1 else (
            f" · ✓ 1 selected" if marks == 1 else ""
        )
        base = getattr(self, "_scope_text", "") or ""
        self.status_left.set_text(f"{base}{mark_bit}")
        self.update_inspector()

    def _marked_items(self) -> list[Item]:
        """Selected items in current view order, then any ids not in this result."""
        seen: set[str] = set()
        out: list[Item] = []
        for it in self._all_items or self._items:
            if it.id in self._marked:
                out.append(it)
                seen.add(it.id)
        for mid in self._marked:
            if mid in seen:
                continue
            it = self.library.items_by_id.get(mid)
            if it is not None:
                out.append(it)
        return out

    def _effective_hand_off_items(self) -> list[Item]:
        """Multi-selection if any; otherwise the focused item alone."""
        marked = self._marked_items()
        if marked:
            return marked
        if self.selected_item is not None:
            return [self.selected_item]
        return []

    def _id_at_index(self, idx: int) -> str | None:
        if idx < 0 or idx >= len(self._items):
            return None
        return self._items[idx].id

    def _select_index(
        self,
        idx: int,
        *,
        ctrl: bool = False,
        shift: bool = False,
        from_click: bool = False,
    ) -> None:
        """Update focus + multi-selection (replace / Ctrl-toggle / Shift-range)."""
        n = len(self._items)
        if n == 0 or idx < 0 or idx >= n:
            return
        self._keep_grid_unselected = False
        item = self._items[idx]
        self._last_focus_idx = idx
        self.selection.set_selected(idx)
        self.grid.scroll_to(
            idx, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None
        )

        if shift:
            # Fill every item from the click/keyboard anchor to here.
            anchor = self._sel_anchor
            if anchor < 0 or anchor >= n:
                anchor = idx
            lo, hi = (anchor, idx) if anchor <= idx else (idx, anchor)
            self._marked = {self._items[i].id for i in range(lo, hi + 1)}
        elif ctrl:
            if item.id in self._marked:
                self._marked.discard(item.id)
                # Keep at least the focused item selected if emptied
                if not self._marked:
                    self._marked.add(item.id)
            else:
                self._marked.add(item.id)
            # Anchor stays put for further Shift paths
        else:
            self._marked = {item.id}
            self._sel_anchor = idx

        self.selected_item = item
        # In-place mark overlays — full rebind would break double-click open.
        self._sync_mark_overlays()
        self._refresh_status()
        self._update_path_label()
        if from_click:
            self.grid.grab_focus()

    def _sync_mark_overlays(self) -> None:
        """Update ✓ / accent on currently bound tiles without rebuilding store."""
        for li in list(self._live_list_items):
            obj = li.get_item()
            if obj is None:
                continue
            item: Item = obj.item
            mark: Gtk.Label = getattr(li, "mark", None)  # type: ignore[assignment]
            card: Gtk.Box = getattr(li, "card", None)  # type: ignore[assignment]
            outline: Gtk.Box = getattr(li, "selection_outline", None)  # type: ignore[assignment]
            if mark is None or card is None or outline is None:
                continue
            marked = item.id in self._marked
            mark.set_visible(marked)
            outline.set_visible(marked)
            if marked:
                card.add_css_class("asset-selected")
            else:
                card.remove_css_class("asset-selected")

    def _sync_star_overlays(self) -> None:
        """Update ★ overlays on visible tiles without rebuilding the grid."""
        for li in list(self._live_list_items):
            obj = li.get_item()
            if obj is None:
                continue
            item: Item = obj.item
            stars_lbl: Gtk.Label = getattr(li, "stars", None)  # type: ignore[assignment]
            if stars_lbl is None:
                continue
            if item.star and 1 <= item.star <= 5:
                stars_lbl.set_text("★" * item.star)
                stars_lbl.set_visible(True)
            else:
                stars_lbl.set_text("")
                stars_lbl.set_visible(False)

    def _clipboard_set_text(self, text: str) -> bool:
        display = Gdk.Display.get_default()
        if display is None:
            self._toast("No display for clipboard")
            return False
        display.get_clipboard().set(text)
        try:
            subprocess.run(["wl-copy", text], check=False, timeout=3)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return True

    def _rebind_grid_keep_selection(self) -> None:
        """Rebuild ListStore so mark overlays rebind; keep cursor and scroll."""
        scroll = self._grid_scroll_value()
        idx = self.selection.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION:
            idx = 0
        items = list(self._items)
        self.store.remove_all()
        for item in items:
            self.store.append(ItemObject(item))
        n = self.store.get_n_items()
        if n and int(idx) < n:
            self.selection.set_selected(int(idx))
        self._restore_grid_scroll(scroll)

    def adjust_thumb_size(self, direction: int) -> None:
        """direction +1 larger, -1 smaller (keyboard +/-)."""
        new = self._thumb_size + direction * THUMB_SIZE_STEP
        new = max(THUMB_SIZE_MIN, min(THUMB_SIZE_MAX, new))
        if new == self._thumb_size:
            self._toast(f"Thumb size · {self._thumb_size}px (limit)")
            return
        self._thumb_size = new
        self._rebind_grid_keep_selection()
        self._sync_columns()
        self._toast(f"Thumb size · {self._thumb_size}px")

    def delete_selected(self) -> None:
        """
        Soft-delete focused / multi-selected items (Eagle isDeleted).

        Files stay on disk; Ctrl+Z restores the last batch.
        """
        from write import WriteError

        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing to delete")
            return
        ids = [it.id for it in items if not it.is_deleted]
        if not ids:
            self._toast("Already deleted")
            return
        try:
            ok_ids, errors = self.library.set_items_deleted(ids, deleted=True)
        except WriteError as exc:
            self._toast(str(exc))
            return
        if ok_ids:
            self._delete_undo_stack.append(list(ok_ids))
            # Cap undo history
            if len(self._delete_undo_stack) > 50:
                self._delete_undo_stack = self._delete_undo_stack[-50:]
            if self.is_viewer_open():
                self.close_inline_viewer(restore_scroll=True)
            self._marked.clear()
            self.refresh_items(reset_selection=False, scroll_to_top=False)
            msg = f"Deleted {len(ok_ids)} · Ctrl+Z undo"
            if errors:
                msg += f" · {len(errors)} failed"
            self._toast(msg)
        elif errors:
            self._toast(errors[0])

    def undo_delete(self) -> None:
        """Restore the last soft-deleted batch (Ctrl+Z)."""
        from write import WriteError

        if not self._delete_undo_stack:
            self._toast("Nothing to undo")
            return
        ids = self._delete_undo_stack.pop()
        try:
            ok_ids, errors = self.library.set_items_deleted(ids, deleted=False)
        except WriteError as exc:
            # Put batch back so user can retry
            self._delete_undo_stack.append(ids)
            self._toast(str(exc))
            return
        if ok_ids:
            self.refresh_items()
            # Re-select restored items if still in view
            self._marked = set(ok_ids)
            msg = f"Restored {len(ok_ids)}"
            if errors:
                msg += f" · {len(errors)} failed"
            self._toast(msg)
        else:
            self._toast(errors[0] if errors else "Undo failed")

    # ── Actions ───────────────────────────────────────────────────────

    def toggle_mark_selected(self) -> None:
        """Space: toggle focused item in multi-selection (like Ctrl+click)."""
        idx = self.selection.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION:
            return
        self._select_index(int(idx), ctrl=True, shift=False)

    def select_all_visible(self) -> None:
        """Multi-select every asset in the current view (Ctrl+A), including not-yet-loaded."""
        pool = self._all_items or self._items
        if not pool:
            self._toast("Nothing to select")
            return
        self._keep_grid_unselected = False
        self._marked = {it.id for it in pool}
        # Keep focus; anchor at first item for further Shift ranges
        self._sel_anchor = 0
        idx = self.selection.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or int(idx) >= len(self._items):
            self.selection.set_selected(0)
            idx = 0
        if self._items:
            self.selected_item = self._items[int(idx)]
            self._last_focus_idx = int(idx)
        self._sync_mark_overlays()
        self._rebuild_scope_text()
        self._refresh_status()
        self._update_path_label()
        self.update_inspector()
        self.grid.grab_focus()
        n = len(self._marked)
        scope = getattr(self, "_scope_text", "") or ""
        self._toast(f"Selected all {n} in view" + (f" · {scope}" if scope else ""))

    def clear_marks(self) -> bool:
        if len(self._marked) <= 1:
            # Collapse multi-select to focused only
            if self.selected_item and len(self._marked) == 1:
                return False
            if not self._marked:
                return False
        n = len(self._marked)
        if self.selected_item:
            self._marked = {self.selected_item.id}
            idx = self.selection.get_selected()
            if idx != Gtk.INVALID_LIST_POSITION:
                self._sel_anchor = int(idx)
        else:
            self._marked.clear()
        self._rebind_grid_keep_selection()
        self._refresh_status()
        self._toast(f"Selection cleared · was {n}")
        return True

    def copy_selected_path(self) -> None:
        """Copy focused path, or all selected paths if multi-selected."""
        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return
        if len(items) > 1:
            text = "\n".join(str(it.path.resolve()) for it in items)
            if not self._clipboard_set_text(text):
                return
            self._toast(f"Copied {len(items)} paths")
            return
        path = str(items[0].path)
        if not self._clipboard_set_text(path):
            return
        # Hint for GTK/Omarchy file pickers (website uploads, etc.)
        self._toast("Copied · Ctrl+L in file dialog, paste path, Enter")

    def copy_selected_ids(self) -> None:
        """Copy Eagle item id(s) — plain `y`. Safe to paste into agent CLIs (not a file path)."""
        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return
        if len(items) > 1:
            text = "\n".join(it.id for it in items)
            if not self._clipboard_set_text(text):
                return
            self._toast(f"Copied {len(items)} Eagle ids")
            return
        iid = items[0].id
        if not self._clipboard_set_text(iid):
            return
        self._toast(f"Copied Eagle id · {iid}")

    def open_selected_in_clip_editor(self, *, new_project: bool = False) -> None:
        """Shift+E adds to the current clip-editor project. Ctrl+Shift+E starts a new one."""
        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return
        item = items[0]
        sid = self.selected_item.id if self.selected_item is not None else None
        if sid:
            for it in items:
                if it.id == sid:
                    item = it
                    break
        if not item.path.is_file():
            self._toast("File missing")
            return
        if item.is_video:
            flag = "--video"
        elif item.is_audio:
            flag = "--audio"
        else:
            self._toast("Clip editor is for video or audio")
            return
        exe = shutil.which("clip-editor")
        if not exe:
            bundled = Path.home() / "tech/clip-editor/clip-editor"
            exe = str(bundled) if bundled.is_file() else None
        if not exe:
            self._toast("clip-editor not found")
            return
        path = str(item.path.resolve())
        cmd = [exe, "gui"]
        if new_project:
            cmd.append("--new")
        cmd.extend([flag, path])
        if not _spawn_detached(cmd):
            self._toast("Could not open clip editor")
            return
        if new_project:
            self._toast(f"Editor new · {item.display_name}")
        elif item.is_audio:
            self._toast(f"Editor audio · {item.display_name}")
        else:
            self._toast(f"Editor add · {item.display_name}")

    def reveal_selected_in_files(self) -> None:
        """Open Nautilus with the focused (or first marked) file selected."""
        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return
        path = str(items[0].path.resolve())
        # Prefer selecting the file; fall back to opening its folder
        commands = [
            ["nautilus", "--select", path],
            ["xdg-open", str(Path(path).parent)],
        ]
        for cmd in commands:
            if not _spawn_detached(cmd):
                continue
            if cmd[0] == "nautilus":
                self._toast("Opened in Files · drag into the upload dialog if needed")
            return
        self._toast("Could not open file manager")

    def copy_marked_paths(self, *, as_file_uris: bool = False) -> None:
        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing to copy — mark with Space, or select one")
            return
        if as_file_uris:
            lines = [
                "file://" + urllib.parse.quote(str(it.path.resolve()), safe="/:")
                for it in items
            ]
        else:
            lines = [str(it.path.resolve()) for it in items]
        text = "\n".join(lines)
        if not self._clipboard_set_text(text):
            return
        kind = "file URIs" if as_file_uris else "paths"
        if len(items) == 1:
            self._toast(f"Copied {kind} · {items[0].display_name}")
        else:
            self._toast(f"Copied {len(items)} {kind}")

    def set_rating(self, star: int) -> None:
        """star 1–5, or 0 to clear. Applies to marked items, else focused."""
        from write import WriteError

        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return
        ids = [it.id for it in items]
        try:
            if len(ids) == 1:
                self.library.update_item(ids[0], star=star if star else None)
                ok, errors = 1, []
            else:
                ok, errors = self.library.update_items_batch(
                    ids, star=star if star else None
                )
        except WriteError as exc:
            self._toast(str(exc))
            return
        self._sync_star_overlays()
        self.refresh_items(reset_selection=False, scroll_to_top=False)
        self._update_path_label()
        self.update_inspector()
        if star:
            msg = f"Rated {'★' * star} · {ok} item(s)"
        else:
            msg = f"Cleared rating · {ok} item(s)"
        if errors:
            msg += f" · {len(errors)} failed"
        self._toast(msg)

    def open_rename_dialog(self) -> None:
        """Rename the focused item's file stem. Thumbnail is renamed with it."""
        if self._picker_blocking:
            # Rename is an explicit action, so never swallow it behind the
            # shared picker flag. Close a tracked transient and repair any
            # orphaned state before opening the rename window.
            self._close_open_dialog()
            self._picker_blocking = False
        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return
        if len(items) != 1:
            self._toast("Rename one file at a time")
            return
        item = items[0]
        if item.item_dir is None or not item.path.is_file():
            self._toast("Cannot rename this item")
            return

        dialog = Adw.AlertDialog(
            heading="Rename file",
            body="The Eagle id stays the same.",
        )
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        entry = Gtk.Entry()
        entry.set_text(item.name)
        entry.set_hexpand(True)
        entry.set_activates_default(True)
        row.append(entry)
        if item.ext:
            ext_lbl = Gtk.Label(label=f".{item.ext}")
            ext_lbl.add_css_class("dim-label")
            row.append(ext_lbl)
        dialog.set_extra_child(row)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance(
            "rename", Adw.ResponseAppearance.SUGGESTED
        )
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response != "rename":
                return
            from write import WriteError

            raw = entry.get_text()
            try:
                self._invalidate_thumb_cache_for(item)
                it = self.library.rename_item(item.id, raw)
            except WriteError as exc:
                self._toast(str(exc))
                return
            except OSError as exc:
                self._toast(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                # GUI launches normally have no terminal, so an unexpected
                # failure must be visible in-app instead of looking like a
                # dead button.
                self._toast(f"Could not rename: {exc}")
                return
            self._refresh_after_in_place_edit(it)
            if self._sort_key.startswith("name"):
                self.refresh_items(reset_selection=False)
            self._toast(f"Renamed · {it.display_name}")

        dialog.connect("response", on_response)
        dialog.present(self)

        def focus_name() -> bool:
            entry.grab_focus()
            entry.select_region(0, -1)
            return False

        GLib.idle_add(focus_name)

    def edit_notes_dialog(self) -> None:
        """View or edit Eagle annotation (notes) for the selection."""
        if self._picker_blocking:
            return
        from write import WriteError

        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return

        n = len(items)
        notes = [(it.annotation or "") for it in items]
        unique = set(notes)
        initial = next(iter(unique)) if len(unique) == 1 else ""
        mixed = len(unique) > 1

        win = Gtk.Window(
            title="Notes" if n == 1 else f"Notes · {n} items",
            transient_for=self,
            modal=True,
            default_width=480,
            default_height=360,
        )
        self._remember_dialog(win)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        win.set_child(box)

        if n == 1:
            subtitle = items[0].display_name
        elif mixed:
            subtitle = f"{n} items · notes differ — saving replaces all"
        else:
            subtitle = f"{n} items · shared note"
        hint = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        hint.add_css_class("dim-label")
        box.append(hint)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_hexpand(True)
        scroll.set_min_content_height(180)
        try:
            scroll.set_has_frame(True)
        except AttributeError:
            pass
        text = Gtk.TextView()
        text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text.set_accepts_tab(False)
        text.set_top_margin(8)
        text.set_bottom_margin(8)
        text.set_left_margin(8)
        text.set_right_margin(8)
        buf = text.get_buffer()
        buf.set_text(initial)
        scroll.set_child(text)
        box.append(scroll)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.add_css_class("flat")
        clear_btn.set_halign(Gtk.Align.START)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        cancel = Gtk.Button(label="Cancel")
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        btns.append(clear_btn)
        btns.append(spacer)
        btns.append(cancel)
        btns.append(save)
        box.append(btns)

        def close_win(*_a) -> None:
            if self._open_dialog is win:
                self._open_dialog = None
            self._picker_blocking = False
            try:
                win.close()
            except Exception:
                win.destroy()

        def read_text() -> str:
            start = buf.get_start_iter()
            end = buf.get_end_iter()
            return buf.get_text(start, end, include_hidden_chars=False)

        def do_save(value: str) -> None:
            ids = [it.id for it in items]

            def apply() -> None:
                try:
                    if len(ids) == 1:
                        self.library.update_item(ids[0], annotation=value)
                        ok, errors = 1, []
                    else:
                        ok, errors = self.library.update_items_batch(
                            ids, annotation=value
                        )
                except WriteError as exc:
                    self._toast(str(exc))
                    return
                self.update_inspector()
                if value.strip():
                    msg = f"Note saved · {ok} item(s)"
                else:
                    msg = f"Note cleared · {ok} item(s)"
                if errors:
                    msg += f" · {len(errors)} failed"
                self._toast(msg)
                close_win()

            if n > BULK_EDIT_CONFIRM:
                self._confirm_bulk_edit(
                    n,
                    heading=f"Update notes on {n} items?",
                    body="The same note text will be written to every selected item.",
                    apply_fn=apply,
                    parent=win,
                )
                return
            apply()

        def on_save(*_a) -> None:
            do_save(read_text())

        def on_clear(*_a) -> None:
            buf.set_text("")
            text.grab_focus()

        cancel.connect("clicked", close_win)
        save.connect("clicked", on_save)
        clear_btn.connect("clicked", on_clear)

        def on_key(_c, keyval: int, _kc: int, state: Gdk.ModifierType) -> bool:
            if keyval == Gdk.KEY_Escape:
                close_win()
                return True
            # Ctrl+Enter / Ctrl+S saves (plain Enter inserts a newline in TextView)
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            if ctrl and keyval in (
                Gdk.KEY_Return,
                Gdk.KEY_KP_Enter,
                Gdk.KEY_s,
                Gdk.KEY_S,
            ):
                on_save()
                return True
            return False

        # Capture on the window so shortcuts work while the TextView has focus
        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", on_key)
        win.add_controller(key)
        win.connect("close-request", lambda *_: False)
        win.present()
        text.grab_focus()

    def crop_selected_videos_916(self) -> None:
        """Center-crop selected videos to 9:16 as new untagged items."""
        if self._picker_blocking:
            return
        items = self._effective_hand_off_items()
        videos = [it for it in items if it.is_video and it.path.is_file()]
        if not videos:
            self._toast("Select a video")
            return
        from crop import resolve_crop_rect, save_video_crop_as_new_item
        from import_media import _video_meta
        from write import WriteError

        created: list[Item] = []
        skipped = 0
        errors: list[str] = []
        for it in videos:
            w, h = int(it.width or 0), int(it.height or 0)
            if w <= 0 or h <= 0:
                w, h, _ = _video_meta(it.path)
            if w > 0 and h > 0 and abs((w / h) - (9 / 16)) < 0.02:
                skipped += 1
                continue
            try:
                rect = resolve_crop_rect(w or 0, h or 0, aspect="9:16", anchor="center")
                new = save_video_crop_as_new_item(self.library.root, it, rect)
                self.library.upsert_item(new)
                new = self._auto_join_set(it, new)
                created.append(new)
            except WriteError as exc:
                errors.append(f"{it.display_name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{it.display_name}: {exc}")

        if created:
            last = created[-1]
            self.selected_item = last
            self._marked = {last.id}
            self.refresh_items(reset_selection=False)
            msg = f"9:16 × {len(created)} · {last.width}×{last.height}"
            if skipped:
                msg += f" · skipped {skipped} already 9:16"
            self._toast(msg)
        elif skipped and not errors:
            self._toast("Already 9:16")
        if errors:
            self._toast(errors[0])

    def open_crop_dialog(self) -> None:
        """Open the crop editor for the focused image or audio file."""
        if self._picker_blocking:
            return
        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return
        if len(items) != 1:
            self._toast("Crop one file at a time")
            return
        item = items[0]
        if item.is_audio:
            from audio_crop import open_audio_crop_dialog as _open
        elif item.is_image:
            from crop import open_crop_dialog as _open
        else:
            self._toast("Crop works on images and audio")
            return

        self._picker_blocking = True
        saved: list[tuple[str, Item]] = []

        def on_done(mode: str, it: Item) -> None:
            saved.append((mode, it))

        def on_close() -> None:
            self._picker_blocking = False

            def after_close() -> bool:
                if saved:
                    mode, it = saved[-1]
                    if mode == "new":
                        self.library.upsert_item(it)
                        it = self._auto_join_set(item, it)
                        self.selected_item = it
                        self._marked = {it.id}
                        self.refresh_items(reset_selection=False)
                        if it.is_audio and it.duration:
                            detail = f"{it.duration:.2f}s"
                        else:
                            detail = f"{it.width}×{it.height}"
                        self._toast(f"Saved as new · {detail}")
                    else:
                        self._refresh_after_in_place_edit(it)
                        if it.is_audio and it.duration:
                            self._toast(f"Saved crop · {it.duration:.2f}s")
                        else:
                            self._toast(f"Saved crop · {it.width}×{it.height}")
                if not self.is_viewer_open():
                    self.grid.grab_focus()
                    self._restore_grid_scroll(scroll)
                return False

            GLib.idle_add(after_close)

        scroll = self._grid_scroll_value()
        _open(
            self,
            item,
            library_root=self.library.root,
            on_done=on_done,
            on_close=on_close,
        )

    def queue_upscale(self) -> None:
        """Queue a PromptForge upscale for the focused still or video."""
        from write import WriteError

        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return
        if len(items) != 1:
            self._toast("Select one still or video")
            return
        item = items[0]
        if not (item.is_image or item.is_video):
            self._toast("Upscale is for stills and videos")
            return
        if not item.path.is_file():
            self._toast("File missing")
            return
        prior = already_reason(item)
        if prior:
            self._toast(prior)
            return
        inflight = getattr(self, "_upscale_inflight", None)
        if inflight is None:
            inflight = set()
            self._upscale_inflight = inflight
        if item.id in inflight:
            self._toast("Already queued")
            return
        inflight.add(item.id)

        def work() -> None:
            try:
                result = post_upscale(item)
            except Exception:  # noqa: BLE001
                result = UpscaleResult("offline", "PromptForge not answering")

            def apply() -> bool:
                inflight.discard(item.id)
                if result.status == "ok":
                    try:
                        self.library.update_item(
                            item.id,
                            add_tags=["upscaling"],
                            remove_tags=["needs-upscale"],
                        )
                    except WriteError:
                        self._toast(
                            "PromptForge queued it but the Eagle tag failed"
                        )
                        return False
                    self.refresh_items(reset_selection=False, scroll_to_top=False)
                    self.update_inspector()
                    self._toast(result.toast)
                    return False
                self._toast(result.toast)
                return False

            GLib.idle_add(apply)

        threading.Thread(target=work, name="eagle-upscale", daemon=True).start()

    def _invalidate_thumb_cache_for(self, item: Item) -> None:
        """Drop cached textures for this item so the grid reloads after crop."""
        paths: list[str] = []
        if item.thumb is not None:
            paths.append(str(item.thumb))
        paths.append(str(item.path))
        with _thumb_lock:
            drop = [
                k
                for k in list(_thumb_textures.keys())
                if any(k.startswith(p + "@") or k == p for p in paths)
            ]
            for k in drop:
                _thumb_textures.pop(k, None)
                _thumb_waiters.pop(k, None)
                _thumb_inflight.discard(k)

    def _refresh_after_in_place_edit(self, it: Item) -> None:
        """Reload grid, inspector, and viewer after overwriting an item on disk."""
        self._invalidate_thumb_cache_for(it)
        self.library.upsert_item(it)
        for lst in (self._items, self._all_items):
            for i, old in enumerate(lst):
                if old.id == it.id:
                    lst[i] = it
        self.selected_item = it
        self.library._invalidate_caches()  # noqa: SLF001
        self._rebind_grid_keep_selection()
        self._update_path_label()
        viewing = (
            self.is_viewer_open()
            and self._viewer_item_id == it.id
            and it.is_image
            and it.path.is_file()
        )
        if viewing:
            self.open_inline_viewer(it)
        else:
            self.update_inspector()

    def _confirm_bulk_edit(
        self,
        n: int,
        *,
        heading: str,
        body: str,
        apply_fn,
        parent: Gtk.Window | None = None,
    ) -> bool:
        """If *n* > 100, ask first. Returns False when the write is deferred."""
        if n <= BULK_EDIT_CONFIRM:
            apply_fn()
            return True
        dialog = Adw.MessageDialog(
            transient_for=parent or self,
            heading=heading,
            body=body,
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_resp(_d: Adw.MessageDialog, response: str) -> None:
            _d.close()
            if response == "apply":
                apply_fn()

        dialog.connect("response", on_resp)
        dialog.present()
        return False

    def edit_tags_dialog(self) -> None:
        """Keyboard tag picker: recent + autocomplete, Enter toggles, Esc closes."""
        from picker import TogglePicker, load_recent
        from write import WriteError

        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return

        # Tag present on all items → active; on some → partial
        # set: tags are membership, not shown in this picker (use Group / g)
        tag_sets = [{t for t in it.tags if not is_set_tag(t)} for it in items]
        active = set.intersection(*tag_sets) if tag_sets else set()
        union = set.union(*tag_sets) if tag_sets else set()
        partial = union - active
        all_tags = self.library.all_tags(include_set=False)
        # Ensure current tags appear even if rare
        have = set(all_tags)
        for t in union:
            if t not in have:
                all_tags.append(t)
                have.add(t)
        recent = [t for t in load_recent("tags") if not is_set_tag(t)]
        ids = [it.id for it in items]
        n = len(items)
        picker_ref: dict[str, object] = {"p": None}

        def apply_tag(tag: str, turn_on: bool, *, update_picker: bool) -> None:
            try:
                if n == 1:
                    if turn_on:
                        self.library.update_item(ids[0], add_tags=[tag])
                    else:
                        self.library.update_item(ids[0], remove_tags=[tag])
                else:
                    if turn_on:
                        self.library.update_items_batch(ids, add_tags=[tag])
                    else:
                        self.library.update_items_batch(ids, remove_tags=[tag])
            except WriteError as exc:
                self._toast(str(exc))
                raise
            # Re-query so the current smart folder / Untagged drops mismatches
            self.refresh_items(reset_selection=False, scroll_to_top=False)
            # Sidebar badge: keep Untagged (N) fresh even when not on that view
            self._refresh_special_counts()
            self._toast(("+ " if turn_on else "− ") + tag)
            picker = picker_ref["p"]
            if update_picker and picker is not None:
                picker.note_toggled(tag, turn_on)  # type: ignore[union-attr]

        def on_toggle(tag: str, turn_on: bool) -> bool:
            if n > BULK_EDIT_CONFIRM:
                verb = "Add" if turn_on else "Remove"
                self._confirm_bulk_edit(
                    n,
                    heading=f"{verb} tag on {n} items?",
                    body=f"{verb} “{tag}” on {n} selected items.",
                    apply_fn=lambda: apply_tag(tag, turn_on, update_picker=True),
                    parent=picker_ref["p"],  # type: ignore[arg-type]
                )
                return False
            apply_tag(tag, turn_on, update_picker=False)
            return True

        def on_close() -> None:
            self.grid.grab_focus()
            self._restore_grid_scroll(scroll)

        scroll = self._grid_scroll_value()
        picker = TogglePicker(
            self,
            title="Tags",
            subtitle=f"{n} item(s) · Enter toggles · Esc closes",
            all_values=all_tags,
            active=active,
            partial=partial,
            recent=recent,
            allow_create=True,
            recent_kind="tags",
            on_toggle=on_toggle,
            on_close=on_close,
        )
        picker_ref["p"] = picker
        picker.present()

    def edit_folder_auto_tags(self, folder_id: str | None = None) -> None:
        """
        Edit Eagle-style auto-tags for a library folder.

        When items are added to this folder, these tags (plus ancestor folder
        auto-tags) are applied automatically — same as Eagle's folder Auto tagging.
        """
        from picker import TogglePicker, load_recent
        from write import WriteError

        fid = folder_id or self.current_folder_id
        if not fid:
            self._toast("Select a folder in the sidebar first")
            return
        folder = self.library.folders_by_id.get(fid)
        if folder is None:
            self._toast("Unknown folder")
            return

        path = self.library.folder_paths.get(fid, folder.name)
        active = {t for t in folder.tags if not is_set_tag(t)}
        # Live set for toggles (written on each Enter)
        current = set(active)
        all_tags = self.library.all_tags(include_set=False)
        recent = [t for t in load_recent("tags") if not is_set_tag(t)]

        def on_toggle(tag: str, turn_on: bool) -> None:
            key = tag.strip().lower()
            if not key:
                return
            if turn_on:
                current.add(key)
                current.difference_update({t for t in list(current) if t != key and t.lower() == key})
            else:
                current.discard(key)
                current.difference_update({t for t in list(current) if t.lower() == key})
            try:
                self.library.set_folder_auto_tags(fid, sorted(current, key=str.lower))
            except WriteError as exc:
                self._toast(str(exc))
                raise
            # Sidebar badge / tooltip
            self._repopulate_sidebar_keep_selection()
            self._toast(
                f"Auto-tags · {path}: "
                + (", ".join(sorted(current, key=str.lower)) or "(none)")
            )

        def on_close() -> None:
            self.folder_list.grab_focus()

        inherited = [
            t
            for t in self.library.auto_tags_for_folders([fid])
            if t not in current
        ]
        sub = (
            f"{path} · Enter toggles · Esc closes\n"
            "Applied when items are added to this folder"
        )
        if inherited:
            sub += f"\nAlso from parents: {', '.join(inherited)}"

        picker = TogglePicker(
            self,
            title=f"Auto-tags · {folder.name}",
            subtitle=sub,
            all_values=all_tags,
            active=active,
            recent=recent,
            allow_create=True,
            recent_kind="tags",
            on_toggle=on_toggle,
            on_close=on_close,
        )
        picker.present()

    def _dismiss_sf_menu(self) -> None:
        if self._sf_menu is not None:
            try:
                self._sf_menu.popdown()
                self._sf_menu.unparent()
            except Exception:  # noqa: BLE001
                pass
            self._sf_menu = None

    def _popover_menu(
        self, widget: Gtk.Widget, x: float, y: float, items: list[tuple[str, object]]
    ) -> None:
        self._dismiss_sf_menu()
        pop = Gtk.Popover()
        pop.set_parent(widget)
        pop.set_has_arrow(True)
        pop.set_autohide(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)
        for label, cb in items:
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.set_halign(Gtk.Align.FILL)

            def on_click(_b: Gtk.Button, action=cb) -> None:
                self._dismiss_sf_menu()
                action()

            btn.connect("clicked", on_click)
            box.append(btn)
        pop.set_child(box)
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)
        self._sf_menu = pop
        pop.popup()

    def _open_smart_header_menu(
        self, widget: Gtk.Widget, x: float, y: float
    ) -> None:
        self._popover_menu(
            widget,
            x,
            y,
            [("New smart folder", self.open_smart_folder_editor)],
        )

    def _open_smart_folder_menu(
        self, widget: Gtk.Widget, x: float, y: float, smart_id: str
    ) -> None:
        self._popover_menu(
            widget,
            x,
            y,
            [
                ("Edit rules", lambda: self.open_smart_folder_editor(smart_id)),
                (
                    "New child",
                    lambda: self.open_smart_folder_editor(parent_id=smart_id),
                ),
                ("Delete", lambda: self.confirm_delete_smart_folder(smart_id)),
            ],
        )

    def open_smart_folder_editor(
        self,
        folder_id: str | None = None,
        *,
        parent_id: str | None = None,
    ) -> None:
        from smart_folder_editor import SmartFolderEditor
        from write import WriteError

        if self._sf_editor is not None:
            try:
                self._sf_editor.present()
            except Exception:  # noqa: BLE001
                self._sf_editor = None
            if self._sf_editor is not None:
                return
        try:
            editor = SmartFolderEditor(
                self,
                self.library,
                folder_id=folder_id,
                default_parent_id=parent_id,
                on_saved=self._after_smart_folder_saved,
                on_closed=lambda: setattr(self, "_sf_editor", None),
            )
        except WriteError as exc:
            self._toast(str(exc))
            return
        self._sf_editor = editor
        editor.present()

    def _after_smart_folder_saved(self, smart_id: str) -> None:
        self._smart_counts.clear()
        if smart_id in self.library.smart_folders_by_id:
            self.current_smart_folder_id = smart_id
            self.current_folder_id = None
            self._special_view = None
            self._ensure_smart_expanded_path(smart_id)
            parent = self.library.smart_folders_by_id[smart_id].parent_id
            if parent:
                self._smart_expanded.add(parent)
        self._populate_sidebar(select_current=True)
        self.refresh_items()
        path = self.library.smart_folder_paths.get(smart_id, smart_id)
        self._toast(f"Smart folder · {path}")

    def confirm_delete_smart_folder(self, smart_id: str) -> None:
        sf = self.library.smart_folders_by_id.get(smart_id)
        if sf is None:
            self._toast("Unknown smart folder")
            return
        n_children = 0

        def walk(node) -> None:
            nonlocal n_children
            for child in node.children:
                n_children += 1
                walk(child)

        walk(sf)
        path = self.library.smart_folder_paths.get(smart_id, sf.name)
        if n_children:
            body = (
                f"Remove “{path}” and {n_children} nested smart folder"
                f"{'s' if n_children != 1 else ''}? Items stay in the library."
            )
        else:
            body = f"Remove “{path}”? Items stay in the library."
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Delete smart folder?",
            body=body,
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_resp(_d: Adw.MessageDialog, response: str) -> None:
            _d.close()
            if response == "delete":
                self._delete_smart_folder(smart_id)

        dialog.connect("response", on_resp)
        dialog.present()

    def _delete_smart_folder(self, smart_id: str) -> None:
        from write import WriteError, delete_smart_folder_node, write_session

        sf = self.library.smart_folders_by_id.get(smart_id)
        parent_id = sf.parent_id if sf else None
        name = self.library.smart_folder_paths.get(smart_id, smart_id)
        current = self.current_smart_folder_id
        leaving = False
        if current:
            if current == smart_id:
                leaving = True
            else:
                chain = self._smart_ancestors(current)
                if smart_id in chain:
                    leaving = True
        try:
            with write_session(self.library.root):
                delete_smart_folder_node(self.library.root, smart_id)
            self.library.reload_metadata_trees()
        except WriteError as exc:
            self._toast(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._toast(str(exc))
            return
        self._smart_counts.clear()
        self._smart_expanded.discard(smart_id)
        if leaving:
            self.current_smart_folder_id = (
                parent_id if parent_id in self.library.smart_folders_by_id else None
            )
            if self.current_smart_folder_id is None:
                self._special_view = None
                self.current_folder_id = None
        self._populate_sidebar(select_current=True)
        self.refresh_items()
        self._toast(f"Deleted · {name}")

    def edit_folders_dialog(self) -> None:
        """Keyboard folder/category picker (same UX as tags)."""
        from picker import TogglePicker, load_recent
        from write import WriteError

        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return

        # Map display path ↔ folder id
        id_to_path = dict(self.library.folder_paths)
        path_to_id = {v: k for k, v in id_to_path.items()}
        # Also bare names for short folders
        for fid, path in id_to_path.items():
            name = path.split(" / ")[-1]
            path_to_id.setdefault(name, fid)

        folder_sets = [set(it.folders) for it in items]
        active_ids = set.intersection(*folder_sets) if folder_sets else set()
        union_ids = set.union(*folder_sets) if folder_sets else set()
        partial_ids = union_ids - active_ids

        all_paths = sorted(id_to_path.values(), key=str.lower)
        active_paths = {id_to_path[i] for i in active_ids if i in id_to_path}
        partial_paths = {id_to_path[i] for i in partial_ids if i in id_to_path}
        # Recent stored as paths
        recent = [r for r in load_recent("folders") if r in path_to_id or r in id_to_path.values()]
        ids = [it.id for it in items]
        n = len(items)
        picker_ref: dict[str, object] = {"p": None}

        def resolve_fid(path_label: str) -> str | None:
            fid = path_to_id.get(path_label)
            if fid:
                return fid
            for k, v in id_to_path.items():
                if v == path_label:
                    return k
            return None

        def apply_folder(path_label: str, turn_on: bool, *, update_picker: bool) -> None:
            fid = resolve_fid(path_label)
            if not fid:
                self._toast(f"Unknown folder: {path_label}")
                raise WriteError(f"Unknown folder: {path_label}")
            try:
                if n == 1:
                    if turn_on:
                        self.library.update_item(ids[0], add_folders=[fid])
                    else:
                        self.library.update_item(ids[0], remove_folders=[fid])
                else:
                    if turn_on:
                        self.library.update_items_batch(ids, add_folders=[fid])
                    else:
                        self.library.update_items_batch(ids, remove_folders=[fid])
            except WriteError as exc:
                self._toast(str(exc))
                raise
            # Re-query so the current smart folder / Uncategorized drops mismatches
            self.refresh_items(reset_selection=False, scroll_to_top=False)
            # Sidebar badge: keep Uncategorized (N) fresh even when not on that view
            self._refresh_special_counts()
            msg = ("+ " if turn_on else "− ") + path_label
            if turn_on:
                auto = self.library.auto_tags_for_folders([fid])
                if auto:
                    msg += " · auto-tags " + ", ".join(auto)
            self._toast(msg)
            picker = picker_ref["p"]
            if update_picker and picker is not None:
                picker.note_toggled(path_label, turn_on)  # type: ignore[union-attr]

        def on_toggle(path_label: str, turn_on: bool) -> bool:
            if n > BULK_EDIT_CONFIRM:
                verb = "Add" if turn_on else "Remove"
                extra = ""
                fid = resolve_fid(path_label)
                if turn_on and fid:
                    auto = self.library.auto_tags_for_folders([fid])
                    if auto:
                        extra = f" Auto-tags {', '.join(auto)} will also be applied."
                self._confirm_bulk_edit(
                    n,
                    heading=f"{verb} category on {n} items?",
                    body=f"{verb} “{path_label}” on {n} selected items.{extra}",
                    apply_fn=lambda: apply_folder(
                        path_label, turn_on, update_picker=True
                    ),
                    parent=picker_ref["p"],  # type: ignore[arg-type]
                )
                return False
            apply_folder(path_label, turn_on, update_picker=False)
            return True

        def on_close() -> None:
            self.grid.grab_focus()
            self._restore_grid_scroll(scroll)

        scroll = self._grid_scroll_value()
        picker = TogglePicker(
            self,
            title="Folders / categories",
            subtitle=f"{n} item(s) · Enter toggles · Esc closes · no new folders here",
            all_values=all_paths,
            active=active_paths,
            partial=partial_paths,
            recent=recent,
            allow_create=False,
            recent_kind="folders",
            on_toggle=on_toggle,
            on_close=on_close,
        )
        picker_ref["p"] = picker
        picker.present()

    def _rebuild_filter_chips(self) -> None:
        """Show active view filters as removable chips."""
        while (c := self.filter_chips.get_first_child()) is not None:
            self.filter_chips.remove(c)
        vf = self._view_filters
        if not vf.active():
            empty = Gtk.Label(label="No view filters", xalign=0)
            empty.add_css_class("dim-label")
            empty.add_css_class("caption")
            self.filter_chips.append(empty)
            return

        def chip(label: str, clear_fn) -> None:
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.add_css_class("circular")
            btn.set_tooltip_text("Click to remove this filter")
            btn.connect("clicked", lambda *_: clear_fn() or self.refresh_items())
            self.filter_chips.append(btn)

        for t in sorted(vf.tags_include):
            chip(f"+tag:{t}", lambda t=t: vf.tags_include.discard(t))
        for t in sorted(vf.tags_exclude):
            chip(f"-tag:{t}", lambda t=t: vf.tags_exclude.discard(t))
        for fid in sorted(vf.folders_include):
            name = self.library.folder_paths.get(fid, fid)[:40]
            chip(f"+folder:{name}", lambda f=fid: vf.folders_include.discard(f))
        for fid in sorted(vf.folders_exclude):
            name = self.library.folder_paths.get(fid, fid)[:40]
            chip(f"-folder:{name}", lambda f=fid: vf.folders_exclude.discard(f))
        for t in sorted(vf.types_include):
            chip(f"+type:{t}", lambda t=t: vf.types_include.discard(t))
        for t in sorted(vf.types_exclude):
            chip(f"-type:{t}", lambda t=t: vf.types_exclude.discard(t))
        if vf.width_min is not None:
            chip(f"w≥{vf.width_min}", lambda: setattr(vf, "width_min", None))
        if vf.width_max is not None:
            chip(f"w≤{vf.width_max}", lambda: setattr(vf, "width_max", None))
        if vf.height_min is not None:
            chip(f"h≥{vf.height_min}", lambda: setattr(vf, "height_min", None))
        if vf.height_max is not None:
            chip(f"h≤{vf.height_max}", lambda: setattr(vf, "height_max", None))
        if vf.duration_min is not None:
            chip(f"dur≥{vf.duration_min:g}s", lambda: setattr(vf, "duration_min", None))
        if vf.duration_max is not None:
            chip(f"dur≤{vf.duration_max:g}s", lambda: setattr(vf, "duration_max", None))
        if vf.created_from is not None:
            chip(f"created≥{vf.created_from}", lambda: setattr(vf, "created_from", None))
        if vf.created_to is not None:
            chip(f"created≤{vf.created_to}", lambda: setattr(vf, "created_to", None))
        if vf.added_from is not None:
            chip(f"added≥{vf.added_from}", lambda: setattr(vf, "added_from", None))
        if vf.added_to is not None:
            chip(f"added≤{vf.added_to}", lambda: setattr(vf, "added_to", None))
        if vf.rating is not None:
            chip(
                rating_chip_label(vf.rating_op, vf.rating),
                lambda: setattr(vf, "rating", None),
            )

    def clear_view_filters(self) -> None:
        self._view_filters.clear()
        self.refresh_items()
        self._toast("View filters cleared")

    def open_view_tag_filter(self) -> None:
        from picker import TogglePicker, load_recent

        vf = self._view_filters
        all_tags = self.library.all_tags(include_set=False)
        recent = [t for t in load_recent("filter_tags") if not is_set_tag(t)]

        def on_include(tag: str, turn_on: bool) -> None:
            key = tag.strip().lower()
            if turn_on:
                vf.tags_include.add(key)
                vf.tags_exclude.discard(key)
            else:
                vf.tags_include.discard(key)
            self.refresh_items()

        def on_exclude(tag: str, turn_on: bool) -> None:
            key = tag.strip().lower()
            if turn_on:
                vf.tags_exclude.add(key)
                vf.tags_include.discard(key)
            else:
                vf.tags_exclude.discard(key)
            self.refresh_items()

        TogglePicker(
            self,
            title="Filter · tags",
            subtitle="Enter = include · Shift+Enter / right-click = exclude · Esc close",
            all_values=all_tags,
            active=set(vf.tags_include),
            excluded=set(vf.tags_exclude),
            recent=recent,
            allow_create=False,
            allow_exclude=True,
            recent_kind="filter_tags",
            on_toggle=on_include,
            on_exclude=on_exclude,
            on_close=lambda s=self._grid_scroll_value(): (
                self.grid.grab_focus(),
                self._restore_grid_scroll(s),
            ),
        ).present()

    def open_view_folder_filter(self) -> None:
        from picker import TogglePicker, load_recent

        vf = self._view_filters
        id_to_path = dict(self.library.folder_paths)
        path_to_id = {v: k for k, v in id_to_path.items()}
        all_paths = sorted(id_to_path.values(), key=str.lower)
        active_paths = {id_to_path[i] for i in vf.folders_include if i in id_to_path}
        excl_paths = {id_to_path[i] for i in vf.folders_exclude if i in id_to_path}
        recent = [r for r in load_recent("filter_folders") if r in path_to_id]

        def resolve(path_label: str) -> str | None:
            return path_to_id.get(path_label)

        def on_include(path_label: str, turn_on: bool) -> None:
            fid = resolve(path_label)
            if not fid:
                return
            if turn_on:
                vf.folders_include.add(fid)
                vf.folders_exclude.discard(fid)
            else:
                vf.folders_include.discard(fid)
            self.refresh_items()

        def on_exclude(path_label: str, turn_on: bool) -> None:
            fid = resolve(path_label)
            if not fid:
                return
            if turn_on:
                vf.folders_exclude.add(fid)
                vf.folders_include.discard(fid)
            else:
                vf.folders_exclude.discard(fid)
            self.refresh_items()

        TogglePicker(
            self,
            title="Filter · folders",
            subtitle="Enter = include · Shift+Enter / right-click = exclude",
            all_values=all_paths,
            active=active_paths,
            excluded=excl_paths,
            recent=recent,
            allow_create=False,
            allow_exclude=True,
            recent_kind="filter_folders",
            on_toggle=on_include,
            on_exclude=on_exclude,
            on_close=lambda s=self._grid_scroll_value(): (
                self.grid.grab_focus(),
                self._restore_grid_scroll(s),
            ),
        ).present()

    def open_view_type_filter(self) -> None:
        """Filter grid by media kind (image/video/audio) and/or extension."""
        from collections import Counter

        from picker import TogglePicker, load_recent

        counts: Counter[str] = Counter()
        for it in self.library.items:
            if it.is_deleted:
                continue
            if it.ext_lower:
                counts[it.ext_lower] += 1
            if it.is_image:
                counts["image"] += 1
            if it.is_video:
                counts["video"] += 1
            if it.is_audio:
                counts["audio"] += 1

        categories = ["image", "video", "audio"]
        exts = [ext for ext, _n in counts.most_common() if ext not in categories]
        all_values = categories + exts
        recent = [r for r in load_recent("types") if r in counts or r in categories]
        vf = self._view_filters

        def on_include(value: str, turn_on: bool) -> None:
            key = value.lower().lstrip(".")
            if turn_on:
                vf.types_include.add(key)
                vf.types_exclude.discard(key)
            else:
                vf.types_include.discard(key)
            self.refresh_items()

        def on_exclude(value: str, turn_on: bool) -> None:
            key = value.lower().lstrip(".")
            if turn_on:
                vf.types_exclude.add(key)
                vf.types_include.discard(key)
            else:
                vf.types_exclude.discard(key)
            self.refresh_items()

        TogglePicker(
            self,
            title="Filter · type",
            subtitle=(
                "Enter = include · Shift+Enter / right-click = exclude · "
                "image / video / audio or png, mp4…"
            ),
            all_values=all_values,
            active=set(vf.types_include),
            excluded=set(vf.types_exclude),
            recent=recent,
            allow_create=False,
            allow_exclude=True,
            recent_kind="types",
            on_toggle=on_include,
            on_exclude=on_exclude,
            on_close=lambda s=self._grid_scroll_value(): (
                self.grid.grab_focus(),
                self._restore_grid_scroll(s),
            ),
        ).present()

    def open_dimension_filter(self) -> None:
        self._open_range_dialog(
            title="Filter · dimensions (pixels)",
            fields=(
                ("Width min", "width_min"),
                ("Width max", "width_max"),
                ("Height min", "height_min"),
                ("Height max", "height_max"),
            ),
            as_int=True,
        )

    def open_duration_filter(self) -> None:
        self._open_range_dialog(
            title="Filter · duration (seconds)",
            fields=(
                ("Duration min (s)", "duration_min"),
                ("Duration max (s)", "duration_max"),
            ),
            as_int=False,
        )

    def open_date_filter(self) -> None:
        """Filter by original file date and library-add date (inclusive days)."""
        vf = self._view_filters
        scroll = self._grid_scroll_value()
        win = Gtk.Window(
            title="Filter · dates",
            transient_for=self,
            modal=False,
            default_width=420,
        )
        self._remember_dialog(win)
        closing = {"v": False}
        outside: dict[str, Gtk.GestureClick | None] = {"g": None}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        win.set_child(box)
        title_lbl = Gtk.Label(label="Filter · dates", xalign=0)
        title_lbl.add_css_class("title-3")
        box.append(title_lbl)
        hint = Gtk.Label(
            label=(
                "Created is the original file time. Added is when it entered "
                "this library. Local dates, inclusive. YYYY-MM-DD."
            ),
            xalign=0,
            wrap=True,
        )
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)

        fields = (
            ("Created from", "created_from"),
            ("Created to", "created_to"),
            ("Added from", "added_from"),
            ("Added to", "added_to"),
        )
        entries: dict[str, Gtk.Entry] = {}
        for label, attr in fields:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            ent = Gtk.Entry()
            cur = getattr(vf, attr)
            if cur:
                ent.set_text(str(cur))
            ent.set_placeholder_text("YYYY-MM-DD")
            ent.set_width_chars(12)
            entries[attr] = ent
            row.append(ent)
            box.append(row)

        def close_win(*_a) -> None:
            if closing["v"]:
                return
            closing["v"] = True
            if outside["g"] is not None:
                try:
                    self.remove_controller(outside["g"])
                except Exception:  # noqa: BLE001
                    pass
                outside["g"] = None
            self._picker_blocking = False
            win.destroy()
            self.grid.grab_focus()
            self._restore_grid_scroll(scroll)

        def apply(*_a) -> None:
            parsed: dict[str, str | None] = {}
            for attr, ent in entries.items():
                text = (ent.get_text() or "").strip()
                if not text:
                    parsed[attr] = None
                    continue
                d = parse_filter_date(text)
                if d is None:
                    self._toast(f"Invalid date: {text}")
                    return
                parsed[attr] = format_filter_date(d)
            for attr, val in parsed.items():
                setattr(vf, attr, val)
            self.refresh_items()
            close_win()

        def clear_dates(*_a) -> None:
            vf.created_from = vf.created_to = None
            vf.added_from = vf.added_to = None
            self.refresh_items()
            close_win()

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", clear_dates)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", close_win)
        ok = Gtk.Button(label="Apply")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", apply)
        btns.append(clear)
        btns.append(cancel)
        btns.append(ok)
        box.append(btns)

        key = Gtk.EventControllerKey()

        def on_key(_c, keyval, _kc, _state):
            if keyval == Gdk.KEY_Escape:
                close_win()
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                apply()
                return True
            return False

        key.connect("key-pressed", on_key)
        win.add_controller(key)
        win.connect("close-request", lambda *_: (close_win() or True))

        def arm_outside() -> bool:
            if closing["v"]:
                return False
            click = Gtk.GestureClick()
            click.set_button(1)
            click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            click.connect("pressed", lambda *_: close_win())
            self.add_controller(click)
            outside["g"] = click
            return False

        GLib.timeout_add(200, arm_outside)
        win.present()
        if entries:
            next(iter(entries.values())).grab_focus()

    def open_star_filter(self) -> None:
        """Filter the grid by star rating: 1–5 with =, ≥, or ≤."""
        vf = self._view_filters
        scroll = self._grid_scroll_value()
        win = Gtk.Window(
            title="Filter · stars",
            transient_for=self,
            modal=False,
            default_width=360,
        )
        self._remember_dialog(win)
        closing = {"v": False}
        outside: dict[str, Gtk.GestureClick | None] = {"g": None}
        chosen = {
            "op": vf.rating_op if vf.rating_op in RATING_OP_SYMBOLS else RATING_OP_EQ,
            "n": vf.rating if vf.rating and 1 <= vf.rating <= 5 else 3,
        }

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        win.set_child(box)
        title_lbl = Gtk.Label(label="Filter · stars", xalign=0)
        title_lbl.add_css_class("title-3")
        box.append(title_lbl)
        hint = Gtk.Label(
            label="Match items whose rating is equal, at least, or at most the stars you pick. Unrated counts as 0.",
            xalign=0,
            wrap=True,
        )
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)

        op_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        op_row.append(Gtk.Label(label="Match", xalign=0, hexpand=True))
        op_btns: dict[str, Gtk.ToggleButton] = {}
        group: Gtk.ToggleButton | None = None
        for op, tooltip in (
            (RATING_OP_EQ, "Equal"),
            (RATING_OP_GTE, "Greater than or equal"),
            (RATING_OP_LTE, "Less than or equal"),
        ):
            btn = Gtk.ToggleButton(label=RATING_OP_SYMBOLS[op])
            btn.set_tooltip_text(tooltip)
            if group is None:
                group = btn
            else:
                btn.set_group(group)
            if op == chosen["op"]:
                btn.set_active(True)
            op_btns[op] = btn
            op_row.append(btn)
        box.append(op_row)

        star_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        star_row.append(Gtk.Label(label="Stars", xalign=0, hexpand=True))
        star_btns: list[Gtk.Button] = []

        def paint_stars(n: int) -> None:
            chosen["n"] = n
            for i, b in enumerate(star_btns, start=1):
                b.set_label("★" if i <= n else "☆")

        for n in range(1, 6):
            btn = Gtk.Button(label="☆")
            btn.add_css_class("flat")
            btn.set_tooltip_text(f"{n} star{'s' if n != 1 else ''}")
            btn.connect("clicked", lambda _b, s=n: paint_stars(s))
            star_btns.append(btn)
            star_row.append(btn)
        paint_stars(chosen["n"])
        box.append(star_row)

        def close_win(*_a) -> None:
            if closing["v"]:
                return
            closing["v"] = True
            if outside["g"] is not None:
                try:
                    self.remove_controller(outside["g"])
                except Exception:  # noqa: BLE001
                    pass
                outside["g"] = None
            self._picker_blocking = False
            win.destroy()
            self.grid.grab_focus()
            self._restore_grid_scroll(scroll)

        def current_op() -> str:
            for op, btn in op_btns.items():
                if btn.get_active():
                    return op
            return RATING_OP_EQ

        def apply(*_a) -> None:
            vf.rating_op = current_op()
            vf.rating = int(chosen["n"])
            self.refresh_items()
            close_win()

        def clear_rating(*_a) -> None:
            vf.rating = None
            vf.rating_op = RATING_OP_EQ
            self.refresh_items()
            close_win()

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", clear_rating)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", close_win)
        ok = Gtk.Button(label="Apply")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", apply)
        btns.append(clear)
        btns.append(cancel)
        btns.append(ok)
        box.append(btns)

        key = Gtk.EventControllerKey()

        def on_key(_c, keyval, _kc, state):
            if keyval == Gdk.KEY_Escape:
                close_win()
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                apply()
                return True
            rating_keys = {
                Gdk.KEY_1: 1,
                Gdk.KEY_2: 2,
                Gdk.KEY_3: 3,
                Gdk.KEY_4: 4,
                Gdk.KEY_5: 5,
                Gdk.KEY_KP_1: 1,
                Gdk.KEY_KP_2: 2,
                Gdk.KEY_KP_3: 3,
                Gdk.KEY_KP_4: 4,
                Gdk.KEY_KP_5: 5,
            }
            if keyval in rating_keys:
                paint_stars(rating_keys[keyval])
                return True
            return False

        key.connect("key-pressed", on_key)
        win.add_controller(key)
        win.connect("close-request", lambda *_: (close_win() or True))

        def arm_outside() -> bool:
            if closing["v"]:
                return False
            click = Gtk.GestureClick()
            click.set_button(1)
            click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            click.connect("pressed", lambda *_: close_win())
            self.add_controller(click)
            outside["g"] = click
            return False

        GLib.timeout_add(200, arm_outside)
        win.present()

    def _open_range_dialog(
        self,
        *,
        title: str,
        fields: tuple[tuple[str, str], ...],
        as_int: bool,
    ) -> None:
        vf = self._view_filters
        scroll = self._grid_scroll_value()
        win = Gtk.Window(
            title=title,
            transient_for=self,
            # Non-modal: click on main app closes; hover alone does not.
            modal=False,
            default_width=360,
        )
        self._remember_dialog(win)
        closing = {"v": False}
        outside: dict[str, Gtk.GestureClick | None] = {"g": None}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        win.set_child(box)
        title_lbl = Gtk.Label(label=title, xalign=0)
        title_lbl.add_css_class("title-3")
        box.append(title_lbl)
        entries: dict[str, Gtk.Entry] = {}
        for label, attr in fields:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            ent = Gtk.Entry()
            ent.set_input_purpose(Gtk.InputPurpose.NUMBER)
            cur = getattr(vf, attr)
            if cur is not None:
                ent.set_text(str(int(cur) if as_int else cur))
            ent.set_placeholder_text("any")
            ent.set_width_chars(8)
            entries[attr] = ent
            row.append(ent)
            box.append(row)

        def close_win(*_a) -> None:
            if closing["v"]:
                return
            closing["v"] = True
            if outside["g"] is not None:
                try:
                    self.remove_controller(outside["g"])
                except Exception:  # noqa: BLE001
                    pass
                outside["g"] = None
            self._picker_blocking = False
            win.destroy()
            self.grid.grab_focus()
            self._restore_grid_scroll(scroll)

        def apply(*_a) -> None:
            for attr, ent in entries.items():
                text = (ent.get_text() or "").strip()
                if not text:
                    setattr(vf, attr, None)
                    continue
                try:
                    val = int(float(text)) if as_int else float(text)
                    setattr(vf, attr, val)
                except ValueError:
                    self._toast(f"Invalid number: {text}")
                    return
            self.refresh_items()
            close_win()

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", close_win)
        ok = Gtk.Button(label="Apply")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", apply)
        btns.append(cancel)
        btns.append(ok)
        box.append(btns)

        key = Gtk.EventControllerKey()

        def on_key(_c, keyval, _kc, state):
            if keyval == Gdk.KEY_Escape:
                close_win()
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                apply()
                return True
            return False

        key.connect("key-pressed", on_key)
        win.add_controller(key)
        win.connect("close-request", lambda *_: (close_win() or True))

        def arm_outside() -> bool:
            if closing["v"]:
                return False
            click = Gtk.GestureClick()
            click.set_button(1)
            click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            click.connect("pressed", lambda *_: close_win())
            self.add_controller(click)
            outside["g"] = click
            return False

        GLib.timeout_add(200, arm_outside)
        win.present()

    def _open_folder(self, folder: Path) -> bool:
        """Open a directory in the file manager. Returns True if a process started."""
        commands = [
            ["nautilus", str(folder)],
            ["xdg-open", str(folder)],
        ]
        for cmd in commands:
            if _spawn_detached(cmd):
                return True
        return False

    def stage_marked(self) -> None:
        """Copy marked (or focused) files into the stage/outbox directory and open it."""
        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing to stage — mark with Space, or select one")
            return
        stage = self._stage_dir
        self._toast(f"Staging {len(items)} → {stage.name}…")

        def work() -> None:
            try:
                stage.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                GLib.idle_add(lambda: self._toast(f"Stage dir failed: {exc}") or False)
                return
            ok = 0
            errors = 0
            for it in items:
                src = it.path
                if not src.is_file():
                    errors += 1
                    continue
                dest = stage / src.name
                if dest.exists():
                    stem, suf = dest.stem, dest.suffix
                    n = 2
                    while dest.exists():
                        dest = stage / f"{stem}_{n}{suf}"
                        n += 1
                try:
                    shutil.copy2(src, dest)
                    ok += 1
                except OSError:
                    errors += 1

            def done() -> bool:
                msg = f"Staged {ok} → {stage}"
                if errors:
                    msg += f" · {errors} failed"
                if ok and not self._open_folder(stage):
                    msg += " · could not open folder"
                self._toast(msg)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="eagle-stage", daemon=True).start()

    # ── Live ingest of watcher imports ────────────────────────────────
    # The GUI never consumes PICS/Eunbi. It only watches a signal file
    # the watcher writes after each successful library item, then loads
    # that one folder instead of rescanning 25k items.

    def _start_inbox_watch(self) -> None:
        try:
            images = self.library.root / "images"
            if images.is_dir():
                self._inbox_images_mtime = images.stat().st_mtime
        except OSError:
            pass
        try:
            if self._inbox_signal_path.is_file():
                raw = json.loads(
                    self._inbox_signal_path.read_text(encoding="utf-8")
                )
                self._inbox_signal_ts = float(raw.get("ts") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

        try:
            gfile = Gio.File.new_for_path(str(self._inbox_signal_path))
            self._inbox_monitor = gfile.monitor_file(
                Gio.FileMonitorFlags.NONE, None
            )
            self._inbox_monitor.connect("changed", self._on_inbox_signal_changed)
        except Exception:  # noqa: BLE001
            self._inbox_monitor = None

        self._inbox_poll_id = GLib.timeout_add(1000, self._poll_inbox_signal)

    def _on_inbox_signal_changed(self, *_args: object) -> None:
        self._ingest_from_signal()

    def _poll_inbox_signal(self) -> bool:
        self._ingest_from_signal()
        self._retry_pending_imports()
        self._scan_images_if_changed()
        return True

    def _scan_images_if_changed(self) -> None:
        """If images/ gained a folder, queue unknown ids (signal-file backup)."""
        images = self.library.root / "images"
        try:
            mt = images.stat().st_mtime
        except OSError:
            return
        if mt == self._inbox_images_mtime:
            return
        self._inbox_images_mtime = mt
        self._start_images_scan()

    def _start_images_scan(self) -> None:
        """One images/ walker at a time. Extra mtime bumps queue a follow-up."""
        if self._images_scan_running:
            self._images_scan_again = True
            return
        self._images_scan_running = True
        images = self.library.root / "images"

        def work() -> None:
            unknown: list[str] = []
            try:
                for p in images.iterdir():
                    if not p.is_dir() or not p.name.endswith(".info"):
                        continue
                    iid = p.name.removesuffix(".info")
                    if iid not in self.library.items_by_id:
                        unknown.append(iid)
            except OSError:
                unknown = []

            def done() -> bool:
                self._images_scan_running = False
                if unknown:
                    self._ingest_item_ids(unknown)
                if self._images_scan_again:
                    self._images_scan_again = False
                    self._start_images_scan()
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="eagle-scan-new", daemon=True).start()

    def _ingest_from_signal(self) -> None:
        path = self._inbox_signal_path
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        try:
            ts = float(raw.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and ts <= self._inbox_signal_ts:
            return
        ids = [str(i) for i in (raw.get("ids") or []) if i]
        dups = [str(n) for n in (raw.get("dups") or []) if n]
        if ts:
            self._inbox_signal_ts = ts
        if ids:
            self._ingest_item_ids(ids)
        if dups:
            self.import_inbox(manual=False, only_names=set(dups))

    _PENDING_MAX = 200
    _PENDING_MAX_TRIES = 8
    _PENDING_BACKOFF = (2.0, 5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0)

    def _retry_pending_imports(self) -> None:
        if not self._pending_imports:
            return
        now = time.time()
        due = [iid for iid, (_tries, nxt) in self._pending_imports.items() if nxt <= now]
        if due:
            self._ingest_item_ids(due)

    def _note_pending_import(self, iid: str) -> None:
        tries, _nxt = self._pending_imports.get(iid, (0, 0.0))
        tries += 1
        if tries > self._PENDING_MAX_TRIES:
            self._pending_imports.pop(iid, None)
            return
        if iid not in self._pending_imports and len(self._pending_imports) >= self._PENDING_MAX:
            return
        delay = self._PENDING_BACKOFF[min(tries, len(self._PENDING_BACKOFF)) - 1]
        self._pending_imports[iid] = (tries, time.time() + delay)

    def _ingest_item_ids(self, item_ids: list[str]) -> None:
        new_items: list[Item] = []
        for iid in item_ids:
            if iid in self.library.items_by_id:
                self._pending_imports.pop(iid, None)
                continue
            item = self.library.load_item(iid)
            if item is None:
                self._note_pending_import(iid)
                continue
            self._pending_imports.pop(iid, None)
            new_items.append(item)
        if new_items:
            self._apply_new_items(new_items, toast=True)

    def _item_matches_current_view(self, item: Item) -> bool:
        if item.is_deleted:
            return False
        search = self._filter_text.strip().lower()
        if search:
            tokens = [t for t in search.split() if t]
            hay = " ".join(
                [
                    item.id.lower(),
                    item.name_lower,
                    item.ext_lower,
                    (item.annotation or "").lower(),
                    " ".join(item.tags).lower(),
                ]
            )
            if not all(tok in hay for tok in tokens):
                return False
        if self._view_filters.active() and not item_matches_view_filters(
            item, self._view_filters
        ):
            return False
        if self._special_view == "set":
            tag = self._set_view_tag
            return bool(tag) and tag in item.tag_set
        if self._special_view == "untagged":
            return not item.tags
        if self._special_view == "uncategorized":
            return not item.folders
        if self.current_folder_id:
            folder_ids = (
                self.library.folder_and_descendants(self.current_folder_id)
                if self.include_descendants
                else {self.current_folder_id}
            )
            return not item.folder_set.isdisjoint(folder_ids)
        if self.current_smart_folder_id:
            sf = self.library.smart_folders_by_id.get(self.current_smart_folder_id)
            if sf is None:
                return False
            return eval_smart_conditions(item, sf.inherited_conditions)
        return True

    def _apply_new_items(self, items: list[Item], *, toast: bool = False) -> None:
        """Show newly ingested items without rebuilding the whole grid.

        Never steal focus from the asset the user is already viewing or has
        selected — rating/tag edits must keep applying to that item even while
        new imports land at the top of an "Added · newest" grid.
        """
        if not items:
            return
        visible = [it for it in items if self._item_matches_current_view(it)]
        can_prepend = (
            bool(visible)
            and self._sort_key == "added_desc"
            and not self._filter_text.strip()
        )
        if can_prepend:
            newest_first = sorted(
                visible,
                key=self._added_sort_key,
                reverse=True,
            )
            existing = {it.id for it in self._all_items}
            to_add = [it for it in newest_first if it.id not in existing]
            for i, it in enumerate(to_add):
                self._all_items.insert(i, it)
                self._items.insert(i, it)
                self.store.insert(i, ItemObject(it))
            if to_add:
                self._restore_selection_after_grid_insert()
            self._rebuild_scope_text()
            self._refresh_status()
            self._update_path_label()
        elif visible:
            # refresh_items preserves keep_focus_id / _marked by default
            self.refresh_items(reset_selection=False, scroll_to_top=False)
        self._refresh_special_counts()
        if toast and items:
            self._toast(f"{len(items)} new")
            # Watcher skips its chime while the GUI pid file is live.
            play_sound("notification", once=True)

    def _restore_selection_after_grid_insert(self) -> None:
        """After prepending store rows, re-pin selection by item id (not index).

        Gtk.SingleSelection is position-based: inserting at 0 would otherwise
        make "selected position 0" point at the newcomer while the user still
        thinks they are editing the asset they had open.
        """
        if self._keep_grid_unselected:
            self.selected_item = None
            self._marked.clear()
            self._sel_anchor = 0
            self._last_focus_idx = 0
            try:
                self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
            except Exception:  # noqa: BLE001
                pass
            return

        # Prefer the open viewer, then current focus, then a sole mark.
        keep_id: str | None = None
        if self.is_viewer_open() and self._viewer_item_id:
            keep_id = self._viewer_item_id
        elif self.selected_item is not None:
            keep_id = self.selected_item.id
        elif len(self._marked) == 1:
            keep_id = next(iter(self._marked))

        if keep_id is None and not self._marked:
            # Nothing was selected — only then auto-focus the newest arrival.
            if self._items:
                self.selected_item = self._items[0]
                self._marked = {self._items[0].id}
                self._sel_anchor = 0
                self._last_focus_idx = 0
                if self._grid_has_focus:
                    try:
                        self.selection.set_selected(0)
                    except Exception:  # noqa: BLE001
                        pass
            return

        # Multi-select: keep the mark set; only re-resolve the focus row.
        id_to_idx = {it.id: i for i, it in enumerate(self._items)}
        focus_id = keep_id
        if focus_id is None or focus_id not in id_to_idx:
            for mid in self._marked:
                if mid in id_to_idx:
                    focus_id = mid
                    break

        if focus_id is not None and focus_id in id_to_idx:
            idx = id_to_idx[focus_id]
            self.selected_item = self._items[idx]
            self._last_focus_idx = idx
            self._sel_anchor = idx
            # Marks are id-based (safe across prepend). Single-select: pin the
            # mark to the preserved asset. Multi-select: leave the set alone.
            if not self._marked or len(self._marked) == 1:
                self._marked = {focus_id}
            if self._grid_has_focus:
                try:
                    self.selection.set_selected(idx)
                except Exception:  # noqa: BLE001
                    pass
        else:
            # Focused asset not in the loaded page (rare) — keep id-based marks
            # and selected_item from the library so edits still hit the right item.
            if keep_id is not None:
                it = self.library.items_by_id.get(keep_id)
                if it is not None:
                    self.selected_item = it
                if not self._marked or len(self._marked) == 1:
                    self._marked = {keep_id}

    # ── Inbox import (manual only) ────────────────────────────────────
    # Auto-import belongs exclusively to eagle-inbox-watch on one machine.
    # The GUI must not poll the intake folder — open browsers on multiple hosts
    # would race the watcher and each other (double library entries).

    def import_inbox(
        self,
        *,
        manual: bool = True,
        only_names: set[str] | None = None,
    ) -> None:
        """Import media from the configured inbox into the Eagle library.

        Manual only (hotkey ``i``). Prefer the headless watcher for day-to-day
        intake so the GUI can stay open without consuming the inbox.
        """
        if self._inbox_importing:
            if manual:
                self._toast("Import already running…")
            return
        inbox = self._inbox_dir
        if not inbox.is_dir():
            self._toast(f"Inbox not found: {inbox}")
            return

        self._inbox_importing = True
        if manual:
            self._toast(f"Importing from {inbox.name}…")

        def work() -> None:
            from import_media import (
                check_media_complete,
                classify_inbox_files,
                import_file,
                is_not_ready_error,
                list_inbox_files,
                reimport_existing,
                unpack_inbox_zips,
            )
            from write import WriteError, write_session

            unzipped = 0
            for _zpath, n, zerr in unpack_inbox_zips(inbox):
                if zerr is None:
                    unzipped += n
            files = list_inbox_files(inbox)
            if only_names is not None:
                files = [p for p in files if p.name in only_names]
            # Drop zero-byte / incomplete stubs (leave partials in inbox)
            ready: list[Path] = []
            deferred_names: list[str] = []
            for f in files:
                try:
                    if f.stat().st_size <= 0:
                        continue
                except OSError:
                    continue
                ok_media, reason = check_media_complete(f)
                if ok_media:
                    ready.append(f)
                else:
                    deferred_names.append(f.name)

            results = []
            unique: list[Path] = []
            dups = []
            try:
                unique, dups = classify_inbox_files(ready, self.library.items)
            except Exception:  # noqa: BLE001
                # Fall back to treating everything as unique
                unique, dups = ready, []

            # 1) Unique files → import immediately
            try:
                if unique:
                    with write_session(self.library.root):
                        for f in unique:
                            results.append(
                                import_file(
                                    self.library.root,
                                    f,
                                    move_source=True,
                                    hold_lock=True,
                                    force_new=True,
                                )
                            )
            except WriteError as exc:
                err = str(exc)

                def fail() -> bool:
                    self._inbox_importing = False
                    self._toast(f"Import locked: {err}")
                    return False

                GLib.idle_add(fail)
                return

            # 2) Duplicates → interactive review on the UI thread
            if dups:
                decisions = self._review_duplicates_blocking(dups)
                try:
                    with write_session(self.library.root):
                        for match, action in decisions:
                            if action == "skip":
                                continue
                            if action == "reuse":
                                results.append(
                                    reimport_existing(
                                        self.library.root,
                                        match.existing_id,
                                        source=match.source,
                                        move_source=True,
                                        hold_lock=True,
                                    )
                                )
                            elif action == "new":
                                results.append(
                                    import_file(
                                        self.library.root,
                                        match.source,
                                        move_source=True,
                                        hold_lock=True,
                                        force_new=True,
                                    )
                                )
                except WriteError as exc:
                    err = str(exc)

                    def fail2() -> bool:
                        self._inbox_importing = False
                        self._toast(f"Import locked: {err}")
                        return False

                    GLib.idle_add(fail2)
                    return

            ok = sum(1 for r in results if getattr(r, "ok", False))
            reused = sum(
                1 for r in results if getattr(r, "ok", False) and getattr(r, "reused", False)
            )
            new_n = ok - reused
            fail_n = sum(
                1
                for r in results
                if not getattr(r, "ok", False)
                and not getattr(r, "skipped", False)
                and not is_not_ready_error(getattr(r, "error", None))
            )
            err_msgs = [
                getattr(r, "error", None)
                for r in results
                if getattr(r, "error", None)
                and not getattr(r, "ok", False)
                and not is_not_ready_error(getattr(r, "error", None))
            ][:3]
            defer_n = len(deferred_names)

            def done() -> bool:
                self._inbox_importing = False
                if ok:
                    play_sound("notification", once=True)
                    self._rebuild_set_counts(force=True)
                    new_items: list[Item] = []
                    for r in results:
                        if (
                            getattr(r, "ok", False)
                            and getattr(r, "item_id", None)
                            and not getattr(r, "reused", False)
                        ):
                            item = self.library.ingest_imported(r.item_id)
                            if item is not None:
                                new_items.append(item)
                    if new_items:
                        self._apply_new_items(new_items, toast=False)
                    else:
                        self.refresh_items()
                elif fail_n:
                    play_sound("error")
                parts: list[str] = []
                if new_n:
                    parts.append(f"{new_n} new")
                if reused:
                    parts.append(f"{reused} existing")
                if parts:
                    msg = "Imported " + " · ".join(parts)
                else:
                    msg = f"Imported {ok} into library"
                if ok:
                    msg += " · ♪"
                if fail_n:
                    msg += f" · {fail_n} failed"
                    if err_msgs and err_msgs[0]:
                        msg += f" ({err_msgs[0]})"
                if defer_n and not ok:
                    msg = (
                        f"Waiting for {defer_n} file(s) to finish downloading "
                        "(incomplete media left in inbox)"
                    )
                elif defer_n:
                    msg += f" · {defer_n} still downloading"
                if not ready and not defer_n and manual:
                    msg = f"Inbox empty · {inbox}"
                elif not results and not dups and not defer_n and manual:
                    msg = f"Inbox empty · {inbox}"
                if ok or manual or fail_n or dups or (defer_n and manual):
                    self._toast(msg)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="eagle-import", daemon=True).start()

    def _review_duplicates_blocking(
        self, dups: list
    ) -> list[tuple[Any, str]]:
        """
        Show Eagle-style duplicate dialogs on the GTK main loop.

        Returns list of (DuplicateMatch, action) where action is
        ``reuse`` | ``new`` | ``skip``. Blocks the import worker thread.
        """
        from import_media import DuplicateMatch

        remaining = list(dups)
        out: list[tuple[Any, str]] = []
        apply_all_action: str | None = None
        event = threading.Event()
        state: dict[str, Any] = {"apply_all": False, "action": "skip"}

        def present_one(match: DuplicateMatch, index: int, total: int) -> bool:
            play_sound("duplicate")
            self._show_duplicate_dialog(
                match,
                index=index,
                total=total,
                state=state,
                on_done=lambda: event.set(),
            )
            return False

        for i, match in enumerate(remaining):
            if apply_all_action is not None:
                out.append((match, apply_all_action))
                # Still consume? for apply-all reuse/new we process later in batch;
                # for skip, leave file? Eagle applies same action including consuming.
                continue
            event.clear()
            state["apply_all"] = False
            state["action"] = "skip"
            GLib.idle_add(present_one, match, i + 1, len(remaining))
            # Wait until user responds (main loop runs dialog)
            while not event.wait(timeout=0.05):
                # keep waiting; import thread is not the main loop
                if not self.get_realized():
                    state["action"] = "skip"
                    break
            action = state.get("action") or "skip"
            if state.get("apply_all"):
                apply_all_action = action
                # Current + rest
                out.append((match, action))
                for rest in remaining[i + 1 :]:
                    out.append((rest, action))
                break
            out.append((match, action))
        return out

    def _show_duplicate_dialog(
        self,
        match: Any,
        *,
        index: int,
        total: int,
        state: dict[str, Any],
        on_done: Any,
    ) -> bool:
        """Modal UI: show existing asset vs incoming; reuse / import new / skip."""
        win = Gtk.Window(
            title=f"Duplicate · {index} of {total}",
            transient_for=self,
            modal=True,
            default_width=720,
            default_height=480,
        )
        self._remember_dialog(win)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(16)
        root.set_margin_bottom(16)
        root.set_margin_start(16)
        root.set_margin_end(16)
        win.set_child(root)

        head = Gtk.Label(
            label="This file already exists in the library",
            xalign=0,
        )
        head.add_css_class("title-3")
        root.append(head)
        sub = Gtk.Label(
            label=(
                f"Incoming: {match.source.name}  ·  "
                f"{match.size:,} bytes\n"
                f"Existing: {match.existing_name}"
            ),
            xalign=0,
            wrap=True,
        )
        sub.add_css_class("dim-label")
        root.append(sub)

        # Side-by-side previews
        panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        panes.set_homogeneous(True)
        panes.set_hexpand(True)
        panes.set_vexpand(True)
        root.append(panes)

        def _load_preview_paintable(*candidates: Path | None) -> Gdk.Paintable | None:
            """Load a scaled texture; set_filename is unreliable for Dropbox paths."""
            for candidate in candidates:
                if candidate is None:
                    continue
                p = Path(candidate)
                if not p.is_file():
                    continue
                # Prefer images we can decode with GdkPixbuf
                ext = p.suffix.lower()
                if ext in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".webp",
                    ".gif",
                    ".bmp",
                    ".tif",
                    ".tiff",
                    ".avif",
                }:
                    pb = _pixbuf_from_path(str(p), 560, 560)
                    if pb is not None:
                        return Gdk.Texture.new_for_pixbuf(pb)
                    continue
                # Non-image (video): try sibling-style nothing; fall through
            return None

        def preview_col(title: str, path: Path | None, thumb: Path | None) -> Gtk.Box:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            col.append(Gtk.Label(label=title, xalign=0))
            frame = Gtk.Frame()
            frame.set_hexpand(True)
            frame.set_vexpand(True)
            pic = Gtk.Picture()
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_can_shrink(True)
            pic.set_size_request(280, 280)
            # Thumb first (small), then full file for images
            paintable = _load_preview_paintable(thumb, path)
            if paintable is not None:
                pic.set_paintable(paintable)
            else:
                # Last resort: file URI (helps some formats / gio loaders)
                shown = False
                for candidate in (thumb, path):
                    if candidate is None:
                        continue
                    p = Path(candidate)
                    if p.is_file():
                        try:
                            pic.set_file(Gio.File.new_for_path(str(p)))
                            shown = True
                            break
                        except Exception:  # noqa: BLE001
                            continue
                if not shown:
                    placeholder = Gtk.Label(label="No preview")
                    placeholder.add_css_class("dim-label")
                    frame.set_child(placeholder)
                    col.append(frame)
                    if path is not None:
                        path_lbl = Gtk.Label(
                            label=str(path), xalign=0, wrap=True, ellipsize=3
                        )
                        path_lbl.add_css_class("caption")
                        path_lbl.add_css_class("dim-label")
                        col.append(path_lbl)
                    return col
            frame.set_child(pic)
            col.append(frame)
            if path is not None:
                path_lbl = Gtk.Label(
                    label=str(path),
                    xalign=0,
                    wrap=True,
                    ellipsize=3,
                )
                path_lbl.add_css_class("caption")
                path_lbl.add_css_class("dim-label")
                col.append(path_lbl)
            return col

        panes.append(
            preview_col("In library", match.existing_path, match.existing_thumb)
        )
        # Incoming: no separate thumb; decode the source (or its path as image)
        panes.append(preview_col("Incoming", match.source, None))

        remaining_n = total - index + 1
        apply_all = Gtk.CheckButton(
            label=f"Apply to all {remaining_n} duplicates"
        )
        if remaining_n <= 1:
            apply_all.set_sensitive(False)
            apply_all.set_active(False)
        root.append(apply_all)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        root.append(btns)

        finished = {"v": False}

        def finish(action: str) -> None:
            if finished["v"]:
                return
            finished["v"] = True
            state["action"] = action
            state["apply_all"] = bool(apply_all.get_active()) and remaining_n > 1
            self._picker_blocking = False
            win.destroy()
            on_done()

        skip = Gtk.Button(label="Skip")
        skip.connect("clicked", lambda *_: finish("skip"))
        reuse = Gtk.Button(label="Use existing")
        reuse.add_css_class("suggested-action")
        reuse.set_tooltip_text(
            "Keep the library copy; set its imported-at time to now; discard inbox file"
        )
        reuse.connect("clicked", lambda *_: finish("reuse"))
        new_btn = Gtk.Button(label="Import as new")
        new_btn.set_tooltip_text("Create another library item with this file")
        new_btn.connect("clicked", lambda *_: finish("new"))
        btns.append(skip)
        btns.append(new_btn)
        btns.append(reuse)

        key = Gtk.EventControllerKey()

        def on_key(_c, keyval, _kc, _state):
            if keyval == Gdk.KEY_Escape:
                finish("skip")
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                finish("reuse")
                return True
            return False

        key.connect("key-pressed", on_key)
        win.add_controller(key)

        def on_close(*_a):
            finish("skip")
            return True  # we destroy ourselves in finish

        win.connect("close-request", on_close)
        win.present()
        return False

    def is_viewer_open(self) -> bool:
        if bool(getattr(self, "_viewer_open", False)):
            return True
        stack = getattr(self, "center_stack", None)
        if stack is None:
            return False
        try:
            return stack.get_visible_child_name() == "viewer"
        except Exception:  # noqa: BLE001
            return False

    def _set_inline_video_muted(self, muted: bool) -> bool:
        """True if the media stream is not ready yet (caller may retry)."""
        video = getattr(self, "viewer_video", None)
        if video is None:
            return False
        try:
            stream = video.get_media_stream()
        except Exception:  # noqa: BLE001
            return False
        if stream is None:
            return True
        try:
            stream.set_muted(bool(muted))
            stream.set_volume(0.0 if muted else 1.0)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _mute_inline_video(self) -> bool:
        """Mute Gtk.Video only while the sidecar player is alive. Cap retries."""
        self._viewer_mute_tries = getattr(self, "_viewer_mute_tries", 0) + 1
        if self._viewer_mute_tries > 40:
            return False
        proc = self._viewer_audio_proc
        if proc is None or proc.poll() is not None:
            self._set_inline_video_muted(False)
            return False
        return self._set_inline_video_muted(True)

    def _viewer_audio_watchdog(self) -> bool:
        proc = self._viewer_audio_proc
        if proc is None:
            return False
        if proc.poll() is not None:
            self._viewer_audio_proc = None
            self._set_inline_video_muted(False)
        return False

    def _stop_viewer_audio(self) -> None:
        proc = self._viewer_audio_proc
        self._viewer_audio_proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.4)
        except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def _start_viewer_audio(self, path: Path | str, start: float = 0.0) -> None:
        """Play soundtrack out of process. Gtk.Video/GStreamer is often silent."""
        self._stop_viewer_audio()
        start = max(0.0, float(start))
        cmd: list[str] | None = None
        # mpv is what notification chimes use on this machine.
        if shutil.which("mpv"):
            cmd = [
                "mpv",
                "--no-video",
                "--force-window=no",
                "--really-quiet",
                "--audio-display=no",
                "--no-resume-playback",
                "--keep-open=no",
                f"--start={start:.3f}",
                str(path),
            ]
        elif shutil.which("ffplay"):
            cmd = [
                "ffplay",
                "-vn",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.3f}",
                str(path),
            ]
        if cmd is None:
            return
        try:
            self._viewer_audio_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            self._viewer_audio_proc = None
            return
        self._viewer_mute_tries = 0
        GLib.idle_add(self._mute_inline_video)
        GLib.timeout_add(250, self._viewer_audio_watchdog)

    def _disconnect_viewer_playing(self) -> None:
        hid = getattr(self, "_viewer_playing_handler", 0)
        stream = getattr(self, "_viewer_playing_stream", None)
        self._viewer_playing_handler = 0
        self._viewer_playing_stream = None
        if hid and stream is not None:
            try:
                stream.disconnect(hid)
            except Exception:  # noqa: BLE001
                pass

        error_hid = getattr(self, "_viewer_error_handler", 0)
        error_stream = getattr(self, "_viewer_error_stream", None)
        self._viewer_error_handler = 0
        self._viewer_error_stream = None
        if error_hid and error_stream is not None:
            try:
                error_stream.disconnect(error_hid)
            except Exception:  # noqa: BLE001
                pass

    def _connect_viewer_playing(self, stream) -> None:
        self._disconnect_viewer_playing()
        if stream is None:
            return
        try:
            hid = stream.connect("notify::playing", self._on_viewer_playing)
        except Exception:  # noqa: BLE001
            return
        self._viewer_playing_handler = hid
        self._viewer_playing_stream = stream
        try:
            error_hid = stream.connect("notify::error", self._on_viewer_media_error)
        except Exception:  # noqa: BLE001
            error_hid = 0
        self._viewer_error_handler = error_hid
        self._viewer_error_stream = stream if error_hid else None

    def _on_viewer_media_error(self, stream, *_a) -> None:
        """Surface asynchronous GStreamer failures instead of a black viewer."""
        try:
            error = stream.get_error()
        except Exception:  # noqa: BLE001
            error = None
        if error is None:
            return
        detail = str(getattr(error, "message", error) or "Playback failed").strip()
        lower = detail.lower()
        if "missing" in lower and ("plug-in" in lower or "plugin" in lower):
            message = "Video decoder missing · install the required GStreamer codec plugin"
        else:
            message = f"Could not play video · {detail}"
        self._stop_viewer_audio()
        self._toast(message)

    def _on_viewer_playing(self, stream, *_a) -> None:
        """Keep sidecar audio in lockstep with Gtk.Video's play/pause button."""
        if getattr(self, "_viewer_audio_ignore_playing", False):
            return
        if not self.is_viewer_open() or self._viewer_mode != "video":
            return
        try:
            playing = bool(stream.get_playing())
        except Exception:  # noqa: BLE001
            return
        if not playing:
            self._stop_viewer_audio()
            return
        proc = self._viewer_audio_proc
        if proc is not None and proc.poll() is None:
            return
        seconds = 0.0
        try:
            seconds = max(0.0, int(stream.get_timestamp()) / 1_000_000.0)
        except Exception:  # noqa: BLE001
            seconds = 0.0
        item = self._viewer_item()
        if item is not None and item.path.is_file():
            self._start_viewer_audio(item.path, seconds)

    def _stop_inline_video(self) -> None:
        """Pause and detach any in-frame video stream."""
        self._disconnect_viewer_playing()
        self._stop_viewer_audio()
        video = getattr(self, "viewer_video", None)
        if video is None:
            return
        try:
            stream = video.get_media_stream()
            if stream is not None:
                stream.set_playing(False)
        except Exception:  # noqa: BLE001
            pass
        try:
            video.set_file(None)
        except Exception:  # noqa: BLE001
            try:
                video.set_media_stream(None)
            except Exception:  # noqa: BLE001
                pass

    SET_STRIP_MAX = 4

    def _rebuild_set_counts(self, *, force: bool = False) -> None:
        if self._set_counts_ready and not force:
            return
        counts: dict[str, int] = {}
        prefix = SET_PREFIX
        for it in self.library.items:
            if it.is_deleted:
                continue
            for t in it.tags:
                if t.startswith(prefix):
                    counts[t] = counts.get(t, 0) + 1
        self._set_counts = counts
        self._set_counts_ready = True

    def _sync_set_ui(self, items: list[Item]) -> None:
        if not hasattr(self, "insp_set_open"):
            return
        self._rebuild_set_counts()
        n_sel = len(items)
        tag: str | None = None
        if n_sel == 1:
            tag = set_tag_of(items[0])
        elif n_sel > 1:
            tags = {set_tag_of(it) for it in items}
            tags.discard(None)
            if len(tags) == 1:
                tag = next(iter(tags))  # type: ignore[assignment]
        count = self._set_counts.get(tag, 0) if tag else 0
        if tag:
            token = tag[len(SET_PREFIX) :] or tag
            self.insp_set_title.set_text(f"Set · {count}" if count else "Set")
            self.insp_set_open.set_tooltip_text(f"{tag} · Open set (Ctrl+G)")
        else:
            self.insp_set_title.set_text("Set")
            self.insp_set_open.set_tooltip_text("Open the focused asset's set (Ctrl+G)")
        self.insp_set_open.set_sensitive(bool(tag))
        self.insp_set_group.set_sensitive(n_sel >= 2)
        self.insp_set_remove.set_sensitive(
            any(set_tag_of(it) for it in items)
        )
        self._fill_set_thumbs(tag)

    def _fill_set_thumbs(self, tag: str | None) -> None:
        box = getattr(self, "insp_set_thumbs", None)
        if box is None:
            return
        self._clear_box(box)
        if not tag:
            return
        members = self.library.items_in_set(tag)
        members = self._sort_items(members)
        shown = members[: self.SET_STRIP_MAX]
        extra = max(0, len(members) - len(shown))
        for i, it in enumerate(shown):
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("insp-set-thumb")
            pic = Gtk.Picture()
            pic.set_size_request(36, 36)
            pic.set_content_fit(Gtk.ContentFit.COVER)
            path = _thumb_path_for(it)
            pix = _pixbuf_from_path(path, 72, 72) if path else None
            if pix is not None:
                pic.set_paintable(Gdk.Texture.new_for_pixbuf(pix))
            host = Gtk.Overlay()
            host.set_size_request(36, 36)
            host.set_hexpand(False)
            host.set_vexpand(False)
            try:
                host.set_overflow(Gtk.Overflow.HIDDEN)
            except (AttributeError, TypeError):
                pass
            host.set_child(pic)
            overflow = extra > 0 and i == len(shown) - 1
            if overflow:
                more = Gtk.Label(label=f"+{extra}")
                more.add_css_class("insp-set-more")
                more.set_halign(Gtk.Align.CENTER)
                more.set_valign(Gtk.Align.CENTER)
                host.add_overlay(more)
                btn.set_tooltip_text(f"+{extra} more · Open set")
                btn.connect("clicked", lambda *_a, t=tag: self.open_set_view(t))
            else:
                btn.set_tooltip_text(it.display_name)
                iid = it.id
                btn.connect("clicked", lambda *_a, j=iid: self._focus_set_member(j))
            btn.set_child(host)
            box.append(btn)

    def _focus_set_member(self, iid: str) -> None:
        item = self.library.items_by_id.get(iid)
        if item is None or item.is_deleted:
            return
        tag = set_tag_of(item)
        if tag and (self._special_view != "set" or self._set_view_tag != tag):
            self.open_set_view(tag, keep_id=iid)
            return
        self.selected_item = item
        self._marked = {iid}
        for i, it in enumerate(self._items):
            if it.id == iid:
                self._select_index(i)
                break
        self.update_inspector()

    def open_focused_set(self) -> None:
        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return
        tag = set_tag_of(items[0])
        if not tag:
            self._toast("Not in a set")
            return
        keep = items[0].id if len(items) == 1 else (
            self.selected_item.id if self.selected_item else items[0].id
        )
        self.open_set_view(tag, keep_id=keep)

    def open_set_view(self, tag: str, *, keep_id: str | None = None) -> None:
        """Temporary grid of every item with this set: tag."""
        before = self._view_loc()
        if self.is_viewer_open():
            self.close_inline_viewer(restore_scroll=False)
        self._rebuild_set_counts()
        self._set_view_tag = tag
        self._special_view = "set"
        self.current_folder_id = None
        self.current_smart_folder_id = None
        self._sidebar_nav_lock = True
        try:
            self.folder_list.unselect_all()
        except Exception:  # noqa: BLE001
            pass
        GLib.idle_add(self._unlock_sidebar_nav)
        if keep_id:
            item = self.library.items_by_id.get(keep_id)
            if item is not None:
                self.selected_item = item
                self._marked = {keep_id}
        self.refresh_items(reset_selection=False, scroll_to_top=True)
        self._record_view_change(before)
        n = self._set_counts.get(tag, 0)
        self._toast(f"Set · {n}")

    def _leave_set_view(self) -> None:
        before = self._view_loc()
        self._set_view_tag = None
        self._special_view = None
        self.current_folder_id = None
        self.current_smart_folder_id = None
        self.refresh_items(reset_selection=False, scroll_to_top=False)
        self._restore_sidebar_selection()
        self._save_sidebar_state()
        self._record_view_change(before)

    def _view_loc(self) -> _ViewLoc:
        viewer_id = None
        if self.is_viewer_open() and self._viewer_item_id:
            viewer_id = self._viewer_item_id
        return _ViewLoc(
            smart_id=self.current_smart_folder_id,
            folder_id=self.current_folder_id,
            special=self._special_view,
            set_tag=self._set_view_tag if self._special_view == "set" else None,
            descendants=self.include_descendants,
            viewer_id=viewer_id,
        )

    def _sync_nav_buttons(self) -> None:
        if hasattr(self, "nav_back_btn"):
            self.nav_back_btn.set_sensitive(bool(self._nav_back))
            self.nav_fwd_btn.set_sensitive(bool(self._nav_forward))

    def _record_view_change(self, before: _ViewLoc) -> None:
        if self._nav_restoring:
            return
        after = self._view_loc()
        if after == before:
            return
        self._nav_back.append(before)
        if len(self._nav_back) > NAV_HISTORY_MAX:
            self._nav_back = self._nav_back[-NAV_HISTORY_MAX:]
        self._nav_forward.clear()
        self._sync_nav_buttons()

    def _scope_changed(self, loc: _ViewLoc) -> bool:
        return (
            loc.smart_id != self.current_smart_folder_id
            or loc.folder_id != self.current_folder_id
            or loc.special != self._special_view
            or (loc.set_tag if loc.special == "set" else None)
            != (self._set_view_tag if self._special_view == "set" else None)
            or loc.descendants != self.include_descendants
        )

    def _apply_view_loc(self, loc: _ViewLoc) -> None:
        scope_changed = self._scope_changed(loc)
        self._nav_restoring = True
        self._sidebar_nav_lock = True
        try:
            if scope_changed:
                self.current_smart_folder_id = loc.smart_id
                self.current_folder_id = loc.folder_id
                self._special_view = loc.special
                self._set_view_tag = loc.set_tag if loc.special == "set" else None
                self.include_descendants = loc.descendants
                self._restore_sidebar_selection()
                keep = loc.viewer_id
                if keep:
                    item = self.library.items_by_id.get(keep)
                    if item is not None:
                        self.selected_item = item
                        self._marked = {keep}
                self.refresh_items(
                    reset_selection=keep is None, scroll_to_top=True
                )
                self._save_sidebar_state()
            if loc.viewer_id:
                item = self.library.items_by_id.get(loc.viewer_id)
                if item is not None and (item.is_image or item.is_video):
                    self.open_inline_viewer(item)
                elif self.is_viewer_open():
                    self.close_inline_viewer(restore_scroll=not scope_changed)
            elif self.is_viewer_open():
                self.close_inline_viewer(restore_scroll=not scope_changed)
        finally:
            self._nav_restoring = False
            GLib.idle_add(self._unlock_sidebar_nav)
            self._sync_nav_buttons()

    def nav_back(self) -> None:
        if not self._nav_back:
            return
        self._nav_forward.append(self._view_loc())
        loc = self._nav_back.pop()
        self._apply_view_loc(loc)

    def nav_forward(self) -> None:
        if not self._nav_forward:
            return
        self._nav_back.append(self._view_loc())
        loc = self._nav_forward.pop()
        self._apply_view_loc(loc)

    def group_selection_into_set(self) -> None:
        from write import WriteError

        items = self._effective_hand_off_items()
        if len(items) < 2:
            self._toast("Select at least two items")
            return
        tag = set_tag_of(items[0])
        if tag is None:
            for it in items[1:]:
                tag = set_tag_of(it)
                if tag:
                    break
        if tag is None:
            tag = mint_set_tag(items[0].id)
        ids = [
            it.id
            for it in items
            if set_tags_of(it) != [tag]
        ]
        if not ids:
            self._toast("Already grouped")
            return
        try:
            _ok, errors = self.library.update_items_batch(ids, add_tags=[tag])
        except WriteError as exc:
            self._toast(str(exc))
            return
        self._rebuild_set_counts(force=True)
        self.refresh_items(reset_selection=False)
        n = self._set_counts.get(tag, 0)
        msg = f"Set · {n}"
        if errors:
            msg += f" · {len(errors)} failed"
        self._toast(msg)

    def remove_selection_from_set(self) -> None:
        from write import WriteError

        items = self._effective_hand_off_items()
        drop = list(dict.fromkeys(t for it in items for t in set_tags_of(it)))
        if not drop:
            self._toast("Not in a set")
            return
        ids = [it.id for it in items if set_tags_of(it)]
        try:
            _ok, errors = self.library.update_items_batch(ids, remove_tags=drop)
        except WriteError as exc:
            self._toast(str(exc))
            return
        self._rebuild_set_counts(force=True)
        if self._special_view == "set":
            still = self._set_counts.get(self._set_view_tag or "", 0)
            if still < 1:
                self._leave_set_view()
                self._toast("Set empty")
                return
        self.refresh_items(reset_selection=False)
        msg = "Removed from set"
        if errors:
            msg += f" · {len(errors)} failed"
        self._toast(msg)

    def _auto_join_set(self, source: Item, new_item: Item) -> Item:
        """Put a browse-created derivative in the source's set (mint if needed)."""
        from sets import join_into_set

        join_into_set(self.library, source, new_item)
        self._rebuild_set_counts(force=True)
        return self.library.items_by_id.get(new_item.id) or new_item

    def _compare_current_still(self) -> Item | None:
        """The still the inspector is showing — focused item if it is an image."""
        it = self.selected_item
        if it is None:
            items = self._effective_hand_off_items()
            if len(items) == 1:
                it = items[0]
        if it is None or not it.is_image or not it.path.is_file():
            return None
        return it

    def _compare_item(self, slot: str) -> Item | None:
        iid = self._compare_a_id if slot == "a" else self._compare_b_id
        if not iid:
            return None
        it = self.library.items_by_id.get(iid)
        if it is None or it.is_deleted or not it.is_image or not it.path.is_file():
            if slot == "a":
                self._compare_a_id = None
            else:
                self._compare_b_id = None
            return None
        return it

    def _compare_set_slot(self, slot: str) -> None:
        it = self._compare_current_still()
        if it is None:
            self._toast("Select a still")
            return
        other = "b" if slot == "a" else "a"
        other_id = self._compare_a_id if other == "a" else self._compare_b_id
        if other_id == it.id:
            if other == "a":
                self._compare_a_id = None
            else:
                self._compare_b_id = None
        if slot == "a":
            self._compare_a_id = it.id
        else:
            self._compare_b_id = it.id
        self._sync_compare_ui()
        self._toast(f"Compare {slot.upper()} · {it.display_name}")

    def _compare_swap_slots(self) -> None:
        self._compare_a_id, self._compare_b_id = self._compare_b_id, self._compare_a_id
        if self.is_viewer_open() and self._viewer_mode == "compare":
            self._compare_a_pixbuf, self._compare_b_pixbuf = (
                self._compare_b_pixbuf,
                self._compare_a_pixbuf,
            )
            self.viewer_picture.queue_draw()
            self._sync_compare_viewer_title()
        self._sync_compare_ui()

    def _compare_clear_slots(self) -> None:
        self._compare_a_id = None
        self._compare_b_id = None
        if self.is_viewer_open() and self._viewer_mode == "compare":
            self.close_inline_viewer()
        self._sync_compare_ui()

    def _sync_compare_ui(self) -> None:
        if not hasattr(self, "insp_cmp_go"):
            return
        a = self._compare_item("a")
        b = self._compare_item("b")

        def fill(pic: Gtk.Picture, lbl: Gtk.Label, item: Item | None, empty: str) -> None:
            if item is None:
                pic.set_paintable(None)
                lbl.set_text(empty)
                return
            lbl.set_text(item.display_name)
            path = _thumb_path_for(item)
            pix = _pixbuf_from_path(path, 36, 36) if path else None
            if pix is None:
                pic.set_paintable(None)
            else:
                pic.set_paintable(Gdk.Texture.new_for_pixbuf(pix))

        fill(self.insp_cmp_a_pic, self.insp_cmp_a_name, a, "(empty)")
        fill(self.insp_cmp_b_pic, self.insp_cmp_b_name, b, "(empty)")
        ready = a is not None and b is not None and a.id != b.id
        self.insp_cmp_go.set_sensitive(ready)
        self.insp_cmp_swap.set_sensitive(a is not None or b is not None)
        self.insp_cmp_clear.set_sensitive(a is not None or b is not None)

    def _sync_compare_viewer_title(self) -> None:
        a = self._compare_item("a")
        b = self._compare_item("b")
        left = a.display_name if a else "?"
        right = b.display_name if b else "?"
        self.viewer_title.set_text(f"{left}  |  {right}")
        self.viewer_hint.set_text("Drag the line · Esc close")

    def open_compare_viewer(self) -> None:
        """Open the center viewer with A | B and a drag split."""
        a = self._compare_item("a")
        b = self._compare_item("b")
        if a is None or b is None:
            self._toast("Set both A and B stills first")
            return
        if a.id == b.id:
            self._toast("A and B are the same image")
            return
        pa = _pixbuf_from_path(str(a.path), 4096, 4096)
        pb = _pixbuf_from_path(str(b.path), 4096, 4096)
        if pa is None:
            self._toast(f"Could not load A · {a.display_name}")
            return
        if pb is None:
            self._toast(f"Could not load B · {b.display_name}")
            return

        before = self._view_loc()
        if not self.is_viewer_open():
            self._saved_grid_scroll = {
                "value": self._grid_scroll_value(),
                "loaded": len(self._items),
                "focus_id": (self.selected_item.id if self.selected_item else a.id),
            }
            self._snapshot_viewer_pane()

        self._stop_inline_video()
        self._viewer_src_pixbuf = None
        self._compare_a_pixbuf = pa
        self._compare_b_pixbuf = pb
        self._compare_split = 0.5
        self._compare_dragging = False
        self._viewer_item_id = a.id
        self._viewer_open = True
        self._viewer_mode = "compare"
        self._viewer_zoom_steps = 0
        self._viewer_fit = True
        self._sync_viewer_toolbar()
        self._sync_compare_viewer_title()
        self.viewer_body.set_visible_child_name("image")
        self.center_stack.set_visible_child_name("viewer")
        cw, ch = self._viewer_fit_canvas_size()
        self.viewer_picture.set_content_width(cw)
        self.viewer_picture.set_content_height(ch)
        self.viewer_picture.set_hexpand(True)
        self.viewer_picture.set_vexpand(True)
        self.viewer_picture.set_halign(Gtk.Align.FILL)
        self.viewer_picture.set_valign(Gtk.Align.FILL)
        try:
            self.viewer_picture.set_cursor_from_name("col-resize")
            self.viewer_scroll.set_cursor_from_name("col-resize")
        except Exception:
            pass
        self.viewer_picture.queue_draw()
        self.viewer_picture.grab_focus()

        def _after_map() -> bool:
            if not self.is_viewer_open() or self._viewer_mode != "compare":
                return False
            self._snapshot_viewer_pane()
            cw2, ch2 = self._viewer_fit_canvas_size()
            self.viewer_picture.set_content_width(cw2)
            self.viewer_picture.set_content_height(ch2)
            self.viewer_picture.queue_draw()
            return False

        GLib.idle_add(_after_map)
        GLib.timeout_add(30, _after_map)
        self._record_view_change(before)

    def open_inline_viewer(self, item: Item | None = None) -> None:
        """Show a still or video in the center pane (Eagle-style detail view).

        Audio still opens in an external player.
        """
        item = item or self.selected_item
        if item is None:
            self._toast("Nothing selected")
            return
        if item.is_audio or (not item.is_image and not item.is_video):
            if self.is_viewer_open():
                self.close_inline_viewer(restore_scroll=True)
            self._open_external_media(item)
            return

        before = self._view_loc()
        # Capture grid offset before the stack unmaps it (that resets scroll).
        if not self.is_viewer_open():
            self._saved_grid_scroll = {
                "value": self._grid_scroll_value(),
                "loaded": len(self._items),
                "focus_id": item.id,
            }
            self._snapshot_viewer_pane()

        if item.is_video:
            self._open_inline_video(item)
            self._record_view_change(before)
            return

        path = item.path
        if not path.is_file():
            self._toast(f"Missing file: {path}")
            return

        self._stop_inline_video()

        # Prefer full image; decode from bytes so an overwrite is not cached
        pb = _pixbuf_from_path(str(path), 4096, 4096)
        if pb is None:
            self._toast(f"Could not load image: {path}")
            return

        self._viewer_src_pixbuf = pb
        self.viewer_title.set_text(item.display_name)
        self.viewer_hint.set_text("")
        self._viewer_item_id = item.id
        self._viewer_open = True
        self._viewer_mode = "image"
        self._sync_viewer_toolbar()
        self._viewer_fit = True
        self._viewer_scale = None
        self._viewer_zoom_steps = 0
        self._viewer_zoom_last = 0.0
        self.viewer_body.set_visible_child_name("image")
        self._apply_viewer_zoom()
        self.center_stack.set_visible_child_name("viewer")
        self.viewer_picture.grab_focus()

        def _after_map() -> bool:
            if not self.is_viewer_open() or self._viewer_mode != "image":
                return False
            self._snapshot_viewer_pane()
            if self._viewer_zoom_steps <= 0:
                self._apply_viewer_zoom()
            self.viewer_picture.queue_draw()
            self.viewer_picture.grab_focus()
            return False

        GLib.idle_add(_after_map)
        GLib.timeout_add(30, _after_map)
        # Keep grid selection in sync for inspector
        self.selected_item = item
        self.update_inspector()
        self._update_path_label()
        self._record_view_change(before)

    def close_inline_viewer(self, *, restore_scroll: bool = True) -> bool:
        """Leave detail view; return True if a viewer was closed."""
        if not self.is_viewer_open():
            return False
        before = self._view_loc()
        self._viewer_open = False
        self._viewer_item_id = None
        self._viewer_in = None
        self._viewer_out = None
        self._viewer_mode = "image"
        self._viewer_src_pixbuf = None
        self._compare_a_pixbuf = None
        self._compare_b_pixbuf = None
        self._compare_dragging = False
        self._viewer_zoom_steps = 0
        self._viewer_scale = None
        self._viewer_lock_center = False
        self._viewer_center_wh = None
        self._stop_inline_video()
        self._sync_viewer_toolbar()
        try:
            self.viewer_body.set_visible_child_name("image")
        except Exception:  # noqa: BLE001
            pass
        self.viewer_picture.queue_draw()
        try:
            self.viewer_picture.set_cursor_from_name("default")
            self.viewer_scroll.set_cursor_from_name("default")
        except Exception:
            pass
        self.center_stack.set_visible_child_name("grid")
        snap = self._saved_grid_scroll
        self._saved_grid_scroll = None
        self.grid.grab_focus()
        GLib.idle_add(self._idle_grab_focus, self.grid)
        if restore_scroll and snap is not None:
            loaded = int(snap.get("loaded") or 0)
            if loaded > len(self._items):
                self._ensure_loaded(loaded)
            self._restore_grid_scroll(float(snap.get("value") or 0.0))
        else:
            self._cancel_scroll_restore()
        self._record_view_change(before)
        return True

    def _open_inline_video(self, item: Item) -> None:
        """Play a video in the center pane (Gtk.Video)."""
        path = item.path
        if not path.is_file():
            self._toast(f"Missing file: {path}")
            return

        self._stop_inline_video()
        self._viewer_src_pixbuf = None
        self.viewer_picture.queue_draw()
        try:
            media = Gtk.MediaFile.new_for_filename(str(path))
            try:
                media.set_loop(False)
            except Exception:  # noqa: BLE001
                pass
            # Connect before attaching/autoplay so immediate backend errors
            # (notably missing decoders) cannot race past notify::error.
            self._viewer_audio_ignore_playing = True
            self._connect_viewer_playing(media)
            self.viewer_video.set_media_stream(media)
            self.viewer_video.set_autoplay(True)
            try:
                media.set_muted(False)
                media.set_volume(1.0)
                media.play()
            except Exception:  # noqa: BLE001
                pass
            self._viewer_audio_ignore_playing = False
        except Exception as exc:  # noqa: BLE001
            self._toast(f"Could not load video: {exc}")
            self._open_external_media(item)
            return
        # Gtk.Video/GStreamer often has picture and no sound. Play audio with
        # mpv; mute the widget only while that sidecar is actually running.
        self._start_viewer_audio(path, 0.0)

        self.viewer_title.set_text(item.display_name)
        self._viewer_item_id = item.id
        self._viewer_open = True
        self._viewer_mode = "video"
        self._load_viewer_marks(item)
        self._sync_viewer_mark_hint()
        self._sync_viewer_toolbar()
        self.viewer_body.set_visible_child_name("video")
        self.center_stack.set_visible_child_name("viewer")
        self.viewer_video.grab_focus()
        GLib.idle_add(self._idle_grab_focus, self.viewer_video)
        self.selected_item = item
        self.update_inspector()
        self._update_path_label()

    def viewer_toggle_play(self) -> None:
        """Space while video is open: play/pause."""
        if not self.is_viewer_open() or self._viewer_mode != "video":
            return
        stream = self.viewer_video.get_media_stream()
        if stream is None:
            return
        playing = stream.get_playing()
        self._viewer_audio_ignore_playing = True
        try:
            stream.set_playing(not playing)
        finally:
            self._viewer_audio_ignore_playing = False
        if playing:
            self._stop_viewer_audio()
            return
        seconds = 0.0
        try:
            seconds = max(0.0, int(stream.get_timestamp()) / 1_000_000.0)
        except Exception:  # noqa: BLE001
            seconds = 0.0
        item = self._viewer_item()
        if item is not None and item.path.is_file():
            self._start_viewer_audio(item.path, seconds)

    def _viewer_item(self) -> Item | None:
        item = None
        if self._viewer_item_id:
            item = self.library.items_by_id.get(self._viewer_item_id)
        return item or self.selected_item

    def _viewer_seconds(self) -> float | None:
        """Current playhead in seconds, or None. Pauses playback."""
        if not self.is_viewer_open() or self._viewer_mode != "video":
            return None
        stream = self.viewer_video.get_media_stream()
        if stream is None:
            return None
        try:
            stream.set_playing(False)
        except Exception:  # noqa: BLE001
            pass
        try:
            ts_us = int(stream.get_timestamp())
        except Exception:  # noqa: BLE001
            return None
        return max(0.0, ts_us / 1_000_000.0)

    def _load_viewer_marks(self, item: Item) -> None:
        from video_trim import load_marks

        marks = load_marks(item)
        self._viewer_in = marks.get("in")
        self._viewer_out = marks.get("out")

    def _sync_viewer_mark_hint(self) -> None:
        from audio_crop import format_time

        bits = ["Space play/pause · i in · o out · x cut · p frame"]
        if self._viewer_in is not None or self._viewer_out is not None:
            inn = format_time(self._viewer_in) if self._viewer_in is not None else "—"
            out = format_time(self._viewer_out) if self._viewer_out is not None else "—"
            bits.append(f"in {inn} → out {out}")
        self.viewer_hint.set_text("  ·  ".join(bits))

    def _mark_viewer(self, which: str) -> None:
        from video_trim import save_marks
        from write import WriteError

        if not self.is_viewer_open() or self._viewer_mode != "video":
            self._toast("Play the video first")
            return
        item = self._viewer_item()
        if item is None or not item.is_video:
            self._toast("No video to mark")
            return
        seconds = self._viewer_seconds()
        if seconds is None:
            self._toast("Could not read time")
            return
        try:
            if which == "in":
                save_marks(item, start=seconds)
                self._viewer_in = seconds
            else:
                save_marks(item, end=seconds)
                self._viewer_out = seconds
        except WriteError as exc:
            self._toast(str(exc))
            return
        self._sync_viewer_mark_hint()
        label = "in" if which == "in" else "out"
        extra = ""
        if (
            self._viewer_in is not None
            and self._viewer_out is not None
            and self._viewer_out <= self._viewer_in
        ):
            extra = " · out must be after in"
        self._toast(f"{label} {seconds:.2f}s{extra}")

    def _export_viewer_trim(self) -> None:
        from audio_crop import MIN_CROP_S
        from video_trim import save_video_trim_as_new_item
        from write import WriteError

        if self._trim_exporting:
            return
        if not self.is_viewer_open() or self._viewer_mode != "video":
            self._toast("Play the video first")
            return
        item = self._viewer_item()
        if item is None or not item.is_video:
            self._toast("No video to cut")
            return
        in_s, out_s = self._viewer_in, self._viewer_out
        if in_s is None or out_s is None:
            self._toast("Mark in and out first")
            return
        if out_s <= in_s + MIN_CROP_S:
            self._toast("out must be after in")
            return
        self._trim_exporting = True
        if hasattr(self, "viewer_trim_btn"):
            self.viewer_trim_btn.set_sensitive(False)
        self._toast(f"Cutting · {in_s:.2f}s → {out_s:.2f}s…")

        def work() -> None:
            try:
                new_item = save_video_trim_as_new_item(
                    self.library.root, item, in_s, out_s
                )
                err = None
            except WriteError as exc:
                new_item = None
                err = exc
            except Exception as exc:  # noqa: BLE001
                new_item = None
                err = exc

            def apply() -> bool:
                self._trim_exporting = False
                if hasattr(self, "viewer_trim_btn"):
                    self.viewer_trim_btn.set_sensitive(True)
                if err is not None or new_item is None:
                    self._toast(f"Cut failed: {err}")
                    return False
                self.library.upsert_item(new_item)
                new_item = self._auto_join_set(item, new_item)
                self.selected_item = new_item
                self._marked = {new_item.id}
                self.refresh_items(reset_selection=False)
                dur = float(new_item.duration or 0)
                self._toast(
                    f"Cut · {dur:.2f}s · {new_item.width}×{new_item.height}"
                )
                return False

            GLib.idle_add(apply)

        threading.Thread(target=work, name="eagle-video-trim", daemon=True).start()

    def save_viewer_frame(self) -> None:
        """Grab the current playhead frame as a new untagged still."""
        from import_media import save_video_frame_as_item
        from write import WriteError

        if self._frame_saving:
            return
        if not self.is_viewer_open() or self._viewer_mode != "video":
            self._toast("Play the video first")
            return
        item = self._viewer_item()
        if item is None or not item.is_video:
            self._toast("No video to grab from")
            return
        if not item.path.is_file():
            self._toast(f"Missing file: {item.path}")
            return
        seconds = self._viewer_seconds()
        if seconds is None:
            self._toast("Could not read time")
            return
        dur_s = float(item.duration or 0.0)
        last_frame = bool(dur_s > 0 and seconds >= max(0.0, dur_s - 0.12))
        where = "last frame" if last_frame else f"{seconds:.2f}s"
        self._frame_saving = True
        if hasattr(self, "viewer_save_frame_btn"):
            self.viewer_save_frame_btn.set_sensitive(False)
        self._toast(f"Saving frame · {where}…")

        def work() -> None:
            try:
                new_item = save_video_frame_as_item(
                    self.library.root, item, seconds, last_frame=last_frame
                )
                err = None
            except WriteError as exc:
                new_item = None
                err = exc
            except Exception as exc:  # noqa: BLE001
                new_item = None
                err = exc

            def apply() -> bool:
                self._frame_saving = False
                if hasattr(self, "viewer_save_frame_btn"):
                    self.viewer_save_frame_btn.set_sensitive(True)
                if err is not None or new_item is None:
                    self._toast(f"Save frame failed: {err}")
                    return False
                self.library.upsert_item(new_item)
                new_item = self._auto_join_set(item, new_item)
                self._toast(
                    f"Saved frame · {where} · {new_item.width}×{new_item.height}"
                )
                return False

            GLib.idle_add(apply)

        threading.Thread(target=work, name="eagle-save-frame", daemon=True).start()

    def viewer_navigate(self, delta: int) -> None:
        """Prev/next image or video while the inline viewer is open."""
        if not self.is_viewer_open() or self._viewer_mode == "compare":
            return
        if not self._items:
            return
        # Find current index by id
        cur = 0
        if self._viewer_item_id:
            for i, it in enumerate(self._items):
                if it.id == self._viewer_item_id:
                    cur = i
                    break
        if delta > 0 and cur >= len(self._items) - 2:
            self._load_more_items()
        new = max(0, min(len(self._items) - 1, cur + delta))
        step = 1 if delta >= 0 else -1
        i = new
        while 0 <= i < len(self._items):
            it = self._items[i]
            if it.is_image or it.is_video:
                self._select_index(i, ctrl=False, shift=False)
                self.open_inline_viewer(it)
                return
            i += step
        self._toast("No more media in this view")

    def _viewer_native_size(self) -> tuple[int, int] | None:
        pb = self._viewer_src_pixbuf
        if pb is not None:
            w, h = int(pb.get_width()), int(pb.get_height())
            if w > 0 and h > 0:
                return w, h
        return None

    def _snapshot_viewer_pane(self) -> None:
        """Record the visible center pane before the image can inflate it."""
        for wdg in (self.viewer_scroll, self.viewer_body, self.center_stack, self.grid_scroll):
            w = int(wdg.get_allocated_width())
            h = int(wdg.get_allocated_height())
            if w >= 64 and h >= 64:
                self._viewer_pane = (max(1, w - 8), max(1, h - 8))
                return

    def _viewer_viewport_px(self) -> tuple[int, int]:
        if self._viewer_pane is not None:
            return self._viewer_pane
        self._snapshot_viewer_pane()
        if self._viewer_pane is not None:
            return self._viewer_pane
        return 1, 1

    def _viewer_fit_canvas_size(self) -> tuple[int, int]:
        """Allocation the fitted image should fill so the DrawingArea actually draws."""
        w = int(self.viewer_scroll.get_allocated_width())
        h = int(self.viewer_scroll.get_allocated_height())
        if w >= 32 and h >= 32:
            return w, h
        pw, ph = self._viewer_viewport_px()
        return max(32, pw), max(32, ph)

    def _on_viewer_scroll_resized(self, *_a) -> None:
        if not self.is_viewer_open() or self._viewer_mode not in ("image", "compare"):
            return
        if self._viewer_zoom_steps > 0:
            return
        cw, ch = self._viewer_fit_canvas_size()
        if (
            self.viewer_picture.get_content_width() != cw
            or self.viewer_picture.get_content_height() != ch
        ):
            self.viewer_picture.set_content_width(cw)
            self.viewer_picture.set_content_height(ch)
            self.viewer_picture.queue_draw()

    def _viewer_fit_wh(self) -> tuple[int, int] | None:
        size = self._viewer_native_size()
        if size is None:
            return None
        pw, ph = self._viewer_viewport_px()
        fit = min(pw / size[0], ph / size[1])
        return max(1, int(round(size[0] * fit))), max(1, int(round(size[1] * fit)))

    def _viewer_zoom_label(self) -> str:
        if self._viewer_zoom_steps <= 0:
            return ""
        return f"{max(1, int(round((VIEWER_ZOOM_STEP ** self._viewer_zoom_steps) * 100)))}%"

    def _sync_viewer_toolbar(self) -> None:
        """Image tools vs video save-frame on the focused-item bar."""
        video = self._viewer_mode == "video"
        compare = self._viewer_mode == "compare"
        if hasattr(self, "viewer_zoom_in_btn"):
            self.viewer_zoom_in_btn.set_visible(not video and not compare)
            self.viewer_zoom_out_btn.set_visible(not video and not compare)
        if hasattr(self, "viewer_save_frame_btn"):
            self.viewer_save_frame_btn.set_visible(video)
        if hasattr(self, "viewer_trim_btn"):
            self.viewer_trim_btn.set_visible(video)
        if hasattr(self, "crop_916_btn"):
            self.crop_916_btn.set_visible(video)
        if hasattr(self, "crop_btn"):
            item = self.selected_item
            can_crop = bool(
                not video
                and not compare
                and item is not None
                and (item.is_image or item.is_audio)
                and item.path.is_file()
            )
            self.crop_btn.set_visible(not video and not compare)
            self.crop_btn.set_sensitive(can_crop)
        if hasattr(self, "upscale_btn"):
            items = self._effective_hand_off_items()
            item = items[0] if len(items) == 1 else None
            can_upscale = bool(
                not compare
                and item is not None
                and (item.is_image or item.is_video)
                and item.path.is_file()
            )
            self.upscale_btn.set_visible(not compare)
            self.upscale_btn.set_sensitive(can_upscale)

    @staticmethod
    def _fit_pixbuf_rect(
        nw: int, nh: int, dw: int, dh: int
    ) -> tuple[float, float, float, float]:
        """Letterbox (nw×nh) into (dw×dh). Returns (x, y, tw, th)."""
        if nw <= 0 or nh <= 0 or dw <= 0 or dh <= 0:
            return 0.0, 0.0, float(max(1, dw)), float(max(1, dh))
        scale = min(dw / nw, dh / nh)
        tw = nw * scale
        th = nh * scale
        return (dw - tw) / 2.0, (dh - th) / 2.0, tw, th

    def _paint_fitted_pixbuf(self, cr, pb: GdkPixbuf.Pixbuf, dw: int, dh: int) -> None:
        nw, nh = int(pb.get_width()), int(pb.get_height())
        x, y, tw, th = self._fit_pixbuf_rect(nw, nh, dw, dh)
        if tw <= 0 or th <= 0:
            return
        cr.save()
        cr.translate(x, y)
        cr.scale(tw / max(1, nw), th / max(1, nh))
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()
        cr.restore()

    def _on_viewer_draw(self, _area, cr, width: int, height: int, _data) -> None:
        if width <= 0 or height <= 0:
            return
        if self._viewer_mode == "compare":
            pa = self._compare_a_pixbuf
            pb = self._compare_b_pixbuf
            if pa is None or pb is None:
                return
            split_x = max(1.0, min(float(width - 1), width * float(self._compare_split)))
            self._paint_fitted_pixbuf(cr, pa, width, height)
            cr.save()
            cr.rectangle(split_x, 0, width - split_x, height)
            cr.clip()
            self._paint_fitted_pixbuf(cr, pb, width, height)
            cr.restore()
            from theme import cairo_rgba  # noqa: PLC0415

            cr.set_source_rgba(*cairo_rgba("foreground", 0.92))
            cr.set_line_width(2)
            cr.move_to(split_x, 0)
            cr.line_to(split_x, height)
            cr.stroke()
            cr.arc(split_x, height / 2.0, 7, 0, 6.28318)
            cr.set_source_rgba(*cairo_rgba("foreground", 0.95))
            cr.fill_preserve()
            cr.set_source_rgba(*cairo_rgba("background", 0.55))
            cr.set_line_width(1)
            cr.stroke()
            return
        pb = self._viewer_src_pixbuf
        if pb is None:
            return
        nw, nh = int(pb.get_width()), int(pb.get_height())
        if nw <= 0 or nh <= 0:
            return
        if self._viewer_zoom_steps <= 0:
            scale = min(width / nw, height / nh)
            cr.translate((width - nw * scale) / 2.0, (height - nh * scale) / 2.0)
            cr.scale(scale, scale)
        else:
            cr.scale(width / nw, height / nh)
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()

    def _apply_viewer_zoom(self) -> None:
        """Fit, or grow the canvas to fit×1.08^steps and pin the image center."""
        src = self._viewer_src_pixbuf
        if src is None:
            self.viewer_hint.set_text(self._viewer_zoom_label())
            self.viewer_picture.queue_draw()
            return
        if self._viewer_zoom_steps <= 0:
            self._viewer_fit = True
            self._viewer_scale = None
            self._viewer_lock_center = False
            self._viewer_center_wh = None
            # ScrolledWindow sizes its child from content size. 0×0 never draws.
            cw, ch = self._viewer_fit_canvas_size()
            self.viewer_picture.set_content_width(cw)
            self.viewer_picture.set_content_height(ch)
            self.viewer_picture.set_hexpand(True)
            self.viewer_picture.set_vexpand(True)
            self.viewer_picture.set_halign(Gtk.Align.FILL)
            self.viewer_picture.set_valign(Gtk.Align.FILL)
            self.viewer_picture.queue_draw()
            self._viewer_set_pan_cursor()
            self.viewer_hint.set_text(self._viewer_zoom_label())
            return
        fitted = self._viewer_fit_wh()
        if fitted is None:
            return
        factor = VIEWER_ZOOM_STEP ** self._viewer_zoom_steps
        w = max(1, int(round(fitted[0] * factor)))
        h = max(1, int(round(fitted[1] * factor)))
        nat = self._viewer_native_size()
        if nat is not None:
            cap_w = max(1, int(nat[0] * VIEWER_ZOOM_MAX))
            cap_h = max(1, int(nat[1] * VIEWER_ZOOM_MAX))
            if w > cap_w or h > cap_h:
                w, h = cap_w, cap_h
        self._viewer_fit = False
        self._viewer_scale = w / max(1, src.get_width())
        self._viewer_lock_center = True
        self._viewer_center_wh = (w, h)
        self.viewer_picture.set_hexpand(False)
        self.viewer_picture.set_vexpand(False)
        self.viewer_picture.set_halign(Gtk.Align.START)
        self.viewer_picture.set_valign(Gtk.Align.START)
        self.viewer_picture.set_content_width(w)
        self.viewer_picture.set_content_height(h)
        self.viewer_picture.queue_draw()
        self._pin_viewer_center()
        self._viewer_set_pan_cursor()
        self.viewer_hint.set_text(self._viewer_zoom_label())

    def _viewer_zoom_by(self, factor: float) -> None:
        if not self.is_viewer_open() or self._viewer_mode != "image":
            return
        if factor > 1:
            self._viewer_zoom_steps += 1
        elif self._viewer_zoom_steps > 0:
            self._viewer_zoom_steps -= 1
        self._apply_viewer_zoom()

    def _pin_viewer_center(self) -> None:
        """Keep the image center in the pane. Uses known size, not delayed idle."""
        wh = self._viewer_center_wh
        if wh is None or self._viewer_zoom_steps <= 0:
            return
        img_w, img_h = wh
        hadj = self.viewer_scroll.get_hadjustment()
        vadj = self.viewer_scroll.get_vadjustment()
        pw, ph = self._viewer_viewport_px()
        if hadj is not None and hadj.get_page_size() > 1:
            pw = hadj.get_page_size()
        if vadj is not None and vadj.get_page_size() > 1:
            ph = vadj.get_page_size()
        self._viewer_setting_adj = True
        try:
            if hadj is not None:
                self._set_scroll_adj(hadj, (img_w - pw) / 2.0)
            if vadj is not None:
                self._set_scroll_adj(vadj, (img_h - ph) / 2.0)
        finally:
            self._viewer_setting_adj = False

    def _on_viewer_adj_changed(self, _adj) -> None:
        if self._viewer_lock_center:
            self._pin_viewer_center()

    def _on_viewer_adj_value(self, _adj) -> None:
        if self._viewer_setting_adj or not self._viewer_lock_center:
            return
        self._pin_viewer_center()

    @staticmethod
    def _set_scroll_adj(adj: Gtk.Adjustment, value: float) -> None:
        lo = adj.get_lower()
        hi = adj.get_upper() - adj.get_page_size()
        if hi < lo:
            hi = lo
        adj.set_value(max(lo, min(hi, value)))

    def _viewer_can_pan(self) -> bool:
        return (
            self.is_viewer_open()
            and self._viewer_mode == "image"
            and self._viewer_zoom_steps > 0
        )

    def _viewer_set_pan_cursor(self) -> None:
        name = "grab" if self._viewer_can_pan() else "default"
        try:
            self.viewer_picture.set_cursor_from_name(name)
            self.viewer_scroll.set_cursor_from_name(name)
        except Exception:
            pass

    def _on_viewer_drag_begin(self, gesture: Gtk.GestureDrag, x: float, _y: float) -> None:
        if self._viewer_mode == "compare":
            w = max(1, int(self.viewer_picture.get_allocated_width()))
            self._compare_dragging = True
            self._compare_drag_x0 = x
            self._compare_split = max(0.02, min(0.98, x / w))
            self.viewer_picture.queue_draw()
            try:
                self.viewer_picture.set_cursor_from_name("col-resize")
            except Exception:
                pass
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            return
        if not self._viewer_can_pan():
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._viewer_lock_center = False
        hadj = self.viewer_scroll.get_hadjustment()
        vadj = self.viewer_scroll.get_vadjustment()
        self._viewer_drag_h0 = hadj.get_value()
        self._viewer_drag_v0 = vadj.get_value()
        try:
            self.viewer_picture.set_cursor_from_name("grabbing")
            self.viewer_scroll.set_cursor_from_name("grabbing")
        except Exception:
            pass
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_viewer_drag_update(self, _g: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._viewer_mode == "compare" and self._compare_dragging:
            w = max(1, int(self.viewer_picture.get_allocated_width()))
            self._compare_split = max(
                0.02, min(0.98, (self._compare_drag_x0 + dx) / w)
            )
            self.viewer_picture.queue_draw()
            return
        self._set_scroll_adj(self.viewer_scroll.get_hadjustment(), self._viewer_drag_h0 - dx)
        self._set_scroll_adj(self.viewer_scroll.get_vadjustment(), self._viewer_drag_v0 - dy)

    def _on_viewer_drag_end(self, _g: Gtk.GestureDrag, _dx: float, _dy: float) -> None:
        self._compare_dragging = False
        if self._viewer_mode == "compare":
            try:
                self.viewer_picture.set_cursor_from_name("col-resize")
                self.viewer_scroll.set_cursor_from_name("col-resize")
            except Exception:
                pass
            return
        self._viewer_set_pan_cursor()

    def _on_viewer_scroll(self, _c, _dx: float, dy: float) -> bool:
        if not self.is_viewer_open():
            return False
        if self._viewer_mode == "compare":
            return True
        if self._viewer_mode != "image":
            return False
        if dy == 0:
            return False
        now = time.monotonic()
        if now - self._viewer_zoom_last < VIEWER_ZOOM_COOLDOWN_S:
            return True
        self._viewer_zoom_last = now
        if dy < 0:
            self._viewer_zoom_by(VIEWER_ZOOM_STEP)
        else:
            self._viewer_zoom_by(1.0 / VIEWER_ZOOM_STEP)
        return True

    def viewer_toggle_zoom(self, *, larger: bool | None = None) -> None:
        """Step zoom in/out, or toggle fit vs 100% if larger is None."""
        if not self.is_viewer_open() or self._viewer_mode != "image":
            return
        if larger is None:
            if self._viewer_zoom_steps <= 0:
                fitted = self._viewer_fit_wh()
                nat = self._viewer_native_size()
                if fitted and nat and fitted[0] > 0:
                    rel = nat[0] / fitted[0]
                    steps = 0
                    acc = 1.0
                    while acc * VIEWER_ZOOM_STEP <= rel + 0.001 and steps < 40:
                        acc *= VIEWER_ZOOM_STEP
                        steps += 1
                    self._viewer_zoom_steps = max(1, steps)
                else:
                    self._viewer_zoom_steps = 8
            else:
                self._viewer_zoom_steps = 0
            self._apply_viewer_zoom()
            return
        self._viewer_zoom_by(VIEWER_ZOOM_STEP if larger else 1.0 / VIEWER_ZOOM_STEP)

    def _open_external_media(self, item: Item) -> None:
        path = str(item.path)
        if item.is_video or item.is_audio:
            players = [
                [
                    "mpv",
                    "--force-window=yes",
                    "--keep-open=yes",
                    "--osc=yes",
                    path,
                ],
                ["xdg-open", path],
            ]
            for cmd in players:
                if not _spawn_detached(cmd):
                    continue
                if cmd[0] == "mpv":
                    kind = "Video" if item.is_video else "Audio"
                    self._toast(
                        f"{kind} · Space play/pause · ←→ seek · q/Esc close · f fullscreen"
                    )
                return
            self._toast("Could not open media (install mpv?)")
            return
        # Fallback for non-image stills we couldn't show inline
        for cmd in (["xdg-open", path], ["imv", path]):
            if _spawn_detached(cmd):
                return
        self._toast("Could not open file")

    def open_selected(self) -> None:
        item = self.selected_item
        if not item:
            self._toast("Nothing selected")
            return
        # Debounce: ignore a second open within 400ms (double-activate / double-Enter)
        now = time.monotonic()
        last = getattr(self, "_last_open_mono", 0.0)
        if now - last < 0.4:
            return
        self._last_open_mono = now

        if item.is_image or item.is_video:
            self.open_inline_viewer(item)
            return
        # Audio / other: external (mpv)
        self._open_external_media(item)

    def reload_library(self) -> None:
        self._toast("Reloading library…")

        def work() -> None:
            try:
                self.library.load()
                err = None
            except Exception as exc:  # noqa: BLE001
                err = exc

            def apply() -> bool:
                if err is not None:
                    self._toast(f"Reload failed: {err}")
                    return False
                self._smart_counts.clear()
                self._special_counts.clear()
                self._populate_sidebar(select_current=True)
                self.refresh_items()
                self._start_duration_backfill()
                n_smart = len(self.library.smart_folders_by_id)
                self._toast(
                    f"Reloaded · {len(self.library.items)} items · {n_smart} smart folders"
                )
                return False

            GLib.idle_add(apply)

        threading.Thread(target=work, name="eagle-reload", daemon=True).start()

    def focus_search(self) -> None:
        self.search.grab_focus()

    def focus_folders(self) -> None:
        """Focus sidebar, restoring the active smart folder / folder row (not always 'All')."""
        # Expand ancestors so the current smart folder row is actually in the list
        before = set(self._smart_expanded)
        self._ensure_smart_expanded_path(self.current_smart_folder_id)
        if self._smart_expanded != before:
            self._populate_sidebar(select_current=False)

        self._restore_sidebar_selection()
        row = self.folder_list.get_selected_row()
        if row is None:
            row = self.folder_list.get_row_at_index(0)
            if row is not None:
                self.folder_list.select_row(row)
        if row is not None:
            # Focus the row itself so ↑↓ continue from this smart folder, not the top
            row.grab_focus()
            self.folder_list.grab_focus()
            # Re-grab the row after list focus (GTK sometimes focuses first child)
            GLib.idle_add(self._focus_sidebar_row, row)

    def _focus_sidebar_row(self, row: Gtk.ListBoxRow) -> bool:
        if row.get_parent() is None:
            return False
        self.folder_list.select_row(row)
        row.grab_focus()
        return False

    def _idle_grab_focus(self, widget=None) -> bool:
        """One-shot focus. grab_focus() returns True; idle_add would loop forever."""
        w = widget if widget is not None else self.grid
        try:
            w.grab_focus()
        except Exception:  # noqa: BLE001
            pass
        return False

    def focus_grid(self) -> None:
        self.grid.grab_focus()

    def toggle_descendants(self) -> None:
        before = self._view_loc()
        self.include_descendants = not self.include_descendants
        state = "including subfolders" if self.include_descendants else "folder only"
        self._toast(state)
        self.refresh_items()
        self._record_view_change(before)

    def move_selection(
        self, delta: int, *, extend: bool = False, keep_selection: bool = False
    ) -> None:
        """
        Move focus by delta indices.
        extend=True (Shift): select every item from the anchor to the new focus.
        keep_selection=True (Ctrl): move focus only; multi-selection unchanged.
        If multi-select is already active (>1), plain arrows also keep selection
        (only move focus) so checkboxes don't vanish when releasing Shift.
        """
        n = self.store.get_n_items()
        if n == 0:
            return
        idx = self.selection.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION:
            idx = self._last_focus_idx if 0 <= self._last_focus_idx < n else 0
        if delta > 0 and int(idx) >= n - max(self._cols * 4, 8):
            self._load_more_items()
            n = self.store.get_n_items()
        new = max(0, min(n - 1, int(idx) + delta))
        # Preserve multi-select when navigating without Shift after building one
        if not extend and (keep_selection or len(self._marked) > 1):
            self._last_focus_idx = new
            self.selection.set_selected(new)
            self.grid.scroll_to(
                new, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None
            )
            obj = self.selection.get_selected_item()
            self.selected_item = obj.item if obj else None
            self._update_path_label()
            self.update_inspector()
            self.grid.grab_focus()
            return
        self._select_index(new, ctrl=False, shift=extend)
        self.grid.grab_focus()

    def _toast(self, text: str) -> None:
        toast = Adw.Toast(title=text, timeout=2)
        self._toast_overlay.add_toast(toast)

    # ── Keys ──────────────────────────────────────────────────────────

    @staticmethod
    def _widget_is_in(focus, root) -> bool:
        if focus is None or root is None:
            return False
        w = focus
        while w is not None:
            if w is root:
                return True
            try:
                w = w.get_parent()
            except Exception:  # noqa: BLE001
                break
        return False

    def _focus_is_search(self, focus) -> bool:
        """True only when the header search box (or its inner Gtk.Text) has focus.

        Any Gtk.Editable used to count, so a selectable path or inspector field
        swallowed t/f/g as if the user was typing a search.
        """
        return self._widget_is_in(focus, getattr(self, "search", None))

    def _focus_is_sidebar(self, focus) -> bool:
        return self._widget_is_in(focus, getattr(self, "folder_list", None))

    def _key_context(self) -> tuple[Any, ...]:
        """Context in which a pending multi-key command is valid."""
        return (
            self.get_focus(),
            self.current_folder_id,
            self.current_smart_folder_id,
            self._special_view,
            self._set_view_tag,
            self._filter_text,
            self.is_viewer_open(),
            self._viewer_item_id,
        )

    def _cancel_g_prefix(self) -> None:
        if self._g_prefix_source:
            GLib.source_remove(self._g_prefix_source)
            self._g_prefix_source = 0
        self._g_prefix_context = None

    def _start_g_prefix(self) -> None:
        self._cancel_g_prefix()
        self._g_prefix_context = self._key_context()

        def expire() -> bool:
            self._g_prefix_source = 0
            self._g_prefix_context = None
            return False

        self._g_prefix_source = GLib.timeout_add(G_PREFIX_TIMEOUT_MS, expire)

    def _jump_to_view_edge(self, *, last: bool) -> None:
        """Focus the first/last asset, loading the full result for the latter."""
        if not self._all_items:
            return
        if last:
            self._ensure_loaded(len(self._all_items))

        if self.is_viewer_open():
            indices = (
                range(len(self._items) - 1, -1, -1)
                if last
                else range(len(self._items))
            )
            idx = next(
                (
                    i
                    for i in indices
                    if self._items[i].is_image or self._items[i].is_video
                ),
                None,
            )
            if idx is None:
                self._toast("No viewable media in this view")
                return
            item = self._items[idx]
            self._select_index(idx, ctrl=False, shift=False)
            self.open_inline_viewer(item)
            return

        idx = len(self._items) - 1 if last else 0
        self._select_index(idx, ctrl=False, shift=False)
        self.grid.grab_focus()

    def _hotkeys_blocked(self) -> bool:
        """True while a live picker/dialog owns the keyboard."""
        win = self._open_dialog
        if win is not None:
            try:
                if win.get_mapped() and win.get_visible():
                    return True
            except Exception:  # noqa: BLE001
                pass
            self._open_dialog = None
        return bool(self._picker_blocking)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._filter_text = entry.get_text() or ""
        # Debounce: don't re-query 15k items on every keystroke
        if self._search_timeout_id:
            GLib.source_remove(self._search_timeout_id)
            self._search_timeout_id = 0

        def fire() -> bool:
            self._search_timeout_id = 0
            self.refresh_items()
            return False

        self._search_timeout_id = GLib.timeout_add(SEARCH_DEBOUNCE_MS, fire)

    def _on_search_escape(self, *_a) -> None:
        """Gtk.SearchEntry Esc — close the focused asset if the viewer is open."""
        self._handle_escape()

    def _on_viewer_escape(
        self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            return self._handle_escape()
        return False

    def _handle_escape(self) -> bool:
        """Esc: dismiss picker, else close the focused asset, else unwind the view."""
        if self._handling_escape:
            return True
        now = time.monotonic()
        last = getattr(self, "_last_escape_mono", 0.0)
        if now - last < 0.12:
            return True
        self._last_escape_mono = now
        self._handling_escape = True
        try:
            if self._close_open_dialog():
                return True
            if self.is_viewer_open():
                self.close_inline_viewer()
                return True
            if self._focus_is_search(self.get_focus()):
                self.search.set_text("")
                self.focus_grid()
                return True
            if self.clear_marks():
                return True
            if self._special_view == "set":
                self._leave_set_view()
                return True
            if self._view_filters.active():
                self.clear_view_filters()
                return True
            if self._filter_text:
                self.search.set_text("")
                return True
            return False
        finally:
            self._handling_escape = False

    def _on_key(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        # Modal tag/folder/type pickers own the keyboard — do not steal letters
        # (was eating s/o/f/i/b/… so filter text became "ie" from "Sofie")
        if self._hotkeys_blocked():
            self._cancel_g_prefix()
            if keyval == Gdk.KEY_Escape:
                return self._handle_escape()
            return False

        focus = self.get_focus()
        # SearchEntry as the header title often keeps focus after opening an
        # asset. Letter hotkeys still apply while the viewer is showing.
        in_search = self._focus_is_search(focus) and not self.is_viewer_open()
        in_sidebar = self._focus_is_sidebar(focus)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        super_mod = bool(state & Gdk.ModifierType.SUPER_MASK)
        # Alt+letter must not fire single-letter hotkeys (mnemonics / OS binds).
        alt = bool(state & Gdk.ModifierType.ALT_MASK)

        pending_g = self._g_prefix_context is not None
        if pending_g and self._g_prefix_context != self._key_context():
            self._cancel_g_prefix()
            pending_g = False
        if pending_g:
            plain = not ctrl and not alt and not super_mod
            if plain and keyval in (Gdk.KEY_g, Gdk.KEY_s, Gdk.KEY_r):
                self._cancel_g_prefix()
                if keyval == Gdk.KEY_g:
                    self._jump_to_view_edge(last=False)
                elif keyval == Gdk.KEY_s:
                    self.group_selection_into_set()
                else:
                    self.remove_selection_from_set()
                return True
            self._cancel_g_prefix()

        if (
            alt
            and not ctrl
            and not super_mod
            and keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left)
        ):
            self.nav_back()
            return True
        if (
            alt
            and not ctrl
            and not super_mod
            and keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right)
        ):
            self.nav_forward()
            return True
        # Undo soft-delete — Ctrl+Z (standard Omarchy / Linux app undo)
        if keyval in (Gdk.KEY_z, Gdk.KEY_Z) and ctrl and not super_mod and not in_search:
            self.undo_delete()
            return True
        # Soft-delete selection (not while typing search)
        if (
            keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_BackSpace)
            and not in_search
            and not in_sidebar
            and not ctrl
            and not super_mod
        ):
            self.delete_selected()
            return True
        if keyval == Gdk.KEY_Escape:
            self._cancel_g_prefix()
            return self._handle_escape()
        if keyval == Gdk.KEY_slash and not in_search and not ctrl:
            self.focus_search()
            return True
        if keyval in (Gdk.KEY_f,) and ctrl:
            self.focus_search()
            return True
        if in_search:
            if keyval == Gdk.KEY_Return:
                self.focus_grid()
                return True
            return False

        if not ctrl and not alt and not super_mod:
            if keyval in (Gdk.KEY_v, Gdk.KEY_V):
                self.open_location_picker()
                return True
            if keyval in (Gdk.KEY_i, Gdk.KEY_I):
                if self.is_viewer_open() and self._viewer_mode == "video":
                    self._mark_viewer("in")
                else:
                    self.open_special_view("uncategorized")
                return True

        if keyval == Gdk.KEY_question and not ctrl and not alt and not super_mod:
            self.open_keyboard_help()
            return True

        # Sidebar: ↑↓ move list; ←→ / Enter collapse-expand smart folders
        if in_sidebar:
            if keyval in (Gdk.KEY_a, Gdk.KEY_A) and not ctrl and not alt and not super_mod:
                # Auto-tags for the selected library folder
                row = self.folder_list.get_selected_row()
                fid = getattr(row, "folder_id", None) if row else None
                if fid or self.current_folder_id:
                    self.edit_folder_auto_tags(fid or self.current_folder_id)
                    return True
            if keyval in (Gdk.KEY_e, Gdk.KEY_E) and not ctrl and not alt and not super_mod:
                row = self.folder_list.get_selected_row()
                sid = getattr(row, "smart_folder_id", None) if row else None
                if sid:
                    self.open_smart_folder_editor(sid)
                    return True
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            if (
                shift
                and not ctrl
                and not alt
                and not super_mod
                and keyval
                in (
                    Gdk.KEY_Up,
                    Gdk.KEY_Down,
                    Gdk.KEY_KP_Up,
                    Gdk.KEY_KP_Down,
                )
            ):
                row = self.folder_list.get_selected_row()
                sid = getattr(row, "smart_folder_id", None) if row else None
                if sid:
                    delta = (
                        -1
                        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up)
                        else 1
                    )
                    self._nudge_smart_folder(sid, delta)
                    return True
            if (
                keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_BackSpace)
                and not ctrl
                and not alt
                and not super_mod
            ):
                row = self.folder_list.get_selected_row()
                sid = getattr(row, "smart_folder_id", None) if row else None
                if sid:
                    self.confirm_delete_smart_folder(sid)
                    return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                if self._sidebar_toggle_selected():
                    return True
                # Non-collapsible row: keep selection, don't copy path
                return True
            if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
                if self._sidebar_expand_selected():
                    return True
                # Already open / no children → move into the image grid
                self.focus_grid()
                return True
            if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
                if self._sidebar_collapse_selected():
                    return True
                return False
            if keyval in (
                Gdk.KEY_Up,
                Gdk.KEY_Down,
                Gdk.KEY_KP_Up,
                Gdk.KEY_KP_Down,
            ):
                return False  # ListBox handles vertical movement

        # Multi-select handoff (grid); in video viewer Space = play/pause
        if not in_sidebar and keyval == Gdk.KEY_space:
            if self.is_viewer_open() and self._viewer_mode == "video":
                self.viewer_toggle_play()
                return True
            self.toggle_mark_selected()
            return True
        if (
            not in_sidebar
            and not in_search
            and not ctrl
            and not alt
            and not super_mod
            and keyval in (Gdk.KEY_p, Gdk.KEY_P)
        ):
            if self.is_viewer_open() and self._viewer_mode == "video":
                self.save_viewer_frame()
                return True
            if self.selected_item is not None and self.selected_item.is_video:
                self._toast("Play the video first")
                return True
        if (
            not in_sidebar
            and not in_search
            and not ctrl
            and not alt
            and not super_mod
            and self.is_viewer_open()
            and self._viewer_mode == "video"
        ):
            if keyval in (Gdk.KEY_o, Gdk.KEY_O):
                self._mark_viewer("out")
                return True
            if keyval in (Gdk.KEY_x, Gdk.KEY_X):
                self._export_viewer_trim()
                return True
        # Ctrl+A — select all assets in the current view
        if (
            not in_sidebar
            and not in_search
            and keyval in (Gdk.KEY_a, Gdk.KEY_A)
            and ctrl
            and not super_mod
        ):
            self.select_all_visible()
            return True
        # Ratings 1–5 / 0 clear (not while typing search; not in sidebar)
        # Ignore Alt so Alt+letter never collides with these (Hyprland, mnemonics).
        if not in_sidebar and not in_search and not ctrl and not alt and not super_mod:
            rating_keys = {
                Gdk.KEY_0: 0,
                Gdk.KEY_1: 1,
                Gdk.KEY_2: 2,
                Gdk.KEY_3: 3,
                Gdk.KEY_4: 4,
                Gdk.KEY_5: 5,
                Gdk.KEY_KP_0: 0,
                Gdk.KEY_KP_1: 1,
                Gdk.KEY_KP_2: 2,
                Gdk.KEY_KP_3: 3,
                Gdk.KEY_KP_4: 4,
                Gdk.KEY_KP_5: 5,
            }
            if keyval in rating_keys:
                self.set_rating(rating_keys[keyval])
                return True
            if keyval in (Gdk.KEY_t, Gdk.KEY_T):
                self.edit_tags_dialog()
                return True
            if keyval in (Gdk.KEY_f, Gdk.KEY_F):
                self.edit_folders_dialog()
                return True
            if keyval in (Gdk.KEY_m, Gdk.KEY_M):
                self.open_view_type_filter()
                return True
            if keyval in (Gdk.KEY_x, Gdk.KEY_X):
                self.open_crop_dialog()
                return True
            if keyval == Gdk.KEY_F2:
                self.open_rename_dialog()
                return True
            if keyval == Gdk.KEY_N:
                self.open_rename_dialog()
                return True
            if keyval == Gdk.KEY_n:
                self.edit_notes_dialog()
                return True
        # y = Eagle id(s) — paste-safe for agent CLIs (not rewritten as image)
        # Shift+Y / c = path(s)
        # Ctrl+Y = file:// URI list
        if keyval in (Gdk.KEY_y, Gdk.KEY_Y) and ctrl:
            self.copy_marked_paths(as_file_uris=True)
            return True
        if keyval == Gdk.KEY_Y and not alt and not ctrl and not super_mod:
            self.copy_selected_path()
            return True
        if keyval == Gdk.KEY_y and not alt and not ctrl and not super_mod:
            self.copy_selected_ids()
            return True
        if keyval in (Gdk.KEY_c, Gdk.KEY_C) and not alt and not ctrl and not super_mod:
            self.copy_selected_path()
            return True
        if keyval in (Gdk.KEY_s, Gdk.KEY_S) and not ctrl and not alt and not super_mod:
            self.stage_marked()
            return True
        # e = Files; Shift+E = add to clip editor; Ctrl+Shift+E = new project.
        # Match g/G: use keyval, not SHIFT_MASK — GTK often reports KEY_E with
        # shift already applied and the modifier bit cleared.
        if (
            keyval in (Gdk.KEY_e, Gdk.KEY_E)
            and not alt
            and not super_mod
            and not in_sidebar
        ):
            shift = keyval == Gdk.KEY_E or bool(state & Gdk.ModifierType.SHIFT_MASK)
            if shift and ctrl:
                self.open_selected_in_clip_editor(new_project=True)
                return True
            if shift and not ctrl:
                self.open_selected_in_clip_editor(new_project=False)
                return True
            if not shift and not ctrl:
                self.reveal_selected_in_files()
                return True
        # In viewer: +/- toggle fit/actual; grid: thumb size
        if keyval in (
            Gdk.KEY_plus,
            Gdk.KEY_equal,  # unshifted = on many keyboards
            Gdk.KEY_KP_Add,
        ):
            if self.is_viewer_open():
                self.viewer_toggle_zoom(larger=True)
            else:
                self.adjust_thumb_size(+1)
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            if self.is_viewer_open():
                self.viewer_toggle_zoom(larger=False)
            else:
                self.adjust_thumb_size(-1)
            return True
        # Enter on image grid: open larger (sidebar Enter is handled above)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self.is_viewer_open():
                self.close_inline_viewer()
            else:
                self.open_selected()
            return True
        if keyval in (Gdk.KEY_o, Gdk.KEY_O) and not alt and not ctrl and not super_mod:
            if self.is_viewer_open():
                self.close_inline_viewer()
            else:
                self.open_selected()
            return True
        if keyval in (Gdk.KEY_r, Gdk.KEY_R) and not alt and not ctrl and not super_mod:
            self.reload_library()
            return True
        # Sidebar focus was f (Eagle-style); now b — f is folders/categories
        if keyval in (Gdk.KEY_b, Gdk.KEY_B) and not ctrl and not alt and not super_mod:
            self.focus_folders()
            return True
        # No hotkey for "All items" — only click the sidebar row.
        if keyval in (Gdk.KEY_d, Gdk.KEY_D) and not alt and not ctrl and not super_mod:
            self.toggle_descendants()
            return True
        if (
            keyval == Gdk.KEY_g
            and ctrl
            and not alt
            and not super_mod
            and not in_sidebar
        ):
            self.open_focused_set()
            return True
        if (
            keyval == Gdk.KEY_g
            and not alt
            and not ctrl
            and not super_mod
            and not in_sidebar
        ):
            self._start_g_prefix()
            return True
        if (
            keyval == Gdk.KEY_G
            and not alt
            and not ctrl
            and not super_mod
            and not in_sidebar
        ):
            self._jump_to_view_edge(last=True)
            return True

        # Inline viewer: left/right step images in current view
        if self.is_viewer_open() and not in_search and not in_sidebar:
            if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right) or (
                keyval in (Gdk.KEY_l, Gdk.KEY_L) and not ctrl
            ):
                self.viewer_navigate(+1)
                return True
            if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left) or (
                keyval in (Gdk.KEY_h, Gdk.KEY_H) and not ctrl
            ):
                self.viewer_navigate(-1)
                return True
            if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up, Gdk.KEY_Down, Gdk.KEY_KP_Down):
                # Ignore vertical in viewer (or map to prev/next)
                if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
                    self.viewer_navigate(+1)
                else:
                    self.viewer_navigate(-1)
                return True

        # Grid movement is intentionally scoped to the asset grid. Sidebar,
        # inspector, and filter controls keep their native/dedicated commands.
        if not self._grid_has_focus:
            return False

        # Grid movement (reading order is left→right, top→bottom):
        #   Left/Right / h/l  → previous / next image
        #   Up/Down / k/j     → image above / below (exactly one row)
        #   Shift+arrows      → range select from anchor through focus
        #   Ctrl+arrows       → move focus only (keep multi-selection); then Space to add
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        # Note: ctrl already computed above for other bindings
        if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right) or (
            keyval in (Gdk.KEY_l, Gdk.KEY_L) and not ctrl
        ):
            self.move_selection(1, extend=shift, keep_selection=ctrl and not shift)
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left) or (
            keyval in (Gdk.KEY_h, Gdk.KEY_H) and not ctrl
        ):
            self.move_selection(-1, extend=shift, keep_selection=ctrl and not shift)
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down) or (
            keyval in (Gdk.KEY_j, Gdk.KEY_J) and not ctrl
        ):
            self.move_selection(
                self._cols, extend=shift, keep_selection=ctrl and not shift
            )
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up) or (
            keyval in (Gdk.KEY_k, Gdk.KEY_K) and not ctrl
        ):
            self.move_selection(
                -self._cols, extend=shift, keep_selection=ctrl and not shift
            )
            return True

        return False

    def _hook_surface_resize(self, *_a) -> None:
        if getattr(self, "_surf_size_hooked", False):
            return
        surf = self.get_surface()
        if surf is None:
            return
        self._surf_size_hooked = True
        surf.connect("notify::width", lambda *_: self._schedule_column_sync())
        surf.connect("notify::height", lambda *_: self._schedule_column_sync())
        surf.connect("notify::scale-factor", lambda *_: self._schedule_column_sync())

    def _schedule_column_sync(self) -> None:
        if self._cols_sync_timeout_id:
            GLib.source_remove(self._cols_sync_timeout_id)
            self._cols_sync_timeout_id = 0

        def fire() -> bool:
            self._cols_sync_timeout_id = 0
            self._sync_columns()
            return False

        self._cols_sync_timeout_id = GLib.timeout_add(80, fire)

    def _grid_layout_width(self) -> int:
        """Allocated width the GridView can actually fill (not default-width)."""
        for getter in (
            lambda: self.grid_scroll.get_width(),
            lambda: self.grid.get_width(),
        ):
            try:
                w = int(getter())
            except Exception:  # noqa: BLE001
                w = 0
            if w > 32:
                return w
        surf = self.get_surface()
        if surf is not None:
            try:
                sw = int(surf.get_width())
            except Exception:  # noqa: BLE001
                sw = 0
            if sw > 32:
                side = 0
                if getattr(self, "_left_sidebar_open", True):
                    side += int(getattr(self, "_left_pane_w", 280))
                if getattr(self, "_right_sidebar_open", True):
                    side += int(getattr(self, "_insp_pane_w", 260))
                return max(160, sw - side - 32)
        return 0

    def _sync_columns(self) -> bool:
        """
        Force GridView min_columns == max_columns to a width-fitting count.

        Only called on resize (debounced), not on every arrow key — changing
        min/max columns reflows the grid and feels like lag.
        """
        width = self._grid_layout_width()
        if width <= 32:
            return False

        cell_w = _cell_w(self._thumb_size) + 16
        cols = max(1, min(16, int(width) // int(cell_w)))
        if (
            cols == self._cols
            and self.grid.get_min_columns() == cols
            and self.grid.get_max_columns() == cols
        ):
            return False
        self._cols = cols
        self.grid.set_min_columns(cols)
        self.grid.set_max_columns(cols)
        try:
            self.grid.queue_resize()
        except Exception:  # noqa: BLE001
            pass
        return False


class EagleBrowseApp(Adw.Application):
    def __init__(self, library_path: Path):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.library_path = library_path
        self.library = EagleLibrary(library_path)
        self.connect("activate", self._on_activate)
        self.connect("shutdown", self._on_shutdown)

    def _on_shutdown(self, *_args) -> None:
        mark_gui_stopped()
        try:
            _thumb_executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            try:
                _thumb_executor.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def _on_activate(self, app: Adw.Application) -> None:
        win = self.props.active_window
        if win is not None:
            win.present()
            return
        # No active window: either first launch, or a zombie process after a
        # compositor kill left us running. Show chrome immediately; the
        # library scan runs in a background thread (see _start_library_load).
        window = EagleBrowseWindow(app, self.library)
        window.present()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browse an Eagle.cool library and copy image paths")
    parser.add_argument(
        "library",
        nargs="?",
        default=os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)),
        help=f"Path to .library directory (default: {DEFAULT_LIBRARY})",
    )
    args = parser.parse_args(argv)

    Adw.init()
    from theme import apply_omarchy_theme  # noqa: PLC0415

    apply_omarchy_theme()
    app = EagleBrowseApp(Path(args.library).expanduser())
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main())
