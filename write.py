"""Safe write helpers for Eagle.cool library JSON (tags, ratings, etc.)."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator  # Any used by rename_item_media


LOCK_FILENAME = ".eagle-browse.write.lock"
BACKUP_DIRNAME = "backup/eagle-browse-writes"
# Written after each successful new import so open browsers can ingest
# that one item instead of rescanning the whole library.
INBOX_SIGNAL_FILENAME = ".eagle-browse.inbox-signal"


def _load_inbox_signal(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def _write_inbox_signal(
    library_root: Path, *, ids: list[str], dups: list[str]
) -> None:
    path = library_root / INBOX_SIGNAL_FILENAME
    try:
        atomic_write_json(
            path,
            {"ids": ids[-200:], "dups": dups[-50:], "ts": time.time()},
        )
    except OSError:
        pass


def announce_imported_ids(library_root: Path, item_ids: list[str]) -> None:
    """Atomically record newly imported item ids for GUI / phone watchers.

    Merges with unread ids already in the file so a rapid batch of one-id
    announces does not drop earlier ids (phone polls once a second).
    """
    ids = [i for i in item_ids if i]
    if not ids:
        return
    path = library_root / INBOX_SIGNAL_FILENAME
    raw = _load_inbox_signal(path)
    existing = [str(i) for i in (raw.get("ids") or []) if i]
    dups = [str(n) for n in (raw.get("dups") or []) if n]
    merged = list(dict.fromkeys(existing + ids))
    _write_inbox_signal(library_root, ids=merged, dups=dups)


def announce_inbox_dups(library_root: Path, names: list[str]) -> None:
    """Tell an open GUI to review these intake filenames as duplicates."""
    names = [n for n in names if n]
    if not names:
        return
    path = library_root / INBOX_SIGNAL_FILENAME
    raw = _load_inbox_signal(path)
    ids = [str(i) for i in (raw.get("ids") or []) if i]
    existing = [str(n) for n in (raw.get("dups") or []) if n]
    merged = list(dict.fromkeys(existing + names))
    _write_inbox_signal(library_root, ids=ids, dups=merged)


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
            holder_dead = self._holder_is_dead(text)
            if age < self.stale_seconds and not holder_dead:
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

    @staticmethod
    def _holder_is_dead(text: str) -> bool:
        """True if the lock names this host and that pid is gone."""
        pid = None
        host = None
        for line in text.splitlines():
            if line.startswith("pid="):
                try:
                    pid = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pid = None
            elif line.startswith("host="):
                host = line.split("=", 1)[1].strip()
        if pid is None:
            return False
        if host and host != os.uname().nodename:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

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


def write_item_duration(item_dir: Path, duration: float) -> None:
    """Set duration seconds without bumping modificationTime (backfill)."""
    data = load_item_metadata(item_dir)
    data["duration"] = float(duration)
    atomic_write_json(item_dir / "metadata.json", data)


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


def sanitize_item_name(name: str, *, ext: str = "") -> str:
    """Stem only. Strips a trailing ``.ext`` if the user typed the filename."""
    name = (name or "").strip()
    ext = (ext or "").lstrip(".")
    if ext and name.lower().endswith("." + ext.lower()):
        name = name[: -(len(ext) + 1)].rstrip()
    cleaned: list[str] = []
    for ch in name:
        if ch in '/\\:\0' or ord(ch) < 32:
            continue
        cleaned.append(ch)
    name = "".join(cleaned).strip(" .")
    if not name or name in {".", ".."}:
        raise WriteError("Name cannot be empty")
    if len(name) > 200:
        raise WriteError("Name is too long")
    return name


def _safe_rename(src: Path, dest: Path) -> None:
    if src == dest:
        return
    if not src.is_file():
        raise WriteError(f"Missing file: {src.name}")
    try:
        same = src.resolve() == dest.resolve()
    except OSError:
        same = False
    if dest.exists() and not same:
        raise WriteError(f"Already exists: {dest.name}")
    if same and src.name != dest.name:
        tmp = dest.with_name(f".{dest.name}.renametmp")
        if tmp.exists():
            raise WriteError("Rename temp file already exists")
        src.rename(tmp)
        tmp.rename(dest)
        return
    src.rename(dest)


def rename_item_media(
    library_root: Path,
    item: Any,
    new_name: str,
) -> tuple[str, Path, Path | None]:
    """Rename media + ``{name}_thumbnail.*`` and update metadata ``name``.

    Returns ``(cleaned_name, new_media_path, new_thumb_path_or_None)``.
    The ``.info`` folder id is unchanged.
    """
    item_dir = getattr(item, "item_dir", None)
    if item_dir is None or not Path(item_dir).is_dir():
        raise WriteError("No item directory")
    item_dir = Path(item_dir)
    old_name = str(item.name or "")
    ext = (getattr(item, "ext", None) or Path(item.path).suffix.lstrip(".")).lstrip(".")
    new_name = sanitize_item_name(new_name, ext=ext)
    if new_name == old_name:
        return new_name, Path(item.path), getattr(item, "thumb", None)

    old_media = Path(item.path)
    if not old_media.is_file():
        raise WriteError(f"Media file missing: {old_media}")
    new_media = item_dir / (f"{new_name}.{ext}" if ext else new_name)

    thumb_moves: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    candidates: list[Path] = []
    if getattr(item, "thumb", None) is not None:
        candidates.append(Path(item.thumb))
    for suf in (".png", ".jpg", ".jpeg", ".webp"):
        candidates.append(item_dir / f"{old_name}_thumbnail{suf}")
    for src_t in candidates:
        try:
            key = src_t.resolve()
        except OSError:
            key = src_t
        if key in seen or not src_t.is_file():
            continue
        seen.add(key)
        dest_t = item_dir / f"{new_name}_thumbnail{src_t.suffix}"
        if src_t != dest_t:
            thumb_moves.append((src_t, dest_t))

    _safe_rename(old_media, new_media)
    done_thumbs: list[tuple[Path, Path]] = []
    try:
        for src_t, dest_t in thumb_moves:
            _safe_rename(src_t, dest_t)
            done_thumbs.append((src_t, dest_t))
        data = load_item_metadata(item_dir)
        data["name"] = new_name
        save_item_metadata(library_root, item_dir, data)
    except Exception:
        for src_t, dest_t in reversed(done_thumbs):
            try:
                dest_t.rename(src_t)
            except OSError:
                pass
        try:
            if new_media.is_file() and not old_media.is_file():
                new_media.rename(old_media)
        except OSError:
            pass
        raise

    new_thumb: Path | None = None
    if done_thumbs:
        new_thumb = done_thumbs[0][1]
    elif getattr(item, "thumb", None) is not None and Path(item.thumb).is_file():
        new_thumb = Path(item.thumb)
    return new_name, new_media, new_thumb


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


def apply_annotation(data: dict[str, Any], annotation: str | None) -> dict[str, Any]:
    """Set Eagle item annotation (notes). None or empty string clears it."""
    text = "" if annotation is None else str(annotation)
    # Normalize newlines; strip trailing whitespace on each line but keep content.
    # Eagle stores plain text; empty means no note.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > 20000:
        raise WriteError("Note is too long (max 20000 characters)")
    data["annotation"] = text
    return data


def normalize_tag(tag: str) -> str:
    """Tags are case-insensitive. Store and compare as lowercase."""
    return (tag or "").strip().lower()


def canonicalize_tags(tags: Any) -> list[str]:
    """Unique lowercase tags, first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    if not tags:
        return out
    for raw in tags:
        t = normalize_tag(str(raw) if raw is not None else "")
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def apply_tags(
    data: dict[str, Any],
    *,
    set_tags: list[str] | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
) -> dict[str, Any]:
    if set_tags is not None:
        tags = canonicalize_tags(set_tags)
    else:
        tags = canonicalize_tags(data.get("tags") or [])
        if add_tags:
            added = canonicalize_tags(add_tags)
            incoming_set = [t for t in added if t.startswith("set:")]
            if incoming_set:
                keep = incoming_set[-1]
                tags = [t for t in tags if not t.startswith("set:")]
                added = [t for t in added if not t.startswith("set:")] + [keep]
            existing = set(tags)
            for t in added:
                if t not in existing:
                    tags.append(t)
                    existing.add(t)
        if remove_tags:
            remove = set(canonicalize_tags(remove_tags))
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
            tags = canonicalize_tags(raw) if isinstance(raw, list) else []
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


