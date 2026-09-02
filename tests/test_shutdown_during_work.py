"""Shutdown / cancel while background library work is in flight."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from latest_job import LatestJobWorker
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
        tag_set=frozenset(),
        folder_set=frozenset(),
        name_lower=f"asset-{number}",
        ext_lower="png",
    )


class ShutdownDuringWorkTest(unittest.TestCase):
    def test_query_cancel_mid_scan(self) -> None:
        library = EagleLibrary("/tmp/not-used-shutdown")
        library.items = [item(i) for i in range(80)]
        library.items_by_id = {it.id: it for it in library.items}
        entered = threading.Event()
        checks = 0

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            if checks == 3:
                entered.set()
                return True
            return False

        with self.assertRaises(QueryCancelled):
            library.query(cancelled=cancelled)
        self.assertTrue(entered.is_set())
        self.assertEqual(library._query_cache, {})  # noqa: SLF001

    def test_latest_job_shutdown_while_active(self) -> None:
        worker = LatestJobWorker(name="test-shutdown-526")
        started = threading.Event()
        finished = threading.Event()

        def job(cancel: threading.Event) -> None:
            started.set()
            while not cancel.wait(timeout=0.05):
                pass
            finished.set()

        try:
            worker.submit(job)
            self.assertTrue(started.wait(timeout=5))
            worker.shutdown(wait=True)
            self.assertTrue(finished.wait(timeout=5))
        finally:
            worker.shutdown(wait=True)
