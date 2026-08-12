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
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk  # noqa: E402

from filters import ViewFilters, item_matches_view_filters  # noqa: E402
from import_media import DEFAULT_INBOX  # noqa: E402
from library import (  # noqa: E402
    DEFAULT_LIBRARY,
    EagleLibrary,
    Item,
    SmartFolder,
    eval_smart_conditions,
)
from write import INBOX_SIGNAL_FILENAME  # noqa: E402

APP_ID = "cool.eagle.Browse"
THUMB_SIZE_DEFAULT = 160  # square cell edge (Eagle-style uniform tiles)
THUMB_SIZE_MIN = 72
THUMB_SIZE_MAX = 360
THUMB_SIZE_STEP = 24
PAGE_CHUNK = 500  # first page + each infinite-scroll increment
SEARCH_DEBOUNCE_MS = 150
# Staging handoff (copy out of library — never writes into .library)
DEFAULT_STAGE_DIR = Path.home() / "Dropbox/ISAAC/GENNIE/Eunbi/outbox"
# UI sounds extracted from Eagle.app (app/sounds/*.wav)
_SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"
# Keep Gtk.MediaFile refs alive until playback finishes (otherwise GC stops audio).
_playing_media: list[Any] = []

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


def _cell_w(thumb: int) -> int:
    return thumb + 12


def _cell_h(thumb: int) -> int:
    return thumb + 36


def _sound_path(name: str) -> Path | None:
    """Resolve WAV path; prefer louder stereo notification variant."""
    if name == "notification":
        boosted = _SOUNDS_DIR / "notification_play.wav"
        if boosted.is_file():
            return boosted
    path = _SOUNDS_DIR / f"{name}.wav"
    return path if path.is_file() else None


def play_sound(name: str = "notification") -> None:
    """Play an Eagle UI sound (notification / remove / duplicate / error).

    Primary path: Gtk.MediaFile (same process / session audio as the app).
    Fallbacks: canberra-gtk-play, pw-play, paplay, mpv.
    Safe to call from the GTK main loop (e.g. GLib.idle_add).
    """
    path = _sound_path(name)
    if path is None:
        return
    path_s = str(path)

    # 1) In-process GStreamer via GTK (most reliable under uwsm / Wayland)
    try:
        media = Gtk.MediaFile.new_for_filename(path_s)
        media.set_loop(False)

        def _on_ended(m: Gtk.MediaStream, *_args: object) -> None:
            try:
                _playing_media.remove(m)
            except ValueError:
                pass

        media.connect("notify::ended", _on_ended)
        _playing_media.append(media)
        media.set_playing(True)
        # If stream errors immediately, fall through to external players
        err = media.get_error()
        if err is None:
            return
        try:
            _playing_media.remove(media)
        except ValueError:
            pass
    except Exception:  # noqa: BLE001
        pass

    # 2) External CLI players (inherit session env for PipeWire/Pulse)
    env = os.environ.copy()
    players: list[list[str]] = []
    if shutil.which("canberra-gtk-play"):
        players.append(["canberra-gtk-play", "-f", path_s])
    if shutil.which("pw-play"):
        players.append(["pw-play", path_s])
    if shutil.which("paplay"):
        players.append(["paplay", path_s])
    if shutil.which("mpv"):
        players.append(
            [
                "mpv",
                "--no-video",
                "--really-quiet",
                "--volume=150",
                "--audio-display=no",
                path_s,
            ]
        )
    if shutil.which("gst-play-1.0"):
        players.append(["gst-play-1.0", path_s])
    if shutil.which("ffplay"):
        players.append(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path_s]
        )

    for cmd in players:
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            return
        except OSError:
            continue

# Decode thumbs off the UI thread; textures are applied on the main loop.
_thumb_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="eagle-thumb")
# path → Gdk.Texture (main thread only) or "loading" sentinel handled via generation
_thumb_textures: dict[str, Gdk.Texture] = {}
_THUMB_CACHE_MAX = 400
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
    try:
        # Decode already scaled down — much faster than full-res then crop
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size * 2, size * 2)
        return _center_crop_square(pixbuf, size)
    except Exception:
        return None


def _thumb_cache_key(path: str, size: int) -> str:
    return f"{path}@{size}"


