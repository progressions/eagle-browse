"""Modal editor for creating and editing Eagle smart folders."""

from __future__ import annotations

import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from filters import (  # noqa: E402
    RATING_OP_EQ,
    RATING_OP_GTE,
    RATING_OP_LTE,
    RATING_OP_SYMBOLS,
)
from library import EagleLibrary  # noqa: E402
from picker import TogglePicker, load_recent  # noqa: E402
from smart_folder_rules import (  # noqa: E402
    GROUP_ALL,
    GROUP_ANY,
    GROUP_LABELS,
    GROUP_NONE,
    SET_ALL,
    SET_ANY,
    SET_LABELS,
    CategoriesRule,
    EditorGroup,
    EditorRule,
    EditorSpec,
    RatingRule,
    TagsRule,
    empty_spec,
    encode_conditions,
    rule_summary,
    spec_from_folder,
    summarize_conditions,
)
from write import (  # noqa: E402
    WriteError,
    create_smart_folder_node,
    update_smart_folder_node,
    write_session,
)


class SmartFolderEditor(Gtk.Window):
    """Create or edit a smart folder and write it to metadata.json."""

    def __init__(
        self,
        parent: Gtk.Window,
        library: EagleLibrary,
        *,
        folder_id: str | None = None,
        default_parent_id: str | None = None,
        on_saved: Callable[[str], None] | None = None,
        on_closed: Callable[[], None] | None = None,
    ):
        title = "Edit smart folder" if folder_id else "New smart folder"
        super().__init__(
            title=title,
            transient_for=parent,
            modal=True,
            default_width=580,
            default_height=700,
        )
        self.set_destroy_with_parent(True)
        app = parent.get_application() if hasattr(parent, "get_application") else None
        if app is not None:
            self.set_application(app)
        self._parent_win = parent
        self.library = library
        self._folder_id = folder_id
        self._on_saved = on_saved
        self._on_closed = on_closed
        self._picker_blocking = False
        self._closing = False
        self._count_gen = 0
        self._count_timeout = 0
        self._rule_labels: dict[tuple[int, int], Gtk.Label] = {}

        if folder_id:
            sf = library.smart_folders_by_id.get(folder_id)
            if sf is None:
                raise WriteError(f"Unknown smart folder: {folder_id}")
            self._spec = spec_from_folder(
                name=sf.name,
                parent_id=sf.parent_id,
                conditions=sf.conditions,
            )
        else:
            self._spec = empty_spec(parent_id=default_parent_id)

        if hasattr(parent, "_remember_dialog"):
            parent._remember_dialog(self)  # type: ignore[attr-defined]
        elif hasattr(parent, "_picker_blocking"):
            parent._picker_blocking = True  # type: ignore[attr-defined]

        self._build_ui()
        self.connect("close-request", self._on_close_request)
        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)
        self._sync_save_sensitive()
        self._schedule_count()

    # ── layout ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(16)
        root.set_margin_bottom(16)
        root.set_margin_start(16)
        root.set_margin_end(16)
        self.set_child(root)

        title = Gtk.Label(
            label="Edit smart folder" if self._folder_id else "New smart folder",
            xalign=0,
        )
        title.add_css_class("title-3")
        root.append(title)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_lbl = Gtk.Label(label="Name", xalign=0)
        name_lbl.set_size_request(72, -1)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        self.name_entry.set_placeholder_text("Smart folder name")
        self.name_entry.set_text(self._spec.name)
        self.name_entry.connect("changed", lambda *_: self._sync_save_sensitive())
        name_row.append(name_lbl)
        name_row.append(self.name_entry)
        root.append(name_row)

        parent_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        parent_lbl = Gtk.Label(label="Parent", xalign=0)
        parent_lbl.set_size_request(72, -1)
        self._parent_ids: list[str | None] = []
        parent_labels: list[str] = []
        forbidden = self._forbidden_parent_ids()
        self._parent_ids.append(None)
        parent_labels.append("(root)")
        selected_idx = 0
        for sf, depth in self.library.flatten_smart_folders():
            if sf.id in forbidden:
                continue
            if sf.id == self._spec.parent_id:
                selected_idx = len(self._parent_ids)
            self._parent_ids.append(sf.id)
            parent_labels.append(("  " * depth) + sf.name)
        store = Gtk.StringList.new(parent_labels)
        self.parent_drop = Gtk.DropDown(model=store)
        self.parent_drop.set_hexpand(True)
        self.parent_drop.set_selected(selected_idx)
        parent_row.append(parent_lbl)
        parent_row.append(self.parent_drop)
        root.append(parent_row)

        add_group = Gtk.Button(label="+ Add group")
        add_group.set_halign(Gtk.Align.START)
        add_group.connect("clicked", lambda *_: self._add_group())
        root.append(add_group)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(scroll)
        self.groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scroll.set_child(self.groups_box)

        self.inherited_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        root.append(self.inherited_box)

        self.other_groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        root.append(self.other_groups_box)

        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.count_lbl = Gtk.Label(label="Matches …", xalign=0, hexpand=True)
        self.count_lbl.add_css_class("dim-label")
        foot.append(self.count_lbl)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self._save())
        foot.append(cancel)
        foot.append(self.save_btn)
        root.append(foot)

        self._rebuild_groups()
        self._refresh_inherited()
        self._refresh_other_groups()
        self.parent_drop.connect("notify::selected", self._on_parent_changed)

    def _forbidden_parent_ids(self) -> set[str]:
        if not self._folder_id:
            return set()
        ids = {self._folder_id}
        sf = self.library.smart_folders_by_id.get(self._folder_id)
        if sf is None:
            return ids

        def walk(node) -> None:
            ids.add(node.id)
            for child in node.children:
                walk(child)

        walk(sf)
        return ids

    def _selected_parent_id(self) -> str | None:
        idx = int(self.parent_drop.get_selected())
        if idx < 0 or idx >= len(self._parent_ids):
            return None
        return self._parent_ids[idx]

    def _inherited_conditions(self) -> list[dict]:
        pid = self._selected_parent_id()
        if not pid:
            return []
        parent = self.library.smart_folders_by_id.get(pid)
        if parent is None:
            return []
        return list(parent.inherited_conditions)

    # ── groups / rules ────────────────────────────────────────────────

    def _rebuild_groups(self) -> None:
        self._rule_labels.clear()
        while (child := self.groups_box.get_first_child()) is not None:
            self.groups_box.remove(child)
        for i, group in enumerate(self._spec.groups):
            self.groups_box.append(self._build_group_widget(i, group))
        self._sync_save_sensitive()
        self._schedule_count()

    def _build_group_widget(self, g_idx: int, group: EditorGroup) -> Gtk.Widget:
        frame = Gtk.Frame()
        frame.add_css_class("card")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        col.set_margin_top(10)
        col.set_margin_bottom(10)
        col.set_margin_start(10)
        col.set_margin_end(10)
        frame.set_child(col)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.append(Gtk.Label(label=f"Group {g_idx + 1}", xalign=0))
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        mode_box.set_hexpand(True)
        mode_btns: dict[str, Gtk.ToggleButton] = {}
        group_btn: Gtk.ToggleButton | None = None
        for mode, label in (
            (GROUP_ALL, GROUP_LABELS[GROUP_ALL]),
            (GROUP_ANY, GROUP_LABELS[GROUP_ANY]),
            (GROUP_NONE, GROUP_LABELS[GROUP_NONE]),
        ):
            btn = Gtk.ToggleButton(label=label)
            if group_btn is None:
                group_btn = btn
            else:
                btn.set_group(group_btn)
            if mode == group.mode:
                btn.set_active(True)
            btn.connect(
                "toggled",
                lambda b, m=mode, gi=g_idx: self._set_group_mode(gi, m, b),
            )
            mode_btns[mode] = btn
            mode_box.append(btn)
        head.append(mode_box)
        rm = Gtk.Button(label="Remove group")
        rm.add_css_class("flat")
        rm.set_sensitive(len(self._spec.groups) > 1)
        rm.connect("clicked", lambda *_ , gi=g_idx: self._remove_group(gi))
        head.append(rm)
        col.append(head)

        for r_idx, rule in enumerate(group.rules):
            col.append(self._build_rule_widget(g_idx, r_idx, rule))

        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, handler in (
            ("+ Rating", lambda gi=g_idx: self._add_rating(gi)),
            ("+ Tags", lambda gi=g_idx: self._add_tags(gi)),
            ("+ Categories", lambda gi=g_idx: self._add_categories(gi)),
        ):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_ , h=handler: h())
            add_row.append(b)
        col.append(add_row)
        return frame

    def _build_rule_widget(
        self, g_idx: int, r_idx: int, rule: EditorRule
    ) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if isinstance(rule, RatingRule):
            row.append(Gtk.Label(label="Rating", xalign=0))
            op_btns: dict[str, Gtk.ToggleButton] = {}
            group_btn: Gtk.ToggleButton | None = None
            for op, tip in (
                (RATING_OP_EQ, "Equal"),
                (RATING_OP_GTE, "At least"),
                (RATING_OP_LTE, "At most"),
            ):
                btn = Gtk.ToggleButton(label=RATING_OP_SYMBOLS[op])
                btn.set_tooltip_text(tip)
                if group_btn is None:
                    group_btn = btn
                else:
                    btn.set_group(group_btn)
                if op == rule.op:
                    btn.set_active(True)
                btn.connect(
                    "toggled",
                    lambda b, o=op, gi=g_idx, ri=r_idx: self._set_rating_op(
                        gi, ri, o, b
                    ),
                )
                op_btns[op] = btn
                row.append(btn)
            for n in range(1, 6):
                star = Gtk.Button(label="★" if n <= rule.stars else "☆")
                star.add_css_class("flat")
                star.connect(
                    "clicked",
                    lambda *_ , s=n, gi=g_idx, ri=r_idx: self._set_rating_stars(
                        gi, ri, s
                    ),
                )
                row.append(star)
            row.append(self._remove_rule_btn(g_idx, r_idx))
            return row

        if isinstance(rule, (TagsRule, CategoriesRule)):
            kind = "Tags" if isinstance(rule, TagsRule) else "Categories"
            row.append(Gtk.Label(label=kind, xalign=0))
            mode_drop = Gtk.DropDown.new_from_strings(
                [SET_LABELS[SET_ANY], SET_LABELS[SET_ALL]]
            )
            mode_drop.set_selected(0 if rule.mode == SET_ANY else 1)
            mode_drop.connect(
                "notify::selected",
                lambda drop, *_ , gi=g_idx, ri=r_idx: self._set_set_mode(
                    gi, ri, SET_ANY if drop.get_selected() == 0 else SET_ALL
                ),
            )
            row.append(mode_drop)
            summary = Gtk.Label(
                label=self._values_label(rule),
                xalign=0,
                hexpand=True,
                ellipsize=3,
            )
            self._rule_labels[(g_idx, r_idx)] = summary
            row.append(summary)
            edit = Gtk.Button(label="Edit")
            edit.add_css_class("flat")
            if isinstance(rule, TagsRule):
                edit.connect(
                    "clicked", lambda *_ , gi=g_idx, ri=r_idx: self._open_tags_picker(gi, ri)
                )
            else:
                edit.connect(
                    "clicked",
                    lambda *_ , gi=g_idx, ri=r_idx: self._open_categories_picker(gi, ri),
                )
            row.append(edit)
            row.append(self._remove_rule_btn(g_idx, r_idx))
            return row

        # OtherRule — read-only, kept on save
        lab = Gtk.Label(
            label=rule_summary(rule, folder_paths=self.library.folder_paths),
            xalign=0,
            hexpand=True,
            ellipsize=3,
        )
        lab.add_css_class("dim-label")
        lab.set_tooltip_text("Kept as-is (not edited here)")
        row.append(lab)
        return row

    def _remove_rule_btn(self, g_idx: int, r_idx: int) -> Gtk.Button:
        btn = Gtk.Button(label="×")
        btn.add_css_class("flat")
        btn.set_tooltip_text("Remove rule")
        btn.connect("clicked", lambda *_ , gi=g_idx, ri=r_idx: self._remove_rule(gi, ri))
        return btn

    def _values_label(self, rule: TagsRule | CategoriesRule) -> str:
        if isinstance(rule, TagsRule):
            return ", ".join(rule.tags) if rule.tags else "(none)"
        names = [
            self.library.folder_paths.get(i, i) for i in rule.folder_ids
        ]
        return ", ".join(names) if names else "(none)"

    def _rule_at(self, g_idx: int, r_idx: int) -> EditorRule | None:
        if g_idx < 0 or g_idx >= len(self._spec.groups):
            return None
        rules = self._spec.groups[g_idx].rules
        if r_idx < 0 or r_idx >= len(rules):
            return None
        return rules[r_idx]

    def _add_group(self) -> None:
        self._spec.groups.append(EditorGroup(mode=GROUP_ALL, rules=[]))
        self._rebuild_groups()

    def _remove_group(self, g_idx: int) -> None:
        if len(self._spec.groups) <= 1:
            return
        if 0 <= g_idx < len(self._spec.groups):
            del self._spec.groups[g_idx]
            self._rebuild_groups()

    def _set_group_mode(self, g_idx: int, mode: str, btn: Gtk.ToggleButton) -> None:
        if not btn.get_active():
            return
        if 0 <= g_idx < len(self._spec.groups):
            self._spec.groups[g_idx].mode = mode
            self._schedule_count()

    def _add_rating(self, g_idx: int) -> None:
        self._spec.groups[g_idx].rules.append(RatingRule())
        self._rebuild_groups()

    def _add_tags(self, g_idx: int) -> None:
        self._spec.groups[g_idx].rules.append(TagsRule())
        r_idx = len(self._spec.groups[g_idx].rules) - 1
        self._rebuild_groups()
        self._open_tags_picker(g_idx, r_idx, remove_if_empty=True)

    def _add_categories(self, g_idx: int) -> None:
        self._spec.groups[g_idx].rules.append(CategoriesRule())
        r_idx = len(self._spec.groups[g_idx].rules) - 1
        self._rebuild_groups()
        self._open_categories_picker(g_idx, r_idx, remove_if_empty=True)

    def _remove_rule(self, g_idx: int, r_idx: int) -> None:
        rules = self._spec.groups[g_idx].rules
        if 0 <= r_idx < len(rules):
            del rules[r_idx]
            self._rebuild_groups()

    def _set_rating_op(
        self, g_idx: int, r_idx: int, op: str, btn: Gtk.ToggleButton
    ) -> None:
        if not btn.get_active():
            return
        rule = self._rule_at(g_idx, r_idx)
        if isinstance(rule, RatingRule):
            rule.op = op
            self._schedule_count()

    def _set_rating_stars(self, g_idx: int, r_idx: int, stars: int) -> None:
        rule = self._rule_at(g_idx, r_idx)
        if isinstance(rule, RatingRule):
            rule.stars = stars
            self._rebuild_groups()

    def _set_set_mode(self, g_idx: int, r_idx: int, mode: str) -> None:
        rule = self._rule_at(g_idx, r_idx)
        if isinstance(rule, (TagsRule, CategoriesRule)):
            rule.mode = mode
            self._schedule_count()

    # ── pickers ───────────────────────────────────────────────────────

    def _open_tags_picker(
        self, g_idx: int, r_idx: int, *, remove_if_empty: bool = False
    ) -> None:
        rule = self._rule_at(g_idx, r_idx)
        if not isinstance(rule, TagsRule):
            return
        current = set(rule.tags)

        def on_toggle(tag: str, turn_on: bool) -> None:
            if turn_on:
                current.add(tag)
            else:
                current.discard(tag)
            rule.tags = sorted(current, key=str.lower)
            lab = self._rule_labels.get((g_idx, r_idx))
            if lab is not None:
                lab.set_text(self._values_label(rule))
            self._sync_save_sensitive()
            self._schedule_count()

        def on_close() -> None:
            if remove_if_empty and not rule.tags:
                self._remove_rule(g_idx, r_idx)

        picker = TogglePicker(
            self,
            title="Tags for this rule",
            subtitle="Enter toggles · Esc closes",
            all_values=self.library.all_tags(),
            active=set(rule.tags),
            recent=load_recent("tags"),
            allow_create=True,
            recent_kind="tags",
            on_toggle=on_toggle,
            on_close=on_close,
        )
        picker.present()

    def _open_categories_picker(
        self, g_idx: int, r_idx: int, *, remove_if_empty: bool = False
    ) -> None:
        rule = self._rule_at(g_idx, r_idx)
        if not isinstance(rule, CategoriesRule):
            return
        path_to_id = {
            path: fid for fid, path in self.library.folder_paths.items()
        }
        all_paths = [
            self.library.folder_paths.get(folder.id, folder.name)
            for folder, _depth in self.library.flatten_folders()
        ]
        current_paths = {
            self.library.folder_paths.get(i, i) for i in rule.folder_ids
        }

        def on_toggle(path: str, turn_on: bool) -> None:
            fid = path_to_id.get(path)
            if not fid:
                return
            if turn_on:
                if fid not in rule.folder_ids:
                    rule.folder_ids.append(fid)
            else:
                rule.folder_ids = [i for i in rule.folder_ids if i != fid]
            lab = self._rule_labels.get((g_idx, r_idx))
            if lab is not None:
                lab.set_text(self._values_label(rule))
            self._sync_save_sensitive()
            self._schedule_count()

        def on_close() -> None:
            if remove_if_empty and not rule.folder_ids:
                self._remove_rule(g_idx, r_idx)

        picker = TogglePicker(
            self,
            title="Categories for this rule",
            subtitle="Enter toggles · Esc closes",
            all_values=all_paths,
            active=current_paths,
            recent=[],
            allow_create=False,
            recent_kind="folders",
            on_toggle=on_toggle,
            on_close=on_close,
        )
        picker.present()

    # ── inherited / other ─────────────────────────────────────────────

    def _refresh_inherited(self) -> None:
        while (child := self.inherited_box.get_first_child()) is not None:
            self.inherited_box.remove(child)
        inherited = self._inherited_conditions()
        if not inherited:
            return
        head = Gtk.Label(label="Inherited from parent (always applied)", xalign=0)
        head.add_css_class("heading")
        self.inherited_box.append(head)
        for line in summarize_conditions(
            inherited, folder_paths=self.library.folder_paths
        ):
            lab = Gtk.Label(label=line, xalign=0, wrap=True)
            lab.add_css_class("dim-label")
            lab.add_css_class("caption")
            self.inherited_box.append(lab)

    def _refresh_other_groups(self) -> None:
        while (child := self.other_groups_box.get_first_child()) is not None:
            self.other_groups_box.remove(child)
        if not self._spec.other_groups:
            return
        head = Gtk.Label(
            label="Other groups (kept as-is — not editable here)", xalign=0
        )
        head.add_css_class("heading")
        self.other_groups_box.append(head)
        for og in self._spec.other_groups:
            for line in summarize_conditions(
                [og.raw], folder_paths=self.library.folder_paths
            ):
                lab = Gtk.Label(label=line, xalign=0, wrap=True)
                lab.add_css_class("dim-label")
                lab.add_css_class("caption")
                self.other_groups_box.append(lab)

    def _on_parent_changed(self, *_a) -> None:
        self._spec.parent_id = self._selected_parent_id()
        self._refresh_inherited()
        self._schedule_count()

    # ── count / save ──────────────────────────────────────────────────

    def _encoded(self) -> list[dict]:
        return encode_conditions(self._spec.groups, self._spec.other_groups)

    def _sync_save_sensitive(self) -> None:
        if not hasattr(self, "save_btn"):
            return
        name_ok = bool(self.name_entry.get_text().strip())
        self.save_btn.set_sensitive(name_ok and bool(self._encoded()))

    def _schedule_count(self) -> None:
        self._count_gen += 1
        gen = self._count_gen
        if self._count_timeout:
            GLib.source_remove(self._count_timeout)
            self._count_timeout = 0

        def fire() -> bool:
            self._count_timeout = 0
            self._run_count(gen)
            return False

        self._count_timeout = GLib.timeout_add(180, fire)

    def _run_count(self, gen: int) -> None:
        conditions = self._encoded()
        inherited = self._inherited_conditions()

        def work() -> None:
            try:
                n = self.library.count_conditions(conditions, inherited=inherited)
                err = None
            except Exception as exc:  # noqa: BLE001
                n = 0
                err = exc

            def apply() -> bool:
                if gen != self._count_gen or self._closing:
                    return False
                if err is not None:
                    self.count_lbl.set_text("Matches —")
                else:
                    self.count_lbl.set_text(f"Matches {n} items")
                return False

            GLib.idle_add(apply)

        threading.Thread(target=work, name="sf-count", daemon=True).start()

    def _save(self) -> None:
        name = self.name_entry.get_text().strip()
        if not name:
            return
        conditions = self._encoded()
        if not conditions:
            return
        parent_id = self._selected_parent_id()
        try:
            with write_session(self.library.root):
                if self._folder_id:
                    node = update_smart_folder_node(
                        self.library.root,
                        self._folder_id,
                        name=name,
                        conditions=conditions,
                        parent_id=parent_id,
                    )
                    sid = self._folder_id
                else:
                    node = create_smart_folder_node(
                        self.library.root,
                        name=name,
                        conditions=conditions,
                        parent_id=parent_id,
                    )
                    sid = str(node["id"])
        except WriteError as exc:
            self._error_toast(str(exc))
            return
        try:
            self.library.reload_metadata_trees()
        except Exception as exc:  # noqa: BLE001
            self._error_toast(str(exc))
            return
        if self._on_saved:
            self._on_saved(sid)
        self.close()

    def _error_toast(self, text: str) -> None:
        parent = self._parent_win
        if hasattr(parent, "_toast"):
            parent._toast(text)  # type: ignore[attr-defined]
        else:
            self.count_lbl.set_text(text)

    def _on_key(self, _c, keyval, _kc, _state) -> bool:
        if self._picker_blocking:
            return False
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            focus = self.get_focus()
            if isinstance(focus, Gtk.Entry) and focus is self.name_entry:
                return False
            if self.save_btn.get_sensitive():
                self._save()
                return True
        return False

    def _on_close_request(self, *_a) -> bool:
        self._closing = True
        if self._count_timeout:
            GLib.source_remove(self._count_timeout)
            self._count_timeout = 0
        if getattr(self._parent_win, "_open_dialog", None) is self:
            self._parent_win._open_dialog = None  # type: ignore[attr-defined]
        if hasattr(self._parent_win, "_picker_blocking"):
            self._parent_win._picker_blocking = False  # type: ignore[attr-defined]
        if self._on_closed:
            self._on_closed()
        return False
