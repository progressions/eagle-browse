"""Shutdown gate for worker → UI idle callbacks (#525)."""

from __future__ import annotations

import threading
import unittest

from shutdown_gate import idle_allowed, wrap_idle_callback


class ShutdownGateTest(unittest.TestCase):
    def test_idle_allowed_tracks_event(self) -> None:
        ev = threading.Event()
        self.assertTrue(idle_allowed(ev))
        ev.set()
        self.assertFalse(idle_allowed(ev))

    def test_wrap_skips_callback_after_shutdown(self) -> None:
        ev = threading.Event()
        ran: list[int] = []

        def cb() -> bool:
            ran.append(1)
            return False

        wrapped = wrap_idle_callback(ev, cb)
        self.assertFalse(wrapped())
        self.assertEqual(ran, [1])
        ev.set()
        self.assertFalse(wrapped())
        self.assertEqual(ran, [1])

    def test_wrap_passes_args_when_allowed(self) -> None:
        ev = threading.Event()
        seen: list[tuple] = []

        def cb(a: int, b: str) -> bool:
            seen.append((a, b))
            return False

        wrap_idle_callback(ev, cb)(3, "x")
        self.assertEqual(seen, [(3, "x")])
