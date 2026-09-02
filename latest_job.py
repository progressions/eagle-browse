"""Single-worker queue that keeps only the latest pending job."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable

Job = Callable[[threading.Event], None]


class LatestJobWorker:
    """Run one job at a time and replace pending work with the newest request."""

    def __init__(self, *, name: str):
        self._condition = threading.Condition()
        self._pending: tuple[Job, threading.Event] | None = None
        self._active_cancel: threading.Event | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, job: Job) -> None:
        with self._condition:
            if self._closed:
                return
            if self._active_cancel is not None:
                self._active_cancel.set()
            if self._pending is not None:
                self._pending[1].set()
            cancel = threading.Event()
            self._pending = (job, cancel)
            self._condition.notify()

    def shutdown(self, *, wait: bool = False) -> None:
        with self._condition:
            self._closed = True
            if self._active_cancel is not None:
                self._active_cancel.set()
            if self._pending is not None:
                self._pending[1].set()
                self._pending = None
            self._condition.notify()
        if wait and self._thread is not threading.current_thread():
            self._thread.join()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                job, cancel = self._pending
                self._pending = None
                self._active_cancel = cancel
            try:
                if not cancel.is_set():
                    job(cancel)
            except Exception:  # noqa: BLE001
                # One failed query must not permanently kill the window's worker.
                traceback.print_exc()
            finally:
                with self._condition:
                    if self._active_cancel is cancel:
                        self._active_cancel = None
