"""Race coverage for query-cache invalidation vs in-flight queries."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path

from library import EagleLibrary, Item, SmartFolder


def item(number: int, *, tags: list[str] | None = None) -> Item:
    tag_list = list(tags or [])
    return Item(
        id=f"item-{number}",
        name=f"asset-{number}",
        ext="png",
        tags=tag_list,
        folders=[],
        path=Path(f"/tmp/asset-{number}.png"),
        thumb=None,
        is_deleted=False,
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


def tag_rule(tag: str) -> list[dict]:
    return [
        {
            "rules": [
                {
                    "property": "tags",
                    "method": "intersection",
                    "value": [tag],
                }
            ]
        }
    ]


class LibraryCacheRaceTest(unittest.TestCase):
    def test_stale_query_does_not_repopulate_after_invalidate(self) -> None:
        library = EagleLibrary("/tmp/not-used")
        library.items = [item(i) for i in range(40)]
        library.items_by_id = {it.id: it for it in library.items}

        entered = threading.Event()
        release = threading.Event()
        checks = 0
        worker_result: list[Item] = []
        worker_error: list[BaseException] = []

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            if checks == 3:
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("release never arrived")
            return False

        def worker() -> None:
            try:
                worker_result.extend(library.query(cancelled=cancelled))
            except BaseException as exc:  # noqa: BLE001
                worker_error.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(timeout=5), "query never reached mid-scan")

        with library._lock:  # noqa: SLF001
            library.items = []
            library.items_by_id = {}
        library._invalidate_caches()  # noqa: SLF001
        generation_after = library._cache_generation  # noqa: SLF001

        release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(worker_error, [])
        # In-flight caller may still see the pre-mutation snapshot.
        self.assertEqual(len(worker_result), 40)
        # But that snapshot must not re-enter the cache after invalidation.
        self.assertEqual(library._query_cache, {})  # noqa: SLF001
        self.assertEqual(library._cache_generation, generation_after)  # noqa: SLF001
        self.assertEqual(library.query(), [])

    def test_invalidate_methods_take_library_lock(self) -> None:
        library = EagleLibrary("/tmp/not-used")
        library.items = [item(0)]
        held = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def hold_lock() -> None:
            with library._lock:  # noqa: SLF001
                held.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.assertTrue(held.wait(timeout=5))

        def invalidate() -> None:
            library._invalidate_caches()  # noqa: SLF001
            finished.set()

        inv = threading.Thread(target=invalidate)
        inv.start()
        # While the library lock is held, invalidate must block.
        self.assertFalse(finished.wait(timeout=0.2))
        release.set()
        inv.join(timeout=5)
        holder.join(timeout=5)
        self.assertTrue(finished.is_set())
        self.assertFalse(inv.is_alive())

    def test_nested_smart_folder_query_survives_concurrent_invalidate(self) -> None:
        library = EagleLibrary("/tmp/not-used")
        library.items = [
            item(0, tags=["keep"]),
            item(1, tags=["keep", "child"]),
            item(2, tags=["other"]),
        ]
        library.items_by_id = {it.id: it for it in library.items}
        parent = SmartFolder(
            id="parent",
            name="Parent",
            conditions=tag_rule("keep"),
            inherited_conditions=tag_rule("keep"),
        )
        child = SmartFolder(
            id="child",
            name="Child",
            conditions=tag_rule("child"),
            inherited_conditions=tag_rule("keep") + tag_rule("child"),
            parent_id="parent",
        )
        library.smart_folders = [parent, child]
        library.smart_folders_by_id = {"parent": parent, "child": child}

        entered = threading.Event()
        release = threading.Event()
        checks = 0
        result: list[str] = []
        errors: list[BaseException] = []

        def cancelled() -> bool:
            nonlocal checks
            checks += 1
            # Pause while nested parent/child evaluation is underway.
            if checks == 2:
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("release never arrived")
            return False

        def worker() -> None:
            try:
                got = library.query(smart_folder_id="child", cancelled=cancelled)
                result.extend(it.id for it in got)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        library.invalidate_smart_folder_cache("child")
        release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        # Nested path must complete without deadlocking; stale publish blocked.
        self.assertNotIn(
            (
                None,
                "child",
                True,
                (),
                False,
                False,
                None,
            ),
            library._query_cache,  # noqa: SLF001
        )
        fresh = library.query(smart_folder_id="child")
        self.assertEqual([it.id for it in fresh], ["item-1"])
