"""Concurrency coverage for the replaceable desktop query queue."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from latest_job import LatestJobWorker


class LatestJobWorkerTest(unittest.TestCase):
    def test_replaces_pending_and_cancels_active_job(self) -> None:
        worker = LatestJobWorker(name="test-latest-job")
        active_started = threading.Event()
        release_active = threading.Event()
        finished = threading.Event()
        ran: list[str] = []
        active_cancelled: list[bool] = []

        def active(cancel: threading.Event) -> None:
            ran.append("active")
            active_started.set()
            release_active.wait(1)
            active_cancelled.append(cancel.is_set())

        def obsolete(_cancel: threading.Event) -> None:
            ran.append("obsolete")

        def latest(cancel: threading.Event) -> None:
            if not cancel.is_set():
                ran.append("latest")
            finished.set()

        try:
            worker.submit(active)
            self.assertTrue(active_started.wait(1))
            worker.submit(obsolete)
            worker.submit(latest)
            release_active.set()
            self.assertTrue(finished.wait(1))
        finally:
            worker.shutdown(wait=True)

        self.assertEqual(ran, ["active", "latest"])
        self.assertEqual(active_cancelled, [True])

    def test_shutdown_cancels_active_and_discards_pending(self) -> None:
        worker = LatestJobWorker(name="test-latest-job-shutdown")
        started = threading.Event()
        cancelled = threading.Event()
        pending_ran = threading.Event()

        def active(cancel: threading.Event) -> None:
            started.set()
            cancel.wait(1)
            if cancel.is_set():
                cancelled.set()

        try:
            worker.submit(active)
            self.assertTrue(started.wait(1))
            worker.submit(lambda _cancel: pending_ran.set())
            worker.shutdown(wait=True)
        finally:
            worker.shutdown(wait=True)

        self.assertTrue(cancelled.is_set())
        self.assertFalse(pending_ran.is_set())

    def test_failed_job_does_not_kill_worker(self) -> None:
        worker = LatestJobWorker(name="test-latest-job-error")
        failed = threading.Event()
        recovered = threading.Event()

        def bad_job(_cancel: threading.Event) -> None:
            failed.set()
            raise RuntimeError("query failed")

        try:
            with mock.patch("latest_job.traceback.print_exc"):
                worker.submit(bad_job)
                self.assertTrue(failed.wait(1))
                worker.submit(lambda _cancel: recovered.set())
                self.assertTrue(recovered.wait(1))
        finally:
            worker.shutdown(wait=True)
