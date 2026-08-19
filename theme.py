"""Map the current Omarchy theme onto libadwaita / GTK named colors.

Omarchy itself only flips Adwaita light vs dark. This module reads
``~/.local/state/omarchy/current/theme/colors.toml`` and overrides
libadwaita CSS variables so the window follows accent, background, and
foreground of the active theme.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

THEME_DIR = Path.home() / ".local" / "state" / "omarchy" / "current" / "theme"
COLORS_FILE = THEME_DIR / "colors.toml"

# Resolved hex strings, e.g. {"background": "#060B1E", "mode": "dark"}
PALETTE: dict[str, str] = {}

_provider: Any = None
_monitor: Any = None


def _parse_colors(path: Path) -> dict[str, str]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        return {}
    out: dict[str, str] = {}
    for key, val in raw.items():
        if isinstance(val, str) and val.strip():
            out[str(key)] = val.strip()
    return out


def _hex_rgb(color: str) -> tuple[float, float, float] | None:
    h = (color or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
    except ValueError:
        return None
    return r / 255.0, g / 255.0, b / 255.0


def _luminance(color: str) -> float:
    rgb = _hex_rgb(color)
    if rgb is None:
        return 0.0

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _on_color(bg: str) -> str:
    return "#1a1a1a" if _luminance(bg) > 0.55 else "#ffffff"


def cairo_rgb(name: str, fallback: tuple[float, float, float] = (1.0, 1.0, 1.0)):
    rgb = _hex_rgb(PALETTE.get(name) or "")
    return rgb if rgb is not None else fallback


def cairo_rgba(
    name: str,
    alpha: float = 1.0,
    fallback: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> tuple[float, float, float, float]:
    r, g, b = cairo_rgb(name, fallback)
    return r, g, b, alpha


def _pick(colors: dict[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        val = colors.get(key)
        if val:
            return val
    return default


def build_css(colors: dict[str, str]) -> tuple[bytes, str]:
    """Return (css, mode) for the given palette."""
    mode = (colors.get("mode") or colors.get("theme_type") or "dark").lower()
    if mode not in ("dark", "light"):
        mode = "dark"
    bg = _pick(colors, "background", "bg", default="#1e1e2e")
    dark_bg = _pick(colors, "dark_background", "dark_bg", default=bg)
    darker = _pick(colors, "darker_background", "darker_bg", default=dark_bg)
    lighter = _pick(colors, "lighter_background", "lighter_bg", default=bg)
    fg = _pick(colors, "foreground", "fg", default="#cdd6f4")
    muted = _pick(colors, "muted", "dark_foreground", "dark_fg", default=fg)
    accent = _pick(colors, "accent", "blue", default="#89b4fa")
    selection = _pick(colors, "selection", default=accent)
    red = _pick(colors, "red", default="#e64553")
    green = _pick(colors, "green", default="#40a02b")
    yellow = _pick(colors, "yellow", default="#df8e1d")
    accent_fg = _on_color(accent)
    window_fg_on = _on_color(bg)

    PALETTE.clear()
    PALETTE.update(colors)
    PALETTE.update(
        {
            "mode": mode,
            "background": bg,
            "dark_background": dark_bg,
            "darker_background": darker,
            "lighter_background": lighter,
            "foreground": fg,
            "muted": muted,
            "accent": accent,
            "selection": selection,
            "red": red,
            "green": green,
            "yellow": yellow,
        }
    )

    css = f"""
    :root {{
      --accent-bg-color: {accent};
      --accent-color: {accent};
      --accent-fg-color: {accent_fg};
      --destructive-bg-color: {red};
      --destructive-color: {red};
      --destructive-fg-color: {_on_color(red)};
      --success-bg-color: {green};
      --success-color: {green};
      --success-fg-color: {_on_color(green)};
      --warning-bg-color: {yellow};
      --warning-color: {yellow};
      --warning-fg-color: {_on_color(yellow)};
      --error-bg-color: {red};
      --error-color: {red};
      --error-fg-color: {_on_color(red)};
      --window-bg-color: {bg};
      --window-fg-color: {fg};
      --view-bg-color: {dark_bg};
      --view-fg-color: {fg};
      --headerbar-bg-color: {darker};
      --headerbar-fg-color: {fg};
      --headerbar-backdrop-color: {bg};
      --headerbar-border-color: {lighter};
      --sidebar-bg-color: {darker};
      --sidebar-fg-color: {fg};
      --secondary-sidebar-bg-color: {dark_bg};
      --secondary-sidebar-fg-color: {fg};
      --card-bg-color: {lighter};
      --card-fg-color: {fg};
      --dialog-bg-color: {dark_bg};
      --dialog-fg-color: {fg};
      --popover-bg-color: {lighter};
      --popover-fg-color: {fg};
    }}
    @define-color accent_bg_color {accent};
    @define-color accent_fg_color {accent_fg};
    @define-color accent_color {accent};
    @define-color theme_bg_color {bg};
    @define-color theme_fg_color {fg};
    @define-color theme_base_color {dark_bg};
    @define-color theme_text_color {fg};
    @define-color theme_selected_bg_color {selection};
    @define-color theme_selected_fg_color {window_fg_on if _luminance(selection) < 0.45 else fg};
    @define-color insensitive_fg_color {muted};
    @define-color borders {lighter};
    """.encode()
    return css, mode


def apply_omarchy_theme() -> None:
    """Push the Omarchy palette into GTK. No-op off Omarchy."""
    global _provider
    from gi.repository import Adw, Gdk, Gtk  # noqa: PLC0415

    if not COLORS_FILE.is_file():
        return
    colors = _parse_colors(COLORS_FILE)
    if not colors:
        return
    css, mode = build_css(colors)
    sm = Adw.StyleManager.get_default()
    sm.set_color_scheme(
        Adw.ColorScheme.FORCE_LIGHT if mode == "light" else Adw.ColorScheme.FORCE_DARK
    )
    display = Gdk.Display.get_default()
    if display is None:
        return
    if _provider is None:
        _provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            display,
            _provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER,
        )
    _provider.load_from_data(css)
    _watch()


def _watch() -> None:
    global _monitor
    if _monitor is not None:
        return
    from gi.repository import Gio, GLib  # noqa: PLC0415

    try:
        gfile = Gio.File.new_for_path(str(COLORS_FILE))
        _monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
    except Exception:  # noqa: BLE001
        return

    def on_changed(*_a: object) -> None:
        GLib.idle_add(apply_omarchy_theme)

    _monitor.connect("changed", on_changed)
