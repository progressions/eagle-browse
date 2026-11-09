"""GSK renderer selection that must run before GTK initializes."""

from __future__ import annotations

import os
from collections.abc import MutableMapping

# GTK 4.24's default GL renderer (ngl / GskGLRenderer) SIGSEGVs in
# gsk_gl_device_find_gl_format when GStreamer gstgl steals the current EGL
# context during Gtk.Video preview. Vulkan uses a different GSK backend, so
# that race does not apply. cairo is the CPU fallback if the user sets it.
DEFAULT_GSK_RENDERER = "vulkan"


def configure_gsk_renderer(
    environ: MutableMapping[str, str] | None = None,
) -> str:
    """Ensure GSK_RENDERER is set before Gtk/Adw init.

    Honors an explicit GSK_RENDERER (cairo, gl, ngl, vulkan, …). When the
    variable is missing or blank, selects Vulkan.
    """
    env = os.environ if environ is None else environ
    current = (env.get("GSK_RENDERER") or "").strip()
    if current:
        env["GSK_RENDERER"] = current
        return current
    env["GSK_RENDERER"] = DEFAULT_GSK_RENDERER
    return DEFAULT_GSK_RENDERER