def new_library_id() -> str:
    """Eagle-style 13-char id: M + 12 uppercase alphanumeric."""
    import secrets

    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "M" + "".join(secrets.choice(alphabet) for _ in range(12))


def load_library_metadata(library_root: Path) -> dict[str, Any]:
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
    return meta


def save_library_metadata(library_root: Path, meta: dict[str, Any]) -> None:
    meta_path = library_root / "metadata.json"
    backup_file(library_root, meta_path)
    atomic_write_json(meta_path, meta)


def create_smart_folder_node(
    library_root: Path,
    *,
    name: str,
    conditions: list[dict[str, Any]],
    parent_id: str | None = None,
    description: str = "",
    folder_id: str | None = None,
) -> dict[str, Any]:
    """
    Insert a smart folder into metadata.json smartFolders tree.

    Returns the created node (with id). Caller should hold write_session.
    """
    name = (name or "").strip()
    if not name:
        raise WriteError("Smart folder name is required")
    meta = load_library_metadata(library_root)
    roots = meta.get("smartFolders")
    if roots is None:
        roots = []
        meta["smartFolders"] = roots
    if not isinstance(roots, list):
        raise WriteError("smartFolders must be a list")

    node: dict[str, Any] = {
        "id": folder_id or new_library_id(),
        "icon": "grid",
        "name": name,
        "description": description or "",
        "modificationTime": _now_ms(),
        "conditions": conditions or [],
        "children": [],
        "orderBy": "IMPORT",
        "sortIncrease": True,
    }

    if parent_id:
        node["parent"] = parent_id
        found = False

        def walk(nodes: list[Any]) -> bool:
            nonlocal found
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                if n.get("id") == parent_id:
                    children = n.get("children")
                    if not isinstance(children, list):
                        children = []
                        n["children"] = children
                    children.append(node)
                    found = True
                    return True
                ch = n.get("children")
                if isinstance(ch, list) and walk(ch):
                    return True
            return False

        if not walk(roots):
            raise WriteError(f"Parent smart folder not found: {parent_id}")
    else:
        roots.append(node)

    save_library_metadata(library_root, meta)
    return node


