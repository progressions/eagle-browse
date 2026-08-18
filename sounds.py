"""Eagle UI sounds. Shared by the GTK app and the headless inbox watcher.

Import-success chimes are deduped across processes so a watcher import plus
the open browser ingest does not play twice.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"
STATE_DIR = Path.home() / ".local" / "state" / "eagle-browse"
_GUI_PID = STATE_DIR / "gui.pid"
_LAST_SOUND = STATE_DIR / "last-sound.json"
_ONCE_WINDOW_S = 2.0


def _sound_path(name: str) -> Path | None:
    # notification_play.wav is the louder stereo chime. Keep it one ding —
    # a later copy concatenated two hits 0.4s apart.
    if name == "notification":
        boosted = SOUNDS_DIR / "notification_play.wav"
        if boosted.is_file():
            return boosted
    path = SOUNDS_DIR / f"{name}.wav"
    return path if path.is_file() else None


def mark_gui_running() -> None:
    """Record that Eagle Browse is open (inbox-watch skips its import chime)."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _GUI_PID.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def mark_gui_stopped() -> None:
    try:
        if _GUI_PID.is_file() and _GUI_PID.read_text(encoding="utf-8").strip() == str(
            os.getpid()
        ):
            _GUI_PID.unlink()
    except OSError:
        pass


def gui_is_running() -> bool:
    try:
        pid = int(_GUI_PID.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claimed_recent(name: str) -> bool:
    """True if this name was played within the debounce window (claim if not)."""
    now = time.time()
    try:
        raw = json.loads(_LAST_SOUND.read_text(encoding="utf-8"))
        if (
            isinstance(raw, dict)
            and raw.get("name") == name
            and now - float(raw.get("ts") or 0) < _ONCE_WINDOW_S
        ):
            return True
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _LAST_SOUND.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"name": name, "ts": now}) + "\n", encoding="utf-8"
        )
        tmp.replace(_LAST_SOUND)
    except OSError:
        pass
    return False


def play_sound(name: str = "notification", *, once: bool = False) -> None:
    """Play a short WAV out-of-process. *once* skips if just played (any process)."""
    if once and _claimed_recent(name):
        return
    path = _sound_path(name)
    if path is None:
        return
    path_s = str(path)
    env = os.environ.copy()
    players: list[list[str]] = []
    if shutil.which("canberra-gtk-play"):
        players.append(["canberra-gtk-play", "-f", path_s])
    if shutil.which("pw-play"):
        players.append(["pw-play", path_s])
    if shutil.which("paplay"):
        players.append(["paplay", path_s])
    if shutil.which("mpv"):
        players.append(
            [
                "mpv",
                "--no-video",
                "--really-quiet",
                "--volume=150",
                "--audio-display=no",
                path_s,
            ]
        )
    if shutil.which("ffplay"):
        players.append(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path_s]
        )
    for cmd in players:
        try:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
            return
        except (FileNotFoundError, OSError):
            continue
