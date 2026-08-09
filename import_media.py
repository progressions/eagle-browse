"""Import files from an inbox into an Eagle.cool library."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from write import WriteError, atomic_write_json, backup_file, write_session

# Default: user's Dropbox Eunbi inbox (watch/import only — no auto folder/tags)
DEFAULT_INBOX = Path.home() / "Dropbox/ISAAC/GENNIE/Eunbi/PICS/Eunbi"
# Imports go in uncategorized / untagged unless the caller passes folders/tags.
DEFAULT_FOLDER_ID: str | None = None
DEFAULT_TAGS: list[str] = []

ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".aiff", ".aif"}
SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}


@dataclass
class ImportResult:
    source: Path
    item_id: str | None = None
    ok: bool = False
    skipped: bool = False
    error: str | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def new_eagle_id() -> str:
    """13-char id similar to Eagle: M + 12 alphanumeric uppercase."""
    return "M" + "".join(secrets.choice(ID_ALPHABET) for _ in range(12))


def is_importable(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name.startswith("."):
        return False
    if path.name.lower() in SKIP_NAMES:
        return False
    if path.suffix.lower() not in IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS:
        return False
    # Ignore incomplete downloads
    if path.suffix.lower() in {".crdownload", ".part", ".tmp"}:
        return False
    return True


def list_inbox_files(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    files = [p for p in inbox.iterdir() if is_importable(p)]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files


def _unique_item_dir(images_dir: Path) -> tuple[str, Path]:
    for _ in range(64):
        iid = new_eagle_id()
        d = images_dir / f"{iid}.info"
        if not d.exists():
            return iid, d
    raise WriteError("Could not allocate unique item id")


def _file_times_ms(path: Path) -> tuple[int, int]:
    st = path.stat()
    # ns if available
    mtime = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)) / 1_000_000)
    btime = mtime
    if hasattr(st, "st_birthtime"):
        btime = int(st.st_birthtime * 1000)
    return btime, mtime


def _image_size(path: Path) -> tuple[int, int]:
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib

        pb = GdkPixbuf.Pixbuf.new_from_file(str(path))
        return int(pb.get_width()), int(pb.get_height())
    except Exception:
        pass
    # ImageMagick identify
    try:
        out = subprocess.check_output(
            ["identify", "-format", "%w %h", str(path)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        w, h = out.strip().split()
        return int(w), int(h)
    except Exception:
        return 0, 0


def _make_image_thumbnail(src: Path, dest: Path, max_edge: int = 400) -> bool:
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(src), max_edge, max_edge)
        # Always write PNG thumb like Eagle often does
        pb.savev(str(dest), "png", [], [])
        return dest.is_file()
    except Exception:
        pass
    try:
        subprocess.check_call(
            [
                "convert",
                str(src),
                "-thumbnail",
                f"{max_edge}x{max_edge}>",
                str(dest),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        return dest.is_file()
    except Exception:
        return False


def _video_meta(path: Path) -> tuple[int, int, float]:
    """width, height, duration seconds."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(path),
            ],
            text=True,
            timeout=60,
        )
        data = json.loads(out)
        w = h = 0
        duration = 0.0
        for s in data.get("streams") or []:
            if s.get("codec_type") == "video":
                w = int(s.get("width") or 0)
                h = int(s.get("height") or 0)
                break
        fmt = data.get("format") or {}
        try:
            duration = float(fmt.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        return w, h, duration
    except Exception:
        return 0, 0, 0.0


def _make_video_thumbnail(src: Path, dest: Path) -> bool:
    try:
        subprocess.check_call(
            [
                "ffmpeg",
                "-y",
                "-ss",
                "1",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                "scale=400:-1",
                str(dest),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def _make_audio_thumbnail(dest: Path, size: int = 400) -> bool:
    """Simple solid PNG placeholder for audio (no pillow required)."""
    # Minimal 1x1 PNG expanded via convert if available
    try:
        subprocess.check_call(
            [
                "convert",
                "-size",
                f"{size}x{size}",
                "xc:#2a2a2a",
                "-fill",
                "#888888",
                "-gravity",
                "center",
                "-pointsize",
                "48",
                "-annotate",
                "0",
                "♪",
                str(dest),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return dest.is_file()
    except Exception:
        # Tiny valid PNG fallback
        try:
            # 1x1 dark pixel PNG
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
                "de0000000c4944415408d763606460000000020001e221bc330000000049454e44ae426082"
            )
            dest.write_bytes(png)
            return True
        except OSError:
            return False


def import_file(
    library_root: Path,
    source: Path,
    *,
    folder_ids: list[str] | None = None,
    tags: list[str] | None = None,
    move_source: bool = True,
    hold_lock: bool = False,
) -> ImportResult:
    """
    Copy one media file into the Eagle library as a new item.

    If move_source is True (default), deletes the inbox file after a successful
    library copy so it is not re-imported. If hold_lock is False, acquires
    write_session; if True, caller holds the lock.
    """
    source = source.expanduser().resolve()
    if not is_importable(source):
        return ImportResult(source=source, skipped=True, error="not importable")

    if folder_ids is not None:
        folder_ids = list(folder_ids)
    elif DEFAULT_FOLDER_ID:
        folder_ids = [DEFAULT_FOLDER_ID]
    else:
        folder_ids = []
    tags = list(tags if tags is not None else list(DEFAULT_TAGS))
    images_dir = library_root / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    ext = source.suffix.lower().lstrip(".")
    name = source.stem
    # Eagle stores name without problematic path chars; keep stem
    name = name.replace("/", "-").replace("\\", "-")
    size = source.stat().st_size
    btime, mtime = _file_times_ms(source)
    now = _now_ms()

    kind = "image"
    if f".{ext}" in VIDEO_EXTS:
        kind = "video"
    elif f".{ext}" in AUDIO_EXTS:
        kind = "audio"

    width = height = 0
    duration = 0.0
    if kind == "image":
        width, height = _image_size(source)
    elif kind == "video":
        width, height, duration = _video_meta(source)

    def _do() -> ImportResult:
        iid, item_dir = _unique_item_dir(images_dir)
        item_dir.mkdir(parents=True, exist_ok=False)
        dest_media = item_dir / f"{name}.{ext}"
        thumb_path = item_dir / f"{name}_thumbnail.png"

        try:
            shutil.copy2(source, dest_media)
            if kind == "image":
                if not _make_image_thumbnail(dest_media, thumb_path):
                    # Fall back to copying a scaled full image via convert on dest
                    _make_image_thumbnail(dest_media, thumb_path)
            elif kind == "video":
                _make_video_thumbnail(dest_media, thumb_path)
            else:
                _make_audio_thumbnail(thumb_path)

            meta: dict[str, Any] = {
                "id": iid,
                "name": name,
                "size": size,
                "btime": btime,
                "mtime": mtime,
                "ext": ext,
                "tags": tags,
                "folders": folder_ids,
                "isDeleted": False,
                "url": "",
                "annotation": "",
                "modificationTime": now,
                "width": width,
                "height": height,
                "lastModified": now,
                "palettes": [],
            }
            if kind == "video":
                meta["duration"] = duration
                meta["resolutionWidth"] = width
                meta["resolutionHeight"] = height
            if kind == "audio" and duration:
                meta["duration"] = duration

            atomic_write_json(item_dir / "metadata.json", meta)

            # mtime index
            mtime_path = library_root / "mtime.json"
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

            if move_source:
                # Consume inbox file; library already has a full copy.
                try:
                    source.unlink(missing_ok=True)
                except OSError as exc:
                    return ImportResult(
                        source=source,
                        item_id=iid,
                        ok=True,
                        error=f"imported but could not remove source: {exc}",
                    )

            return ImportResult(source=source, item_id=iid, ok=True)
        except Exception as exc:  # noqa: BLE001
            # Cleanup partial item dir
            try:
                if item_dir.is_dir():
                    shutil.rmtree(item_dir, ignore_errors=True)
            except OSError:
                pass
            return ImportResult(source=source, ok=False, error=str(exc))

    if hold_lock:
        return _do()
    try:
        with write_session(library_root):
            return _do()
    except WriteError as exc:
        return ImportResult(source=source, ok=False, error=str(exc))


def import_inbox(
    library_root: Path,
    inbox: Path,
    *,
    folder_ids: list[str] | None = None,
    tags: list[str] | None = None,
    move_source: bool = True,
) -> list[ImportResult]:
    """Import all importable files currently in inbox (single lock)."""
    files = list_inbox_files(inbox)
    if not files:
        return []
    results: list[ImportResult] = []
    try:
        with write_session(library_root):
            for f in files:
                results.append(
                    import_file(
                        library_root,
                        f,
                        folder_ids=folder_ids,
                        tags=tags,
                        move_source=move_source,
                        hold_lock=True,
                    )
                )
    except WriteError as exc:
        return [ImportResult(source=inbox, ok=False, error=str(exc))]
    return results
