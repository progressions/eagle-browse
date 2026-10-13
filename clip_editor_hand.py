"""Build clip-editor argv for Shift+E / Ctrl+Shift+E (#539)."""

from __future__ import annotations


def media_flag(*, is_video: bool, is_audio: bool) -> str | None:
    if is_video:
        return "--video"
    if is_audio:
        return "--audio"
    return None


def clip_editor_argv(
    exe: str,
    pairs: list[tuple[str, str]],
    *,
    new_project: bool = False,
) -> list[str]:
    """``pairs`` is ``(--video|--audio, path)`` in hand-off order."""
    cmd = [exe, "gui"]
    if new_project:
        cmd.append("--new")
    for flag, path in pairs:
        cmd.extend([flag, path])
    return cmd
