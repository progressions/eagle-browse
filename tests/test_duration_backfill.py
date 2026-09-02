"""Duration backfill must probe outside the library write lock."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from library import DURATION_PROBE_MAX_FAILURES, EagleLibrary, Item
from write import LOCK_FILENAME as WRITE_LOCK_FILENAME


def _make_media_item(root: Path, item_id: str, *, ext: str = "mp4") -> Item:
    item_dir = root / "images" / f"{item_id}.info"
    item_dir.mkdir(parents=True)
    media = item_dir / f"{item_id}.{ext}"
    media.write_bytes(b"fake-media")
    metadata = {
        "id": item_id,
        "name": item_id,
        "ext": ext,
        "tags": [],
        "folders": [],
        "isDeleted": False,
        "annotation": "",
        "modificationTime": 1,
    }
    (item_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return Item(
        id=item_id,
        name=item_id,
        ext=ext,
        tags=[],
        folders=[],
        path=media,
        thumb=None,
        is_deleted=False,
        size=media.stat().st_size,
        width=0,
        height=0,
        annotation="",
        modification_time=1,
        item_dir=item_dir,
        name_lower=item_id,
        ext_lower=ext,
    )


class DurationBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "images").mkdir()
        (self.root / "metadata.json").write_text("{}", encoding="utf-8")
        self.library = EagleLibrary(self.root)
        self.skip_path = Path(self.temp.name) / "duration-probe-skips.json"
        self._skip_patch = mock.patch(
            "library._DURATION_SKIP_STATE", self.skip_path
        )
        self._skip_patch.start()
        self.addCleanup(self._skip_patch.stop)

    def test_probe_runs_without_write_lock(self) -> None:
        item = _make_media_item(self.root, "vid1")
        self.library.items = [item]
        self.library.items_by_id = {item.id: item}
        lock_path = self.root / WRITE_LOCK_FILENAME
        seen_lock: list[bool] = []

        def probe(path: Path) -> tuple[int, int, float]:
            seen_lock.append(lock_path.exists())
            return 0, 0, 12.5

        batch = self.library.backfill_missing_durations(
            limit=10, time_budget_s=None, probe_fn=probe
        )

        self.assertEqual(seen_lock, [False])
        self.assertEqual(batch.written, [("vid1", 12.5)])
        self.assertEqual(item.duration, 12.5)
        data = json.loads((item.item_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(data["duration"], 12.5)
        # Backfill must not bump Eagle modificationTime.
        self.assertEqual(data["modificationTime"], 1)

    def test_lock_hold_time_excludes_probe_cost(self) -> None:
        items = [_make_media_item(self.root, f"vid{i}") for i in range(3)]
        self.library.items = items
        self.library.items_by_id = {it.id: it for it in items}
        hold_times: list[float] = []

        def slow_probe(path: Path) -> tuple[int, int, float]:
            time.sleep(0.04)
            return 0, 0, 3.0

        batch = self.library.backfill_missing_durations(
            limit=3,
            time_budget_s=None,
            probe_fn=slow_probe,
            lock_hold_times=hold_times,
        )

        self.assertEqual(len(batch.written), 3)
        self.assertEqual(len(hold_times), 1)
        # Three 40ms probes would be ~120ms if done under the lock.
        self.assertLess(hold_times[0], 0.08)

    def test_revalidation_skips_item_that_gained_duration(self) -> None:
        item = _make_media_item(self.root, "vid2")
        self.library.items = [item]
        self.library.items_by_id = {item.id: item}

        def probe(path: Path) -> tuple[int, int, float]:
            item.duration = 9.0  # concurrent fill before write batch
            return 0, 0, 4.0

        batch = self.library.backfill_missing_durations(
            limit=1, time_budget_s=None, probe_fn=probe
        )

        self.assertEqual(batch.written, [])
        self.assertEqual(item.duration, 9.0)
        data = json.loads((item.item_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertNotIn("duration", data)

    def test_limit_leaves_remaining_for_resume(self) -> None:
        items = [_make_media_item(self.root, f"vid{i}") for i in range(5)]
        self.library.items = items
        self.library.items_by_id = {it.id: it for it in items}

        first = self.library.backfill_missing_durations(
            limit=2, time_budget_s=None, probe_fn=lambda _p: (0, 0, 1.5)
        )
        self.assertEqual(len(first.written), 2)
        self.assertEqual(first.probed, 2)
        self.assertEqual(first.remaining, 3)

        second = self.library.backfill_missing_durations(
            limit=10, time_budget_s=None, probe_fn=lambda _p: (0, 0, 1.5)
        )
        self.assertEqual(len(second.written), 3)
        self.assertEqual(second.remaining, 0)

    def test_repeated_failures_backoff_then_durable_skip(self) -> None:
        item = _make_media_item(self.root, "bad")
        self.library.items = [item]
        self.library.items_by_id = {item.id: item}
        clock = {"t": 1000.0}

        def mono() -> float:
            return clock["t"]

        for i in range(DURATION_PROBE_MAX_FAILURES - 1):
            batch = self.library.backfill_missing_durations(
                limit=1,
                time_budget_s=None,
                probe_fn=lambda _p: (0, 0, 0.0),
                monotonic=mono,
            )
            self.assertEqual(batch.written, [])
            self.assertEqual(batch.probed, 1)
            # Still in backoff — not eligible until clock advances.
            self.assertEqual(batch.remaining, 0)
            clock["t"] += 10_000.0

        final = self.library.backfill_missing_durations(
            limit=1,
            time_budget_s=None,
            probe_fn=lambda _p: (0, 0, 0.0),
            monotonic=mono,
        )
        self.assertEqual(final.probed, 1)
        self.assertEqual(final.remaining, 0)
        self.assertTrue(self.skip_path.is_file())
        payload = json.loads(self.skip_path.read_text(encoding="utf-8"))
        self.assertIn(item.id, payload[str(self.root.resolve())])

        # Durable skip survives a fresh library object on the same root.
        other = EagleLibrary(self.root)
        other.items = [item]
        other.items_by_id = {item.id: item}
        skipped = other.backfill_missing_durations(
            limit=1,
            time_budget_s=None,
            probe_fn=lambda _p: (0, 0, 99.0),
            monotonic=mono,
        )
        self.assertEqual(skipped.probed, 0)
        self.assertEqual(skipped.written, [])

    def test_imports_can_take_lock_during_probe(self) -> None:
        item = _make_media_item(self.root, "vid3")
        self.library.items = [item]
        self.library.items_by_id = {item.id: item}
        entered = threading.Event()
        release = threading.Event()
        writer_ok = threading.Event()
        errors: list[BaseException] = []

        def probe(path: Path) -> tuple[int, int, float]:
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("release never arrived")
            return 0, 0, 2.0

        def writer() -> None:
            try:
                self.assertTrue(entered.wait(timeout=5))
                from write import write_session

                with write_session(self.root):
                    writer_ok.set()
                release.set()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                release.set()

        thread = threading.Thread(target=writer)
        thread.start()
        batch = self.library.backfill_missing_durations(
            limit=1, time_budget_s=None, probe_fn=probe
        )
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(writer_ok.is_set())
        self.assertEqual(batch.written, [("vid3", 2.0)])
