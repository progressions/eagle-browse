"""Window-owned shutdown gate for background → UI handoff (#525)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


def idle_allowed(shutdown: threading.Event) -> bool:
    """True when UI callbacks from workers may still run."""
    return not shutdown.is_set()


def wrap_idle_callback(
    shutdown: threading.Event,
    callback: Callable[..., Any],
) -> Callable[..., bool]:
    """Return an idle callback that no-ops once *shutdown* is set.

    GLib idle handlers should return False to not reschedule. Any truthy
    return from *callback* is preserved when shutdown has not begun.
    """

    def wrapped(*args: Any) -> bool:
        if shutdown.is_set():
            return False
        result = callback(*args)
        return bool(result) if result is not None else False

    return wrapped
