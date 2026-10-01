"""Pick up metadata.json edits made outside the window (eagle-api, Dropbox)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import library as library_mod
from library import EagleLibrary
from write import atomic_write_json, save_item_metadata

READY = {
    "id": "sf-ready",
    "name": "Ready to Post",
    "conditions": [
        {
            "match": "AND",
            "boolean": "TRUE",
            "rules": [{"property": "tags", "method": "union", "value": ["ready"]}],
        }
    ],
}


class TempLibrary:
    def __init__(self, n: int = 3) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Test.library"
        (self.root / "images").mkdir(parents=True)
        atomic_write_json(
            self.root / "metadata.json", {"folders": [], "smartFolders": [READY]}
        )
        atomic_write_json(self.root / "mtime.json", {})
        for i in range(n):
            d = self.item_dir(f"ITEM{i}")
            d.mkdir()
            (d / f"asset{i}.png").write_bytes(b"png")
            atomic_write_json(
                d / "metadata.json",
                {
                    "id": f"ITEM{i}",
                    "name": f"asset{i}",
                    "ext": "png",
                    "tags": [],
                    "folders": [],
                    "btime": 1000 + i,
                    "modificationTime": 1000 + i,
                },
            )

    def item_dir(self, iid: str) -> Path:
        return self.root / "images" / f"{iid}.info"

    def external_edit(self, iid: str, **changes) -> None:
        """What eagle-api / another process does: rewrite metadata.json."""
        meta_path = self.item_dir(iid) / "metadata.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(changes)
        save_item_metadata(self.root, self.item_dir(iid), data, do_backup=False)

    def cleanup(self) -> None:
        self._tmp.cleanup()


class RefreshChangedItemsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TempLibrary()
        self.addCleanup(self.tmp.cleanup)
        self.lib = EagleLibrary(self.tmp.root)
        self.lib.load()

    def ready_ids(self) -> list[str]:
        return sorted(it.id for it in self.lib.query(smart_folder_id="sf-ready"))

    def test_external_tag_add_updates_item_in_place_and_smart_folder(self) -> None:
        self.assertEqual(self.ready_ids(), [])  # cached
        before = self.lib.items_by_id["ITEM1"]

        self.tmp.external_edit("ITEM1", tags=["Ready"], star=4)
        # Stale until a refresh pass runs
        self.assertEqual(self.ready_ids(), [])

        changes = self.lib.refresh_changed_items()
        self.assertEqual(changes.changed, ["ITEM1"])
        self.assertEqual(changes.checked, 3)
        self.assertFalse(changes.trees_changed)
        after = self.lib.items_by_id["ITEM1"]
        self.assertIs(after, before)  # grid / selection keep the same object
        self.assertEqual(after.tags, ["ready"])
        self.assertEqual(after.tag_set, frozenset({"ready"}))
        self.assertEqual(after.star, 4)
        self.assertEqual(self.ready_ids(), ["ITEM1"])

    def test_external_tag_remove_leaves_smart_folder(self) -> None:
        self.tmp.external_edit("ITEM0", tags=["ready"])
        self.lib.refresh_changed_items()
        self.assertEqual(self.ready_ids(), ["ITEM0"])
        self.tmp.external_edit("ITEM0", tags=[])
        self.assertEqual(self.lib.refresh_changed_items().changed, ["ITEM0"])
        self.assertEqual(self.ready_ids(), [])

    def test_unchanged_files_are_not_reparsed(self) -> None:
        with patch.object(
            library_mod, "_item_from_dir", wraps=library_mod._item_from_dir
        ) as parse:
            changes = self.lib.refresh_changed_items()
        self.assertEqual(changes.changed, [])
        parse.assert_not_called()

    def test_refresh_never_writes(self) -> None:
        self.tmp.external_edit("ITEM2", tags=["ready"])
        snap = {
            p: p.stat().st_mtime_ns for p in self.tmp.root.rglob("*") if p.is_file()
        }
        self.lib.refresh_changed_items()
        after = {
            p: p.stat().st_mtime_ns for p in self.tmp.root.rglob("*") if p.is_file()
        }
        self.assertEqual(snap, after)

    def test_in_window_edit_is_not_reported_or_clobbered(self) -> None:
        self.lib.update_item("ITEM0", add_tags=["mine"], star=3)
        changes = self.lib.refresh_changed_items()
        self.assertEqual(changes.changed, [])  # disk == memory
        it = self.lib.items_by_id["ITEM0"]
        self.assertEqual((it.tags, it.star), (["mine"], 3))
        # Stamp caught up: a second pass parses nothing
        with patch.object(
            library_mod, "_item_from_dir", wraps=library_mod._item_from_dir
        ) as parse:
            self.lib.refresh_changed_items()
        parse.assert_not_called()

    def test_edit_racing_with_parse_wins(self) -> None:
        """An in-window write landing mid-pass must not be overwritten."""
        self.tmp.external_edit("ITEM1", tags=["external"])
        real = library_mod._item_from_dir

        def parse_then_edit(item_dir: Path):
            fresh = real(item_dir)  # sees tags=["external"]
            self.lib.update_item("ITEM1", set_tags=["window"], star=5)
            return fresh

        with patch.object(library_mod, "_item_from_dir", side_effect=parse_then_edit):
            changes = self.lib.refresh_changed_items()
        self.assertEqual(changes.changed, [])
        it = self.lib.items_by_id["ITEM1"]
        self.assertEqual((it.tags, it.star), (["window"], 5))
        # Next pass reads the window's own write back: no visible change
        self.assertEqual(self.lib.refresh_changed_items().changed, [])
        self.assertEqual((it.tags, it.star), (["window"], 5))

    def test_half_written_file_is_skipped_then_picked_up(self) -> None:
        meta = self.tmp.item_dir("ITEM2") / "metadata.json"
        good = meta.read_text(encoding="utf-8")
        meta.write_text('{"id": "ITEM2", "tags": ["rea', encoding="utf-8")
        self.assertEqual(self.lib.refresh_changed_items().changed, [])
        self.assertEqual(self.lib.items_by_id["ITEM2"].tags, [])
        data = json.loads(good)
        data["tags"] = ["ready"]
        atomic_write_json(meta, data)
        self.assertEqual(self.lib.refresh_changed_items().changed, ["ITEM2"])
        self.assertEqual(self.ready_ids(), ["ITEM2"])

    def test_missing_item_folder_is_ignored(self) -> None:
        meta = self.tmp.item_dir("ITEM0") / "metadata.json"
        meta.unlink()
        changes = self.lib.refresh_changed_items()
        self.assertEqual(changes.changed, [])
        self.assertIn("ITEM0", self.lib.items_by_id)

    def test_library_metadata_change_is_reported_until_reloaded(self) -> None:
        self.assertFalse(self.lib.refresh_changed_items().trees_changed)
        rule = dict(READY, name="Ready (renamed)")
        atomic_write_json(
            self.tmp.root / "metadata.json", {"folders": [], "smartFolders": [rule]}
        )
        self.assertTrue(self.lib.refresh_changed_items().trees_changed)
        self.lib.reload_metadata_trees()
        self.assertFalse(self.lib.refresh_changed_items().trees_changed)
        self.assertEqual(
            self.lib.smart_folders_by_id["sf-ready"].name, "Ready (renamed)"
        )

    def test_cancelled_pass_changes_nothing(self) -> None:
        self.tmp.external_edit("ITEM1", tags=["ready"])
        changes = self.lib.refresh_changed_items(cancelled=lambda: True)
        self.assertEqual(changes.changed, [])
        self.assertEqual(self.lib.items_by_id["ITEM1"].tags, [])
        self.assertEqual(self.lib.refresh_changed_items().changed, ["ITEM1"])

    def test_change_bumps_cache_generation(self) -> None:
        self.ready_ids()
        gen = self.lib._cache_generation  # noqa: SLF001
        self.tmp.external_edit("ITEM1", tags=["ready"])
        self.lib.refresh_changed_items()
        self.assertGreater(self.lib._cache_generation, gen)  # noqa: SLF001
        self.assertEqual(self.lib._query_cache, {})  # noqa: SLF001


class ExternalChangesUITest(unittest.TestCase):
    """Window-side scheduling without a real GTK window."""

    def setUp(self) -> None:
        from app import EagleBrowseWindow

        self.W = EagleBrowseWindow
        self.item = SimpleNamespace(id="ITEM1", star=None)
        self.other = SimpleNamespace(id="ITEM9", star=None)
        self.win = SimpleNamespace(
            _shutdown=threading.Event(),
            _library_ready=True,
            _metadata_batch_busy=False,
            _picker_blocking=False,
            _open_dialog=None,
            _sf_editor=None,
            _ext_pending_ids=set(),
            _ext_pending_trees=False,
            _ext_refresh_source=0,
            _ext_refresh_running=False,
            _ext_refresh_again=False,
            _ext_refresh_last=0.0,
            _all_items=[self.item],
            _marked=set(),
            selected_item=None,
            library=SimpleNamespace(
                items_by_id={"ITEM1": self.item, "ITEM9": self.other}
            ),
            refresh_items=Mock(),
            _sync_star_overlays=Mock(),
            _after_external_item_changes=Mock(),
            _reload_trees_after_external_change=Mock(),
            _item_matches_current_view=Mock(return_value=False),
            update_inspector=Mock(),
            _update_path_label=Mock(),
            is_viewer_open=Mock(return_value=False),
        )
        win = self.win
        win._external_refresh_blocked = lambda: self.W._external_refresh_blocked(win)

    def apply(self) -> None:
        self.W._apply_external_changes(self.win)

    def test_shown_item_change_refreshes_view_keeping_selection(self) -> None:
        self.win._ext_pending_ids = {"ITEM1"}
        self.apply()
        self.win.refresh_items.assert_called_once_with(
            reset_selection=False, scroll_to_top=False
        )
        self.win._after_external_item_changes.assert_called_once()
        self.assertEqual(self.win._ext_pending_ids, set())

    def test_item_entering_view_refreshes(self) -> None:
        self.win._item_matches_current_view.return_value = True
        self.win._ext_pending_ids = {"ITEM9"}
        self.apply()
        self.win.refresh_items.assert_called_once()

    def test_irrelevant_change_skips_requery(self) -> None:
        self.win._ext_pending_ids = {"ITEM9"}
        self.apply()
        self.win.refresh_items.assert_not_called()
        self.win._sync_star_overlays.assert_called_once()
        self.win._after_external_item_changes.assert_called_once()

    def test_deferred_while_editing_then_applied(self) -> None:
        for attr, value in (
            ("_picker_blocking", True),
            ("_metadata_batch_busy", True),
            ("_open_dialog", object()),
            ("_sf_editor", object()),
        ):
            with self.subTest(attr=attr):
                self.win.refresh_items.reset_mock()
                self.win._ext_pending_ids = {"ITEM1"}
                setattr(self.win, attr, value)
                self.apply()
                self.win.refresh_items.assert_not_called()
                self.assertEqual(self.win._ext_pending_ids, {"ITEM1"})
                setattr(self.win, attr, None if attr in ("_open_dialog", "_sf_editor") else False)
                self.apply()
                self.win.refresh_items.assert_called_once()

    def test_selected_item_change_updates_inspector(self) -> None:
        self.win.selected_item = self.item
        self.win._ext_pending_ids = {"ITEM1"}
        self.apply()
        self.win.update_inspector.assert_called_once()

    def test_tree_change_reloads_trees_and_view(self) -> None:
        self.win._ext_pending_trees = True
        self.apply()
        self.win._reload_trees_after_external_change.assert_called_once()
        self.win.refresh_items.assert_called_once()

    def test_requests_coalesce_into_one_timer(self) -> None:
        with patch("app.GLib.timeout_add", return_value=7) as add:
            self.W._request_external_refresh(self.win, 400)
            self.W._request_external_refresh(self.win, 0)
        add.assert_called_once()
        self.assertEqual(self.win._ext_refresh_source, 7)

    def test_no_request_before_library_ready_or_after_shutdown(self) -> None:
        with patch("app.GLib.timeout_add") as add:
            self.win._library_ready = False
            self.W._request_external_refresh(self.win, 0)
            self.win._library_ready = True
            self.win._shutdown.set()
            self.W._request_external_refresh(self.win, 0)
        add.assert_not_called()

    def test_tick_runs_periodic_pass_and_flushes_deferred(self) -> None:
        self.win._apply_external_changes = Mock()
        self.win._request_external_refresh = Mock()
        self.win._ext_pending_ids = {"ITEM1"}
        with patch("app.time.monotonic", return_value=1000.0):
            self.W._external_refresh_tick(self.win)
        self.win._apply_external_changes.assert_called_once()
        self.win._request_external_refresh.assert_called_once_with(0)
        self.win._request_external_refresh.reset_mock()
        self.win._ext_refresh_last = 995.0
        with patch("app.time.monotonic", return_value=1000.0):
            self.W._external_refresh_tick(self.win)
        self.win._request_external_refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
