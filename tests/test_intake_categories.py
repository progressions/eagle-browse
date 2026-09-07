"""Opt-in intake categories, duplicate reuse, and independent readiness."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from config import Settings, load_settings
from import_media import import_file, list_inbox_files, reimport_existing
from inbox_watch import _stable_ready, process_ready


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


if __name__ == "__main__":
    unittest.main()
