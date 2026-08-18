"""Video in/out marks and ffmpeg trim for Eagle Browse.

Marks live in images/<id>.info/eagle-browse.json (not Eagle metadata).
Trim always writes a new untagged H.264/AAC item.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from audio_crop import MIN_CROP_S, format_time  # noqa: F401
from write import (
    WriteError,
    announce_imported_ids,
    atomic_write_json,
    backup_file,
    write_session,
)

MARKS_FILENAME = "eagle-browse.json"
_UNSET = object()


def _compact_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    frac = int(round((seconds - whole) * 100))
    if frac >= 100:
        whole += 1
        frac = 0
    minutes, secs = divmod(whole, 60)
    return f"{minutes}m{secs:02d}.{frac:02d}s"


def marks_path(item: Any) -> Path:
    item_dir = getattr(item, "item_dir", None)
    if item_dir is None:
        raise WriteError("No item directory")
    return Path(item_dir) / MARKS_FILENAME


def load_marks(item: Any) -> dict[str, float]:
    """Return present marks only: ``{"in": float}`` and/or ``{"out": float}``."""
    item_dir = getattr(item, "item_dir", None)
    if item_dir is None:
        return {}
    path = Path(item_dir) / MARKS_FILENAME
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key in ("in", "out"):
        if key not in raw or raw[key] is None:
            continue
        try:
            val = float(raw[key])
        except (TypeError, ValueError):
            continue
        if val >= 0:
            out[key] = val
    return out


def save_marks(
    item: Any,
    *,
    start: float | None | object = _UNSET,
    end: float | None | object = _UNSET,
) -> dict[str, float]:
    """Merge *start*/*end* into the sidecar. None clears that side."""
    item_dir = getattr(item, "item_dir", None)
    if item_dir is None:
        raise WriteError("No item directory")
    data = load_marks(item)
    if start is not _UNSET:
        if start is None:
            data.pop("in", None)
        else:
            data["in"] = max(0.0, float(start))
    if end is not _UNSET:
        if end is None:
            data.pop("out", None)
        else:
            data["out"] = max(0.0, float(end))
    path = Path(item_dir) / MARKS_FILENAME
    if not data:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return {}
    atomic_write_json(path, data)
    return data


def clear_marks(item: Any) -> None:
    item_dir = getattr(item, "item_dir", None)
    if item_dir is None:
        return
    path = Path(item_dir) / MARKS_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _probe_video_codec(path: Path) -> str:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "csv=p=0",
                str(path),
            ],
            timeout=30,
            text=True,
        )
        return (out or "").strip().splitlines()[0].strip() if out.strip() else ""
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, IndexError):
        return ""


def clamp_range(start: float, end: float, duration: float) -> tuple[float, float]:
    total = max(0.0, float(duration or 0))
    start = max(0.0, float(start))
    end = max(0.0, float(end))
    if total > 0:
        start = min(start, max(0.0, total - MIN_CROP_S))
        end = min(end, total)
    if end - start < MIN_CROP_S:
        raise WriteError("Need in and out at least 0.05s apart")
    if total > 0 and start <= 0.001 and end >= total - 0.001:
        raise WriteError("Range matches the full file — nothing to do")
    return start, end


def write_video_segment(src: Path, dest: Path, start: float, end: float) -> None:
    """Re-encode [start, end] of *src* to Buffer-safe H.264/AAC *dest*."""
    start = max(0.0, float(start))
    end = max(start + MIN_CROP_S, float(end))
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = dest.with_name(f".{dest.name}.ff.mp4")
    if work.exists():
        work.unlink()
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(work),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=600,
            text=True,
        )
    except FileNotFoundError as exc:
        raise WriteError("ffmpeg is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        if work.exists():
            work.unlink(missing_ok=True)
        raise WriteError("ffmpeg timed out trimming the video") from exc
    if proc.returncode != 0 or not work.is_file() or work.stat().st_size <= 0:
        err = (proc.stderr or "").strip()
        if work.exists():
            work.unlink(missing_ok=True)
        detail = err.splitlines()[-1] if err else "encode failed"
        raise WriteError(f"ffmpeg trim failed · {detail}")
    codec = _probe_video_codec(work)
    if codec != "h264":
        work.unlink(missing_ok=True)
        raise WriteError(f"trim output is {codec or 'unknown'}, not h264")
    os.replace(work, dest)


def save_video_trim_as_new_item(
    library_root: Path,
    item: Any,
    start: float,
    end: float,
) -> Any:
    """Write [start, end] as a new untagged / uncategorized H.264 item."""
    from import_media import (
        _make_video_thumbnail,
        _now_ms,
        _unique_item_dir,
        _video_meta,
    )
    from library import Item

    src = Path(getattr(item, "path", "") or "")
    if not src.is_file():
        raise WriteError(f"Video file missing: {src}")
    if not getattr(item, "is_video", False):
        raise WriteError("Not a video item")

    vw, vh, duration = _video_meta(src)
    if duration <= 0:
        duration = float(getattr(item, "duration", 0) or 0)
    start, end = clamp_range(start, end, duration)

    stem = str(getattr(item, "name", None) or src.stem).replace("/", "-").replace("\\", "-")
    stem = (stem or "clip").replace("%", "_").strip() or "clip"
    stem = f"{stem}-cut-{_compact_time(start)}-{_compact_time(end)}"
    ext = "mp4"

    with write_session(library_root):
        images_dir = Path(library_root) / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        iid, item_dir = _unique_item_dir(images_dir)
        try:
            item_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise WriteError(f"Item dir already exists: {item_dir}") from exc

        dest_media = item_dir / f"{stem}.{ext}"
        try:
            write_video_segment(src, dest_media, start, end)
        except Exception:
            try:
                if dest_media.is_file():
                    dest_media.unlink()
                item_dir.rmdir()
            except OSError:
                pass
            raise

        new_w, new_h, new_dur = _video_meta(dest_media)
        if new_w <= 0:
            new_w, new_h = int(getattr(item, "width", 0) or vw), int(
                getattr(item, "height", 0) or vh
            )
        new_size = dest_media.stat().st_size
        thumb_path = item_dir / f"{stem}_thumbnail.png"
        thumb_ok = _make_video_thumbnail(dest_media, thumb_path)
        now = _now_ms()
        meta: dict[str, Any] = {
            "id": iid,
            "name": stem,
            "size": new_size,
            "btime": now,
            "mtime": now,
            "ext": ext,
            "tags": [],
            "folders": [],
            "isDeleted": False,
            "url": "",
            "annotation": "",
            "modificationTime": now,
            "width": new_w,
            "height": new_h,
            "lastModified": now,
            "palettes": [],
            "duration": new_dur,
            "resolutionWidth": new_w,
            "resolutionHeight": new_h,
        }
        atomic_write_json(item_dir / "metadata.json", meta)

        mtime_path = Path(library_root) / "mtime.json"
        if mtime_path.is_file():
            try:
                with mtime_path.open("r", encoding="utf-8") as f:
                    mt = json.load(f)
                if isinstance(mt, dict):
                    mt[iid] = now
                    backup_file(library_root, mtime_path)
                    atomic_write_json(mtime_path, mt)
            except (OSError, json.JSONDecodeError):
                pass

        announce_imported_ids(library_root, [iid])

    return Item(
        id=iid,
        name=stem,
        ext=ext,
        tags=[],
        folders=[],
        path=dest_media.resolve(),
        thumb=thumb_path.resolve() if thumb_ok else None,
        is_deleted=False,
        size=new_size,
        width=new_w,
        height=new_h,
        annotation="",
        modification_time=now,
        btime=now,
        star=None,
        duration=new_dur or None,
        item_dir=item_dir.resolve(),
        tag_set=frozenset(),
        folder_set=frozenset(),
        name_lower=stem.lower(),
        ext_lower=ext.lower(),
    )