def _type_badge(item: Item) -> str:
    if item.is_video:
        return "▶"
    if item.is_audio:
        return "♪"
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
        super().__init__(application=app, title="Eagle Browse", default_width=1280, default_height=800)
        self.library = library
        self.current_folder_id: str | None = None
        self.current_smart_folder_id: str | None = None
        # Virtual views: None | "untagged" | "uncategorized"
        self._special_view: str | None = None
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
        # View filters: tags/folders/types include+exclude, dimensions, duration
        self._view_filters = ViewFilters()
        # Multi-selection (item ids) — Shift range / Ctrl add / Space toggle
        # Used for path copy, tags, folders, rate, stage
        self._marked: set[str] = set()
        self._sel_anchor: int = 0  # index for Shift-range selection
        self._stage_dir = Path(
            os.environ.get("EAGLE_STAGE_DIR", str(DEFAULT_STAGE_DIR))
        ).expanduser()
        self._inbox_dir = Path(
            os.environ.get("EAGLE_INBOX", str(DEFAULT_INBOX))
        ).expanduser()
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
        self._cols = 4
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
        # Ignore the row-selected that follows a viewer-open sidebar click
        self._sidebar_nav_lock = False
        # Soft-delete undo: each entry is a batch of item ids (newest last)
        self._delete_undo_stack: list[list[str]] = []
        # Inbox signal: watcher writes this after each new import
        self._inbox_signal_path = self.library.root / INBOX_SIGNAL_FILENAME
        self._inbox_signal_ts = 0.0
        self._pending_import_ids: set[str] = set()
        self._inbox_poll_id = 0
        self._inbox_monitor: Gio.FileMonitor | None = None
        self._inbox_images_mtime = 0.0
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)
        # Super+W (killactive) / window close must quit the process, not leave
        # a headless instance with a ThreadPoolExecutor alive.
        self.connect("close-request", self._on_window_close_request)

        self._build_ui()
        self._install_keybinds()
        self._populate_sidebar()
        self.refresh_items()
        self._start_inbox_watch()
        # Do NOT start an inbox poller here. Auto-import is eagle-inbox-watch
        # only (one machine). Opening the GUI on multiple hosts must not race
        # the watcher or each other over PICS/Eunbi. Manual import: key `i`.


    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._toast_overlay.set_child(root)

        header = Adw.HeaderBar()
        root.append(header)

        self.search = Gtk.SearchEntry(placeholder_text="Search name, tags, folders…  (/)")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search_changed)
        header.set_title_widget(self.search)

        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload library (r)")
        reload_btn.connect("clicked", lambda *_: self.reload_library())
        header.pack_end(reload_btn)

        self.crop_btn = Gtk.Button(
            icon_name="image-crop-symbolic",
            tooltip_text="Crop image (x) — ratios, drag overlay",
        )
        self.crop_btn.set_sensitive(False)
        self.crop_btn.connect("clicked", lambda *_: self.open_crop_dialog())
        header.pack_end(self.crop_btn)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_vexpand(True)
        root.append(body)

        # Left sidebar: folders (collapsible pane).
        # Pin width on a Box wrapper — GTK4 ScrolledWindow ignores max-width CSS,
        # so a bare scrolled window can grow or shrink and clip children on HiDPI.
        LEFT_W = 280
        self.left_sidebar = Gtk.ScrolledWindow()
        self.left_sidebar.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.left_sidebar.set_hexpand(True)
        self.left_sidebar.set_vexpand(True)
        self.left_sidebar.add_css_class("sidebar")
        self.left_sidebar.add_css_class("left-sidebar")

        self.folder_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.folder_list.add_css_class("navigation-sidebar")
        self.folder_list.connect("row-selected", self._on_sidebar_selected)
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
        body.append(main)

        # ── View filter bar ───────────────────────────────────────────
        filter_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        filter_bar.add_css_class("toolbar")
        filter_bar.set_margin_start(8)
        filter_bar.set_margin_end(8)
        filter_bar.set_margin_top(6)
        filter_bar.set_margin_bottom(2)

        filter_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.btn_toggle_left = Gtk.Button(label="◀ Nav")
        self.btn_toggle_left.add_css_class("flat")
        self.btn_toggle_left.set_tooltip_text("Collapse/expand left sidebar")
        self.btn_toggle_left.connect("clicked", lambda *_: self.toggle_left_sidebar())
        filter_btns.append(self.btn_toggle_left)

        for label, handler in (
            ("Tags", self.open_view_tag_filter),
            ("Folders", self.open_view_folder_filter),
            ("Type", self.open_view_type_filter),
            ("Size", self.open_dimension_filter),
            ("Duration", self.open_duration_filter),
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

        self.btn_toggle_right = Gtk.Button(label="Inspector ▶")
        self.btn_toggle_right.add_css_class("flat")
        self.btn_toggle_right.set_tooltip_text("Collapse/expand right inspector")
        self.btn_toggle_right.connect("clicked", lambda *_: self.toggle_right_sidebar())
        filter_btns.append(self.btn_toggle_right)
        filter_bar.append(filter_btns)

        self.filter_chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.filter_chips.set_hexpand(True)
        chip_scroll = Gtk.ScrolledWindow()
        chip_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        chip_scroll.set_child(self.filter_chips)
        chip_scroll.set_size_request(-1, 28)
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

        # Inline viewer (Eagle-style detail pane) — images + in-frame video
        self._viewer_open = False
        self._viewer_item_id: str | None = None
        self._viewer_mode: str = "image"  # "image" | "video"
        self._viewer_fit = True  # True = contain; False = actual size (scroll)
        viewer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        viewer.set_hexpand(True)
        viewer.set_vexpand(True)
        viewer.add_css_class("view")

        vbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbar.add_css_class("toolbar")
        vbar.set_margin_start(8)
        vbar.set_margin_end(8)
        vbar.set_margin_top(4)
        vbar.set_margin_bottom(4)
        self.viewer_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.viewer_title.add_css_class("heading")
        vbar.append(self.viewer_title)
        self.viewer_hint = Gtk.Label(label="←→ · Esc close · +/- zoom")
        self.viewer_hint.add_css_class("dim-label")
        self.viewer_hint.add_css_class("caption")
        vbar.append(self.viewer_hint)
        close_btn = Gtk.Button(label="Close")
        close_btn.add_css_class("flat")
        close_btn.connect("clicked", lambda *_: self.close_inline_viewer())
        vbar.append(close_btn)
        viewer.append(vbar)

        # Stack: still image (Picture) vs video (Gtk.Video with controls)
        self.viewer_body = Gtk.Stack()
        self.viewer_body.set_hexpand(True)
        self.viewer_body.set_vexpand(True)
        self.viewer_body.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.viewer_body.set_transition_duration(80)

        self.viewer_scroll = Gtk.ScrolledWindow()
        self.viewer_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.viewer_scroll.set_hexpand(True)
        self.viewer_scroll.set_vexpand(True)
        self.viewer_picture = Gtk.Picture()
        self.viewer_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.viewer_picture.set_can_shrink(True)
        self.viewer_picture.set_halign(Gtk.Align.CENTER)
        self.viewer_picture.set_valign(Gtk.Align.CENTER)
        self.viewer_picture.set_hexpand(True)
        self.viewer_picture.set_vexpand(True)
        self.viewer_scroll.set_child(self.viewer_picture)
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

        # Double-click still image to close (video uses its own controls)
        vclick = Gtk.GestureClick()
        vclick.set_button(1)

        def on_viewer_click(_g, n_press: int, _x, _y) -> None:
            if n_press == 2 and self._viewer_mode == "image":
                self.close_inline_viewer()

        vclick.connect("pressed", on_viewer_click)
        self.viewer_picture.add_controller(vclick)

        self.center_stack.add_named(viewer, "viewer")
        self.center_stack.set_visible_child_name("grid")

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
            max_columns=self._cols,
            min_columns=self._cols,
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
        # Debounced column sync only on real resize — never on every arrow key
        self.grid.connect("map", lambda *_: self._schedule_column_sync())
        self.connect("notify::default-width", lambda *_: self._schedule_column_sync())
        GLib.idle_add(self._sync_columns)

        # Only show blue selection highlight when the grid actually has focus
        grid_focus = Gtk.EventControllerFocus()
        grid_focus.connect("enter", lambda *_: self._set_grid_focus(True))
        grid_focus.connect("leave", lambda *_: self._set_grid_focus(False))
        self.grid.add_controller(grid_focus)
        # Also track clicks that focus the grid
        self.grid.connect("notify::has-focus", self._on_grid_has_focus_notify)

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
                "Enter open (image/video) · Esc close viewer · Space play/pause · "
                "t tags · f folders · Ctrl+A all · Del · Ctrl+Z · Super+W"
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

    def _build_inspector(self) -> Gtk.Widget:
        """Right sidebar: preview + rating + tags + folders for selection."""
        # Hard-pin the pane width on a Box (ScrolledWindow cannot take max-width).
        # Keep the preview well under the pane width so HiDPI / fractional-scale
        # clip on the window's right edge cannot hide Edit buttons.
        # Keep pane + preview narrow enough that a ~45px right-edge clip from
        # Hyprland fractional scaling (surface wider than client) still leaves
        # Edit buttons and the thumbnail fully visible.
        INSPECTOR_WIDTH = 260
        SIDE_PAD = 10
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
        css = Gtk.CssProvider()
        css.load_from_data(
            b"""
            scrolledwindow.inspector-sidebar button {
                min-width: 0;
                min-height: 0;
                padding: 2px 6px;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
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

        def _section_head(title: str, on_edit) -> Gtk.Box:
            """Heading + Edit stacked on the left.

            Edit is left-aligned on its own row so a right-edge window clip
            (Hyprland fractional scale) cannot hide the button.
            """
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            col.set_hexpand(True)
            lbl = Gtk.Label(label=title, xalign=0)
            lbl.add_css_class("heading")
            lbl.set_hexpand(True)
            lbl.set_ellipsize(3)
            col.append(lbl)
            edit = Gtk.Button(label="Edit")
            edit.add_css_class("flat")
            edit.set_hexpand(False)
            edit.set_halign(Gtk.Align.START)
            edit.connect("clicked", lambda *_: on_edit())
            col.append(edit)
            return col

        self.insp_title = _narrow_label(Gtk.Label(xalign=0), chars=22)
        self.insp_title.add_css_class("heading")
        box.append(self.insp_title)

        # Dimensions — large and readable (main reason you glance at the inspector)
        self.insp_dims = _narrow_label(Gtk.Label(xalign=0), chars=18)
        self.insp_dims.add_css_class("insp-dims")
        css_dims = Gtk.CssProvider()
        css_dims.load_from_data(
            b"""
            label.insp-dims {
                font-size: 18px;
                font-weight: 600;
                letter-spacing: 0.02em;
                margin-top: 2px;
                margin-bottom: 2px;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_dims,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        box.append(self.insp_dims)

        self.insp_subtitle = _narrow_label(Gtk.Label(xalign=0), chars=24)
        self.insp_subtitle.add_css_class("dim-label")
        self.insp_subtitle.add_css_class("caption")
        box.append(self.insp_subtitle)

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

        # Rating — compact stars only (Clear on next row)
        rate_lbl = Gtk.Label(label="Rating", xalign=0)
        rate_lbl.add_css_class("heading")
        box.append(rate_lbl)
        self.insp_stars_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.insp_stars_box.set_halign(Gtk.Align.START)
        self.insp_stars_box.set_hexpand(False)
        self.insp_star_buttons: list[Gtk.Button] = []
        for n in range(1, 6):
            btn = Gtk.Button(label="☆")
            btn.add_css_class("flat")
            btn.add_css_class("circular")
            btn.set_tooltip_text(f"Set {n} star(s)")
            btn.set_size_request(28, 28)
            btn.set_hexpand(False)
            btn.connect("clicked", lambda _b, s=n: self.set_rating(s))
            self.insp_star_buttons.append(btn)
            self.insp_stars_box.append(btn)
        box.append(self.insp_stars_box)
        clear_r = Gtk.Button(label="Clear")
        clear_r.add_css_class("flat")
        clear_r.set_halign(Gtk.Align.START)
        clear_r.connect("clicked", lambda *_: self.set_rating(0))
        box.append(clear_r)
        self.insp_rating_note = _narrow_label(Gtk.Label(xalign=0), chars=28)
        self.insp_rating_note.add_css_class("dim-label")
        self.insp_rating_note.add_css_class("caption")
        box.append(self.insp_rating_note)

        # Tags
        box.append(_section_head("Tags", self.edit_tags_dialog))
        self.insp_tags = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.insp_tags.set_hexpand(True)
        box.append(self.insp_tags)

        # Folders
        box.append(_section_head("Folders", self.edit_folders_dialog))
        self.insp_folders = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.insp_folders.set_hexpand(True)
        box.append(self.insp_folders)

        # Path
        path_lbl = Gtk.Label(label="Path", xalign=0)
        path_lbl.add_css_class("heading")
        box.append(path_lbl)
        self.insp_path = _narrow_label(
            Gtk.Label(xalign=0, selectable=True), chars=28
        )
        self.insp_path.add_css_class("caption")
        self.insp_path.add_css_class("dim-label")
        box.append(self.insp_path)

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
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, size, size, True
            )
            if pix is None:
                self.insp_picture.set_paintable(None)
                return
            self.insp_picture.set_paintable(Gdk.Texture.new_for_pixbuf(pix))
        except GLib.Error:
            try:
                self.insp_picture.set_paintable(Gdk.Texture.new_from_filename(path))
            except GLib.Error:
                self.insp_picture.set_paintable(None)

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
        self.insp_picture.set_paintable(None)
        self.insp_rating_note.set_text("")
        for b in self.insp_star_buttons:
            b.set_label("☆")
        self._clear_box(self.insp_tags)
        self._clear_box(self.insp_folders)
        self.insp_path.set_text("")

    @staticmethod
    def _clear_box(box: Gtk.Box) -> None:
        while (c := box.get_first_child()) is not None:
            box.remove(c)

    def _add_chip_label(self, box: Gtk.Box, text: str, *, dim: bool = False) -> None:
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        lbl.set_max_width_chars(16)
        lbl.set_ellipsize(3)
        lbl.set_hexpand(False)
        if dim:
            lbl.add_css_class("dim-label")
        lbl.add_css_class("caption")
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
                self.crop_btn.set_sensitive(bool(it.is_image and it.path.is_file()))
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

        # Rating commonality
        stars = {it.star for it in items}
        if len(stars) == 1:
            s = next(iter(stars))
            rating = s if s else 0
            self.insp_rating_note.set_text(
                f"{'★' * rating}{'☆' * (5 - rating)}" if rating else "Unrated (all)"
            )
            for i, b in enumerate(self.insp_star_buttons, start=1):
                b.set_label("★" if rating and i <= rating else "☆")
        else:
            self.insp_rating_note.set_text("Mixed ratings")
            for b in self.insp_star_buttons:
                b.set_label("☆")

        # Tags: intersection (common) and partial
        tag_sets = [set(it.tags) for it in items]
        common_tags = set.intersection(*tag_sets) if tag_sets else set()
        union_tags = set.union(*tag_sets) if tag_sets else set()
        partial_tags = union_tags - common_tags
        self._clear_box(self.insp_tags)
        if common_tags:
            for t in sorted(common_tags, key=str.lower):
                self._add_chip_label(self.insp_tags, f"✓ {t}")
        if partial_tags and n > 1:
            for t in sorted(partial_tags, key=str.lower):
                self._add_chip_label(self.insp_tags, f"± {t}", dim=True)
        if not common_tags and not partial_tags:
            self._add_chip_label(self.insp_tags, "(none)", dim=True)

        # Folders commonality
        folder_sets = [set(it.folders) for it in items]
        common_f = set.intersection(*folder_sets) if folder_sets else set()
        union_f = set.union(*folder_sets) if folder_sets else set()
        partial_f = union_f - common_f
        self._clear_box(self.insp_folders)
        if common_f:
            for fid in sorted(common_f):
                name = self.library.folder_paths.get(fid, fid)
                self._add_chip_label(self.insp_folders, f"✓ {name}")
        if partial_f and n > 1:
            for fid in sorted(partial_f):
                name = self.library.folder_paths.get(fid, fid)
                self._add_chip_label(self.insp_folders, f"± {name}", dim=True)
        if not common_f and not partial_f:
            self._add_chip_label(self.insp_folders, "(none)", dim=True)

    def _install_keybinds(self) -> None:
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key)
        self.add_controller(controller)

    def _shutdown_background(self) -> None:
        """Stop timers / workers so the process can actually exit."""
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
        self._shutdown_background()
        app = self.get_application()
        if app is not None:
            # Quit after this close finishes so D-Bus single-instance releases.
            GLib.idle_add(app.quit)
        return False  # allow destroy

    # ── Sidebar ───────────────────────────────────────────────────────

    def _make_header_row(
        self,
        title: str,
        *,
        collapsible: bool = False,
        section_id: str | None = None,
        expanded: bool = True,
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
        row.set_child(box)
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

        row.set_child(box)
        return row

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

    def _toggle_smart_expand(self, smart_id: str) -> None:
        if not smart_id:
            return
        if smart_id in self._smart_expanded:
            self._smart_expanded.discard(smart_id)
            # Collapse descendants too so re-expand starts clean
            to_drop = {
                sid
                for sid in self._smart_expanded
                if smart_id in self._smart_ancestors(sid) or sid == smart_id
            }
            # Also drop any expanded node whose ancestor chain includes smart_id
            for sid in list(self._smart_expanded):
                if smart_id in self._smart_ancestors(sid):
                    to_drop.add(sid)
            self._smart_expanded -= to_drop
            self._smart_expanded.discard(smart_id)
        else:
            self._smart_expanded.add(smart_id)
        self._repopulate_sidebar_keep_selection()

    def _repopulate_sidebar_keep_selection(self) -> None:
        # Keep current selection visible if it's nested
        self._ensure_smart_expanded_path(self.current_smart_folder_id)
        if self.current_folder_id:
            self._folders_section_expanded = True
        self._populate_sidebar(select_current=True)

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
        """Sidebar label for Untagged / Uncategorized, e.g. 'Untagged (42)'."""
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
        """Patch Untagged / Uncategorized sidebar label without full rebuild."""
        self._special_counts[view] = count
        base = "Untagged" if view == "untagged" else "Uncategorized"
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
        """Recount Untagged / Uncategorized in the background and patch labels."""

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
                label=self._special_label("uncategorized", "Uncategorized"),
                kind="special",
                special_view="uncategorized",
            )
        )

        # Smart folders first — primary navigation; top levels collapsed by default
        if self.library.smart_folders:
            self.folder_list.append(self._make_header_row("Smart folders"))
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
        """While the inline viewer is open, a sidebar click must close it and switch views.

        ``row-selected`` does not fire when the clicked row is already selected,
        which is the usual case (same smart folder / Uncategorized you opened from).
        """
        if n_press != 1 or not self.is_viewer_open():
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
            new_smart == self.current_smart_folder_id
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
        # Reset selection when changing scope, or after closing a preview from the sidebar
        self.refresh_items(reset_selection=not same_place or was_viewing)

    def _restore_sidebar_selection(self) -> None:
        target_smart = self.current_smart_folder_id
        target_folder = self.current_folder_id
        target_special = self._special_view
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
            self.current_smart_folder_id = sf.parent_id
            self.current_folder_id = None
            # Ensure parent visible; collapse is optional
            self._repopulate_sidebar_keep_selection()
            self.refresh_items()
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
        if self._special_view == "untagged":
            return "Untagged"
        if self._special_view == "uncategorized":
            return "Uncategorized"
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
        self.btn_toggle_left.set_label(
            "◀ Nav" if self._left_sidebar_open else "▶ Nav"
        )
        self._schedule_column_sync()

    def toggle_right_sidebar(self) -> None:
        self._right_sidebar_open = not self._right_sidebar_open
        self.inspector_sidebar.set_visible(self._right_sidebar_open)
        self.btn_toggle_right.set_label(
            "Inspector ▶" if self._right_sidebar_open else "Inspector ◀"
        )
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
            if n == 0:
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

    def refresh_items(self, *, reset_selection: bool = False) -> None:
        """
        Kick off a background query so the UI never freezes on smart folders.

        By default, multi-selection (_marked) is preserved across refresh so
        tagging/categorizing on Untagged/Uncategorized can remove items from
        the view without clearing the selection. Pass reset_selection=True
        when changing sidebar scope.
        """
        self._query_gen += 1
        gen = self._query_gen
        folder_id = self.current_folder_id
        smart_id = self.current_smart_folder_id
        special = self._special_view
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
        keep_scroll = 0.0 if reset_selection else self._grid_scroll_value()
        keep_loaded = 0 if reset_selection else len(self._items)
        if reset_selection:
            self._cancel_scroll_restore()

        def work() -> None:
            if special in ("untagged", "uncategorized"):
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

                if reset_selection or not keep_marks:
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
                if not reset_selection:
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
            # Eagle btime = added-to-library (our importer uses source birth/mtime).
            # Fall back to modificationTime when btime is missing.
            return int(it.btime or it.modification_time or 0)

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

        # Multi-select mark (top-left)
        mark = Gtk.Label(label="✓")
        mark.add_css_class("osd")
        mark.add_css_class("heading")
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

        # Type badge (video / audio / other)
        badge = Gtk.Label(xalign=1.0)
        badge.add_css_class("osd")
        badge.add_css_class("caption")
        badge.set_halign(Gtk.Align.END)
        badge.set_valign(Gtk.Align.END)
        badge.set_margin_end(4)
        badge.set_margin_bottom(2)
        tile.add_overlay(badge)

        # Fallback icon when no thumb decodes
        icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        icon.set_pixel_size(48)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        icon.set_visible(False)
        tile.add_overlay(icon)

        card.append(tile)

        label = Gtk.Label(xalign=0.5, ellipsize=3, max_width_chars=18)
        label.add_css_class("caption")
        card.append(label)

        list_item.set_child(card)
        list_item.picture = picture  # type: ignore[attr-defined]
        list_item.label = label  # type: ignore[attr-defined]
        list_item.badge = badge  # type: ignore[attr-defined]
        list_item.icon = icon  # type: ignore[attr-defined]
        list_item.mark = mark  # type: ignore[attr-defined]
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
        if marked:
            card.add_css_class("accent")
        else:
            card.remove_css_class("accent")

        if item.star and 1 <= item.star <= 5:
            stars_lbl.set_text("★" * item.star)
            stars_lbl.set_visible(True)
        else:
            stars_lbl.set_text("")
            stars_lbl.set_visible(False)

        badge_text = _type_badge(item)
        badge.set_text(badge_text)
        badge.set_visible(bool(badge_text))

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
        # Instant if cached
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

        def work() -> None:
            pixbuf = _decode_square_pixbuf(path, size)

            def apply() -> bool:
                if getattr(list_item, "_thumb_gen", None) != gen:
                    return False
                if pixbuf is None:
                    return False
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                # Cap cache
                if len(_thumb_textures) >= _THUMB_CACHE_MAX:
                    for _ in range(40):
                        try:
                            _thumb_textures.pop(next(iter(_thumb_textures)))
                        except StopIteration:
                            break
                _thumb_textures[cache_key] = texture
                picture.set_paintable(texture)
                picture.set_visible(True)
                icon.set_visible(False)
                return False

            GLib.idle_add(apply)

        _thumb_executor.submit(work)

    def _on_grid_selection(self, selection: Gtk.SingleSelection, _pspec) -> None:
        obj = selection.get_selected_item()
        self.selected_item = obj.item if obj else None
        self._update_path_label()
        self.update_inspector()

    def _on_grid_activate(self, _grid: Gtk.GridView, _position: int) -> None:
        # Do not open here. Card GestureClick handles double-click (n_press==2).
        # Calling open_selected from both paths opened two imv/mpv windows.
        return

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
        """Update focus + multi-selection (replace / Ctrl-toggle / Shift-path)."""
        n = len(self._items)
        if n == 0 or idx < 0 or idx >= n:
            return
        item = self._items[idx]
        self._last_focus_idx = idx
        self.selection.set_selected(idx)
        self.grid.scroll_to(
            idx, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None
        )

        if shift:
            # Path-style multi-select: keep existing marks and add only the
            # newly focused cell (and the shift-anchor). Does NOT fill a
            # rectangle or linear range — Shift+Right then Shift+Down yields
            # an L of 3, not a square of 4.
            if 0 <= self._sel_anchor < n:
                self._marked.add(self._items[self._sel_anchor].id)
            self._marked.add(item.id)
            # Anchor stays at the start of the shift gesture (set on non-shift)
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
            if mark is None or card is None:
                continue
            marked = item.id in self._marked
            mark.set_visible(marked)
            if marked:
                card.add_css_class("accent")
            else:
                card.remove_css_class("accent")

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
            self._marked.clear()
            self.refresh_items()
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
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if cmd[0] == "nautilus":
                    self._toast("Opened in Files · drag into the upload dialog if needed")
                return
            except FileNotFoundError:
                continue
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
        self._rebind_grid_keep_selection()
        self._update_path_label()
        self.update_inspector()
        if star:
            msg = f"Rated {'★' * star} · {ok} item(s)"
        else:
            msg = f"Cleared rating · {ok} item(s)"
        if errors:
            msg += f" · {len(errors)} failed"
        self._toast(msg)

    def open_crop_dialog(self) -> None:
        """Open the interactive crop editor for the focused image."""
        from crop import open_crop_dialog

        if self._picker_blocking:
            return
        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return
        if len(items) != 1:
            self._toast("Crop one image at a time")
            return
        item = items[0]
        if not item.is_image:
            self._toast("Crop works on images only")
            return

        self._picker_blocking = True

        def on_done(mode: str, it: Item) -> None:
            if mode == "new":
                # Fresh item: no tags/folders — inject into in-memory library
                self.library.upsert_item(it)
                # Aim selection at the new item after re-query (if visible)
                self.selected_item = it
                self._marked = {it.id}
                self.refresh_items(reset_selection=False)
                self._toast(
                    f"Saved as new · {it.width}×{it.height} · untagged / uncategorized"
                )
                return

            # Overwrite original
            self._invalidate_thumb_cache_for(it)
            if it.id in self.library.items_by_id:
                self.library.items_by_id[it.id] = it
            self.library._invalidate_caches()  # noqa: SLF001
            self._rebind_grid_keep_selection()
            self._update_path_label()
            self.update_inspector()
            self._toast(f"Saved crop · {it.width}×{it.height}")

        def on_close() -> None:
            self._picker_blocking = False
            self.grid.grab_focus()
            self._restore_grid_scroll(scroll)

        scroll = self._grid_scroll_value()
        open_crop_dialog(
            self,
            item,
            library_root=self.library.root,
            on_done=on_done,
            on_close=on_close,
        )

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

    def edit_tags_dialog(self) -> None:
        """Keyboard tag picker: recent + autocomplete, Enter toggles, Esc closes."""
        from picker import TogglePicker, load_recent
        from write import WriteError

        items = self._effective_hand_off_items()
        if not items:
            self._toast("Nothing selected")
            return

        # Tag present on all items → active; on some → partial
        tag_sets = [set(it.tags) for it in items]
        active = set.intersection(*tag_sets) if tag_sets else set()
        union = set.union(*tag_sets) if tag_sets else set()
        partial = union - active
        all_tags = self.library.all_tags()
        # Ensure current tags appear even if rare
        for t in union:
            if t not in all_tags:
                all_tags.append(t)
        all_tags = sorted(set(all_tags), key=str.lower)
        recent = load_recent("tags")
        ids = [it.id for it in items]
        n = len(items)

        def on_toggle(tag: str, turn_on: bool) -> None:
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
            # Re-query so Untagged drops just-tagged items; keep multi-select
            self.refresh_items()
            # Sidebar badge: keep Untagged (N) fresh even when not on that view
            self._refresh_special_counts()
            self._toast(("+ " if turn_on else "− ") + tag)

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
        active = set(folder.tags)
        # Live set for toggles (written on each Enter)
        current = set(active)
        all_tags = self.library.all_tags()
        recent = load_recent("tags")

        def on_toggle(tag: str, turn_on: bool) -> None:
            if turn_on:
                current.add(tag)
            else:
                current.discard(tag)
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

        def on_toggle(path_label: str, turn_on: bool) -> None:
            fid = path_to_id.get(path_label)
            if not fid:
                # Try exact path match
                for k, v in id_to_path.items():
                    if v == path_label:
                        fid = k
                        break
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
            # Re-query so Uncategorized drops just-filed items; keep multi-select
            self.refresh_items()
            # Sidebar badge: keep Uncategorized (N) fresh even when not on that view
            self._refresh_special_counts()
            msg = ("+ " if turn_on else "− ") + path_label
            if turn_on:
                auto = self.library.auto_tags_for_folders([fid])
                if auto:
                    msg += " · auto-tags " + ", ".join(auto)
            self._toast(msg)

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

    def clear_view_filters(self) -> None:
        self._view_filters.clear()
        self.refresh_items()
        self._toast("View filters cleared")

    def open_view_tag_filter(self) -> None:
        from picker import TogglePicker, load_recent

        vf = self._view_filters
        all_tags = self.library.all_tags()
        recent = load_recent("filter_tags")

        def on_include(tag: str, turn_on: bool) -> None:
            if turn_on:
                vf.tags_include.add(tag)
                vf.tags_exclude.discard(tag)
            else:
                vf.tags_include.discard(tag)
            self.refresh_items()

        def on_exclude(tag: str, turn_on: bool) -> None:
            if turn_on:
                vf.tags_exclude.add(tag)
                vf.tags_include.discard(tag)
            else:
                vf.tags_exclude.discard(tag)
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
        self._picker_blocking = True
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

    def stage_marked(self) -> None:
        """Copy marked (or focused) files into the stage/outbox directory."""
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
                return
            if unknown:
                GLib.idle_add(lambda: self._ingest_item_ids(unknown) or False)

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
        if not ids:
            if ts:
                self._inbox_signal_ts = ts
            return
        if ts:
            self._inbox_signal_ts = ts
        self._ingest_item_ids(ids)

    def _retry_pending_imports(self) -> None:
        if not self._pending_import_ids:
            return
        pending = list(self._pending_import_ids)
        self._ingest_item_ids(pending)

    def _ingest_item_ids(self, item_ids: list[str]) -> None:
        new_items: list[Item] = []
        for iid in item_ids:
            if iid in self.library.items_by_id:
                self._pending_import_ids.discard(iid)
                continue
            item = self.library.load_item(iid)
            if item is None:
                self._pending_import_ids.add(iid)
                continue
            self._pending_import_ids.discard(iid)
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
        """Show newly ingested items without rebuilding the whole grid."""
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
                key=lambda it: it.btime or it.modification_time,
                reverse=True,
            )
            existing = {it.id for it in self._all_items}
            to_add = [it for it in newest_first if it.id not in existing]
            for i, it in enumerate(to_add):
                self._all_items.insert(i, it)
                self._items.insert(i, it)
                self.store.insert(i, ItemObject(it))
            if to_add and len(self._marked) <= 1:
                self.selected_item = to_add[0]
                self._marked = {to_add[0].id}
                self._sel_anchor = 0
                self._last_focus_idx = 0
                if self._grid_has_focus:
                    try:
                        self.selection.set_selected(0)
                    except Exception:
                        pass
            self._rebuild_scope_text()
            self._refresh_status()
            self._update_path_label()
        elif visible:
            self.refresh_items()
        self._refresh_special_counts()
        if toast and items:
            self._toast(f"{len(items)} new")

    # ── Inbox import (manual only) ────────────────────────────────────
    # Auto-import belongs exclusively to eagle-inbox-watch on one machine.
    # The GUI must not poll PICS/Eunbi — open browsers on multiple hosts
    # would race the watcher and each other (double library entries).

    def import_inbox(
        self,
        *,
        manual: bool = True,
        only_names: set[str] | None = None,
    ) -> None:
        """Import media from the Dropbox Eunbi inbox into the Eagle library.

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
            )
            from write import WriteError, write_session

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
                    play_sound("notification")
                    new_items: list[Item] = []
                    for r in results:
                        if (
                            getattr(r, "ok", False)
                            and getattr(r, "item_id", None)
                            and not getattr(r, "reused", False)
                        ):
                            item = self.library.load_item(r.item_id)
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
        self._picker_blocking = True

        win = Gtk.Window(
            title=f"Duplicate · {index} of {total}",
            transient_for=self,
            modal=True,
            default_width=720,
            default_height=480,
        )
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
                    try:
                        pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(p), 560, 560)
                        return Gdk.Texture.new_for_pixbuf(pb)
                    except Exception:  # noqa: BLE001
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

    def _stop_inline_video(self) -> None:
        """Pause and detach any in-frame video stream."""
        try:
            stream = self.viewer_video.get_media_stream()
            if stream is not None:
                stream.set_playing(False)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.viewer_video.set_file(None)
        except Exception:  # noqa: BLE001
            try:
                self.viewer_video.set_media_stream(None)
            except Exception:  # noqa: BLE001
                pass

    def open_inline_viewer(self, item: Item | None = None) -> None:
        """Show image or video in the center pane (between sidebars), Eagle-style."""
        item = item or self.selected_item
        if item is None:
            self._toast("Nothing selected")
            return
        # Capture grid offset before the stack unmaps it (that resets scroll).
        if not self.is_viewer_open():
            self._saved_grid_scroll = {
                "value": self._grid_scroll_value(),
                "loaded": len(self._items),
                "focus_id": item.id,
            }
        if item.is_video:
            self._open_inline_video(item)
            return
        if not item.is_image:
            # Audio / other: external player
            self._open_external_media(item)
            return
        path = item.path
        if not path.is_file():
            self._toast(f"Missing file: {path}")
            return

        # Leave any previous video stream before showing a still
        self._stop_inline_video()

        # Prefer full image; GdkPixbuf scales for display memory
        paintable = None
        try:
            # Cap decode size so huge assets don't OOM
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), 4096, 4096)
            paintable = Gdk.Texture.new_for_pixbuf(pb)
        except Exception:
            try:
                paintable = Gdk.Texture.new_from_filename(str(path))
            except Exception as exc:  # noqa: BLE001
                self._toast(f"Could not load image: {exc}")
                return

        self.viewer_picture.set_paintable(paintable)
        self.viewer_title.set_text(item.display_name)
        self.viewer_hint.set_text("←→ · Esc close · +/- zoom")
        self._viewer_item_id = item.id
        self._viewer_open = True
        self._viewer_mode = "image"
        self._viewer_fit = True
        self.viewer_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.viewer_picture.set_can_shrink(True)
        self.viewer_body.set_visible_child_name("image")
        self.center_stack.set_visible_child_name("viewer")
        self.viewer_picture.grab_focus()
        # Keep grid selection in sync for inspector
        self.selected_item = item
        self.update_inspector()
        self._update_path_label()

    def _open_inline_video(self, item: Item) -> None:
        """Play a video in the center pane (Gtk.Video / GStreamer)."""
        path = item.path
        if not path.is_file():
            self._toast(f"Missing file: {path}")
            return

        # Clear still image; load file into Gtk.Video
        self.viewer_picture.set_paintable(None)
        try:
            self.viewer_video.set_filename(str(path))
            self.viewer_video.set_autoplay(True)
        except Exception as exc:  # noqa: BLE001
            self._toast(f"Could not load video: {exc}")
            self._open_external_media(item)
            return

        self.viewer_title.set_text(item.display_name)
        self.viewer_hint.set_text("Space play/pause · ←→ next · Esc close")
        self._viewer_item_id = item.id
        self._viewer_open = True
        self._viewer_mode = "video"
        self.viewer_body.set_visible_child_name("video")
        self.center_stack.set_visible_child_name("viewer")
        self.viewer_video.grab_focus()
        self.selected_item = item
        self.update_inspector()
        self._update_path_label()

    def close_inline_viewer(self, *, restore_scroll: bool = True) -> bool:
        """Leave detail view; return True if a viewer was closed."""
        if not self.is_viewer_open():
            return False
        self._viewer_open = False
        self._viewer_item_id = None
        self._viewer_mode = "image"
        self._stop_inline_video()
        self.viewer_picture.set_paintable(None)
        self.center_stack.set_visible_child_name("grid")
        snap = self._saved_grid_scroll
        self._saved_grid_scroll = None
        self.grid.grab_focus()
        if restore_scroll and snap is not None:
            loaded = int(snap.get("loaded") or 0)
            if loaded > len(self._items):
                self._ensure_loaded(loaded)
            self._restore_grid_scroll(float(snap.get("value") or 0.0))
        else:
            self._cancel_scroll_restore()
        return True

    def viewer_toggle_play(self) -> None:
        """Space while video is open: play/pause."""
        if not self.is_viewer_open() or self._viewer_mode != "video":
            return
        stream = self.viewer_video.get_media_stream()
        if stream is None:
            return
        stream.set_playing(not stream.get_playing())

    def viewer_navigate(self, delta: int) -> None:
        """Prev/next image or video in current view while inline viewer is open."""
        if not self.is_viewer_open() or not self._items:
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
        # Prefer images and videos (skip audio / unknown)
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

    def viewer_toggle_zoom(self, *, larger: bool | None = None) -> None:
        """Toggle fit vs actual, or nudge fit mode with +/-. Images only."""
        if not self.is_viewer_open() or self._viewer_mode != "image":
            return
        if larger is None:
            self._viewer_fit = not self._viewer_fit
        else:
            self._viewer_fit = not larger  # + → actual-ish; - → fit
        if self._viewer_fit:
            self.viewer_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            self.viewer_picture.set_can_shrink(True)
            self._toast("Viewer · fit")
        else:
            self.viewer_picture.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
            # Prefer native pixels when smaller than pane; still allow scroll
            self.viewer_picture.set_can_shrink(False)
            self._toast("Viewer · actual size")

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
                try:
                    subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    if cmd[0] == "mpv":
                        kind = "Video" if item.is_video else "Audio"
                        self._toast(
                            f"{kind} · Space play/pause · ←→ seek · q/Esc close · f fullscreen"
                        )
                    return
                except FileNotFoundError:
                    continue
            self._toast("Could not open media (install mpv?)")
            return
        # Fallback for non-image stills we couldn't show inline
        for cmd in (["xdg-open", path], ["imv", path]):
            try:
                subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                return
            except FileNotFoundError:
                continue
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

    def focus_grid(self) -> None:
        self.grid.grab_focus()

    def toggle_descendants(self) -> None:
        self.include_descendants = not self.include_descendants
        state = "including subfolders" if self.include_descendants else "folder only"
        self._toast(state)
        self.refresh_items()

    def move_selection(
        self, delta: int, *, extend: bool = False, keep_selection: bool = False
    ) -> None:
        """
        Move focus by delta indices.
        extend=True (Shift): path multi-select — add only the cell you move to
        (keeps prior marks; L-shapes, no filled rectangles).
        keep_selection=True (Ctrl): move focus only; multi-selection unchanged.
        If multi-select is already active (>1), plain arrows also keep selection
        (only move focus) so checkboxes don't vanish when releasing Shift.
        """
        n = self.store.get_n_items()
        if n == 0:
            return
        idx = self.selection.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION:
            idx = 0
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

    def _focus_is_search(self, focus) -> bool:
        if focus is None:
            return False
        if focus is self.search or isinstance(focus, (Gtk.Entry, Gtk.SearchEntry)):
            return True
        if isinstance(focus, Gtk.Editable):
            return True
        try:
            return focus.is_ancestor(self.search)
        except TypeError:
            return False

    def _focus_is_sidebar(self, focus) -> bool:
        if focus is None:
            return False
        if focus is self.folder_list:
            return True
        try:
            return focus is self.folder_list or focus.is_ancestor(self.folder_list)
        except TypeError:
            return False

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

    def _on_key(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        # Modal tag/folder/type pickers own the keyboard — do not steal letters
        # (was eating s/o/f/i/b/… so filter text became "ie" from "Sofie")
        if self._picker_blocking:
            return False

        focus = self.get_focus()
        in_search = self._focus_is_search(focus)
        in_sidebar = self._focus_is_sidebar(focus)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        super_mod = bool(state & Gdk.ModifierType.SUPER_MASK)
        # Alt+letter must not fire single-letter hotkeys (mnemonics / OS binds).
        alt = bool(state & Gdk.ModifierType.ALT_MASK)

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
            if self.is_viewer_open():
                self.close_inline_viewer()
                return True
            if in_search:
                self.search.set_text("")
                self.focus_grid()
                return True
            if self.clear_marks():
                return True
            if self._view_filters.active():
                self.clear_view_filters()
                return True
            if self._filter_text:
                self.search.set_text("")
                return True
            return False
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

        # Sidebar: ↑↓ move list; ←→ / Enter collapse-expand smart folders
        if in_sidebar:
            if keyval in (Gdk.KEY_a, Gdk.KEY_A) and not ctrl and not alt and not super_mod:
                # Auto-tags for the selected library folder
                row = self.folder_list.get_selected_row()
                fid = getattr(row, "folder_id", None) if row else None
                if fid or self.current_folder_id:
                    self.edit_folder_auto_tags(fid or self.current_folder_id)
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
        if (
            keyval in (Gdk.KEY_i, Gdk.KEY_I)
            and not ctrl
            and not alt
            and not super_mod
            and not in_sidebar
        ):
            self.import_inbox(manual=True)
            return True
        if (
            keyval in (Gdk.KEY_e, Gdk.KEY_E)
            and not ctrl
            and not alt
            and not super_mod
            and not in_sidebar
        ):
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
        if keyval in (Gdk.KEY_g,) and not alt and not ctrl and not super_mod:
            if self.store.get_n_items():
                self._select_index(
                    0, shift=bool(state & Gdk.ModifierType.SHIFT_MASK)
                )
            return True
        if keyval in (Gdk.KEY_G,) and not alt and not ctrl and not super_mod:
            n = self.store.get_n_items()
            if n:
                self._select_index(
                    n - 1, shift=bool(state & Gdk.ModifierType.SHIFT_MASK)
                )
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

        # Grid movement (reading order is left→right, top→bottom):
        #   Left/Right / h/l  → previous / next image
        #   Up/Down / k/j     → image above / below (exactly one row)
        #   Shift+arrows      → path multi-select
        #   Ctrl+arrows       → move focus only (keep multi-selection); then Space to add
        #   Left on first column → focus sidebar
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
            idx = self.selection.get_selected()
            n = self.store.get_n_items()
            cols = max(1, self._cols)
            if (
                not shift
                and not ctrl
                and (n == 0 or idx == Gtk.INVALID_LIST_POSITION or int(idx) % cols == 0)
            ):
                # Leftmost cell in the row (or empty grid) → jump to sidebar
                self.focus_folders()
                return True
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

    def _schedule_column_sync(self) -> None:
        if self._cols_sync_timeout_id:
            GLib.source_remove(self._cols_sync_timeout_id)
            self._cols_sync_timeout_id = 0

        def fire() -> bool:
            self._cols_sync_timeout_id = 0
            self._sync_columns()
            return False

        self._cols_sync_timeout_id = GLib.timeout_add(120, fire)

    def _sync_columns(self) -> bool:
        """
        Force GridView min_columns == max_columns to a width-fitting count.

        Only called on resize (debounced), not on every arrow key — changing
        min/max columns reflows the grid and feels like lag.
        """
        width = self.grid_scroll.get_width()
        if width <= 1:
            width = self.grid.get_width()
        if width <= 1:
            width = max(400, self.get_width() - 200)

        cell_w = _cell_w(self._thumb_size) + 16
        cols = max(1, min(16, int(width) // int(cell_w)))
        if cols == self._cols and self.grid.get_min_columns() == cols:
            return False
        self._cols = cols
        self.grid.set_min_columns(cols)
        self.grid.set_max_columns(cols)
        return False


class EagleBrowseApp(Adw.Application):
    def __init__(self, library_path: Path):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.library_path = library_path
        self.library = EagleLibrary(library_path)
        self.connect("activate", self._on_activate)
        self.connect("shutdown", self._on_shutdown)

    def _on_shutdown(self, *_args) -> None:
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
        # compositor kill left us running. Create a fresh main window.
        try:
            self.library.load()
        except Exception as exc:  # noqa: BLE001
            dialog = Adw.MessageDialog(
                heading="Could not open Eagle library",
                body=str(exc),
            )
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.connect("response", lambda d, *_: d.close())
            # Need a transient parent; create a bare window
            bare = Adw.ApplicationWindow(application=app, title="Eagle Browse")
            bare.present()
            dialog.set_transient_for(bare)
            dialog.present()
            return

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
    app = EagleBrowseApp(Path(args.library).expanduser())
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main())
