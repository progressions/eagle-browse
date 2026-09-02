"""Byte-bounded LRU thumbnail cache."""

from __future__ import annotations

import unittest

from thumb_cache import (
    ThumbTextureCache,
    best_reuse_size,
    estimate_texture_bytes,
)


class ThumbTextureCacheTest(unittest.TestCase):
    def test_estimate_rgba_square(self) -> None:
        self.assertEqual(estimate_texture_bytes(360), 360 * 360 * 4)
        self.assertEqual(estimate_texture_bytes(0), 0)

    def test_get_is_lru_hit(self) -> None:
        cache = ThumbTextureCache(byte_budget=estimate_texture_bytes(10) * 3)
        cache.put("a", "tex-a", size=10)
        cache.put("b", "tex-b", size=10)
        cache.put("c", "tex-c", size=10)
        self.assertEqual(cache.get("a"), "tex-a")  # a becomes most-recent
        cache.put("d", "tex-d", size=10)  # evicts LRU (b)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), "tex-a")
        self.assertEqual(cache.get("c"), "tex-c")
        self.assertEqual(cache.get("d"), "tex-d")
        self.assertGreaterEqual(cache.evictions, 1)

    def test_byte_budget_not_entry_count(self) -> None:
        # One large thumb exceeds budget alone after a second insert.
        small = 10
        large = 100
        budget = estimate_texture_bytes(large) + estimate_texture_bytes(small) // 2
        cache = ThumbTextureCache(byte_budget=budget)
        cache.put("small", "s", size=small)
        cache.put("large", "L", size=large)
        # small must be evicted to stay near budget
        self.assertIsNone(cache.get("small"))
        self.assertEqual(cache.get("large"), "L")
        self.assertLessEqual(cache.bytes, budget)

    def test_discard_other_zoom_sizes(self) -> None:
        cache = ThumbTextureCache(byte_budget=10_000_000)
        cache.put("p@72@1", "a", size=72)
        cache.put("p@160@1", "b", size=160)
        cache.put("q@160@1", "c", size=160)
        dropped = cache.discard_sizes_except(160)
        self.assertEqual(dropped, 1)
        self.assertIsNone(cache.get("p@72@1"))
        self.assertEqual(cache.get("p@160@1"), "b")
        self.assertEqual(cache.get("q@160@1"), "c")

    def test_metrics_expose_counters(self) -> None:
        cache = ThumbTextureCache(byte_budget=estimate_texture_bytes(10) * 2)
        cache.put("a", "A", size=10)
        cache.get("a")
        cache.get("missing")
        cache.put("b", "B", size=10)
        cache.put("c", "C", size=10)  # forces eviction
        m = cache.metrics(inflight=3)
        self.assertEqual(m.entries, len(cache))
        self.assertEqual(m.bytes, cache.bytes)
        self.assertEqual(m.byte_budget, cache.byte_budget)
        self.assertEqual(m.hits, 1)
        self.assertEqual(m.misses, 1)
        self.assertGreaterEqual(m.evictions, 1)
        self.assertEqual(m.inflight, 3)

    def test_replace_same_key_does_not_double_count_bytes(self) -> None:
        cache = ThumbTextureCache(byte_budget=1_000_000)
        cache.put("k", "old", size=50)
        before = cache.bytes
        cache.put("k", "new", size=50)
        self.assertEqual(cache.bytes, before)
        self.assertEqual(cache.get("k"), "new")
        self.assertEqual(len(cache), 1)

    def test_best_reuse_size_picks_largest_sufficient(self) -> None:
        self.assertEqual(best_reuse_size(180, [72, 160, 192, 360]), 360)
        self.assertEqual(best_reuse_size(180, [72, 160]), None)
        self.assertEqual(best_reuse_size(160, [160]), 160)
        self.assertIsNone(best_reuse_size(180, []))
