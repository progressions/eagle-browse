"""GSK renderer selection that must run before GTK initializes."""

from __future__ import annotations

import os
import re
from collections.abc import MutableMapping

# Vulkan alone still lets Gtk.MediaFile create GL textures. Their cleanup can
# abort in libepoxy when no EGL/GLX context is current. Disable GDK GL as well
# so video frames use the non-GL path. Keep Vulkan for accelerated UI drawing.
DEFAULT_GSK_RENDERER = "vulkan"


def configure_gsk_renderer(
    environ: MutableMapping[str, str] | None = None,
) -> str:
    """Choose a renderer and disable video GL interop before GTK initializes.

    Explicit GL renderers opt out of the workaround. Existing GDK_DISABLE
    features are preserved; repeated configuration is harmless.
    """
    env = os.environ if environ is None else environ
    renderer = (env.get("GSK_RENDERER") or "").strip() or DEFAULT_GSK_RENDERER
    env["GSK_RENDERER"] = renderer
    if renderer not in {"gl", "ngl", "opengl"}:
        disabled = env.get("GDK_DISABLE", "")
        tokens = re.split(r"[:,\s]+", disabled.strip())
        if "gl" not in tokens and "all" not in tokens:
            env["GDK_DISABLE"] = f"{disabled},gl" if disabled.strip() else "gl"
    return renderer
