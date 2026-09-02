"""Thumbnail cache bounds + decode waiter coalescing."""

from __future__ import annotations

import unittest

from thumb_cache import (
    ThumbTextureCache,
    ThumbWaiterRegistry,
    estimate_texture_bytes,
)


class ThumbWaiterRegistryTest(unittest.TestCase):
    def test_second_waiter_does_not_start_second_decode(self) -> None:
        reg = ThumbWaiterRegistry()
        self.assertTrue(reg.begin("k", "w1"))
        self.assertFalse(reg.begin("k", "w2"))
        self.assertEqual(reg.inflight_count, 1)
        waiters = reg.finish("k")
        self.assertEqual(waiters, ["w1", "w2"])
        self.assertEqual(reg.inflight_count, 0)

    def test_finish_unknown_key_is_safe(self) -> None:
        reg = ThumbWaiterRegistry()
        self.assertEqual(reg.finish("missing"), [])

    def test_discard_clears_inflight_and_waiters(self) -> None:
        reg = ThumbWaiterRegistry()
        reg.begin("k", "w")
        reg.discard("k")
        self.assertEqual(reg.inflight_count, 0)
        self.assertEqual(reg.finish("k"), [])


class ThumbCacheBoundsTest(unittest.TestCase):
    def test_stays_within_byte_budget_under_churn(self) -> None:
        budget = estimate_texture_bytes(64) * 5
        cache = ThumbTextureCache(byte_budget=budget)
        for i in range(40):
            cache.put(f"k{i}", f"tex-{i}", size=64)
            self.assertLessEqual(cache.bytes, budget)
        self.assertLessEqual(len(cache), 5)
        self.assertGreaterEqual(cache.evictions, 35)
