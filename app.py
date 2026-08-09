#!/usr/bin/env python3
"""Eagle Browse — keyboard-first read-only Eagle.cool library picker for Omarchy."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk  # noqa: E402

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
        self.include_descendants = True
        self.selected_item: Item | None = None
        self._items: list[Item] = []
        self._filter_text = ""
        # Multi-select marks (item ids) — hand off via Y (paths) / s (stage)
        self._marked: set[str] = set()
        self._stage_dir = Path(
            os.environ.get("EAGLE_STAGE_DIR", str(DEFAULT_STAGE_DIR))
        ).expanduser()
        # Smart-folder ids that are expanded. Empty = all top levels collapsed.
        self._smart_expanded: set[str] = set()
        # Sidebar section headers (Smart folders / Folders)
        self._folders_section_expanded = False  # collapsible; start closed
        # Forced grid column count (min_columns == max_columns). Arrow-down
        # must step by exactly this many items or selection drifts diagonally.
        self._cols = 4
        self._query_gen = 0  # bump to cancel stale background queries
        self._search_timeout_id = 0
        self._cols_sync_timeout_id = 0
        self._scope_text = "all"
        self._thumb_size = THUMB_SIZE_DEFAULT
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        self._build_ui()
        self._install_keybinds()
        self._populate_sidebar()
        self.refresh_items()

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

        # Sidebar: folders
        side_scroll = Gtk.ScrolledWindow()
        side_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side_scroll.set_size_request(280, -1)
        side_scroll.add_css_class("sidebar")

        self.folder_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.folder_list.add_css_class("navigation-sidebar")
        self.folder_list.connect("row-selected", self._on_sidebar_selected)
        side_scroll.set_child(self.folder_list)
        body.append(side_scroll)

        # Main: grid + status
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main.set_hexpand(True)
        main.set_vexpand(True)
        body.append(main)

        self.grid_scroll = Gtk.ScrolledWindow()
        self.grid_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.grid_scroll.set_vexpand(True)
        self.grid_scroll.set_hexpand(True)
        main.append(self.grid_scroll)

        self.store = Gio.ListStore(item_type=ItemObject)
        self.selection = Gtk.SingleSelection(model=self.store)
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
        main.append(status)

        hints = Gtk.Label(
            label=(
                "Space mark · Y copy marked · s stage · e reveal in Files · y copy path · "
                "+/- size · Enter open · file dialogs: Ctrl+L then paste · q quit"
            ),
            xalign=0,
        )
        hints.add_css_class("dim-label")
        hints.set_margin_start(10)
        hints.set_margin_end(10)
        hints.set_margin_bottom(8)
        hints.set_wrap(True)
        main.append(hints)

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
        dim: bool = False,
        has_children: bool = False,
        expanded: bool = False,
    ) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.row_kind = kind  # type: ignore[attr-defined]
        row.folder_id = folder_id  # type: ignore[attr-defined]
        row.smart_folder_id = smart_folder_id  # type: ignore[attr-defined]
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
        if kind == "smart":
            new_smart = getattr(row, "smart_folder_id", None)
            new_folder = None
        elif kind == "folder":
            new_smart = None
            new_folder = getattr(row, "folder_id", None)
        else:  # all
            new_smart = None
            new_folder = None

        # Same place as already shown (e.g. returning from grid via ←) — don't re-query
        if (
            new_smart == self.current_smart_folder_id
            and new_folder == self.current_folder_id
        ):
            return

        self.current_smart_folder_id = new_smart
        self.current_folder_id = new_folder
        self.refresh_items()

    def _restore_sidebar_selection(self) -> None:
        target_smart = self.current_smart_folder_id
        target_folder = self.current_folder_id
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
                if not target_smart and not target_folder and kind == "all":
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

    def refresh_items(self) -> None:
        """Kick off a background query so the UI never freezes on smart folders."""
        self._query_gen += 1
        gen = self._query_gen
        folder_id = self.current_folder_id
        smart_id = self.current_smart_folder_id
        descendants = self.include_descendants
        search = self._filter_text
        scope = self._scope_label()
        self.status_left.set_text(f"Loading… · {scope}")

        def work() -> None:
            items = self.library.query(
                folder_id=folder_id,
                smart_folder_id=smart_id,
                include_descendants=descendants,
                search=search,
                include_deleted=False,
            )
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
                    self.selection.set_selected(0)
                else:
                    self.selected_item = None
                note = f" · showing first {PAGE_SOFT_CAP} of {total}" if truncated else ""
                self._scope_text = f"{total} items · {scope}{note}"
                self._refresh_status()
                self._update_path_label()
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
        list_item.card = card  # type: ignore[attr-defined]
        list_item.tile = tile  # type: ignore[attr-defined]

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
        card: Gtk.Box = list_item.card  # type: ignore[attr-defined]

        marked = item.id in self._marked
        mark.set_visible(marked)
        if marked:
            card.add_css_class("accent")
        else:
            card.remove_css_class("accent")

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

    def _on_grid_activate(self, _grid: Gtk.GridView, _position: int) -> None:
        self.open_selected()

    def _update_path_label(self) -> None:
        if self.selected_item:
            self.status_path.set_text(str(self.selected_item.path))
        else:
            self.status_path.set_text("")

    def _refresh_status(self) -> None:
        marks = len(self._marked)
        mark_bit = f" · ✓ {marks} marked" if marks else ""
        base = getattr(self, "_scope_text", "") or ""
        self.status_left.set_text(f"{base}{mark_bit}")

    def _marked_items(self) -> list[Item]:
        """Marked items in current grid order, then any marks not on this page."""
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
        """Marked set if any; otherwise the focused item alone."""
        marked = self._marked_items()
        if marked:
            return marked
        if self.selected_item is not None:
            return [self.selected_item]
        return []

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
        item = self.selected_item
        if not item:
            self._toast("Nothing selected")
            return
        if item.id in self._marked:
            self._marked.discard(item.id)
            self._toast(f"Unmarked · {item.display_name}")
        else:
            self._marked.add(item.id)
            self._toast(f"Marked · {item.display_name} · {len(self._marked)} total")
        self._rebind_grid_keep_selection()
        self._refresh_status()

    def clear_marks(self) -> bool:
        if not self._marked:
            return False
        n = len(self._marked)
        self._marked.clear()
        self._rebind_grid_keep_selection()
        self._refresh_status()
        self._toast(f"Cleared {n} marks")
        return True

    def copy_selected_path(self) -> None:
        item = self.selected_item
        if not item:
            self._toast("Nothing selected")
            return
        path = str(item.path)
        if not self._clipboard_set_text(path):
            return
        # Hint for GTK/Omarchy file pickers (website uploads, etc.)
        self._toast(f"Copied · Ctrl+L in file dialog, paste path, Enter")

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
        if len(items) == 1 and not self._marked:
            self._toast(f"Copied {kind} · {items[0].display_name}")
        else:
            self._toast(f"Copied {len(items)} {kind}")

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

    def move_selection(self, delta: int) -> None:
        n = self.store.get_n_items()
        if n == 0:
            return
        idx = self.selection.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION:
            idx = 0
        new = max(0, min(n - 1, int(idx) + delta))
        self.selection.set_selected(new)
        self.grid.scroll_to(new, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
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
        if keyval in (Gdk.KEY_f,):
            self.focus_folders()
            return True
        if keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self.select_all_folder()
            return True
        if keyval in (Gdk.KEY_d, Gdk.KEY_D):
            self.toggle_descendants()
            return True
        if keyval in (Gdk.KEY_g,):
            self.selection.set_selected(0)
            if self.store.get_n_items():
                self.grid.scroll_to(0, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
            return True
        if keyval in (Gdk.KEY_G,):
            n = self.store.get_n_items()
            if n:
                self.selection.set_selected(n - 1)
                self.grid.scroll_to(n - 1, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)
            return True

        # Grid movement (reading order is left→right, top→bottom):
        #   Left/Right / h/l  → previous / next image
        #   Up/Down / k/j     → image above / below (exactly one row)
        #   Left on first column → focus sidebar
        # Use cached self._cols — never re-layout the grid on every keypress.
        if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right, Gdk.KEY_l, Gdk.KEY_L):
            self.move_selection(1)
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left, Gdk.KEY_h, Gdk.KEY_H):
            idx = self.selection.get_selected()
            n = self.store.get_n_items()
            cols = max(1, self._cols)
            if n == 0 or idx == Gtk.INVALID_LIST_POSITION or int(idx) % cols == 0:
                # Leftmost cell in the row (or empty grid) → jump to sidebar
                self.focus_folders()
                return True
            self.move_selection(-1)
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down, Gdk.KEY_j, Gdk.KEY_J):
            self.move_selection(self._cols)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up, Gdk.KEY_k, Gdk.KEY_K):
            self.move_selection(-self._cols)
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
