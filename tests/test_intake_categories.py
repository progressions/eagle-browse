"""Opt-in intake categories, duplicate reuse, and independent readiness."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from config import Settings, load_settings
from import_media import import_file, list_inbox_files, reimport_existing
from inbox_watch import _stable_ready, process_ready
from library import EagleLibrary


class IntakeCategoriesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.library = self.root / "test.library"
        self.library.mkdir()
        self.metadata = self.library / "metadata.json"
        self.metadata.write_text('{"folders": []}')
        self.inbox = self.root / "intake"
        self.inbox.mkdir()
        self.settings = Settings(self.inbox, self.library, (), True)
        settings = patch("import_media.load_settings", return_value=self.settings)
        settings.start()
        self.addCleanup(settings.stop)

    def media(self, name):
        path = self.inbox / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 128)
        return path

    def imported(self, path, **kwargs):
        with patch("import_media.check_media_complete", return_value=(True, "")), \
             patch("import_media._image_size", return_value=(1, 1)), \
             patch("import_media._make_image_thumbnail", return_value=True):
            result = import_file(self.library, path, **kwargs)
        self.assertTrue(result.ok, result.error)
        data = json.loads((self.library / "images" / f"{result.item_id}.info" / "metadata.json").read_text())
        return result, data

    def test_scan_opt_in_hidden_and_symlinks(self):
        flat = self.media("flat.png")
        nested = self.media("video/deeper/a.png")
        self.media(".dup-queue/a.png")
        self.media("video/.hidden/a.png")
        (self.inbox / "linked").symlink_to(nested.parent, target_is_directory=True)
        (self.inbox / "alias.png").symlink_to(nested)
        self.assertEqual(set(list_inbox_files(self.inbox)), {flat, nested})
        with patch("import_media.load_settings", return_value=Settings(self.inbox, self.library, ())):
            self.assertEqual(list_inbox_files(self.inbox), [flat])
            _, data = self.imported(nested)
            self.assertEqual(data["folders"], [])

    def test_case_variants_and_deeper_paths_share_category(self):
        first = self.media("IMage/a.png")
        _, a = self.imported(first)
        _, b = self.imported(self.media("image/deeper/b.png"))
        folders = json.loads(self.metadata.read_text())["folders"]
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0]["name"], "image")
        self.assertEqual(a["folders"], b["folders"])
        self.assertFalse(first.exists())
        self.assertTrue(first.parent.is_dir())
        _, flat = self.imported(self.media("flat.png"))
        self.assertEqual(flat["folders"], [])

    def test_existing_category_and_duplicate_reuse(self):
        self.metadata.write_text(json.dumps({"folders": [
            {"id": "EXIST", "name": "IMage", "children": [], "tags": ["portrait"]}
        ]}))
        result, _ = self.imported(self.media("original.png"), folder_ids=["OTHER"])
        source = self.media("image/duplicate.png")
        reused = reimport_existing(self.library, result.item_id, source=source)
        self.assertTrue(reused.ok, reused.error)
        data = json.loads((self.library / "images" / f"{result.item_id}.info" / "metadata.json").read_text())
        self.assertEqual(data["folders"], ["OTHER", "EXIST"])
        self.assertIn("portrait", data["tags"])
        self.assertFalse(source.exists())
        self.assertEqual(len(json.loads(self.metadata.read_text())["folders"]), 1)

    def test_readiness_uses_relative_path(self):
        a = self.media("image/a.png")
        b = self.media("video/a.png")
        b.write_bytes(b"y" * 256)
        with patch("import_media.check_media_complete", return_value=(True, "")):
            ready, sizes = _stable_ready(self.inbox, {})
            self.assertEqual(ready, [])
            ready, _ = _stable_ready(self.inbox, sizes)
            self.assertEqual(set(ready), {a, b})

    def test_failed_category_write_preserves_source(self):
        source = self.media("image/a.png")
        self.metadata.write_text("broken JSON")
        with patch("import_media.check_media_complete", return_value=(True, "")), \
             patch("import_media._image_size", return_value=(1, 1)):
            result = import_file(self.library, source)
        self.assertFalse(result.ok)
        self.assertTrue(source.exists())

    def test_custom_inbox_root(self):
        source = self.root / "custom" / "Video" / "clip.png"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"x" * 128)
        _, data = self.imported(source, inbox_root=self.root / "custom")
        self.assertEqual(len(data["folders"]), 1)
        self.assertEqual(json.loads(self.metadata.read_text())["folders"][0]["name"], "video")

    def test_gui_duplicate_signals_distinguish_same_basename(self):
        a = self.media("image/a.png")
        b = self.media("video/a.png")
        matches = [SimpleNamespace(source=p, existing_id="EXIST", size=128) for p in [a, b]]
        library = SimpleNamespace(root=self.library, items=[])
        with patch("inbox_watch.classify_inbox_files", return_value=([], matches)), \
             patch("inbox_watch.gui_is_running", return_value=True), \
             patch("inbox_watch.announce_inbox_dups") as announce:
            process_ready(library, [a, b], dup_policy="reuse", notify=False,
                          sound=False, inbox_root=self.inbox)
        self.assertEqual(announce.call_args.args[1], ["image/a.png", "video/a.png"])
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())

    def test_config_default_and_boolean_overlay(self):
        config = self.root / "config.toml"
        with patch("config._candidate_files", return_value=[config]):
            self.assertFalse(load_settings().inbox_subfolders_as_categories)
            config.write_text("inbox_subfolders_as_categories = true")
            self.assertTrue(load_settings().inbox_subfolders_as_categories)
            config.write_text('inbox_subfolders_as_categories = "false"')
            self.assertFalse(load_settings().inbox_subfolders_as_categories)

    def window_for(self, library):
        # Exercise actual completion methods without constructing a GTK window.
        try:
            from app import EagleBrowseWindow
        except (ImportError, ValueError) as exc:
            self.skipTest(f"GTK dependencies unavailable: {exc}")
        from types import MethodType

        window = SimpleNamespace(
            library=library, _smart_counts={"stale": 1},
            _populate_sidebar=Mock(), _rebuild_set_counts=Mock(),
            refresh_items=Mock(), _refresh_special_counts=Mock(),
            _item_matches_current_view=Mock(return_value=False),
        )
        for name in ("_refresh_import_categories", "_finish_inbox_import", "_apply_new_items"):
            setattr(window, name, MethodType(getattr(EagleBrowseWindow, name), window))
        return window

    def test_watcher_ingest_refreshes_categories_before_matching(self):
        library = EagleLibrary(self.library)
        library.load()
        result, data = self.imported(self.media("Video/new.png"))
        item = library.load_item(result.item_id)
        window = self.window_for(library)

        def match(item):
            self.assertEqual(library.folder_paths[item.folders[0]], "video")
            return False

        window._item_matches_current_view.side_effect = match
        window._apply_new_items([item])
        self.assertEqual(library.folder_paths[data["folders"][0]], "video")
        window._populate_sidebar.assert_called_once_with(select_current=True)
        self.assertEqual(window._smart_counts, {})

    def test_reuse_completion_refreshes_membership_and_views(self):
        original, _ = self.imported(self.media("original.png"))
        library = EagleLibrary(self.library)
        library.load()
        # Prime derived counts before the disk-only write.
        self.assertEqual(library.count_special_view("uncategorized"), 1)
        result = reimport_existing(self.library, original.item_id,
                                   source=self.media("video/copy.png"))
        window = self.window_for(library)
        window._finish_inbox_import([result])
        self.assertTrue(library.items_by_id[original.item_id].folders)
        self.assertEqual(library.count_special_view("uncategorized"), 0)
        window._populate_sidebar.assert_called_once_with(select_current=True)
        window.refresh_items.assert_called_once_with(reset_selection=False, scroll_to_top=False)
        window._refresh_special_counts.assert_called_once()

    def test_mixed_batch_refreshes_reused_rows_after_new_items(self):
        original, _ = self.imported(self.media("original.png"))
        library = EagleLibrary(self.library)
        library.load()
        reused = reimport_existing(self.library, original.item_id,
                                   source=self.media("video/copy.png"))
        new, _ = self.imported(self.media("video/new.png"))
        window = self.window_for(library)
        window._finish_inbox_import([new, reused])
        self.assertEqual(library.items_by_id[original.item_id].folders,
                         library.items_by_id[new.item_id].folders)
        window.refresh_items.assert_called_once_with(reset_selection=False, scroll_to_top=False)
        window._populate_sidebar.assert_called_once_with(select_current=True)


if __name__ == "__main__":
    unittest.main()
