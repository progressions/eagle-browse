"""Read-only Eagle.cool library access, including smart folders."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from config import DEFAULT_LIBRARY, inbox_path, library_path  # noqa: F401
from write import canonicalize_tags


class QueryCancelled(Exception):
    """Raised when a caller cancels an in-progress library query."""


@dataclass(frozen=True, slots=True)
class DurationBackfillBatch:
    """One bounded backfill pass."""

    written: list[tuple[str, float]]
    probed: int
    remaining: int


# Startup duration backfill: probe outside the library write lock.
DURATION_BACKFILL_LIMIT = 25
DURATION_BACKFILL_TIME_BUDGET_S = 20.0
DURATION_PROBE_MAX_FAILURES = 5
DURATION_PROBE_BASE_BACKOFF_S = 30.0
_DURATION_SKIP_STATE = Path.home() / ".local" / "state" / "eagle-browse" / "duration-probe-skips.json"

VIDEO_EXTS = frozenset({"mp4", "mov", "webm", "mkv", "m4v", "avi", "wmv", "flv"})
AUDIO_EXTS = frozenset({"mp3", "wav", "flac", "aac", "m4a", "ogg", "wma", "aiff", "aif"})
IMAGE_EXTS = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "webp",
        "gif",
        "bmp",
        "tif",
        "tiff",
        "avif",
        "heic",
        "heif",
        "svg",
        "ico",
    }
)


@dataclass(slots=True)
class Folder:
    id: str
    name: str
    children: list[Folder] = field(default_factory=list)
    parent_id: str | None = None
    # Eagle "Auto tagging" on folders — applied when an item is added to this folder
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SmartFolder:
    id: str
    name: str
    conditions: list[dict[str, Any]]
    children: list[SmartFolder] = field(default_factory=list)
    parent_id: str | None = None
    # Conditions from root ancestor → self (inclusive), for evaluation
    inherited_conditions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Item:
    id: str
    name: str
    ext: str
    tags: list[str]
    folders: list[str]
    path: Path
    thumb: Path | None
    is_deleted: bool
    size: int
    width: int
    height: int
    annotation: str
    modification_time: int
    # Eagle btime — when the item was added to the library.
    # Stable across tag/folder edits; use for "Added · newest" sort.
    btime: int = 0
    # Eagle mtime — original file time at import (created). Not modificationTime.
    created_time: int = 0
    star: int | None = None  # Eagle UI "rating"; absent = unrated
    duration: float | None = None  # seconds (video/audio)
    item_dir: Path | None = None  # images/<id>.info for metadata writes
    # Precomputed for fast smart-folder evaluation
    tag_set: frozenset[str] = field(default_factory=frozenset)
    folder_set: frozenset[str] = field(default_factory=frozenset)
    name_lower: str = ""
    ext_lower: str = ""

    @property
    def display_name(self) -> str:
        return f"{self.name}.{self.ext}" if self.ext else self.name

    @property
    def is_image(self) -> bool:
        return self.ext_lower in IMAGE_EXTS

    @property
    def is_video(self) -> bool:
        return self.ext_lower in VIDEO_EXTS

    @property
    def is_audio(self) -> bool:
        return self.ext_lower in AUDIO_EXTS


def _load_json(path: Path) -> dict | list | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _parse_folders(
    nodes: list[dict], parent_id: str | None = None
) -> tuple[list[Folder], dict[str, Folder]]:
    roots: list[Folder] = []
    by_id: dict[str, Folder] = {}
    for node in nodes:
        raw_tags = node.get("tags") or []
        tags = canonicalize_tags(raw_tags) if isinstance(raw_tags, list) else []
        folder = Folder(
            id=node["id"],
            name=node.get("name") or "(unnamed)",
            parent_id=parent_id,
            tags=tags,
        )
        by_id[folder.id] = folder
        children, child_map = _parse_folders(node.get("children") or [], folder.id)
        folder.children = children
        by_id.update(child_map)
        roots.append(folder)
    return roots, by_id


def _parse_smart_folders(
    nodes: list[dict],
    parent_id: str | None = None,
    ancestor_conditions: list[dict[str, Any]] | None = None,
) -> tuple[list[SmartFolder], dict[str, SmartFolder]]:
    ancestor_conditions = ancestor_conditions or []
    roots: list[SmartFolder] = []
    by_id: dict[str, SmartFolder] = {}
    for node in nodes:
        own = list(node.get("conditions") or [])
        inherited = ancestor_conditions + own
        sf = SmartFolder(
            id=node["id"],
            name=node.get("name") or "(unnamed)",
            conditions=own,
            parent_id=parent_id,
            inherited_conditions=inherited,
        )
        by_id[sf.id] = sf
        children, child_map = _parse_smart_folders(
            node.get("children") or [], sf.id, inherited
        )
        sf.children = children
        by_id.update(child_map)
        roots.append(sf)
    return roots, by_id


def _first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        if p.is_file():
            return p
    return None


def _resolve_media_paths(item_dir: Path, name: str, ext: str) -> tuple[Path | None, Path | None]:
    """Find original file and thumbnail inside an Eagle item folder."""
    original = _first_existing(
        [
            item_dir / f"{name}.{ext}",
            item_dir / f"{name}.{ext.lower()}",
            item_dir / f"{name}.{ext.upper()}",
        ]
    )
    thumb = _first_existing(
        [
            item_dir / f"{name}_thumbnail.png",
            item_dir / f"{name}_thumbnail.jpg",
            item_dir / f"{name}_thumbnail.webp",
        ]
    )

    if original is None or thumb is None:
        try:
            entries = list(item_dir.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_file() or entry.name == "metadata.json":
                continue
            lower = entry.name.lower()
            if thumb is None and "_thumbnail." in lower:
                thumb = entry
            elif original is None and not lower.endswith((".json",)):
                if ext and lower.endswith(f".{ext.lower()}"):
                    original = entry
                elif original is None and "_thumbnail." not in lower:
                    original = entry

    return original, thumb


def _item_from_dir(item_dir: Path) -> Item | None:
    """Parse one images/<id>.info folder. None if metadata or media is missing."""
    if not item_dir.name.endswith(".info"):
        return None
    raw = _load_json(item_dir / "metadata.json")
    if not isinstance(raw, dict):
        return None

    name = raw.get("name") or item_dir.name
    ext = (raw.get("ext") or "").lstrip(".")
    original, thumb = _resolve_media_paths(item_dir, name, ext)
    if original is None:
        return None

    star_raw = raw.get("star")
    try:
        star = int(star_raw) if star_raw is not None else None
    except (TypeError, ValueError):
        star = None

    try:
        duration = float(raw["duration"]) if raw.get("duration") is not None else None
    except (TypeError, ValueError):
        duration = None

    tags = canonicalize_tags(raw.get("tags") or [])
    folders = list(raw.get("folders") or [])
    return Item(
        id=raw.get("id") or item_dir.name.removesuffix(".info"),
        name=name,
        ext=ext,
        tags=tags,
        folders=folders,
        path=original,
        thumb=thumb,
        is_deleted=bool(raw.get("isDeleted")),
        size=int(raw.get("size") or 0),
        width=int(raw.get("width") or 0),
        height=int(raw.get("height") or 0),
        annotation=str(raw.get("annotation") or ""),
        modification_time=int(raw.get("modificationTime") or 0),
        btime=int(raw.get("btime") or 0),
        created_time=int(raw.get("mtime") or 0),
        star=star,
        duration=duration,
        item_dir=item_dir,
        tag_set=frozenset(tags),
        folder_set=frozenset(folders),
        name_lower=name.lower(),
        ext_lower=ext.lower(),
    )


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _type_matches(ext: str, value: str) -> bool:
    v = value.lower().lstrip(".")
    e = ext.lower().lstrip(".")
    if v == "video":
        return e in VIDEO_EXTS
    if v == "audio":
        return e in AUDIO_EXTS
    if v in ("image", "img", "photo"):
        return e in IMAGE_EXTS
    return e == v


def _eval_rule(item: Item, property_name: str, method: str, value: Any) -> bool:
    """
    Evaluate one Eagle smart-folder rule.

    Empirically calibrated against this library's metadata:

    - tags/folders + intersection | union → has any of the listed values
    - tags/folders + subset | contain | all → has all of the listed values
    - tags/folders + identity           → has none of the listed values (exclude)
    - type/name/rating use equal/unequal/contain/uncontain
    - property "rating" maps to item.star (missing star treated as 0)
    """
    method = (method or "").lower()
    prop = (property_name or "").lower()

    if prop == "tags":
        vals = set(canonicalize_tags(_as_str_list(value)))
        tags = item.tag_set
        if method in ("intersection", "union"):
            return not tags.isdisjoint(vals)
        if method in ("subset", "contain", "all"):
            return bool(vals) and vals.issubset(tags)
        if method == "identity":
            return tags.isdisjoint(vals)
        if method == "equal":
            return tags == vals
        if method == "unequal":
            return tags != vals
        return False

    if prop == "folders":
        vals = set(_as_str_list(value))
        folders = item.folder_set
        if method in ("intersection", "union"):
            return not folders.isdisjoint(vals)
        if method in ("subset", "contain", "all"):
            return bool(vals) and vals.issubset(folders)
        if method == "identity":
            return folders.isdisjoint(vals)
        if method == "equal":
            return folders == vals
        if method == "unequal":
            return folders != vals
        return False

    if prop == "type":
        ok = _type_matches(item.ext_lower, str(value))
        if method == "equal":
            return ok
        if method == "unequal":
            return not ok
        if method in ("intersection", "union"):
            return ok
        if method == "identity":
            return not ok
        return False

    if prop == "name":
        name = item.name_lower
        v = str(value).lower()
        if method == "contain":
            return v in name
        if method == "uncontain":
            return v not in name
        if method == "equal":
            return name == v
        if method == "unequal":
            return name != v
        return False

    if prop == "rating":
        # Eagle stores stars as "star" on items; unrated → 0
        star = 0 if item.star is None else int(item.star)
        try:
            target = int(value)
        except (TypeError, ValueError):
            return False
        if method == "equal":
            return star == target
        if method == "unequal":
            return star != target
        if method in ("gt", "greater"):
            return star > target
        if method in ("lt", "less"):
            return star < target
        if method in ("gte", "ge"):
            return star >= target
        if method in ("lte", "le"):
            return star <= target
        return False

    if prop == "annotation":
        text = (item.annotation or "").lower()
        v = str(value).lower()
        if method == "contain":
            return v in text
        if method == "uncontain":
            return v not in text
        if method == "equal":
            return text == v
        if method == "unequal":
            return text != v
        return False

    if prop in ("createtime", "mtime"):
        from filters import item_created_ms, timestamp_matches_rule

        return timestamp_matches_rule(item_created_ms(item), method, value)
    if prop in ("btime", "importtime", "addedtime"):
        from filters import item_added_ms, timestamp_matches_rule

        return timestamp_matches_rule(item_added_ms(item), method, value)

    # Unknown property: fail closed so we don't over-include
    return False


def _eval_group(item: Item, group: dict[str, Any]) -> bool:
    rules = group.get("rules") or []
    match = str(group.get("match") or "AND").upper()
    boolean = str(group.get("boolean") or "TRUE").upper()

    if not rules:
        ok = True
    else:
        results = [
            _eval_rule(item, r.get("property"), r.get("method"), r.get("value"))
            for r in rules
        ]
        ok = any(results) if match == "OR" else all(results)

    if boolean == "FALSE":
        ok = not ok
    return ok


def eval_smart_conditions(item: Item, conditions: list[dict[str, Any]]) -> bool:
    """All condition groups must pass (AND between groups)."""
    if not conditions:
        return True
    return all(_eval_group(item, g) for g in conditions)


class EagleLibrary:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or os.environ.get("EAGLE_LIBRARY", DEFAULT_LIBRARY)).expanduser()
        self.folders: list[Folder] = []
        self.folders_by_id: dict[str, Folder] = {}
        self.smart_folders: list[SmartFolder] = []
        self.smart_folders_by_id: dict[str, SmartFolder] = {}
        self.items: list[Item] = []
        self.items_by_id: dict[str, Item] = {}
        self.folder_paths: dict[str, str] = {}  # id -> "Parent / Child"
        self.smart_folder_paths: dict[str, str] = {}
        self._query_cache: dict[tuple, list[Item]] = {}
        self._all_tags_cache: list[str] | None = None
        self._user_tags_cache: list[str] | None = None
        self._cache_generation = 0
        self._lock = threading.Lock()
        # id -> (failure_count, next_eligible_monotonic)
        self._duration_probe_failures: dict[str, tuple[int, float]] = {}

    def _clear_derived_caches(self) -> None:
        """Drop derived caches and bump generation. Caller must hold ``_lock``."""
        self._query_cache.clear()
        self._all_tags_cache = None
        self._user_tags_cache = None
        self._cache_generation += 1

    def load(self) -> None:
        if not self.root.is_dir():
            raise FileNotFoundError(f"Eagle library not found: {self.root}")

        meta_path = self.root / "metadata.json"
        meta = _load_json(meta_path)
        if not isinstance(meta, dict):
            raise RuntimeError(f"Invalid library metadata: {meta_path}")

        self.folders, self.folders_by_id = _parse_folders(meta.get("folders") or [])
        self.folder_paths = {}
        self._build_folder_paths(self.folders, [])

        self.smart_folders, self.smart_folders_by_id = _parse_smart_folders(
            meta.get("smartFolders") or []
        )
        self.smart_folder_paths = {}
        self._build_smart_folder_paths(self.smart_folders, [])

        images_dir = self.root / "images"
        items: list[Item] = []
        items_by_id: dict[str, Item] = {}

        if images_dir.is_dir():
            for item_dir in images_dir.iterdir():
                if not item_dir.is_dir():
                    continue
                item = _item_from_dir(item_dir)
                if item is None:
                    continue
                items.append(item)
                items_by_id[item.id] = item

        # Default in-memory order: added-to-library (btime), not modificationTime.
        # File-birth values in the future (clock skew) must not sort above now.
        now_ms = int(time.time() * 1000)

        def added_key(i: Item) -> int:
            t = int(i.btime or 0)
            if t <= 0 or t > now_ms + 60_000:
                t = int(i.modification_time or 0)
            return t

        items.sort(key=added_key, reverse=True)
        with self._lock:
            self.items = items
            self.items_by_id = items_by_id
            self._clear_derived_caches()

    def reload_metadata_trees(self) -> None:
        """Re-read folders + smart folders from metadata.json. Does not rescan items."""
        meta_path = self.root / "metadata.json"
        meta = _load_json(meta_path)
        if not isinstance(meta, dict):
            raise RuntimeError(f"Invalid library metadata: {meta_path}")
        self.folders, self.folders_by_id = _parse_folders(meta.get("folders") or [])
        self.folder_paths = {}
        self._build_folder_paths(self.folders, [])
        self.smart_folders, self.smart_folders_by_id = _parse_smart_folders(
            meta.get("smartFolders") or []
        )
        self.smart_folder_paths = {}
        self._build_smart_folder_paths(self.smart_folders, [])
        with self._lock:
            self._clear_derived_caches()

    def count_conditions(
        self,
        conditions: list[dict[str, Any]],
        *,
        inherited: list[dict[str, Any]] | None = None,
    ) -> int:
        """How many non-deleted items match inherited + own conditions."""
        all_c = list(inherited or []) + list(conditions or [])
        n = 0
        for item in self.items:
            if item.is_deleted:
                continue
            if eval_smart_conditions(item, all_c):
                n += 1
        return n

    def upsert_item(self, item: Item) -> Item:
        """Insert or replace one item in the in-memory model. No disk scan."""
        with self._lock:
            existing = self.items_by_id.get(item.id)
            if existing is not None:
                try:
                    idx = self.items.index(existing)
                    self.items[idx] = item
                except ValueError:
                    self.items.insert(0, item)
            else:
                self.items.insert(0, item)
            self.items_by_id[item.id] = item
            self._clear_derived_caches()
        return item

    def load_item(self, item_id: str) -> Item | None:
        """Read one images/<id>.info folder and upsert it. None if not ready."""
        item_dir = self.root / "images" / f"{item_id}.info"
        if not item_dir.is_dir():
            return None
        item = _item_from_dir(item_dir)
        if item is None:
            return None
        return self.upsert_item(item)

    def ingest_imported(self, item_id: str) -> Item | None:
        """Load a newly imported item and join it to a source set if the name
        contains an existing Eagle id (same as inspector Group)."""
        item = self.load_item(item_id)
        if item is None:
            return None
        from promptforge_stamp import apply_stamp_to_item
        from sets import join_imported_by_name

        # Idempotent: import_file usually stamped already; catches older paths.
        apply_stamp_to_item(self, item)
        item = self.items_by_id.get(item_id) or item
        join_imported_by_name(self, item)
        return self.items_by_id.get(item_id) or item

    def scan_new_items(self) -> list[Item]:
        """Load any .info folders that are not already in memory."""
        images_dir = self.root / "images"
        if not images_dir.is_dir():
            return []
        with self._lock:
            known = set(self.items_by_id)
        new: list[Item] = []
        try:
            dirs = images_dir.iterdir()
        except OSError:
            return []
        for item_dir in dirs:
            if not item_dir.is_dir() or not item_dir.name.endswith(".info"):
                continue
            iid = item_dir.name.removesuffix(".info")
            if iid in known:
                continue
            item = _item_from_dir(item_dir)
            if item is None:
                continue
            self.upsert_item(item)
            known.add(item.id)
            new.append(item)
        return new

    def _build_folder_paths(self, folders: list[Folder], prefix: list[str]) -> None:
        for folder in folders:
            parts = prefix + [folder.name]
            self.folder_paths[folder.id] = " / ".join(parts)
            if folder.children:
                self._build_folder_paths(folder.children, parts)

    def _build_smart_folder_paths(
        self, folders: list[SmartFolder], prefix: list[str]
    ) -> None:
        for folder in folders:
            parts = prefix + [folder.name]
            self.smart_folder_paths[folder.id] = " / ".join(parts)
            if folder.children:
                self._build_smart_folder_paths(folder.children, parts)

    def folder_and_descendants(self, folder_id: str) -> set[str]:
        ids = {folder_id}
        folder = self.folders_by_id.get(folder_id)
        if not folder:
            return ids

        def walk(f: Folder) -> None:
            for child in f.children:
                ids.add(child.id)
                walk(child)

        walk(folder)
        return ids

    def query(
        self,
        *,
        folder_id: str | None = None,
        smart_folder_id: str | None = None,
        include_descendants: bool = True,
        search: str = "",
        include_deleted: bool = False,
        images_only: bool = False,
        limit: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[Item]:
        def check_cancelled() -> None:
            if cancelled is not None and cancelled():
                raise QueryCancelled

        check_cancelled()
        search = search.strip().lower()
        tokens = tuple(t for t in search.split() if t)
        cache_key = (
            folder_id,
            smart_folder_id,
            include_descendants,
            tokens,
            include_deleted,
            images_only,
            limit,
        )
        with self._lock:
            cached = self._query_cache.get(cache_key)
            if cached is not None:
                return cached
            items_snapshot = self.items[:]
            cache_generation = self._cache_generation

        folder_ids: set[str] | None = None
        if folder_id:
            folder_ids = (
                self.folder_and_descendants(folder_id)
                if include_descendants
                else {folder_id}
            )

        smart: SmartFolder | None = None
        pool: list[Item]
        if smart_folder_id:
            smart = self.smart_folders_by_id.get(smart_folder_id)
            if smart is None:
                return []
            # Nested smart folders: filter parent results (much cheaper than full scan)
            if smart.parent_id and not tokens:
                parent_pool = self.query(
                    smart_folder_id=smart.parent_id,
                    include_deleted=include_deleted,
                    images_only=images_only,
                    cancelled=cancelled,
                )
                pool = []
                for item in parent_pool:
                    check_cancelled()
                    if eval_smart_conditions(item, smart.conditions):
                        pool.append(item)
                if limit is not None:
                    pool = pool[:limit]
                check_cancelled()
                with self._lock:
                    if (
                        cache_generation == self._cache_generation
                        and len(self._query_cache) < 256
                    ):
                        self._query_cache[cache_key] = pool
                return pool
            pool = items_snapshot
        else:
            pool = items_snapshot

        results: list[Item] = []
        for item in pool:
            check_cancelled()
            if not include_deleted and item.is_deleted:
                continue
            if images_only and not item.is_image:
                continue
            if folder_ids is not None and item.folder_set.isdisjoint(folder_ids):
                continue
            if smart is not None and not eval_smart_conditions(
                item, smart.inherited_conditions
            ):
                continue
            if tokens:
                hay = " ".join(
                    [
                        item.id.lower(),
                        item.name_lower,
                        item.ext_lower,
                        item.annotation.lower(),
                        " ".join(item.tags).lower(),
                    ]
                )
                if not all(tok in hay for tok in tokens):
                    continue
            results.append(item)
            if limit is not None and len(results) >= limit:
                break

        # Only cache unbounded smart/folder views (search changes too often)
        if not tokens:
            check_cancelled()
            with self._lock:
                if (
                    cache_generation == self._cache_generation
                    and len(self._query_cache) < 256
                ):
                    self._query_cache[cache_key] = results
        return results

    def flatten_folders(self) -> list[tuple[Folder, int]]:
        """Depth-first list of (folder, depth) for sidebar display."""
        out: list[tuple[Folder, int]] = []

        def walk(nodes: Iterable[Folder], depth: int) -> None:
            for node in nodes:
                out.append((node, depth))
                if node.children:
                    walk(node.children, depth + 1)

        walk(self.folders, 0)
        return out

    def flatten_smart_folders(self) -> list[tuple[SmartFolder, int]]:
        """Depth-first list of (smart folder, depth) for sidebar display."""
        out: list[tuple[SmartFolder, int]] = []

        def walk(nodes: Iterable[SmartFolder], depth: int) -> None:
            for node in nodes:
                out.append((node, depth))
                if node.children:
                    walk(node.children, depth + 1)

        walk(self.smart_folders, 0)
        return out

    def invalidate_smart_folder_cache(self, smart_folder_id: str | None = None) -> None:
        """Drop cached query results for one smart folder, or all if None."""
        with self._lock:
            if smart_folder_id is None:
                self._query_cache.clear()
            else:
                self._query_cache = {
                    k: v
                    for k, v in self._query_cache.items()
                    if k[1] != smart_folder_id
                }
            self._cache_generation += 1

    def count_smart_folder(
        self, smart_folder_id: str, *, fresh: bool = False
    ) -> int:
        """Number of non-deleted items matching a smart folder."""
        if fresh:
            self.invalidate_smart_folder_cache(smart_folder_id)
        return len(
            self.query(smart_folder_id=smart_folder_id, include_deleted=False)
        )

    def count_special_view(self, view: str) -> int:
        """Number of non-deleted items in Untagged / Uncategorized virtual views."""
        if view == "untagged":
            return sum(1 for it in self.items if not it.is_deleted and not it.tags)
        if view == "uncategorized":
            return sum(1 for it in self.items if not it.is_deleted and not it.folders)
        raise ValueError(f"unknown special view: {view}")

    def _duration_skip_key(self) -> str:
        try:
            return str(self.root.resolve())
        except OSError:
            return str(self.root)

    def _load_duration_skips(self) -> set[str]:
        try:
            raw = json.loads(_DURATION_SKIP_STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return set()
        if not isinstance(raw, dict):
            return set()
        entry = raw.get(self._duration_skip_key())
        if not isinstance(entry, dict):
            return set()
        return {str(k) for k, v in entry.items() if v}

    def _persist_duration_skip(self, item_id: str) -> None:
        key = self._duration_skip_key()
        try:
            _DURATION_SKIP_STATE.parent.mkdir(parents=True, exist_ok=True)
            try:
                raw = json.loads(_DURATION_SKIP_STATE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            bucket = raw.get(key)
            if not isinstance(bucket, dict):
                bucket = {}
            bucket[item_id] = {"skipped_at": time.time()}
            raw[key] = bucket
            _DURATION_SKIP_STATE.write_text(
                json.dumps(raw, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _duration_probe_eligible(
        self, item_id: str, *, now_mono: float, skipped: set[str]
    ) -> bool:
        if item_id in skipped:
            return False
        state = self._duration_probe_failures.get(item_id)
        if state is None:
            return True
        _count, next_ok = state
        return now_mono >= next_ok

    def _record_duration_probe_failure(self, item_id: str, *, now_mono: float) -> None:
        count, _ = self._duration_probe_failures.get(item_id, (0, 0.0))
        count += 1
        if count >= DURATION_PROBE_MAX_FAILURES:
            self._duration_probe_failures[item_id] = (count, float("inf"))
            self._persist_duration_skip(item_id)
            return
        backoff = DURATION_PROBE_BASE_BACKOFF_S * (2 ** (count - 1))
        self._duration_probe_failures[item_id] = (count, now_mono + backoff)

    def _clear_duration_probe_failure(self, item_id: str) -> None:
        self._duration_probe_failures.pop(item_id, None)

    def backfill_missing_durations(
        self,
        *,
        limit: int | None = DURATION_BACKFILL_LIMIT,
        time_budget_s: float | None = DURATION_BACKFILL_TIME_BUDGET_S,
        probe_fn: Callable[[Path], tuple[int, int, float]] | None = None,
        monotonic: Callable[[], float] | None = None,
        lock_hold_times: list[float] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> DurationBackfillBatch:
        """ffprobe audio/video items that have no duration; write onto disk.

        Probing runs **outside** the shared library write lock. The lock is
        taken only for a short metadata-write batch with per-item revalidation.

        Does not bump Eagle modificationTime. Bounded by ``limit`` and
        ``time_budget_s`` so callers can resume across passes. Repeated probe
        failures use in-process backoff, then a durable skip list.
        """
        from import_media import _video_meta
        from write import WriteError, write_item_duration, write_session

        probe = probe_fn or _video_meta
        now_mono = (monotonic or time.monotonic)()
        skipped = self._load_duration_skips()

        with self._lock:
            candidates = [
                it
                for it in self.items
                if not it.is_deleted
                and (it.is_video or it.is_audio)
                and not it.duration
                and it.item_dir is not None
                and it.path.is_file()
                and self._duration_probe_eligible(
                    it.id, now_mono=now_mono, skipped=skipped
                )
            ]

        if not candidates:
            return DurationBackfillBatch(written=[], probed=0, remaining=0)

        to_probe = candidates if limit is None else candidates[: max(0, int(limit))]
        probed_ok: list[tuple[str, Path, Path, float]] = []
        probed = 0
        deadline = (
            None
            if time_budget_s is None
            else now_mono + max(0.0, float(time_budget_s))
        )

        for it in to_probe:
            if cancelled is not None and cancelled():
                break
            if deadline is not None and (monotonic or time.monotonic)() >= deadline:
                break
            probed += 1
            try:
                _w, _h, duration = probe(it.path)
            except Exception:  # noqa: BLE001
                duration = 0.0
            if duration <= 0:
                self._record_duration_probe_failure(
                    it.id, now_mono=(monotonic or time.monotonic)()
                )
                continue
            probed_ok.append((it.id, it.item_dir, it.path, float(duration)))

        written: list[tuple[str, float]] = []
        # Finish a short write batch for probes already done, even if cancelled.
        if probed_ok:
            hold_start = (monotonic or time.monotonic)()
            try:
                with write_session(self.root):
                    for item_id, item_dir, path, duration in probed_ok:
                        current = self.items_by_id.get(item_id)
                        if current is None or current.is_deleted:
                            continue
                        if current.duration:
                            self._clear_duration_probe_failure(item_id)
                            continue
                        if current.item_dir != item_dir or current.path != path:
                            continue
                        if not item_dir.is_dir() or not path.is_file():
                            continue
                        try:
                            write_item_duration(item_dir, duration)
                        except WriteError:
                            self._record_duration_probe_failure(
                                item_id,
                                now_mono=(monotonic or time.monotonic)(),
                            )
                            continue
                        current.duration = duration
                        self._clear_duration_probe_failure(item_id)
                        written.append((item_id, duration))
            except WriteError:
                pass
            if lock_hold_times is not None:
                lock_hold_times.append((monotonic or time.monotonic)() - hold_start)

        # Recompute remaining eligible after this pass (backoff/skips applied).
        now_after = (monotonic or time.monotonic)()
        skipped_after = self._load_duration_skips()
        with self._lock:
            remaining = sum(
                1
                for it in self.items
                if not it.is_deleted
                and (it.is_video or it.is_audio)
                and not it.duration
                and it.item_dir is not None
                and it.path.is_file()
                and self._duration_probe_eligible(
                    it.id, now_mono=now_after, skipped=skipped_after
                )
            )
        return DurationBackfillBatch(
            written=written, probed=probed, remaining=remaining
        )

    def items_in_set(self, tag: str) -> list[Item]:
        """Non-deleted items that carry this set: tag."""
        from sets import items_with_set_tag

        return items_with_set_tag(self.items, tag)

    # ── Writes (tags, ratings) ────────────────────────────────────────

    def _invalidate_caches(self) -> None:
        with self._lock:
            self._clear_derived_caches()

    def _refresh_item_derived(self, item: Item) -> None:
        item.tags = canonicalize_tags(item.tags)
        item.tag_set = frozenset(item.tags)
        item.folder_set = frozenset(item.folders)
        item.name_lower = item.name.lower()
        item.ext_lower = item.ext.lower()

    def all_tags(self, *, include_set: bool = True) -> list[str]:
        """Unique tags in first-seen order. ``include_set=False`` drops ``set:``."""
        if self._all_tags_cache is None or self._user_tags_cache is None:
            self._fill_tag_caches()
        cached = self._all_tags_cache if include_set else self._user_tags_cache
        return list(cached or [])

    def _fill_tag_caches(self) -> None:
        from sets import is_set_tag

        seen: set[str] = set()
        all_tags: list[str] = []
        user: list[str] = []
        for it in self.items:
            for t in it.tags:
                if t in seen:
                    continue
                seen.add(t)
                all_tags.append(t)
                if not is_set_tag(t):
                    user.append(t)
        for folder in self.folders_by_id.values():
            for t in folder.tags:
                if t in seen:
                    continue
                seen.add(t)
                all_tags.append(t)
                if not is_set_tag(t):
                    user.append(t)
        self._all_tags_cache = all_tags
        self._user_tags_cache = user

    def folder_ancestor_ids(self, folder_id: str) -> list[str]:
        """Root → … → folder_id (inclusive)."""
        chain: list[str] = []
        seen: set[str] = set()
        cur: str | None = folder_id
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            folder = self.folders_by_id.get(cur)
            cur = folder.parent_id if folder else None
        chain.reverse()
        return chain

    def auto_tags_for_folders(self, folder_ids: Iterable[str]) -> list[str]:
        """
        Eagle-style folder auto-tags for the given folders.

        Tags from each folder and its ancestors are merged (order preserved,
        de-duplicated). Nested auto-tag folders stack when an item is filed deep.
        """
        out: list[str] = []
        seen: set[str] = set()
        for fid in folder_ids:
            for aid in self.folder_ancestor_ids(fid):
                folder = self.folders_by_id.get(aid)
                if not folder:
                    continue
                for t in canonicalize_tags(folder.tags):
                    if t and t not in seen:
                        seen.add(t)
                        out.append(t)
        return out

    def set_folder_auto_tags(self, folder_id: str, tags: list[str]) -> Folder:
        """Persist Eagle folder auto-tags into library metadata.json."""
        from write import WriteError, set_folder_auto_tags, write_session

        if folder_id not in self.folders_by_id:
            raise WriteError(f"Unknown folder id: {folder_id}")
        cleaned = canonicalize_tags(tags)
        with write_session(self.root):
            set_folder_auto_tags(self.root, folder_id, cleaned)
        folder = self.folders_by_id[folder_id]
        folder.tags = cleaned
        return folder

    def set_items_deleted(
        self, item_ids: list[str], *, deleted: bool
    ) -> tuple[list[str], list[str]]:
        """
        Soft-delete or restore items (Eagle isDeleted / deletedTime).

        Returns (ok_ids, error messages). Files are not removed from disk.
        """
        from write import (
            WriteError,
            apply_deleted,
            load_item_metadata,
            save_item_metadata,
            write_session,
        )

        ok_ids: list[str] = []
        errors: list[str] = []
        if not item_ids:
            return ok_ids, errors
        try:
            with write_session(self.root):
                for iid in item_ids:
                    item = self.items_by_id.get(iid)
                    if item is None:
                        errors.append(f"{iid}: unknown")
                        continue
                    if item.item_dir is None or not item.item_dir.is_dir():
                        errors.append(f"{iid}: no item dir")
                        continue
                    if bool(item.is_deleted) == bool(deleted):
                        # Already in desired state — still count as ok for undo noop
                        ok_ids.append(iid)
                        continue
                    try:
                        data = load_item_metadata(item.item_dir)
                        apply_deleted(data, deleted)
                        save_item_metadata(self.root, item.item_dir, data)
                        item.is_deleted = deleted
                        item.modification_time = int(
                            data.get("modificationTime") or item.modification_time
                        )
                        self._refresh_item_derived(item)
                        ok_ids.append(iid)
                    except WriteError as exc:
                        errors.append(f"{iid}: {exc}")
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{iid}: {exc}")
        except WriteError as exc:
            return [], [str(exc)]
        self._invalidate_caches()
        return ok_ids, errors

    def update_item(
        self,
        item_id: str,
        *,
        star: int | None | object = ...,  # type: ignore[assignment]
        annotation: str | None | object = ...,  # type: ignore[assignment]
        set_tags: list[str] | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        set_folders: list[str] | None = None,
        add_folders: list[str] | None = None,
        remove_folders: list[str] | None = None,
    ) -> Item:
        """
        Persist rating, annotation, tag, and/or folder changes for one item.

        star: 1–5 to set, 0 or None to clear, omit (Ellipsis) to leave unchanged.
        annotation: text to set, empty string to clear, omit (Ellipsis) to leave unchanged.
        """
        from write import (  # local import avoids cycles
            WriteError,
            apply_annotation,
            apply_folders,
            apply_star,
            apply_tags,
            load_item_metadata,
            save_item_metadata,
            write_session,
        )

        item = self.items_by_id.get(item_id)
        if item is None:
            raise WriteError(f"Unknown item id: {item_id}")
        if item.item_dir is None or not item.item_dir.is_dir():
            raise WriteError(f"No item directory for {item_id}")

        with write_session(self.root):
            data = load_item_metadata(item.item_dir)
            if star is not ...:
                apply_star(data, None if star in (0, None) else int(star))  # type: ignore[arg-type]
            if annotation is not ...:
                apply_annotation(data, "" if annotation is None else str(annotation))
            if set_tags is not None or add_tags is not None or remove_tags is not None:
                apply_tags(
                    data,
                    set_tags=set_tags,
                    add_tags=add_tags,
                    remove_tags=remove_tags,
                )
            if (
                set_folders is not None
                or add_folders is not None
                or remove_folders is not None
            ):
                before = set(data.get("folders") or [])
                apply_folders(
                    data,
                    set_folders=set_folders,
                    add_folders=add_folders,
                    remove_folders=remove_folders,
                )
                after = set(data.get("folders") or [])
                added = after - before
                if added:
                    auto = self.auto_tags_for_folders(added)
                    if auto:
                        apply_tags(data, add_tags=auto)
            save_item_metadata(self.root, item.item_dir, data)

        # Update in-memory model
        if star is not ...:
            item.star = None if star in (0, None) else int(star)  # type: ignore[arg-type]
        if annotation is not ...:
            item.annotation = str(data.get("annotation") or "")
        # Tags may change from explicit edit and/or folder auto-tags
        if (
            set_tags is not None
            or add_tags is not None
            or remove_tags is not None
            or set_folders is not None
            or add_folders is not None
        ):
            item.tags = list(data.get("tags") or [])
        if (
            set_folders is not None
            or add_folders is not None
            or remove_folders is not None
        ):
            item.folders = list(data.get("folders") or [])
        item.modification_time = int(data.get("modificationTime") or item.modification_time)
        self._refresh_item_derived(item)
        self._invalidate_caches()
        return item

    def rename_item(self, item_id: str, new_name: str) -> Item:
        """Rename the item stem. Media file and matching thumbnails move with it."""
        from write import WriteError, rename_item_media, write_session

        item = self.items_by_id.get(item_id)
        if item is None:
            raise WriteError(f"Unknown item id: {item_id}")
        if item.item_dir is None or not item.item_dir.is_dir():
            raise WriteError(f"No item directory for {item_id}")

        with write_session(self.root):
            cleaned, new_path, new_thumb = rename_item_media(
                self.root, item, new_name
            )

        item.name = cleaned
        item.path = new_path
        if new_thumb is not None:
            item.thumb = new_thumb
        item.modification_time = int(time.time() * 1000)
        self._refresh_item_derived(item)
        self._invalidate_caches()
        return item

    def update_items_batch(
        self,
        item_ids: list[str],
        *,
        star: int | None | object = ...,  # type: ignore[assignment]
        annotation: str | None | object = ...,  # type: ignore[assignment]
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        add_folders: list[str] | None = None,
        remove_folders: list[str] | None = None,
    ) -> tuple[int, list[str]]:
        """Apply the same star/annotation/tag/folder delta to many items. Returns (ok_count, errors)."""
        from write import WriteError, write_session

        ok = 0
        errors: list[str] = []
        # One lock for the whole batch
        try:
            with write_session(self.root):
                for iid in item_ids:
                    try:
                        # Nested session would re-lock; do inner write without new lock
                        self._update_item_unlocked(
                            iid,
                            star=star,
                            annotation=annotation,
                            add_tags=add_tags,
                            remove_tags=remove_tags,
                            add_folders=add_folders,
                            remove_folders=remove_folders,
                        )
                        ok += 1
                    except WriteError as exc:
                        errors.append(f"{iid}: {exc}")
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{iid}: {exc}")
        except WriteError as exc:
            return 0, [str(exc)]
        self._invalidate_caches()
        return ok, errors

    def import_inbox(
        self,
        inbox: Path | str | None = None,
        *,
        folder_ids: list[str] | None = None,
        tags: list[str] | None = None,
        move_source: bool = True,
    ) -> list:
        """Import all media from inbox into this library; reload new items into memory."""
        from import_media import import_inbox as _import_inbox

        dest = Path(inbox).expanduser() if inbox else inbox_path()
        results = _import_inbox(
            self.root,
            dest,
            folder_ids=folder_ids,
            tags=tags,
            move_source=move_source,
        )
        for r in results:
            if getattr(r, "ok", False) and getattr(r, "item_id", None) and not getattr(r, "reused", False):
                self.ingest_imported(r.item_id)
        return results

    def import_path(
        self,
        path: Path | str,
        *,
        folder_ids: list[str] | None = None,
        tags: list[str] | None = None,
        move_source: bool = True,
    ):
        from import_media import import_file

        result = import_file(
            self.root,
            Path(path),
            folder_ids=folder_ids,
            tags=tags,
            move_source=move_source,
        )
        if result.ok and result.item_id and not result.reused:
            self.ingest_imported(result.item_id)
        return result

    def _update_item_unlocked(
        self,
        item_id: str,
        *,
        star: int | None | object = ...,
        annotation: str | None | object = ...,
        set_tags: list[str] | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        set_folders: list[str] | None = None,
        add_folders: list[str] | None = None,
        remove_folders: list[str] | None = None,
    ) -> Item:
        from write import (
            WriteError,
            apply_annotation,
            apply_folders,
            apply_star,
            apply_tags,
            load_item_metadata,
            save_item_metadata,
        )

        item = self.items_by_id.get(item_id)
        if item is None:
            raise WriteError(f"Unknown item id: {item_id}")
        if item.item_dir is None or not item.item_dir.is_dir():
            raise WriteError(f"No item directory for {item_id}")

        data = load_item_metadata(item.item_dir)
        if star is not ...:
            apply_star(data, None if star in (0, None) else int(star))  # type: ignore[arg-type]
        if annotation is not ...:
            apply_annotation(data, "" if annotation is None else str(annotation))
        if set_tags is not None or add_tags is not None or remove_tags is not None:
            apply_tags(
                data,
                set_tags=set_tags,
                add_tags=add_tags,
                remove_tags=remove_tags,
            )
        if (
            set_folders is not None
            or add_folders is not None
            or remove_folders is not None
        ):
            before = set(data.get("folders") or [])
            apply_folders(
                data,
                set_folders=set_folders,
                add_folders=add_folders,
                remove_folders=remove_folders,
            )
            after = set(data.get("folders") or [])
            added = after - before
            if added:
                auto = self.auto_tags_for_folders(added)
                if auto:
                    apply_tags(data, add_tags=auto)
        save_item_metadata(self.root, item.item_dir, data)

        if star is not ...:
            item.star = None if star in (0, None) else int(star)  # type: ignore[arg-type]
        if annotation is not ...:
            item.annotation = str(data.get("annotation") or "")
        if (
            set_tags is not None
            or add_tags is not None
            or remove_tags is not None
            or set_folders is not None
            or add_folders is not None
        ):
            item.tags = list(data.get("tags") or [])
        if (
            set_folders is not None
            or add_folders is not None
            or remove_folders is not None
        ):
            item.folders = list(data.get("folders") or [])
        item.modification_time = int(data.get("modificationTime") or item.modification_time)
        self._refresh_item_derived(item)
        return item
