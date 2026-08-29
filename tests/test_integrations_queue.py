"""Unit tests for integrations enqueue helpers (no network)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from integrations_queue import (
    DEFAULT_BUST_ENGINE,
    DEFAULT_WARDROBE_ENGINE,
    character_for,
    normalize_bust_engine,
    normalize_wardrobe_engine,
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


if __name__ == "__main__":
    unittest.main()
