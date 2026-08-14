"""Decode images from file bytes so GdkPixbuf cannot return a stale path cache."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gio", "2.0")

from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402


def pixbuf_from_path(
    path: str | Path,
    max_w: int | None = None,
    max_h: int | None = None,
) -> GdkPixbuf.Pixbuf | None:
    """Read *path* into a pixbuf via a memory stream, not the filename cache."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not data:
        return None
    try:
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
        if max_w and max_h:
            return GdkPixbuf.Pixbuf.new_from_stream_at_scale(
                stream, int(max_w), int(max_h), True, None
            )
        return GdkPixbuf.Pixbuf.new_from_stream(stream, None)
    except Exception:
        return None