def _smart_folder_roots(meta: dict[str, Any]) -> list[Any]:
    roots = meta.get("smartFolders")
    if roots is None:
        roots = []
        meta["smartFolders"] = roots
    if not isinstance(roots, list):
        raise WriteError("smartFolders must be a list")
    return roots


def _find_smart_in_tree(
    roots: list[Any], folder_id: str
) -> tuple[dict[str, Any], list[Any], int] | None:
    for i, node in enumerate(roots):
        if not isinstance(node, dict):
            continue
        if node.get("id") == folder_id:
            return node, roots, i
        children = node.get("children")
        if isinstance(children, list):
            found = _find_smart_in_tree(children, folder_id)
            if found is not None:
                return found
    return None


def _smart_descendant_ids(node: dict[str, Any]) -> set[str]:
    ids: set[str] = set()

    def walk(n: Any) -> None:
        if not isinstance(n, dict):
            return
        nid = n.get("id")
        if nid:
            ids.add(str(nid))
        for child in n.get("children") or []:
            walk(child)

    for child in node.get("children") or []:
        walk(child)
    return ids


def _smart_parent_id(roots: list[Any], folder_id: str) -> str | None | object:
    """Parent id of *folder_id*, or None if it is a root. ``False`` if missing."""

    def walk(nodes: list[Any], pid: str | None) -> tuple[bool, str | None]:
        for n in nodes:
            if not isinstance(n, dict):
                continue
            if n.get("id") == folder_id:
                return True, pid
            ch = n.get("children")
            if isinstance(ch, list):
                found, got = walk(ch, str(n.get("id") or "") or None)
                if found:
                    return True, got
        return False, None

    found, pid = walk(roots, None)
    if not found:
        return False
    return pid


def _count_smart_subtree(node: dict[str, Any]) -> int:
    n = 1
    for child in node.get("children") or []:
        if isinstance(child, dict):
            n += _count_smart_subtree(child)
    return n


