"""Importer duplicate detection and stability."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from import_media import (
    build_size_index,
    classify_inbox_files,
    find_duplicate_item,
    import_file,
)
from library import Item


def _png_bytes(payload: bytes = b"unique-payload") -> bytes:
    # Minimal valid-enough PNG-ish blob for importable extension checks.
    # check_media_complete may reject tiny stubs; keep comfortably above floors.
    return b"\x89PNG\r\n\x1a\n" + payload + (b"\x00" * 64)


def make_item(
    root: Path,
    item_id: str,
    content: bytes,
    *,
    deleted: bool = False,
) -> Item:
    item_dir = root / "images" / f"{item_id}.info"
    item_dir.mkdir(parents=True)
    media = item_dir / f"{item_id}.png"
    media.write_bytes(content)
    return Item(
        id=item_id,
        name=item_id,
        ext="png",
        tags=[],
        folders=[],
        path=media,
        thumb=None,
        is_deleted=deleted,
        size=len(content),
        width=1,
        height=1,
        annotation="",
        modification_time=1,
        item_dir=item_dir,
        name_lower=item_id,
        ext_lower="png",
    )


class ImportDuplicatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "images").mkdir()
        (self.root / "metadata.json").write_text("{}", encoding="utf-8")
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()

    def test_find_duplicate_by_content_hash(self) -> None:
        content = _png_bytes(b"same-bytes")
        existing = make_item(self.root, "EXIST1", content)
        idx = build_size_index([existing])
        src = self.inbox / "copy.png"
        src.write_bytes(content)
        match = find_duplicate_item(src, size_index=idx)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.existing_id, "EXIST1")

    def test_soft_deleted_never_counts_as_duplicate(self) -> None:
        content = _png_bytes(b"trashed")
        existing = make_item(self.root, "GONE1", content, deleted=True)
        idx = build_size_index([existing])
        src = self.inbox / "again.png"
        src.write_bytes(content)
        self.assertIsNone(find_duplicate_item(src, size_index=idx))

    def test_classify_inbox_splits_unique_and_dups(self) -> None:
        shared = _png_bytes(b"shared")
        unique = _png_bytes(b"only-once")
        existing = make_item(self.root, "LIB1", shared)
        dup = self.inbox / "dup.png"
        fresh = self.inbox / "fresh.png"
        dup.write_bytes(shared)
        fresh.write_bytes(unique)
        uniques, dups = classify_inbox_files([dup, fresh], [existing])
        self.assertEqual([p.name for p in uniques], ["fresh.png"])
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0].existing_id, "LIB1")

    def test_import_file_skips_exact_duplicate(self) -> None:
        content = _png_bytes(b"already-in-lib" + b"x" * 200)
        existing = make_item(self.root, "LIB2", content)
        src = self.inbox / "again.png"
        src.write_bytes(content)
        with mock.patch(
            "import_media.check_media_complete", return_value=(True, "")
        ):
            result = import_file(
                self.root,
                src,
                move_source=False,
                force_new=False,
                items=[existing],
            )
        self.assertTrue(result.skipped)
        self.assertEqual(result.error, "duplicate:LIB2")
        self.assertTrue(src.is_file())

    def test_same_size_different_hash_is_unique(self) -> None:
        a = _png_bytes(b"aaa" + b"0" * 100)
        b = _png_bytes(b"bbb" + b"0" * 100)
        self.assertEqual(len(a), len(b))
        existing = make_item(self.root, "LIB3", a)
        src = self.inbox / "other.png"
        src.write_bytes(b)
        uniques, dups = classify_inbox_files([src], [existing])
        self.assertEqual(uniques, [src])
        self.assertEqual(dups, [])
