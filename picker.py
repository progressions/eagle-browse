"""Keyboard-first multi-toggle picker (tags / folders) with autocomplete + recents."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

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
        data: dict = {}
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
      type           — filter list (entry keeps full string)
      Up/Down        — move highlight
      Enter          — toggle include (✓)
      Shift+Enter    — toggle exclude (✗) when exclude mode enabled
      Right-click    — toggle exclude
      Esc            — close
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
        excluded: set[str] | None = None,
        recent: list[str] | None = None,
        allow_create: bool = True,
        allow_exclude: bool = False,
        recent_kind: str = "tags",
        on_toggle: Callable[[str, bool], None],
        on_exclude: Callable[[str, bool], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ):
        super().__init__(
            title=title,
            transient_for=parent,
            # Non-modal so clicks on the main window reach the parent (we close
            # on that click). Do NOT close on is-active — Hyprland
            # focus-follows-mouse would dismiss the picker on mere hover.
            modal=False,
            default_width=420,
            default_height=480,
        )
        self.set_destroy_with_parent(True)
        self._parent = parent
        self._all = list(all_values)
        self._active = set(active)
        self._partial = set(partial or [])
        self._excluded = set(excluded or [])
        self._recent = list(recent or [])
        self._allow_create = allow_create
        self._allow_exclude = allow_exclude and on_exclude is not None
        self._recent_kind = recent_kind
        self._on_toggle = on_toggle
        self._on_exclude = on_exclude
        self._on_close_cb = on_close
        self._rows: list[tuple[str, str]] = []  # (value, kind)
        self._rebuilding = False
        self._closing = False
        self._outside_click: Gtk.GestureClick | None = None

        # Tell parent to ignore global hotkeys while open
        if hasattr(parent, "_picker_blocking"):
            parent._picker_blocking = True  # type: ignore[attr-defined]

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
        self.entry.connect("changed", self._on_entry_changed)
        # Navigation keys while typing in the entry
        entry_keys = Gtk.EventControllerKey()
        entry_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        entry_keys.connect("key-pressed", self._on_key)
        self.entry.add_controller(entry_keys)
        root.append(self.entry)

        hint_bits = ["↑↓ navigate", "Enter include", "Esc close"]
        if self._allow_exclude:
            hint_bits.insert(2, "Shift+Enter / right-click exclude")
        hint = Gtk.Label(label=" · ".join(hint_bits), xalign=0)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        root.append(hint)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(scroll)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("boxed-list")
        self.list.set_can_focus(False)
        self.list.connect("row-activated", self._on_row_activated)
        scroll.set_child(self.list)

        # Window-level keys (when list somehow focused)
        win_keys = Gtk.EventControllerKey()
        win_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        win_keys.connect("key-pressed", self._on_key)
        self.add_controller(win_keys)

        self.connect("close-request", self._on_close_request)

        self._rebuild_list(preserve_entry_focus=False)
        GLib.idle_add(self._focus_entry)
        # Arm after a short delay so the open-click doesn't immediately dismiss
        GLib.timeout_add(200, self._install_outside_click)

    def _focus_entry(self) -> bool:
        self.entry.grab_focus()
        text = self.entry.get_text() or ""
        self.entry.set_position(len(text))
        return False

    def _install_outside_click(self) -> bool:
        """Close when the user clicks the main app (not on mouse-over alone)."""
        if self._closing or self._outside_click is not None:
            return False
        parent = self._parent
        click = Gtk.GestureClick()
        click.set_button(1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def on_press(_g: Gtk.GestureClick, _n: int, _x: float, _y: float) -> None:
            if self._closing:
                return
            # Click landed on parent (or its children) → dismiss picker
            self.close()

        click.connect("pressed", on_press)
        parent.add_controller(click)
        self._outside_click = click
        return False

    def _remove_outside_click(self) -> None:
        if self._outside_click is None:
            return
        try:
            self._parent.remove_controller(self._outside_click)
        except Exception:  # noqa: BLE001
            pass
        self._outside_click = None

    def _on_close_request(self, *_args) -> bool:
        self._closing = True
        self._remove_outside_click()
        if hasattr(self._parent, "_picker_blocking"):
            self._parent._picker_blocking = False  # type: ignore[attr-defined]
        if self._on_close_cb:
            self._on_close_cb()
        return False

    def _on_entry_changed(self, *_args) -> None:
        if self._rebuilding:
            return
        self._rebuild_list(preserve_entry_focus=True)

    def _status_prefix(self, value: str) -> str:
        if value in self._excluded:
            return "✗ "
        if value in self._active:
            return "✓ "
        if value in self._partial:
            return "± "
        return "  "

    def _rebuild_list(self, *, preserve_entry_focus: bool = True) -> None:
        # Preserve filter text + cursor — never let list rebuild eat typing
        cursor = self.entry.get_position()
        text = self.entry.get_text() or ""

        self._rebuilding = True
        try:
            while (child := self.list.get_first_child()) is not None:
                self.list.remove(child)
            self._rows = []

            q = text.strip()
            q_lower = q.lower()

            def add_row(value: str, meta: str = "") -> None:
                if any(v == value for v, _ in self._rows):
                    return
                self._rows.append((value, meta or "item"))
                row = Gtk.ListBoxRow()
                row.set_activatable(True)
                row.set_can_focus(False)
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
                if self._allow_exclude:
                    click = Gtk.GestureClick()
                    click.set_button(3)  # right-click

                    def on_right(_g, _n, _x, _y, val: str = value) -> None:
                        self._toggle_value(val, exclude=True)

                    click.connect("pressed", on_right)
                    row.add_controller(click)
                self.list.append(row)

            if not q:
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
        finally:
            self._rebuilding = False

        if preserve_entry_focus:
            # Restore text if something clobbered it, then focus + cursor
            if (self.entry.get_text() or "") != text:
                self.entry.set_text(text)
            self.entry.grab_focus()
            self.entry.set_position(cursor if cursor >= 0 else len(text))

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
        # Keep typing focus in the entry
        text = self.entry.get_text() or ""
        self.entry.grab_focus()
        self.entry.set_position(len(text))

    def _toggle_selected(self, *, exclude: bool = False) -> None:
        value = self._selected_value()
        if not value:
            q = (self.entry.get_text() or "").strip()
            if q and self._allow_create:
                value = q
            else:
                return
        self._toggle_value(value, exclude=exclude)

    def _toggle_value(self, value: str, *, exclude: bool = False) -> None:
        if exclude and self._allow_exclude and self._on_exclude:
            currently_ex = value in self._excluded
            turn_on = not currently_ex
            try:
                self._on_exclude(value, turn_on)
            except Exception:
                return
            if turn_on:
                self._excluded.add(value)
                self._active.discard(value)
                self._partial.discard(value)
                push_recent(self._recent_kind, value)
                self._recent = [value] + [x for x in self._recent if x != value]
            else:
                self._excluded.discard(value)
        else:
            currently_on = value in self._active and value not in self._partial
            turn_on = not currently_on
            try:
                self._on_toggle(value, turn_on)
            except Exception:
                return
            if turn_on:
                self._active.add(value)
                self._partial.discard(value)
                self._excluded.discard(value)
                push_recent(self._recent_kind, value)
                self._recent = [value] + [x for x in self._recent if x != value]
            else:
                self._active.discard(value)
                self._partial.discard(value)

        keep = self.entry.get_text() or ""
        self._rebuild_list(preserve_entry_focus=True)
        if (self.entry.get_text() or "") != keep:
            self.entry.set_text(keep)
        for i, (v, _) in enumerate(self._rows):
            if v == value:
                r = self.list.get_row_at_index(i)
                if r:
                    self.list.select_row(r)
                break
        self.entry.grab_focus()
        self.entry.set_position(len(self.entry.get_text() or ""))

    def _on_row_activated(self, _list: Gtk.ListBox, _row: Gtk.ListBoxRow) -> None:
        self._toggle_selected(exclude=False)

    def _on_key(
        self, _c: Gtk.EventControllerKey, keyval: int, _keycode: int, state: Gdk.ModifierType
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            exclude = bool(state & Gdk.ModifierType.SHIFT_MASK) and self._allow_exclude
            self._toggle_selected(exclude=exclude)
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._move_selection(1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._move_selection(-1)
            return True
        # All other keys → let the Entry handle them (typing)
        return False