def update_smart_folder_node(
    library_root: Path,
    folder_id: str,
    *,
    name: str | None = None,
    conditions: list[dict[str, Any]] | None = None,
    parent_id: str | None | object = ...,  # Ellipsis = leave parent unchanged
    description: str | None = None,
) -> dict[str, Any]:
    """
    Patch a smart folder in metadata.json.

    *parent_id* ``...`` (default) leaves the parent unchanged. ``None`` moves
    the node to the root. A string moves it under that smart folder.

    Caller should hold write_session.
    """
    if not folder_id:
        raise WriteError("Smart folder id is required")
    meta = load_library_metadata(library_root)
    roots = _smart_folder_roots(meta)
    found = _find_smart_in_tree(roots, folder_id)
    if found is None:
        raise WriteError(f"Smart folder not found: {folder_id}")
    node, parent_list, idx = found

    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise WriteError("Smart folder name is required")
        node["name"] = cleaned
    if description is not None:
        node["description"] = description
    if conditions is not None:
        node["conditions"] = conditions
    node["modificationTime"] = _now_ms()

    if parent_id is not ...:
        new_parent: str | None = parent_id or None
        current_parent = _smart_parent_id(roots, folder_id)
        if current_parent is False:
            raise WriteError(f"Smart folder not found: {folder_id}")
        if current_parent != new_parent:
            if new_parent == folder_id or (
                new_parent and new_parent in _smart_descendant_ids(node)
            ):
                raise WriteError("Cannot move a smart folder under itself")
            dest_list: list[Any]
            if new_parent:
                dest = _find_smart_in_tree(roots, new_parent)
                if dest is None:
                    raise WriteError(f"Parent smart folder not found: {new_parent}")
                dest_node = dest[0]
                children = dest_node.get("children")
                if not isinstance(children, list):
                    children = []
                    dest_node["children"] = children
                dest_list = children
            else:
                dest_list = roots
            moved = parent_list.pop(idx)
            dest_list.append(moved)
            if new_parent:
                moved["parent"] = new_parent
            else:
                moved.pop("parent", None)

    save_library_metadata(library_root, meta)
    return node


def delete_smart_folder_node(
    library_root: Path,
    folder_id: str,
) -> dict[str, Any]:
    """
    Remove a smart folder (and its children) from metadata.json.

    Returns the removed node. Caller should hold write_session.
    """
    if not folder_id:
        raise WriteError("Smart folder id is required")
    meta = load_library_metadata(library_root)
    roots = _smart_folder_roots(meta)
    found = _find_smart_in_tree(roots, folder_id)
    if found is None:
        raise WriteError(f"Smart folder not found: {folder_id}")
    node, parent_list, idx = found
    removed = parent_list.pop(idx)
    save_library_metadata(library_root, meta)
    return removed


def move_smart_folder_node(
    library_root: Path,
    folder_id: str,
    *,
    target_id: str | None = None,
    place: str = "after",
) -> dict[str, Any]:
    """
    Move a smart folder next to *target_id*.

    *place*:
      - ``before`` / ``after`` — sibling of *target_id* (reparents if needed)
      - ``first`` — first among root smart folders (*target_id* ignored)
    """
    if not folder_id:
        raise WriteError("Smart folder id is required")
    place = (place or "after").lower()
    if place not in ("before", "after", "first"):
        raise WriteError(f"Unknown move place: {place}")

    meta = load_library_metadata(library_root)
    roots = _smart_folder_roots(meta)
    src = _find_smart_in_tree(roots, folder_id)
    if src is None:
        raise WriteError(f"Smart folder not found: {folder_id}")
    src_node, src_list, src_idx = src

    if place == "first":
        if src_list is roots and src_idx == 0:
            return src_node
        moved = src_list.pop(src_idx)
        roots.insert(0, moved)
        moved.pop("parent", None)
        moved["modificationTime"] = _now_ms()
        save_library_metadata(library_root, meta)
        return moved

    if not target_id:
        raise WriteError("target_id is required unless place=first")
    if target_id == folder_id:
        return src_node
    if target_id in _smart_descendant_ids(src_node):
        raise WriteError("Cannot move a smart folder under itself")

    tgt = _find_smart_in_tree(roots, target_id)
    if tgt is None:
        raise WriteError(f"Target smart folder not found: {target_id}")
    _tgt_node, tgt_list, tgt_idx = tgt
    new_parent = _smart_parent_id(roots, target_id)
    if new_parent is False:
        raise WriteError(f"Target smart folder not found: {target_id}")

    same_list = src_list is tgt_list
    moved = src_list.pop(src_idx)
    if same_list and src_idx < tgt_idx:
        tgt_idx -= 1
    insert_at = tgt_idx if place == "before" else tgt_idx + 1
    tgt_list.insert(insert_at, moved)
    if new_parent:
        moved["parent"] = new_parent
    else:
        moved.pop("parent", None)
    moved["modificationTime"] = _now_ms()
    save_library_metadata(library_root, meta)
    return moved


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

    cleaned = canonicalize_tags(tags)
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
