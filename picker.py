"""Keyboard-first multi-toggle picker (tags / folders) with autocomplete + recents."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

RECENT_PATH = Path.home() / ".config" / "eagle-browse" / "recent.json"
RECENT_MAX = 20


def load_recent(kind: str) -> list[str]:
    try:
        data = json.loads(RECENT_PATH.read_text(encoding="utf-8"))
        items = data.get(kind) or []
        if isinstance(items, list):
            return [str(x) for x in items if x]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return []


def push_recent(kind: str, value: str) -> None:
    if not value:
        return
    items = load_recent(kind)
    items = [value] + [x for x in items if x != value]
    items = items[:RECENT_MAX]
    RECENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = {}
        if RECENT_PATH.is_file():
            data = json.loads(RECENT_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
        data[kind] = items
        RECENT_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, TypeError):
        pass


class TogglePicker(Gtk.Window):
    """
    Filterable list where Enter toggles membership without closing.

    Keys:
      type     — filter list
      Up/Down  — move highlight
      Enter    — toggle highlighted row
      Esc      — close
    """

    def __init__(
        self,
        parent: Gtk.Window,
        *,
        title: str,
        subtitle: str,
        all_values: list[str],
        active: set[str],
        partial: set[str] | None = None,
        recent: list[str] | None = None,
        allow_create: bool = True,
        recent_kind: str = "tags",
        on_toggle: Callable[[str, bool], None],
        on_close: Callable[[], None] | None = None,
    ):
        super().__init__(
            title=title,
            transient_for=parent,
            modal=True,
            default_width=420,
            default_height=480,
        )
        self.set_destroy_with_parent(True)
        self._all = list(all_values)
        self._active = set(active)
        self._partial = set(partial or [])
        self._recent = list(recent or [])
        self._allow_create = allow_create
        self._recent_kind = recent_kind
        self._on_toggle = on_toggle
        self._on_close = on_close
        self._rows: list[tuple[str, str]] = []  # (value, kind) kind=recent|match|create|active

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.set_child(root)

        head = Gtk.Label(label=title, xalign=0)
        head.add_css_class("title-3")
        root.append(head)
        sub = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        root.append(sub)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Type to filter…")
        self.entry.set_hexpand(True)
        self.entry.connect("changed", lambda *_: self._rebuild_list())
        root.append(self.entry)

        hint = Gtk.Label(
            label="↑↓ navigate · Enter toggle · Esc close",
            xalign=0,
        )
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        root.append(hint)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(scroll)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", self._on_row_activated)
        scroll.set_child(self.list)

        # Capture keys on window
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key)
        self.add_controller(controller)

        self.connect("close-request", self._on_close_request)

        self._rebuild_list()
        GLib.idle_add(self.entry.grab_focus)

    def _on_close_request(self, *_args) -> bool:
        if self._on_close:
            self._on_close()
        return False

    def _status_prefix(self, value: str) -> str:
        if value in self._active:
            return "✓ "
        if value in self._partial:
            return "± "
        return "  "

    def _rebuild_list(self) -> None:
        while (child := self.list.get_first_child()) is not None:
            self.list.remove(child)
        self._rows = []

        q = (self.entry.get_text() or "").strip()
        q_lower = q.lower()

        def add_row(value: str, meta: str = "") -> None:
            if any(v == value for v, _ in self._rows):
                return
            kind = meta or "item"
            self._rows.append((value, kind))
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_start(10)
            box.set_margin_end(10)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            label = Gtk.Label(
                label=f"{self._status_prefix(value)}{value}",
                xalign=0,
                hexpand=True,
                ellipsize=3,
            )
            box.append(label)
            if meta == "recent":
                badge = Gtk.Label(label="recent")
                badge.add_css_class("dim-label")
                badge.add_css_class("caption")
                box.append(badge)
            elif meta == "create":
                badge = Gtk.Label(label="new")
                badge.add_css_class("accent")
                badge.add_css_class("caption")
                box.append(badge)
            elif meta == "active":
                badge = Gtk.Label(label="on item")
                badge.add_css_class("dim-label")
                badge.add_css_class("caption")
                box.append(badge)
            row.set_child(box)
            row._value = value  # type: ignore[attr-defined]
            self.list.append(row)

        if not q:
            # Active first (currently applied), then recents, then rest
            for v in sorted(self._active, key=str.lower):
                add_row(v, "active")
            for v in self._recent:
                if v in self._all or v in self._active:
                    add_row(v, "recent")
            for v in self._all:
                add_row(v)
        else:
            starts = [v for v in self._all if v.lower().startswith(q_lower)]
            contains = [
                v
                for v in self._all
                if q_lower in v.lower() and not v.lower().startswith(q_lower)
            ]
            # Prefer recents that match
            for v in self._recent:
                if q_lower in v.lower():
                    add_row(v, "recent")
            for v in sorted(starts, key=str.lower):
                add_row(v)
            for v in sorted(contains, key=str.lower):
                add_row(v)
            if self._allow_create:
                exact = any(v.lower() == q_lower for v in self._all) or q in self._active
                if not exact and q:
                    add_row(q, "create")

        if self._rows:
            row0 = self.list.get_row_at_index(0)
            if row0:
                self.list.select_row(row0)

    def _selected_value(self) -> str | None:
        row = self.list.get_selected_row()
        if row is None:
            return None
        return getattr(row, "_value", None)

    def _move_selection(self, delta: int) -> None:
        n = len(self._rows)
        if n == 0:
            return
        row = self.list.get_selected_row()
        idx = row.get_index() if row else 0
        new = max(0, min(n - 1, idx + delta))
        r = self.list.get_row_at_index(new)
        if r:
            self.list.select_row(r)
            r.grab_focus()
            # Keep entry focused for typing, but selection updates
            self.entry.grab_focus()
            # Move cursor to end so typing continues
            self.entry.set_position(-1)

    def _toggle_selected(self) -> None:
        value = self._selected_value()
        if not value:
            # If filter has text and create allowed, toggle create value
            q = (self.entry.get_text() or "").strip()
            if q and self._allow_create:
                value = q
            else:
                return
        currently_on = value in self._active and value not in self._partial
        # If partial or off → turn on; if fully on → turn off
        turn_on = not currently_on
        try:
            self._on_toggle(value, turn_on)
        except Exception:
            return
        if turn_on:
            self._active.add(value)
            self._partial.discard(value)
            push_recent(self._recent_kind, value)
            # Keep recent list in UI fresh
            self._recent = [value] + [x for x in self._recent if x != value]
        else:
            self._active.discard(value)
            self._partial.discard(value)
        # Rebuild to update checkmarks; preserve filter
        self._rebuild_list()
        # Re-select same value if present
        for i, (v, _) in enumerate(self._rows):
            if v == value:
                r = self.list.get_row_at_index(i)
                if r:
                    self.list.select_row(r)
                break
        self.entry.grab_focus()
        self.entry.set_position(-1)

    def _on_row_activated(self, _list: Gtk.ListBox, _row: Gtk.ListBoxRow) -> None:
        self._toggle_selected()

    def _on_key(
        self, _c: Gtk.EventControllerKey, keyval: int, _keycode: int, state: Gdk.ModifierType
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._toggle_selected()
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._move_selection(1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._move_selection(-1)
            return True
        # Let entry handle typing; Tab moves to list
        if keyval == Gdk.KEY_Tab:
            row = self.list.get_selected_row()
            if row:
                row.grab_focus()
            return True
        return False
