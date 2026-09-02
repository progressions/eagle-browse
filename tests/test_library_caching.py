"""Derived-cache behavior around library reads and mutations."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from library import EagleLibrary, Folder, Item, SmartFolder


def make_item(
    root: Path,
    *,
    tags: list[str] | None = None,
    folders: list[str] | None = None,
) -> Item:
    item_dir = root / "images" / "M00000000001.info"
    item_dir.mkdir(parents=True)
    metadata = {
        "id": "M00000000001",
        "name": "asset",
        "ext": "png",
        "tags": tags or [],
        "folders": folders or [],
        "isDeleted": False,
        "annotation": "",
        "modificationTime": 1,
    }
    (item_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return Item(
        id=metadata["id"],
        name="asset",
        ext="png",
        tags=list(tags or []),
        folders=list(folders or []),
        path=item_dir / "asset.png",
        thumb=None,
        is_deleted=False,
        size=1,
        width=1,
        height=1,
        annotation="",
        modification_time=1,
        item_dir=item_dir,
        tag_set=frozenset(tags or []),
        folder_set=frozenset(folders or []),
        name_lower="asset",
        ext_lower="png",
    )


class LibraryCachingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = EagleLibrary(self.root)
        self.item = make_item(self.root)
        self.library.items = [self.item]
        self.library.items_by_id = {self.item.id: self.item}
        self.library.smart_folders_by_id = {
            "tagged": SmartFolder(
                id="tagged",
                name="Tagged",
                conditions=[
                    {
                        "rules": [
                            {
                                "property": "tags",
                                "method": "intersection",
                                "value": ["keep"],
                            }
                        ]
                    }
                ],
                inherited_conditions=[
                    {
                        "rules": [
                            {
                                "property": "tags",
                                "method": "intersection",
                                "value": ["keep"],
                            }
                        ]
                    }
                ],
            ),
            "five-star": SmartFolder(
                id="five-star",
                name="Five star",
                conditions=[
                    {
                        "rules": [
                            {"property": "rating", "method": "equal", "value": 5}
                        ]
                    }
                ],
                inherited_conditions=[
                    {
                        "rules": [
                            {"property": "rating", "method": "equal", "value": 5}
                        ]
                    }
                ],
            ),
        }

    def test_unchanged_query_reuses_cache_entry(self) -> None:
        first = self.library.query(smart_folder_id="tagged")
        second = self.library.query(smart_folder_id="tagged")

        self.assertIs(first, second)
        self.assertEqual(first, [])

    def test_item_mutations_invalidate_cached_queries(self) -> None:
        self.assertEqual(self.library.query(smart_folder_id="tagged"), [])
        self.library.update_item(self.item.id, add_tags=["keep"])
        self.assertEqual(
            [it.id for it in self.library.query(smart_folder_id="tagged")],
            [self.item.id],
        )

        self.assertEqual(self.library.query(smart_folder_id="five-star"), [])
        self.library.update_item(self.item.id, star=5)
        self.assertEqual(
            [it.id for it in self.library.query(smart_folder_id="five-star")],
            [self.item.id],
        )

        self.assertEqual(len(self.library.query()), 1)
        self.library.set_items_deleted([self.item.id], deleted=True)
        self.assertEqual(self.library.query(), [])

    def test_folder_auto_tag_metadata_change_invalidates_tag_cache(self) -> None:
        folder = Folder(id="folder-1", name="Folder")
        self.library.folders_by_id = {folder.id: folder}
        self.assertEqual(self.library.all_tags(), [])

        with mock.patch("write.write_session", return_value=nullcontext()):
            with mock.patch("write.set_folder_auto_tags"):
                self.library.set_folder_auto_tags(folder.id, ["future-tag"])

        self.assertEqual(self.library.all_tags(), ["future-tag"])
