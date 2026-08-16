"""Import files from an inbox into an Eagle.cool library."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from config import DEFAULT_INBOX  # noqa: F401
from write import (
    WriteError,
    announce_imported_ids,
    atomic_write_json,
    backup_file,
    load_item_metadata,
    save_item_metadata,
    write_session,
)
# Imports go in uncategorized / untagged unless the caller passes folders/tags.
DEFAULT_FOLDER_ID: str | None = None
DEFAULT_TAGS: list[str] = []

ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".aiff", ".aif"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS
ZIP_EXTS = {".zip"}
SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

# Incomplete Dropbox / progressive writes often leave a tiny ftyp-only stub
# (commonly 32–64 bytes) that stays size-stable until the real payload arrives.
# Reject below these floors before even probing.
MIN_BYTES_IMAGE = 32
MIN_BYTES_VIDEO = 1024
MIN_BYTES_AUDIO = 256

# Error prefix: temporary incomplete download — leave in inbox and retry later.
NOT_READY_PREFIX = "not-ready:"


@dataclass
class ImportResult:
    source: Path
    item_id: str | None = None
    ok: bool = False
    skipped: bool = False
    error: str | None = None
    # True when we re-touched an existing duplicate instead of creating a new item
    reused: bool = False


@dataclass
class DuplicateMatch:
    """An inbox file whose content already exists in the library."""

    source: Path
    content_hash: str
    size: int
    existing_id: str
    existing_name: str
    existing_path: Path
    existing_thumb: Path | None
    existing_width: int = 0
    existing_height: int = 0


def file_content_hash(path: Path, *, chunk: int = 1024 * 1024) -> str:
    """MD5 of full file contents (Eagle-style exact content match)."""
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _item_is_deleted(it: Any) -> bool:
    """True for Eagle soft-deleted assets (never treat as import duplicates)."""
    if getattr(it, "is_deleted", False):
        return True
    # Defensive: raw dicts / alternate attr names
    if getattr(it, "isDeleted", False):
        return True
    return False


def _item_looks_corrupt(it: Any) -> bool:
    """True for unplayable stubs we must never treat as dedupe matches."""
    sz = int(getattr(it, "size", 0) or 0)
    ext = str(getattr(it, "ext", "") or "").lower().lstrip(".")
    dotted = f".{ext}" if ext else ""
    if dotted in VIDEO_EXTS:
        if sz < MIN_BYTES_VIDEO:
            return True
        # Imported incomplete videos end up with width/height 0
        w = int(getattr(it, "width", 0) or 0)
        h = int(getattr(it, "height", 0) or 0)
        if w <= 0 or h <= 0:
            return True
    elif dotted in AUDIO_EXTS:
        if sz < MIN_BYTES_AUDIO:
            return True
    elif dotted in IMAGE_EXTS or ext in {e.lstrip(".") for e in IMAGE_EXTS}:
        if sz < MIN_BYTES_IMAGE:
            return True
    elif sz <= 0:
        return True
    return False


def build_size_index(
    items: Iterable[Any],
) -> dict[int, list[Any]]:
    """size → active (non-deleted) library items for content-hash dedupe."""
    index: dict[int, list[Any]] = {}
    for it in items:
        if _item_is_deleted(it):
            continue
        if _item_looks_corrupt(it):
            continue
        sz = int(getattr(it, "size", 0) or 0)
        if sz <= 0:
            continue
        index.setdefault(sz, []).append(it)
    return index


def find_duplicate_item(
    source: Path,
    *,
    size_index: dict[int, list[Any]],
    hash_cache: dict[str, str] | None = None,
) -> DuplicateMatch | None:
    """
    Return a library match if *source* is an exact content duplicate.

    Candidates are narrowed by file size, then verified with MD5.
    Soft-deleted items (isDeleted) are never considered matches — re-importing
    the same file after trash should create a fresh library item (or prompt
    only against live assets).
    """
    source = source.expanduser().resolve()
    try:
        size = source.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None
    candidates = size_index.get(size) or []
    if not candidates:
        return None

    cache = hash_cache if hash_cache is not None else {}
    src_key = str(source)
    if src_key not in cache:
        try:
            cache[src_key] = file_content_hash(source)
        except OSError:
            return None
    src_hash = cache[src_key]

    for it in candidates:
        if _item_is_deleted(it):
            continue
        path = getattr(it, "path", None)
        if path is None:
            continue
        p = Path(path)
        if not p.is_file():
            continue
        try:
            if p.stat().st_size != size:
                continue
        except OSError:
            continue
        pkey = str(p)
        if pkey not in cache:
            try:
                cache[pkey] = file_content_hash(p)
            except OSError:
                continue
        if cache[pkey] != src_hash:
            continue
        thumb = getattr(it, "thumb", None)
        return DuplicateMatch(
            source=source,
            content_hash=src_hash,
            size=size,
            existing_id=str(getattr(it, "id", "")),
            existing_name=str(
                getattr(it, "display_name", None)
                or f"{getattr(it, 'name', '')}.{getattr(it, 'ext', '')}"
            ),
            existing_path=p,
            existing_thumb=Path(thumb) if thumb else None,
            existing_width=int(getattr(it, "width", 0) or 0),
            existing_height=int(getattr(it, "height", 0) or 0),
        )
    return None


def classify_inbox_files(
    sources: list[Path],
    items: Iterable[Any],
) -> tuple[list[Path], list[DuplicateMatch]]:
    """
    Split inbox paths into unique imports vs content duplicates.

    Soft-deleted library items are excluded from matching.
    """
    size_index = build_size_index(items)
    hash_cache: dict[str, str] = {}
    unique: list[Path] = []
    dups: list[DuplicateMatch] = []
    for src in sources:
        match = find_duplicate_item(src, size_index=size_index, hash_cache=hash_cache)
        if match:
            dups.append(match)
        else:
            unique.append(src)
    return unique, dups


def reimport_existing(
    library_root: Path,
    item_id: str,
    *,
    source: Path | None = None,
    move_source: bool = True,
    hold_lock: bool = False,
) -> ImportResult:
    """
    Eagle "use existing": bump import timestamps, do not create a new item.

    Optionally consume the inbox *source* file afterward.
    """
    library_root = library_root.expanduser().resolve()
    item_dir = library_root / "images" / f"{item_id}.info"
    if not item_dir.is_dir():
        return ImportResult(
            source=source or Path("."),
            ok=False,
            error=f"existing item missing: {item_id}",
        )

    def _do() -> ImportResult:
        try:
            data = load_item_metadata(item_dir)
            # save_item_metadata sets modificationTime + lastModified + mtime.json
            save_item_metadata(library_root, item_dir, data, do_backup=True)
            if move_source and source is not None:
                try:
                    source.unlink(missing_ok=True)
                except OSError as exc:
                    return ImportResult(
                        source=source,
                        item_id=item_id,
                        ok=True,
                        reused=True,
                        error=f"reused but could not remove source: {exc}",
                    )
            return ImportResult(
                source=source or item_dir,
                item_id=item_id,
                ok=True,
                reused=True,
            )
        except WriteError as exc:
            return ImportResult(
                source=source or item_dir,
                ok=False,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return ImportResult(
                source=source or item_dir,
                ok=False,
                error=str(exc),
            )

    if hold_lock:
        return _do()
    try:
        with write_session(library_root):
            return _do()
    except WriteError as exc:
        return ImportResult(source=source or Path("."), ok=False, error=str(exc))


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
    if path.suffix.lower() not in MEDIA_EXTS:
        return False
    # Ignore incomplete downloads
    if path.suffix.lower() in {".crdownload", ".part", ".tmp"}:
        return False
    return True


def _media_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    return "other"


def check_media_complete(path: Path) -> tuple[bool, str]:
    """
    Return (True, \"\") if *path* is fully readable media safe to import.

    Incomplete Dropbox / progressive writes often stabilize at a tiny size
    (ftyp-only MP4 stubs) before the payload arrives. Size-stable checks alone
    treat those as ready; this probe rejects them so the inbox keeps waiting.

    Failures use reason text; callers may prefix with NOT_READY_PREFIX for
    temporary incompleteness (retry) vs permanent unreadable media.
    """
    path = path.expanduser()
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"unreadable: {exc}"
    if size <= 0:
        return False, "empty file"

    kind = _media_kind(path)
    if kind == "video" and size < MIN_BYTES_VIDEO:
        return False, f"video too small ({size} B) — still downloading?"
    if kind == "audio" and size < MIN_BYTES_AUDIO:
        return False, f"audio too small ({size} B) — still downloading?"
    if kind == "image" and size < MIN_BYTES_IMAGE:
        return False, f"image too small ({size} B)"

    if kind == "image":
        w, h = _image_size(path)
        if w <= 0 or h <= 0:
            return False, "image unreadable (decode failed)"
        return True, ""

    if kind == "video":
        w, h, duration = _video_meta(path)
        if w <= 0 or h <= 0:
            # Classic incomplete MP4: "moov atom not found"
            return False, "video unreadable (incomplete or missing moov)"
        return True, ""

    if kind == "audio":
        # Reuse ffprobe path: duration from format, or any audio stream
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
            has_audio = any(
                s.get("codec_type") == "audio" for s in (data.get("streams") or [])
            )
            try:
                duration = float((data.get("format") or {}).get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if not has_audio and duration <= 0:
                return False, "audio unreadable (no stream)"
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"audio unreadable ({exc})"

    return False, "unsupported type"


def is_not_ready_error(error: str | None) -> bool:
    return bool(error) and str(error).startswith(NOT_READY_PREFIX)


def list_inbox_files(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    files = [p for p in inbox.iterdir() if is_importable(p)]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files


def list_inbox_zips(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    zips = [
        p
        for p in inbox.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix.lower() in ZIP_EXTS
    ]
    zips.sort(key=lambda p: p.stat().st_mtime)
    return zips


def check_zip_complete(path: Path) -> tuple[bool, str]:
    """True if *path* is a readable, non-empty zip (Dropbox finished writing)."""
    try:
        if path.stat().st_size < 22:
            return False, "zip too small — still downloading?"
    except OSError as exc:
        return False, f"zip unreadable ({exc})"
    try:
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                return False, "zip corrupt or incomplete"
            if not zf.namelist():
                return False, "zip empty"
        return True, ""
    except zipfile.BadZipFile:
        return False, "zip incomplete or not a zip"
    except Exception as exc:  # noqa: BLE001
        return False, f"zip unreadable ({exc})"


def _skip_zip_member(name: str) -> bool:
    norm = name.replace("\\", "/").lstrip("/")
    lower = norm.lower()
    base = Path(norm).name.lower()
    if not norm or norm.endswith("/"):
        return True
    if lower.startswith("__macosx/") or "/__macosx/" in lower:
        return True
    if base.startswith(".") or base in SKIP_NAMES:
        return True
    return False


def _safe_extract_path(root: Path, member: str) -> Path | None:
    """Resolve a zip member under *root*, or None if it escapes (zip slip)."""
    rel = Path(member.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    dest = (root / rel).resolve()
    try:
        dest.relative_to(root.resolve())
    except ValueError:
        return None
    return dest


def _unique_inbox_path(inbox: Path, name: str) -> Path:
    dest = inbox / name
    if not dest.exists():
        return dest
    stem, suf = Path(name).stem, Path(name).suffix
    n = 2
    while True:
        cand = inbox / f"{stem}_{n}{suf}"
        if not cand.exists():
            return cand
        n += 1


def unpack_zip_to_inbox(zip_path: Path, inbox: Path) -> tuple[int, str | None]:
    """
    Extract media from *zip_path* flat into *inbox*.

    Writes into a hidden ``.unzip-*`` dir, moves importable files to the inbox
    root (unique names on collision), then deletes the zip and the extract dir.
    Returns ``(moved_count, error_or_None)``.
    """
    extract_dir = inbox / f".unzip-{zip_path.stem}-{os.getpid()}"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=False)
    moved = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if _skip_zip_member(info.filename):
                    continue
                dest = _safe_extract_path(extract_dir, info.filename)
                if dest is None:
                    continue
                if dest.suffix.lower() not in MEDIA_EXTS:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)

        for p in extract_dir.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in MEDIA_EXTS:
                continue
            if p.name.lower() in SKIP_NAMES or p.name.startswith("."):
                continue
            target = _unique_inbox_path(inbox, p.name)
            shutil.move(str(p), str(target))
            moved += 1

        shutil.rmtree(extract_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        return moved, None
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(extract_dir, ignore_errors=True)
        return moved, str(exc)


def unpack_inbox_zips(inbox: Path) -> list[tuple[Path, int, str | None]]:
    """Unpack every complete zip sitting in *inbox*. Incomplete zips are skipped."""
    results: list[tuple[Path, int, str | None]] = []
    for z in list_inbox_zips(inbox):
        ok, reason = check_zip_complete(z)
        if not ok:
            results.append((z, 0, reason))
            continue
        n, err = unpack_zip_to_inbox(z, inbox)
        results.append((z, n, err))
    return results


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
        from gi.repository import GdkPixbuf

        # Header-only: do not decode the full bitmap just for width/height.
        _fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(str(path))
        if w and h:
            return int(w), int(h)
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
        from pixbuf_io import pixbuf_from_path

        pb = pixbuf_from_path(src, max_edge, max_edge)
        if pb is None:
            raise RuntimeError(f"could not decode {src}")
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


def _run_ffmpeg(cmd: list[str]) -> str:
    """Run ffmpeg. Return stderr (empty on success). Raise WriteError on tool issues."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
            text=True,
        )
    except FileNotFoundError as exc:
        raise WriteError("ffmpeg is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise WriteError("ffmpeg timed out extracting the frame") from exc
    return (proc.stderr or "").strip()


def extract_video_frame(
    src: Path, dest: Path, seconds: float, *, last_frame: bool = False
) -> None:
    """Write one PNG frame from *src* at *seconds* via ffmpeg.

    The last frame is a special case: ``-ss`` at EOF writes nothing (FFmpeg 9
    still exits 0). Walk the tail with ``-sseof`` + ``-update 1`` so the last
    decoded frame overwrites the output.
    """
    seconds = max(0.0, float(seconds))
    dest.parent.mkdir(parents=True, exist_ok=True)
    _w, _h, duration = _video_meta(src)
    if duration and duration > 0:
        if seconds >= duration - 0.12:
            last_frame = True
        seconds = min(seconds, max(0.0, duration - 0.05))

    def have_frame() -> bool:
        return dest.is_file() and dest.stat().st_size > 32

    one_frame = [
        "-map",
        "0:v:0",
        "-an",
        "-frames:v",
        "1",
        "-vf",
        "format=rgb24",
        str(dest),
    ]
    tail_update = [
        "-map",
        "0:v:0",
        "-an",
        "-update",
        "1",
        "-vf",
        "format=rgb24",
        str(dest),
    ]
    head = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    attempts: list[list[str]] = []
    if last_frame:
        # Decode the last 2s; each frame overwrites dest, so the file is the last.
        attempts.append([*head, "-sseof", "-2", "-i", str(src), *tail_update])
        attempts.append([*head, "-sseof", "-0.5", "-i", str(src), *one_frame])
    attempts.extend(
        [
            [*head, "-i", str(src), "-ss", f"{seconds:.3f}", *one_frame],
            [*head, "-ss", f"{seconds:.3f}", "-i", str(src), *one_frame],
            [*head, "-sseof", "-2", "-i", str(src), *tail_update],
        ]
    )
    last_err = ""
    for cmd in attempts:
        if dest.is_file():
            try:
                dest.unlink()
            except OSError:
                pass
        last_err = _run_ffmpeg(cmd)
        if have_frame():
            return
    detail = last_err.splitlines()[-1] if last_err else "no frame written"
    raise WriteError(f"ffmpeg could not extract a frame at {seconds:.3f}s · {detail}")


def _frame_item_name(stem: str, seconds: float) -> str:
    stem = (stem or "frame").replace("/", "-").replace("\\", "-").replace("%", "_").strip() or "frame"
    whole = int(max(0.0, seconds))
    frac = int(round((max(0.0, seconds) - whole) * 100))
    if frac >= 100:
        whole += 1
        frac = 0
    minutes, secs = divmod(whole, 60)
    return f"{stem}-frame-{minutes}m{secs:02d}.{frac:02d}s"


def save_video_frame_as_item(
    library_root: Path,
    item: Any,
    seconds: float,
    *,
    last_frame: bool = False,
) -> Any:
    """
    Extract one frame from a video item and store it as a new still.

    New item has no tags and no folders (same as crop Save as). Source video
    is unchanged. Returns a ``library.Item``.
    """
    from library import Item

    src = Path(getattr(item, "path", "") or "")
    if not src.is_file():
        raise WriteError(f"Video file missing: {src}")
    seconds = max(0.0, float(seconds))
    name = _frame_item_name(str(getattr(item, "name", None) or src.stem), seconds)
    ext = "png"

    with write_session(library_root):
        images_dir = Path(library_root) / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        iid, item_dir = _unique_item_dir(images_dir)
        try:
            item_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise WriteError(f"Item dir already exists: {item_dir}") from exc

        dest_media = item_dir / f"{name}.{ext}"
        try:
            extract_video_frame(src, dest_media, seconds, last_frame=last_frame)
        except Exception:
            try:
                if dest_media.is_file():
                    dest_media.unlink()
                item_dir.rmdir()
            except OSError:
                pass
            raise

        width, height = _image_size(dest_media)
        new_size = dest_media.stat().st_size
        thumb_path = item_dir / f"{name}_thumbnail.png"
        thumb_ok = _make_image_thumbnail(dest_media, thumb_path)
        now = _now_ms()
        meta: dict[str, Any] = {
            "id": iid,
            "name": name,
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
            "width": width,
            "height": height,
            "lastModified": now,
            "palettes": [],
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
        name=name,
        ext=ext,
        tags=[],
        folders=[],
        path=dest_media.resolve(),
        thumb=thumb_path.resolve() if thumb_ok else None,
        is_deleted=False,
        size=new_size,
        width=width,
        height=height,
        annotation="",
        modification_time=now,
        btime=now,  # library add time, matches metadata.json
        star=None,
        duration=None,
        item_dir=item_dir.resolve(),
        tag_set=frozenset(),
        folder_set=frozenset(),
        name_lower=name.lower(),
        ext_lower=ext.lower(),
    )


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
    force_new: bool = False,
    size_index: dict[int, list[Any]] | None = None,
    hash_cache: dict[str, str] | None = None,
    items: Iterable[Any] | None = None,
) -> ImportResult:
    """
    Copy one media file into the Eagle library as a new item.

    If move_source is True (default), deletes the inbox file after a successful
    library copy so it is not re-imported. If hold_lock is False, acquires
    write_session; if True, caller holds the lock.

    Duplicate handling is normally done by the UI (classify + dialog). If
    *force_new* is False and *items*/*size_index* is provided, exact content
    duplicates return skipped with error ``duplicate:<id>`` instead of importing.
    """
    source = source.expanduser().resolve()
    if not is_importable(source):
        return ImportResult(source=source, skipped=True, error="not importable")

    complete, reason = check_media_complete(source)
    if not complete:
        # Leave source in place; watcher/GUI will retry when the file grows.
        return ImportResult(
            source=source,
            skipped=True,
            error=f"{NOT_READY_PREFIX}{reason}",
        )

    if not force_new:
        idx = size_index
        if idx is None and items is not None:
            idx = build_size_index(items)
        if idx is not None:
            match = find_duplicate_item(
                source, size_index=idx, hash_cache=hash_cache
            )
            if match:
                return ImportResult(
                    source=source,
                    item_id=match.existing_id,
                    skipped=True,
                    error=f"duplicate:{match.existing_id}",
                )

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
    _file_btime, file_mtime = _file_times_ms(source)
    now = _now_ms()
    # Eagle btime = added-to-library. Using the source file's birth/mtime
    # buried fresh imports under older (or future-dated) file timestamps.

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

            # Merge folder auto-tags (Eagle: folder.tags + ancestors) onto import
            from write import canonicalize_tags, folder_auto_tags_from_metadata

            item_tags = canonicalize_tags(tags)
            if folder_ids:
                try:
                    for t in folder_auto_tags_from_metadata(library_root, folder_ids):
                        if t not in item_tags:
                            item_tags.append(t)
                    item_tags = canonicalize_tags(item_tags)
                except Exception:  # noqa: BLE001
                    pass

            meta: dict[str, Any] = {
                "id": iid,
                "name": name,
                "size": size,
                "btime": now,
                "mtime": file_mtime,
                "ext": ext,
                "tags": item_tags,
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
                    announce_imported_ids(library_root, [iid])
                    return ImportResult(
                        source=source,
                        item_id=iid,
                        ok=True,
                        error=f"imported but could not remove source: {exc}",
                    )

            announce_imported_ids(library_root, [iid])
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
