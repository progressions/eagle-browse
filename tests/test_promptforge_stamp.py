"""Unit tests for PromptForge history stamps (Fizzy #482)."""

from __future__ import annotations

import unittest

from promptforge_stamp import (
    annotation_line,
    history_id_from_name,
    merge_annotation,
    pf_tag,
    stamp_metadata,
)


class PromptforgeStampTest(unittest.TestCase):
    def test_history_id_from_build_filename(self) -> None:
        self.assertEqual(
            history_id_from_name("image-1173-zt-eunbi5-075-gtt-055-1788021517_00001_"),
            1173,
        )
        self.assertEqual(history_id_from_name("image-1125-krea2-eunbi5-075-1"), 1125)

    def test_rejects_uuid_and_unrelated_names(self) -> None:
        self.assertIsNone(
            history_id_from_name("image-51eaf80f-d5a7-4e51-b1a9-0868e78f10df")
        )
        self.assertIsNone(history_id_from_name("nyt-sweatshirt-city-night"))
        self.assertIsNone(history_id_from_name("freepik_reference-image-1-keyfram"))
        self.assertIsNone(history_id_from_name("grok-image-43502516-806b-4c0b"))
        self.assertIsNone(history_id_from_name("openart-gpt-image-2-edit-1"))
        self.assertIsNone(history_id_from_name(""))
        self.assertIsNone(history_id_from_name(None))

    def test_stamp_metadata_adds_tag_and_annotation(self) -> None:
        meta = {"name": "image-1125-krea2-x", "tags": ["eunbi"], "annotation": ""}
        self.assertTrue(stamp_metadata(meta))
        self.assertIn("pf:1125", meta["tags"])
        self.assertEqual(meta["annotation"], "promptforge:1125")

    def test_stamp_metadata_is_idempotent(self) -> None:
        meta = {
            "name": "image-1125-krea2-x",
            "tags": ["pf:1125", "eunbi"],
            "annotation": "promptforge:1125",
        }
        self.assertFalse(stamp_metadata(meta))
        self.assertEqual(meta["tags"], ["pf:1125", "eunbi"])

    def test_merge_annotation_keeps_existing_notes(self) -> None:
        self.assertEqual(
            merge_annotation("camera notes\nmore", 99),
            "camera notes\nmore\npromptforge:99",
        )
        self.assertEqual(merge_annotation("promptforge:99", 99), "promptforge:99")
        self.assertEqual(pf_tag(99), "pf:99")
        self.assertEqual(annotation_line(99), "promptforge:99")


if __name__ == "__main__":
    unittest.main()
