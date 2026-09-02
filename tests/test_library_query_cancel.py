"""Cooperative cancellation behavior for library scans."""

from __future__ import annotations

import unittest
from pathlib import Path

from library import EagleLibrary, Item, QueryCancelled


def item(number: int) -> Item:
    return Item(
        id=f"item-{number}",
        name=f"asset-{number}",
        ext="png",
        tags=[],
        folders=[],
        path=Path(f"/tmp/asset-{number}.png"),
        thumb=None,
        is_deleted=False,
        size=1,
        width=1,
        height=1,
        annotation="",
        modification_time=number,
        name_lower=f"asset-{number}",
        ext_lower="png",
    )


class LibraryQueryCancellationTest(unittest.TestCase):
    def test_cancelled_scan_does_not_publish_cache_entry(self) -> None:
        library = EagleLibrary("/tmp/not-used")
        library.items = [item(i) for i in range(20)]
        checks = 0

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            return checks > 5

        with self.assertRaises(QueryCancelled):
            library.query(cancelled=cancelled)

        self.assertEqual(library._query_cache, {})  # noqa: SLF001

    def test_uncancelled_query_still_caches_result(self) -> None:
        library = EagleLibrary("/tmp/not-used")
        library.items = [item(i) for i in range(3)]

        result = library.query(cancelled=lambda: False)

        self.assertEqual([it.id for it in result], ["item-0", "item-1", "item-2"])
        self.assertEqual(len(library._query_cache), 1)  # noqa: SLF001
