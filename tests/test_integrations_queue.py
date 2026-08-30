"""Unit tests for integrations enqueue helpers (no network)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from integrations_queue import (
    DEFAULT_BUST_ENGINE,
    DEFAULT_WARDROBE_ENGINE,
    character_for,
    normalize_bust_engine,
    normalize_wardrobe_engine,
    promptforge_base_url,
    promptforge_build_url,
    resolve_promptforge_history_id,
)


class IntegrationsQueueTest(unittest.TestCase):
    def test_normalize_bust_engines(self) -> None:
        self.assertEqual(normalize_bust_engine("klein"), "klein")
        self.assertEqual(normalize_bust_engine("Flux"), "klein")
        self.assertEqual(normalize_bust_engine("krea"), "krea2")
        self.assertEqual(normalize_bust_engine("qwen"), "qwen")
        self.assertIsNone(normalize_bust_engine("nope"))
        self.assertEqual(DEFAULT_BUST_ENGINE, "klein")

    def test_normalize_wardrobe_engines(self) -> None:
        self.assertEqual(normalize_wardrobe_engine("qwen"), "qwen")
        self.assertEqual(normalize_wardrobe_engine("krea2"), "krea2")
        self.assertEqual(normalize_wardrobe_engine("flux-klein"), "klein")
        self.assertEqual(DEFAULT_WARDROBE_ENGINE, "qwen")

    def test_character_from_tags(self) -> None:
        sofie = SimpleNamespace(tag_set={"sofie", "ready-to-post"}, tags=[])
        eunbi = SimpleNamespace(tag_set={"eunbi"}, tags=[])
        self.assertEqual(character_for(sofie), "Sofie")
        self.assertEqual(character_for(eunbi), "Eunbi")


class ResolvePromptforgeHistoryIdTest(unittest.TestCase):
    def test_prefers_pf_colon_tag(self) -> None:
        item = SimpleNamespace(
            tags=["eunbi", "pf:1125"],
            tag_set=frozenset({"eunbi", "pf:1125"}),
            annotation="promptforge:999",
            name="image-888-zt",
            display_name="image-888-zt.png",
            path=Path("/lib/images/image-888-zt.png"),
        )
        self.assertEqual(resolve_promptforge_history_id(item), 1125)

    def test_pf_dash_tag(self) -> None:
        item = SimpleNamespace(
            tags=["pf-42"],
            tag_set=frozenset({"pf-42"}),
            annotation="",
            name="other",
            display_name="other.png",
            path=Path("/lib/other.png"),
        )
        self.assertEqual(resolve_promptforge_history_id(item), 42)

    def test_annotation_when_no_tag(self) -> None:
        item = SimpleNamespace(
            tags=["eunbi"],
            tag_set=frozenset({"eunbi"}),
            annotation="Built from promptforge:77 for set A",
            name="still",
            display_name="still.png",
            path=Path("/lib/still.png"),
        )
        self.assertEqual(resolve_promptforge_history_id(item), 77)

    def test_filename_image_prefix(self) -> None:
        item = SimpleNamespace(
            tags=[],
            tag_set=frozenset(),
            annotation="",
            name="image-1125-zt-eunbi5-075",
            display_name="image-1125-zt-eunbi5-075.png",
            path=Path("/lib/images/image-1125-zt-eunbi5-075.png"),
        )
        self.assertEqual(resolve_promptforge_history_id(item), 1125)

    def test_path_with_underscore_prefix(self) -> None:
        item = SimpleNamespace(
            tags=[],
            tag_set=frozenset(),
            annotation="",
            name="bust-klein-M50",
            display_name="bust-klein-M50.png",
            path=Path("/tmp/run_image-903-extra.png"),
        )
        self.assertEqual(resolve_promptforge_history_id(item), 903)

    def test_no_link_returns_none(self) -> None:
        item = SimpleNamespace(
            tags=["eunbi", "ready-to-post"],
            tag_set=frozenset({"eunbi", "ready-to-post"}),
            annotation="nice still",
            name="bust-klein-M50RKQ7XLHCIZ",
            display_name="bust-klein-M50RKQ7XLHCIZ.png",
            path=Path("/lib/bust-klein-M50RKQ7XLHCIZ.png"),
        )
        self.assertIsNone(resolve_promptforge_history_id(item))

    def test_build_url_honors_promptforge_url(self) -> None:
        with mock.patch.dict(os.environ, {"PROMPTFORGE_URL": "http://127.0.0.1:4001"}):
            self.assertEqual(promptforge_base_url(), "http://127.0.0.1:4001")
            self.assertEqual(
                promptforge_build_url(1125),
                "http://127.0.0.1:4001/build?id=1125",
            )


if __name__ == "__main__":
    unittest.main()
