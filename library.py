"""Read-only Eagle.cool library access, including smart folders."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEFAULT_LIBRARY = Path.home() / "Dropbox/ISAAC/GENNIE/Eunbi.library"

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
    star: int | None = None  # Eagle UI "rating"; absent = unrated
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
        folder = Folder(
            id=node["id"],
            name=node.get("name") or "(unnamed)",
            parent_id=parent_id,
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


def _resolve_media_paths(item_dir: Path, name: str, ext: str) -> tuple[Path | None, Path | None]:
    """Find original file and thumbnail inside an Eagle item folder."""
    original: Path | None = None
    thumb: Path | None = None

    candidates = [
        item_dir / f"{name}.{ext}",
        item_dir / f"{name}.{ext.lower()}",
        item_dir / f"{name}.{ext.upper()}",
    ]
    for c in candidates:
        if c.is_file():
            original = c
            break

    thumb_candidates = [
        item_dir / f"{name}_thumbnail.png",
        item_dir / f"{name}_thumbnail.jpg",
        item_dir / f"{name}_thumbnail.webp",
    ]
    for c in thumb_candidates:
        if c.is_file():
            thumb = c
            break

    if original is None or thumb is None:
        for entry in item_dir.iterdir():
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
    - tags/folders + identity           → has none of the listed values (exclude)
    - type/name/rating use equal/unequal/contain/uncontain
    - property "rating" maps to item.star (missing star treated as 0)
    """
    method = (method or "").lower()
    prop = (property_name or "").lower()

    if prop == "tags":
        vals = set(_as_str_list(value))
        tags = item.tag_set
        if method in ("intersection", "union"):
            return not tags.isdisjoint(vals)
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
                if not item_dir.is_dir() or not item_dir.name.endswith(".info"):
                    continue
                raw = _load_json(item_dir / "metadata.json")
                if not isinstance(raw, dict):
                    continue

                name = raw.get("name") or item_dir.name
                ext = (raw.get("ext") or "").lstrip(".")
                original, thumb = _resolve_media_paths(item_dir, name, ext)
                if original is None:
                    continue

                star_raw = raw.get("star")
                star: int | None
                try:
                    star = int(star_raw) if star_raw is not None else None
                except (TypeError, ValueError):
                    star = None

                tags = list(raw.get("tags") or [])
                folders = list(raw.get("folders") or [])
                item = Item(
                    id=raw.get("id") or item_dir.name.removesuffix(".info"),
                    name=name,
                    ext=ext,
                    tags=tags,
                    folders=folders,
                    path=original.resolve(),
                    thumb=thumb.resolve() if thumb else None,
                    is_deleted=bool(raw.get("isDeleted")),
                    size=int(raw.get("size") or 0),
                    width=int(raw.get("width") or 0),
                    height=int(raw.get("height") or 0),
                    annotation=str(raw.get("annotation") or ""),
                    modification_time=int(raw.get("modificationTime") or 0),
                    star=star,
                    tag_set=frozenset(tags),
                    folder_set=frozenset(folders),
                    name_lower=name.lower(),
                    ext_lower=ext.lower(),
                )
                items.append(item)
                items_by_id[item.id] = item

        items.sort(key=lambda i: i.modification_time, reverse=True)
        self.items = items
        self.items_by_id = items_by_id
        # Invalidate query caches after reload
        self._query_cache = {}
        # Warm top-level smart folders so first click is instant
        for sf in self.smart_folders:
            self.query(smart_folder_id=sf.id)

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
    ) -> list[Item]:
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
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            return cached

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
                )
                pool = [
                    item
                    for item in parent_pool
                    if eval_smart_conditions(item, smart.conditions)
                ]
                if limit is not None:
                    pool = pool[:limit]
                if len(self._query_cache) < 256:
                    self._query_cache[cache_key] = pool
                return pool
            pool = self.items
        else:
            pool = self.items

        results: list[Item] = []
        for item in pool:
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
        if not tokens and len(self._query_cache) < 256:
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
