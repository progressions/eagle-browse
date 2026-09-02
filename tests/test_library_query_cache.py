"""Query-cache reuse, invalidation, and concurrent edit generations."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from library import EagleLibrary, Item


def item(number: int, *, tags: list[str] | None = None, deleted: bool = False) -> Item:
    tag_list = list(tags or [])
    return Item(
        id=f"item-{number}",
        name=f"asset-{number}",
        ext="png",
        tags=tag_list,
        folders=[],
        path=Path(f"/tmp/asset-{number}.png"),
        thumb=None,
        is_deleted=deleted,
        size=1,
        width=1,
        height=1,
        annotation="",
        modification_time=number,
        tag_set=frozenset(tag_list),
        folder_set=frozenset(),
        name_lower=f"asset-{number}",
        ext_lower="png",
    )


class LibraryQueryCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.library = EagleLibrary("/tmp/not-used-query-cache")
        self.library.items = [item(i, tags=["keep"] if i % 2 == 0 else ["other"]) for i in range(20)]
        self.library.items_by_id = {it.id: it for it in self.library.items}

    def test_identical_query_reuses_cached_list(self) -> None:
        first = self.library.query(include_deleted=False)
        second = self.library.query(include_deleted=False)
        self.assertIs(first, second)
        self.assertEqual(len(first), 20)

    def test_invalidate_forces_fresh_result_object(self) -> None:
        first = self.library.query(include_deleted=False)
        self.library._invalidate_caches()  # noqa: SLF001
        second = self.library.query(include_deleted=False)
        self.assertIsNot(first, second)
        self.assertEqual([it.id for it in first], [it.id for it in second])

    def test_mutation_under_lock_bumps_generation_and_drops_stale_publish(
        self,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        checks = 0
        worker_ids: list[str] = []

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            if checks == 2:
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("release never arrived")
            return False

        def worker() -> None:
            worker_ids.extend(
                it.id for it in self.library.query(cancelled=cancelled)
            )

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))

        # Concurrent edit: soft-delete everything and invalidate.
        with self.library._lock:  # noqa: SLF001
            for it in self.library.items:
                it.is_deleted = True
        gen_before = self.library._cache_generation  # noqa: SLF001
        self.library._invalidate_caches()  # noqa: SLF001
        self.assertGreater(self.library._cache_generation, gen_before)  # noqa: SLF001

        release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        # Stale worker may still return its snapshot, but must not cache it.
        self.assertEqual(self.library._query_cache, {})  # noqa: SLF001
        self.assertEqual(self.library.query(include_deleted=False), [])

    def test_search_queries_are_not_cached(self) -> None:
        a = self.library.query(search="asset-1")
        b = self.library.query(search="asset-1")
        self.assertEqual([it.id for it in a], [it.id for it in b])
        self.assertIsNot(a, b)
        self.assertEqual(self.library._query_cache, {})  # noqa: SLF001
