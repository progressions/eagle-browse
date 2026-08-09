"""Safe write helpers for Eagle.cool library JSON (tags, ratings, etc.)."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


LOCK_FILENAME = ".eagle-browse.write.lock"
BACKUP_DIRNAME = "backup/eagle-browse-writes"


class WriteError(Exception):
    """Raised when a library write cannot proceed safely."""


class LibraryLock:
    """Simple exclusive lock file so only one Eagle Browse writer runs at a time."""

    def __init__(self, library_root: Path, *, stale_seconds: int = 3600):
        self.path = library_root / LOCK_FILENAME
        self.stale_seconds = stale_seconds
        self._held = False

    def acquire(self) -> None:
        if self._held:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                age = time.time() - self.path.stat().st_mtime
                text = self.path.read_text(encoding="utf-8")
            except OSError as exc:
                raise WriteError(f"Cannot read lock file: {exc}") from exc
            if age < self.stale_seconds:
                raise WriteError(
                    f"Library is locked by another writer:\n{text.strip()}\n"
                    f"(lock age {int(age)}s; remove {self.path.name} if stale)"
                )
            # Stale lock — take over
            try:
                self.path.unlink()
            except OSError as exc:
                raise WriteError(f"Cannot clear stale lock: {exc}") from exc

        payload = (
            f"pid={os.getpid()}\n"
            f"host={os.uname().nodename}\n"
            f"time={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"app=eagle-browse\n"
        )
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError as exc:
            raise WriteError("Library lock already held") from exc
        except OSError as exc:
            raise WriteError(f"Cannot create lock: {exc}") from exc
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass
        self._held = False

    def __enter__(self) -> LibraryLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def _now_ms() -> int:
    return int(time.time() * 1000)


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON via temp file + fsync + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=None, separators=(",", ":"))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Best-effort dir fsync
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def backup_file(library_root: Path, src: Path) -> Path | None:
    """Copy src into library backup area; return backup path or None."""
    if not src.is_file():
        return None
    rel = src.relative_to(library_root) if src.is_relative_to(library_root) else Path(src.name)
    dest_dir = library_root / BACKUP_DIRNAME / time.strftime("%Y%m%d")
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    dest = dest_dir / f"{rel.name}.{stamp}.bak"
    # Avoid nested dirs for simplicity — flat name with id
    if src.parent.name.endswith(".info"):
        dest = dest_dir / f"{src.parent.name}__{src.name}.{stamp}.bak"
    try:
        dest.write_bytes(src.read_bytes())
        return dest
    except OSError:
        return None


def load_item_metadata(item_dir: Path) -> dict[str, Any]:
    meta_path = item_dir / "metadata.json"
    try:
        with meta_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise WriteError(f"Cannot read {meta_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WriteError(f"Invalid metadata at {meta_path}")
    return data


def save_item_metadata(
    library_root: Path,
    item_dir: Path,
    data: dict[str, Any],
    *,
    do_backup: bool = True,
) -> None:
    meta_path = item_dir / "metadata.json"
    if do_backup:
        backup_file(library_root, meta_path)
    now = _now_ms()
    data["modificationTime"] = now
    data["lastModified"] = now
    atomic_write_json(meta_path, data)
    _touch_mtime_index(library_root, str(data.get("id") or item_dir.name.removesuffix(".info")), now)


def _touch_mtime_index(library_root: Path, item_id: str, when_ms: int) -> None:
    """Update mtime.json entry for this item if the file exists and is a dict."""
    mtime_path = library_root / "mtime.json"
    if not mtime_path.is_file():
        return
    try:
        with mtime_path.open("r", encoding="utf-8") as f:
            mtime = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(mtime, dict):
        return
    mtime[item_id] = when_ms
    try:
        backup_file(library_root, mtime_path)
        atomic_write_json(mtime_path, mtime)
    except OSError:
        # Non-fatal: item metadata already saved
        pass


def apply_star(data: dict[str, Any], star: int | None) -> dict[str, Any]:
    """Set 1–5 star rating, or clear (None / 0)."""
    if star is None or star == 0:
        data.pop("star", None)
    elif 1 <= int(star) <= 5:
        data["star"] = int(star)
    else:
        raise WriteError("Star rating must be 1–5, or 0 to clear")
    return data


def apply_tags(
    data: dict[str, Any],
    *,
    set_tags: list[str] | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
) -> dict[str, Any]:
    if set_tags is not None:
        tags = list(dict.fromkeys(t.strip() for t in set_tags if t and t.strip()))
    else:
        tags = list(data.get("tags") or [])
        if add_tags:
            for t in add_tags:
                t = t.strip()
                if t and t not in tags:
                    tags.append(t)
        if remove_tags:
            remove = {t.strip() for t in remove_tags if t and t.strip()}
            tags = [t for t in tags if t not in remove]
    data["tags"] = tags
    return data


def apply_folders(
    data: dict[str, Any],
    *,
    set_folders: list[str] | None = None,
    add_folders: list[str] | None = None,
    remove_folders: list[str] | None = None,
) -> dict[str, Any]:
    """Folder membership is a list of Eagle folder ids."""
    if set_folders is not None:
        folders = list(dict.fromkeys(f for f in set_folders if f))
    else:
        folders = list(data.get("folders") or [])
        if add_folders:
            for f in add_folders:
                if f and f not in folders:
                    folders.append(f)
        if remove_folders:
            remove = set(remove_folders)
            folders = [f for f in folders if f not in remove]
    data["folders"] = folders
    return data


def apply_deleted(data: dict[str, Any], deleted: bool) -> dict[str, Any]:
    """Eagle soft-delete: isDeleted + deletedTime (files stay on disk)."""
    now = _now_ms()
    if deleted:
        data["isDeleted"] = True
        data["deletedTime"] = now
    else:
        data["isDeleted"] = False
        data.pop("deletedTime", None)
    return data


def folder_auto_tags_from_metadata(
    library_root: Path,
    folder_ids: list[str],
) -> list[str]:
    """
    Read folder auto-tags from metadata.json without a full library load.

    For each folder id, merges tags from that folder and its ancestors
    (root → leaf), de-duplicated, order-preserving.
    """
    meta_path = library_root / "metadata.json"
    if not meta_path.is_file():
        return []
    try:
        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(meta, dict):
        return []

    # id -> (parent_id, tags)
    index: dict[str, tuple[str | None, list[str]]] = {}

    def index_walk(nodes: list[Any], parent_id: str | None = None) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            fid = node.get("id")
            if not fid:
                continue
            raw = node.get("tags") or []
            tags = [str(t) for t in raw if t] if isinstance(raw, list) else []
            index[str(fid)] = (parent_id, tags)
            children = node.get("children")
            if isinstance(children, list):
                index_walk(children, str(fid))

    roots = meta.get("folders")
    if isinstance(roots, list):
        index_walk(roots)

    out: list[str] = []
    seen: set[str] = set()
    for fid in folder_ids:
        chain: list[str] = []
        cur: str | None = fid
        walked: set[str] = set()
        while cur and cur in index and cur not in walked:
            walked.add(cur)
            chain.append(cur)
            cur = index[cur][0]
        chain.reverse()
        for aid in chain:
            for t in index.get(aid, (None, []))[1]:
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    return out


def set_folder_auto_tags(
    library_root: Path,
    folder_id: str,
    tags: list[str],
) -> None:
    """
    Write Eagle folder auto-tags into library metadata.json.

    Matches official Eagle: each folder node has a ``tags`` array applied to
    items when they are added to that folder.
    """
    meta_path = library_root / "metadata.json"
    if not meta_path.is_file():
        raise WriteError(f"Missing library metadata: {meta_path}")
    try:
        with meta_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise WriteError(f"Cannot read metadata.json: {exc}") from exc
    if not isinstance(meta, dict):
        raise WriteError("metadata.json root must be an object")

    cleaned = list(dict.fromkeys(t.strip() for t in tags if t and str(t).strip()))
    now = int(time.time() * 1000)
    found = False

    def walk(nodes: list[Any]) -> bool:
        nonlocal found
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("id") == folder_id:
                node["tags"] = cleaned
                node["modificationTime"] = now
                found = True
                return True
            children = node.get("children")
            if isinstance(children, list) and walk(children):
                return True
        return False

    roots = meta.get("folders")
    if not isinstance(roots, list) or not walk(roots):
        raise WriteError(f"Folder not found in metadata.json: {folder_id}")

    backup_file(library_root, meta_path)
    atomic_write_json(meta_path, meta)


@contextmanager
def write_session(library_root: Path) -> Iterator[LibraryLock]:
    lock = LibraryLock(library_root)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
