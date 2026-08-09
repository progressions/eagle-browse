#!/usr/bin/env python3
"""Eagle Browse — keyboard-first read-only Eagle.cool library picker for Omarchy."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk  # noqa: E402

from filters import ViewFilters, item_matches_view_filters  # noqa: E402
from import_media import DEFAULT_INBOX  # noqa: E402
from library import DEFAULT_LIBRARY, EagleLibrary, Item, SmartFolder  # noqa: E402

APP_ID = "cool.eagle.Browse"
THUMB_SIZE_DEFAULT = 160  # square cell edge (Eagle-style uniform tiles)
THUMB_SIZE_MIN = 72
THUMB_SIZE_MAX = 360
THUMB_SIZE_STEP = 24
PAGE_SOFT_CAP = 500  # smaller page = snappier folder switches
SEARCH_DEBOUNCE_MS = 150
# Staging handoff (copy out of library — never writes into .library)
DEFAULT_STAGE_DIR = Path.home() / "Dropbox/ISAAC/GENNIE/Eunbi/outbox"


def _cell_w(thumb: int) -> int:
    return thumb + 12


def _cell_h(thumb: int) -> int:
    return thumb + 36

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
        self._items: list[Item] = []
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
        self._inbox_poll_id = 0
        self._inbox_importing = False
        self._known_inbox_names: set[str] = set()
        self._scope_text = "all"
        self._thumb_size = THUMB_SIZE_DEFAULT
        # While tag/folder/type pickers are open, ignore main-window hotkeys
        self._picker_blocking = False
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        self._build_ui()
        self._install_keybinds()
        self._populate_sidebar()
        self.refresh_items()
        self._start_inbox_watcher()

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

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_vexpand(True)
        root.append(body)

        # Left sidebar: folders (collapsible pane)
        LEFT_W = 280
        self.left_sidebar = Gtk.ScrolledWindow()
        self.left_sidebar.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.left_sidebar.set_size_request(LEFT_W, -1)
        self.left_sidebar.set_hexpand(False)
        self.left_sidebar.add_css_class("sidebar")
        self.left_sidebar.add_css_class("left-sidebar")
        css_left = Gtk.CssProvider()
        css_left.load_from_data(
            f"""
            scrolledwindow.left-sidebar {{
                min-width: {LEFT_W}px;
                max-width: {LEFT_W}px;
            }}
            """.encode()
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_left,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self.folder_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.folder_list.add_css_class("navigation-sidebar")
        self.folder_list.connect("row-selected", self._on_sidebar_selected)
        self.left_sidebar.set_child(self.folder_list)
        body.append(self.left_sidebar)

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

        # Left: grid + status
        grid_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        grid_col.set_hexpand(True)
        grid_col.set_vexpand(True)
        mid.append(grid_col)

        self.grid_scroll = Gtk.ScrolledWindow()
        self.grid_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.grid_scroll.set_vexpand(True)
        self.grid_scroll.set_hexpand(True)
        grid_col.append(self.grid_scroll)

        self.store = Gio.ListStore(item_type=ItemObject)
        self.selection = Gtk.SingleSelection(model=self.store)
        self.selection.set_can_unselect(True)
        self.selection.set_autoselect(False)
        self.selection.connect("notify::selected-item", self._on_grid_selection)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)

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

        # Status bar
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        status.add_css_class("toolbar")
        status.set_margin_start(10)
        status.set_margin_end(10)
        status.set_margin_top(4)
        status.set_margin_bottom(6)

        self.status_left = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.status_left.add_css_class("dim-label")
        status.append(self.status_left)

        self.status_path = Gtk.Label(xalign=1, ellipsize=3)
        self.status_path.add_css_class("dim-label")
        self.status_path.set_selectable(True)
        status.append(self.status_path)
        grid_col.append(status)

        hints = Gtk.Label(
            label=(
                "1-5 rate (or inspector) · Shift+arrows select · Space add · "
                "t tags · f folders · Y copy · q quit"
            ),
            xalign=0,
        )
        hints.add_css_class("dim-label")
        hints.set_margin_start(10)
        hints.set_margin_end(10)
        hints.set_margin_bottom(8)
        hints.set_wrap(True)
        grid_col.append(hints)

        # Right: inspector (thumbnail, rating, tags, folders)
        self.inspector_sidebar = self._build_inspector()
        mid.append(self.inspector_sidebar)

    def _build_inspector(self) -> Gtk.Widget:
        """Right sidebar: preview + rating + tags + folders for selection."""
        INSPECTOR_WIDTH = 240
        outer = Gtk.ScrolledWindow()
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.set_size_request(INSPECTOR_WIDTH, -1)
        outer.set_hexpand(False)
        outer.set_vexpand(True)
        outer.add_css_class("inspector-sidebar")
        # CSS min/max width so the pane stays fixed while the grid expands
        css = Gtk.CssProvider()
        css.load_from_data(
            f"""
            scrolledwindow.inspector-sidebar {{
                min-width: {INSPECTOR_WIDTH}px;
                max-width: {INSPECTOR_WIDTH}px;
            }}
            """.encode()
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_hexpand(False)
        outer.set_child(box)

        self.insp_title = Gtk.Label(xalign=0, wrap=True)
        self.insp_title.add_css_class("title-4")
        box.append(self.insp_title)

        self.insp_subtitle = Gtk.Label(xalign=0, wrap=True)
        self.insp_subtitle.add_css_class("dim-label")
        self.insp_subtitle.add_css_class("caption")
        box.append(self.insp_subtitle)

        self.insp_picture = Gtk.Picture()
        self.insp_picture.set_size_request(200, 200)
        self.insp_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.insp_picture.set_can_shrink(True)
        frame = Gtk.Box()
        frame.add_css_class("card")
        frame.set_halign(Gtk.Align.CENTER)
        frame.append(self.insp_picture)
        box.append(frame)

        # Rating
        rate_lbl = Gtk.Label(label="Rating", xalign=0)
        rate_lbl.add_css_class("heading")
        box.append(rate_lbl)
        self.insp_stars_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.insp_star_buttons: list[Gtk.Button] = []
        for n in range(1, 6):
            btn = Gtk.Button(label="☆")
            btn.add_css_class("flat")
            btn.set_tooltip_text(f"Set {n} star(s)")
            btn.connect("clicked", lambda _b, s=n: self.set_rating(s))
            self.insp_star_buttons.append(btn)
            self.insp_stars_box.append(btn)
        clear_r = Gtk.Button(label="Clear")
        clear_r.add_css_class("flat")
        clear_r.connect("clicked", lambda *_: self.set_rating(0))
        self.insp_stars_box.append(clear_r)
        box.append(self.insp_stars_box)
        self.insp_rating_note = Gtk.Label(xalign=0, wrap=True)
        self.insp_rating_note.add_css_class("dim-label")
        self.insp_rating_note.add_css_class("caption")
        box.append(self.insp_rating_note)

        # Tags
        tags_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tags_lbl = Gtk.Label(label="Tags", xalign=0, hexpand=True)
        tags_lbl.add_css_class("heading")
        tags_head.append(tags_lbl)
        edit_t = Gtk.Button(label="Edit")
        edit_t.add_css_class("flat")
        edit_t.connect("clicked", lambda *_: self.edit_tags_dialog())
        tags_head.append(edit_t)
        box.append(tags_head)
        self.insp_tags = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self.insp_tags)

        # Folders
        folders_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        folders_lbl = Gtk.Label(label="Folders", xalign=0, hexpand=True)
        folders_lbl.add_css_class("heading")
        folders_head.append(folders_lbl)
        edit_f = Gtk.Button(label="Edit")
        edit_f.add_css_class("flat")
        edit_f.connect("clicked", lambda *_: self.edit_folders_dialog())
        folders_head.append(edit_f)
        box.append(folders_head)
        self.insp_folders = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self.insp_folders)

        # Path
        path_lbl = Gtk.Label(label="Path", xalign=0)
        path_lbl.add_css_class("heading")
        box.append(path_lbl)
        self.insp_path = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.insp_path.add_css_class("caption")
        self.insp_path.add_css_class("dim-label")
        box.append(self.insp_path)

        self._inspector_empty()
        return outer

    def _inspector_empty(self) -> None:
        self.insp_title.set_text("No selection")
        self.insp_subtitle.set_text("Select an asset in the grid")
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
        lbl = Gtk.Label(label=text, xalign=0, wrap=True)
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
            bits = [it.ext_lower or "?"]
            if it.width and it.height:
                bits.append(f"{it.width}×{it.height}")
            if it.duration:
                bits.append(f"{it.duration:.1f}s")
            self.insp_subtitle.set_text(" · ".join(bits))
            self.insp_path.set_text(str(it.path))
            # Thumbnail
            path = _thumb_path_for(it)
            if path:
                try:
                    tex = Gdk.Texture.new_from_filename(path)
                    self.insp_picture.set_paintable(tex)
                except GLib.Error:
                    self.insp_picture.set_paintable(None)
            else:
                self.insp_picture.set_paintable(None)
        else:
            self.insp_title.set_text(f"{n} assets selected")
            self.insp_subtitle.set_text("Showing values shared by all")
            self.insp_path.set_text("")
            # Preview first selected
            path = _thumb_path_for(items[0])
            if path:
                try:
                    self.insp_picture.set_paintable(Gdk.Texture.new_from_filename(path))
                except GLib.Error:
                    self.insp_picture.set_paintable(None)
            else:
                self.insp_picture.set_paintable(None)

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

    def _append_smart_tree(self, nodes: list[SmartFolder], depth: int = 0) -> None:
        for sf in nodes:
            has_children = bool(sf.children)
            expanded = sf.id in self._smart_expanded
            self.folder_list.append(
                self._make_nav_row(
                    label=sf.name,
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
                label="Untagged", kind="special", special_view="untagged"
            )
        )
        self.folder_list.append(
            self._make_nav_row(
                label="Uncategorized", kind="special", special_view="uncategorized"
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

    def _on_sidebar_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            return
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

        # Same place as already shown (e.g. returning from grid via ←) — don't re-query
        if (
            new_smart == self.current_smart_folder_id
            and new_folder == self.current_folder_id
            and new_special == self._special_view
        ):
            return

        self.current_smart_folder_id = new_smart
        self.current_folder_id = new_folder
        self._special_view = new_special
        self.refresh_items()

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

    def _set_grid_focus(self, focused: bool) -> None:
        self._grid_has_focus = focused
        if focused:
            # Restore blue highlight on last focused asset
            n = self.store.get_n_items()
            if n == 0:
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

    def _on_grid_has_focus_notify(self, *_args) -> None:
        self._set_grid_focus(self.grid.has_focus())

    def refresh_items(self) -> None:
        """Kick off a background query so the UI never freezes on smart folders."""
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

        def work() -> None:
            if special in ("untagged", "uncategorized"):
                items = self.library.query(
                    search=search,
                    include_deleted=False,
                )
                if special == "untagged":
                    items = [it for it in items if not it.tags]
                else:
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
            total = len(items)
            truncated = total > PAGE_SOFT_CAP
            page = items[:PAGE_SOFT_CAP] if truncated else items

            def apply() -> bool:
                if gen != self._query_gen:
                    return False  # stale
                self._items = page
                self.store.remove_all()
                for item in page:
                    self.store.append(ItemObject(item))
                if page:
                    self._sel_anchor = 0
                    self._last_focus_idx = 0
                    self._marked = {page[0].id}
                    self.selected_item = page[0]
                    # Only show blue highlight if grid currently has focus
                    if self._grid_has_focus:
                        self.selection.set_selected(0)
                    else:
                        try:
                            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
                        except Exception:
                            self.selection.set_selected(0)
                else:
                    self.selected_item = None
                    self._marked.clear()
                    self._sel_anchor = 0
                    try:
                        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
                    except Exception:
                        pass
                note = f" · showing first {PAGE_SOFT_CAP} of {total}" if truncated else ""
                self._scope_text = f"{total} items · {scope}{note}"
                self._refresh_status()
                self._update_path_label()
                self._rebuild_filter_chips()
                return False

            GLib.idle_add(apply)

        threading.Thread(target=work, name="eagle-query", daemon=True).start()

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
        click = Gtk.GestureClick()
        click.set_button(1)

        def on_click(
            gesture: Gtk.GestureClick,
            _n: int,
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

        click.connect("pressed", on_click)
        card.add_controller(click)

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
        self.open_selected()

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
        """Selected items in current grid order, then any ids not on this page."""
        seen: set[str] = set()
        out: list[Item] = []
        for it in self._items:
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

    def _apply_range_selection(self, a: int, b: int) -> None:
        lo, hi = (a, b) if a <= b else (b, a)
        lo = max(0, lo)
        hi = min(len(self._items) - 1, hi)
        self._marked = {self._items[i].id for i in range(lo, hi + 1)} if hi >= lo else set()

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
        item = self._items[idx]
        self._last_focus_idx = idx
        self.selection.set_selected(idx)
        self.grid.scroll_to(
            idx, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None
        )

        if shift:
            self._apply_range_selection(self._sel_anchor, idx)
        elif ctrl:
            if item.id in self._marked:
                self._marked.discard(item.id)
                # Keep at least the focused item selected if emptied
                if not self._marked:
                    self._marked.add(item.id)
            else:
                self._marked.add(item.id)
            # Anchor stays put for further Shift ranges (Explorer-style)
        else:
            self._marked = {item.id}
            self._sel_anchor = idx

        self.selected_item = item
        self._rebind_grid_keep_selection()
        self._refresh_status()
        self._update_path_label()
        if from_click:
            self.grid.grab_focus()

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
        """Rebuild ListStore so mark overlays rebind; keep cursor position."""
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
            self.grid.scroll_to(
                int(idx), Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None
            )

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

    # ── Actions ───────────────────────────────────────────────────────

    def toggle_mark_selected(self) -> None:
        """Space: toggle focused item in multi-selection (like Ctrl+click)."""
        idx = self.selection.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION:
            return
        self._select_index(int(idx), ctrl=True, shift=False)

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
            self._rebind_grid_keep_selection()
            self._update_path_label()
            self.update_inspector()
            self._toast(("+ " if turn_on else "− ") + tag)

        def on_close() -> None:
            self.grid.grab_focus()

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
            self._rebind_grid_keep_selection()
            self._update_path_label()
            self.update_inspector()
            self._toast(("+ " if turn_on else "− ") + path_label)

        def on_close() -> None:
            self.grid.grab_focus()

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
            on_close=lambda: self.grid.grab_focus(),
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
            on_close=lambda: self.grid.grab_focus(),
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
            on_close=lambda: self.grid.grab_focus(),
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
        win = Gtk.Window(
            title=title,
            transient_for=self,
            modal=False,  # click outside closes via is-active
            default_width=360,
        )
        self._picker_blocking = True
        closing = {"v": False}
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
            self._picker_blocking = False
            win.destroy()
            self.grid.grab_focus()

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

        def on_is_active(*_a) -> None:
            if win.get_realized() and not win.is_active() and not closing["v"]:
                GLib.idle_add(lambda: (close_win() or False))

        win.connect("notify::is-active", on_is_active)

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

    # ── Inbox import / watcher ────────────────────────────────────────

    def _start_inbox_watcher(self) -> None:
        """Poll inbox every few seconds for new media (Dropbox-friendly)."""
        if self._inbox_poll_id:
            return
        # name -> last seen size (stable size across polls ⇒ ready to import)
        self._inbox_sizes: dict[str, int] = {}
        self._inbox_stable: set[str] = set()

        def tick() -> bool:
            self._poll_inbox()
            return True

        self._inbox_poll_id = GLib.timeout_add_seconds(3, tick)
        # First poll soon after open so files already in inbox get picked up
        GLib.timeout_add_seconds(1, lambda: self._poll_inbox() or False)

    def _poll_inbox(self) -> None:
        if self._inbox_importing:
            return
        from import_media import list_inbox_files

        try:
            files = list_inbox_files(self._inbox_dir)
        except OSError:
            return

        ready: set[str] = set()
        current: dict[str, int] = {}
        for p in files:
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if sz <= 0:
                continue
            current[p.name] = sz
            prev = self._inbox_sizes.get(p.name)
            if prev is not None and prev == sz:
                # Seen twice with same size → settled
                ready.add(p.name)
            # else first sighting or still growing — wait another poll

        self._inbox_sizes = current
        # Forget stability for files that disappeared
        self._inbox_stable &= set(current)

        # Only import files not already processed this session
        to_import = ready - self._known_inbox_names
        if to_import:
            # Mark as attempted so we don't re-queue every poll; cleared if still present after fail
            self._known_inbox_names |= to_import
            self.import_inbox(manual=False, only_names=set(to_import))

    def import_inbox(
        self,
        *,
        manual: bool = True,
        only_names: set[str] | None = None,
    ) -> None:
        """Import media from the Dropbox Eunbi inbox into the Eagle library."""
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
            from import_media import import_file, list_inbox_files
            from write import WriteError, write_session

            files = list_inbox_files(inbox)
            if only_names is not None:
                files = [p for p in files if p.name in only_names]
            results = []
            try:
                if files:
                    with write_session(self.library.root):
                        for f in files:
                            try:
                                if f.stat().st_size == 0:
                                    continue
                            except OSError:
                                continue
                            results.append(
                                import_file(
                                    self.library.root,
                                    f,
                                    move_source=True,
                                    hold_lock=True,
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

            ok = sum(1 for r in results if r.ok)
            fail_n = sum(1 for r in results if not r.ok and not r.skipped)
            err_msgs = [r.error for r in results if r.error and not r.ok][:3]

            def done() -> bool:
                self._inbox_importing = False
                if ok:
                    try:
                        self.library.load()
                    except Exception as exc:  # noqa: BLE001
                        self._toast(f"Imported {ok} but reload failed: {exc}")
                        return False
                    self._populate_sidebar(select_current=True)
                    self.refresh_items()
                msg = f"Imported {ok} into library"
                if fail_n:
                    msg += f" · {fail_n} failed"
                    if err_msgs:
                        msg += f" ({err_msgs[0]})"
                if not results and manual:
                    msg = f"Inbox empty · {inbox}"
                if ok or manual or fail_n:
                    self._toast(msg)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, name="eagle-import", daemon=True).start()

    def open_selected(self) -> None:
        item = self.selected_item
        if not item:
            self._toast("Nothing selected")
            return
        path = str(item.path)

        # Video / audio → mpv (plays with keyboard controls)
        if item.is_video or item.is_audio:
            players = [
                [
                    "mpv",
                    "--force-window=yes",
                    "--keep-open=yes",  # don't quit at end; Esc/q to close
                    "--osc=yes",
                    path,
                ],
                ["xdg-open", path],
            ]
            for cmd in players:
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        # Images → imv with explicit close/zoom binds
        imv_cmd = [
            "imv",
            "-s",
            "full",  # fit image in window
            "-c",
            "bind q quit",
            "-c",
            "bind Q quit",
            "-c",
            "bind <Escape> quit",
            "-c",
            "bind <Ctrl+q> quit",
            "-c",
            "bind f fullscreen",
            "-c",
            "bind i zoom 1",
            "-c",
            "bind o zoom -1",
            "-c",
            "bind <plus> zoom 1",
            "-c",
            "bind <minus> zoom -1",
            "-c",
            "bind a zoom actual",
            "-c",
            "bind r reset",
            path,
        ]
        for cmd in (imv_cmd, ["xdg-open", path]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if cmd[0] == "imv":
                    self._toast("Viewer · q or Esc to close · +/- zoom · f fullscreen")
                return
            except FileNotFoundError:
                continue
        self._toast("Could not open image")

    def reload_library(self) -> None:
        try:
            self.library.load()
        except Exception as exc:  # noqa: BLE001
            self._toast(f"Reload failed: {exc}")
            return
        # Keep expand state; show current selection path
        self._populate_sidebar(select_current=True)
        self.refresh_items()
        n_smart = len(self.library.smart_folders_by_id)
        self._toast(
            f"Reloaded · {len(self.library.items)} items · {n_smart} smart folders"
        )

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

    def select_all_folder(self) -> None:
        row = self.folder_list.get_row_at_index(0)
        if row:
            self.folder_list.select_row(row)
            self.focus_grid()

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
        extend=True (Shift): select range from anchor to new focus.
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

        if keyval in (Gdk.KEY_q, Gdk.KEY_Q) and not in_search:
            self.close()
            return True
        if keyval == Gdk.KEY_Escape:
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

        # Multi-select handoff (grid)
        if not in_sidebar and keyval == Gdk.KEY_space:
            self.toggle_mark_selected()
            return True
        # Ratings 1–5 / 0 clear (not while typing search; not in sidebar)
        if not in_sidebar and not in_search and not ctrl:
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
        # Y = all marked paths (or focused if none marked)
        # Ctrl+Y = file:// URI list
        # y / c = single focused path
        if keyval in (Gdk.KEY_y, Gdk.KEY_Y) and ctrl:
            self.copy_marked_paths(as_file_uris=True)
            return True
        if keyval == Gdk.KEY_Y:
            self.copy_marked_paths(as_file_uris=False)
            return True
        if keyval in (Gdk.KEY_y, Gdk.KEY_c, Gdk.KEY_C):
            self.copy_selected_path()
            return True
        if keyval in (Gdk.KEY_s, Gdk.KEY_S) and not ctrl:
            self.stage_marked()
            return True
        if keyval in (Gdk.KEY_i, Gdk.KEY_I) and not ctrl and not in_sidebar:
            self.import_inbox(manual=True)
            return True
        if keyval in (Gdk.KEY_e, Gdk.KEY_E) and not ctrl and not in_sidebar:
            self.reveal_selected_in_files()
            return True
        # Thumbnail zoom (+ larger / - smaller); skip when typing in search
        if keyval in (
            Gdk.KEY_plus,
            Gdk.KEY_equal,  # unshifted = on many keyboards
            Gdk.KEY_KP_Add,
        ):
            self.adjust_thumb_size(+1)
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.adjust_thumb_size(-1)
            return True
        # Enter on image grid: open larger (sidebar Enter is handled above)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.open_selected()
            return True
        if keyval in (Gdk.KEY_o, Gdk.KEY_O):
            self.open_selected()
            return True
        if keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self.reload_library()
            return True
        # Sidebar focus was f (Eagle-style); now b — f is folders/categories
        if keyval in (Gdk.KEY_b, Gdk.KEY_B) and not ctrl:
            self.focus_folders()
            return True
        if keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self.select_all_folder()
            return True
        if keyval in (Gdk.KEY_d, Gdk.KEY_D):
            self.toggle_descendants()
            return True
        if keyval in (Gdk.KEY_g,):
            if self.store.get_n_items():
                self._select_index(
                    0, shift=bool(state & Gdk.ModifierType.SHIFT_MASK)
                )
            return True
        if keyval in (Gdk.KEY_G,):
            n = self.store.get_n_items()
            if n:
                self._select_index(
                    n - 1, shift=bool(state & Gdk.ModifierType.SHIFT_MASK)
                )
            return True

        # Grid movement (reading order is left→right, top→bottom):
        #   Left/Right / h/l  → previous / next image
        #   Up/Down / k/j     → image above / below (exactly one row)
        #   Shift+arrows      → extend multi-selection range
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
            width = max(400, self.get_width() - 300)

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

    def _on_activate(self, app: Adw.Application) -> None:
        win = self.props.active_window
        if win:
            win.present()
            return
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
