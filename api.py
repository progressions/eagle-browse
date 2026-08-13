"""
Agent-facing API for Eagle.cool libraries.

Use from Python::

    from api import EagleAPI
    api = EagleAPI()  # or EagleAPI("/path/to/Lib.library")
    api.search(tags=["eunbi"], rating_min=3, limit=10)

Or CLI (JSON stdout)::

    eagle-api search --tag eunbi --rating-min 3
    eagle-api crop <id> --aspect 9:16 --mode new
    eagle-api smart-folder show "Eunbi/images"
    eagle-api smart-folder create --name "Sofie videos 3+" --tag sofie --type video --rating-min 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from library import DEFAULT_LIBRARY, EagleLibrary, Item, SmartFolder
from write import (
    WriteError,
    _count_smart_subtree,
    create_smart_folder_node,
    delete_smart_folder_node,
    update_smart_folder_node,
    write_session,
)

# Same sentinel write.update_smart_folder_node uses for “leave parent as-is”.
_UNSET = ...


def _item_to_dict(it: Item, *, library: EagleLibrary | None = None) -> dict[str, Any]:
    folder_names: list[str] = []
    if library is not None:
        for fid in it.folders:
            folder_names.append(library.folder_paths.get(fid, fid))
    return {
        "id": it.id,
        "name": it.name,
        "ext": it.ext,
        "display_name": it.display_name,
        "path": str(it.path),
        "thumb": str(it.thumb) if it.thumb else None,
        "tags": list(it.tags),
        "folders": list(it.folders),
        "folder_names": folder_names,
        "star": it.star,
        "rating": it.star,  # alias
        "width": it.width,
        "height": it.height,
        "duration": it.duration,
        "size": it.size,
        "annotation": it.annotation,
        "is_deleted": it.is_deleted,
        "modification_time": it.modification_time,
        "is_image": it.is_image,
        "is_video": it.is_video,
        "is_audio": it.is_audio,
    }


def _smart_to_dict(sf: SmartFolder, library: EagleLibrary) -> dict[str, Any]:
    return {
        "id": sf.id,
        "name": sf.name,
        "path": library.smart_folder_paths.get(sf.id, sf.name),
        "parent_id": sf.parent_id,
        "conditions": sf.conditions,
        "children": [
            {"id": c.id, "name": c.name, "path": library.smart_folder_paths.get(c.id, c.name)}
            for c in sf.children
        ],
    }


class EagleAPI:
    """High-level query + mutate interface for agents."""

    def __init__(self, library: Path | str | None = None, *, auto_load: bool = True):
        self.library = EagleLibrary(library or os.environ.get("EAGLE_LIBRARY", DEFAULT_LIBRARY))
        if auto_load:
            self.library.load()

    def reload(self) -> dict[str, Any]:
        self.library.load()
        return {
            "ok": True,
            "items": len(self.library.items),
            "folders": len(self.library.folders_by_id),
            "smart_folders": len(self.library.smart_folders_by_id),
            "library": str(self.library.root),
        }

    # ── Resolve helpers ───────────────────────────────────────────────

    def resolve_folder(self, name_or_path_or_id: str) -> str | None:
        """Return folder id from id, full path, or unique leaf name."""
        key = (name_or_path_or_id or "").strip()
        if not key:
            return None
        if key in self.library.folders_by_id:
            return key
        # Full path match
        for fid, path in self.library.folder_paths.items():
            if path == key or path.lower() == key.lower():
                return fid
        # Leaf name (unique)
        matches = [
            fid
            for fid, path in self.library.folder_paths.items()
            if path.split(" / ")[-1].lower() == key.lower()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Prefer exact leaf among shortest paths
            matches.sort(key=lambda f: len(self.library.folder_paths[f]))
            return matches[0]
        return None

    def resolve_smart_folder(self, name_or_path_or_id: str) -> str | None:
        """Return smart folder id from id, path like 'Eunbi/images', or name."""
        key = (name_or_path_or_id or "").strip()
        if not key:
            return None
        if key in self.library.smart_folders_by_id:
            return key
        # Normalize separators
        norm = key.replace("/", " / ").replace("  ", " ")
        for sid, path in self.library.smart_folder_paths.items():
            if path == key or path == norm:
                return sid
            if path.lower() == key.lower() or path.lower() == norm.lower():
                return sid
            # slash form Eunbi/images
            slash = path.replace(" / ", "/")
            if slash.lower() == key.lower().replace(" / ", "/"):
                return sid
        matches = [
            sid
            for sid, path in self.library.smart_folder_paths.items()
            if path.split(" / ")[-1].lower() == key.lower()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            matches.sort(key=lambda s: len(self.library.smart_folder_paths[s]))
            return matches[0]
        return None

    # ── Search ────────────────────────────────────────────────────────

    def search(
        self,
        *,
        query: str = "",
        tags: list[str] | None = None,
        tags_all: list[str] | None = None,
        exclude_tags: list[str] | None = None,
        folder: str | None = None,
        folder_id: str | None = None,
        include_descendants: bool = True,
        smart_folder: str | None = None,
        smart_folder_id: str | None = None,
        name_contains: str | None = None,
        rating: int | None = None,
        rating_min: int | None = None,
        rating_max: int | None = None,
        media_type: str | None = None,  # image|video|audio|ext
        untagged: bool = False,
        uncategorized: bool = False,
        include_deleted: bool = False,
        limit: int | None = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        Search items. Returns JSON-serializable dict with matches.

        *folder* / *smart_folder* accept names, paths, or ids.
        """
        fid = folder_id or (self.resolve_folder(folder) if folder else None)
        if folder and not fid:
            return {"ok": False, "error": f"Unknown folder: {folder}", "items": [], "total": 0}

        sid = smart_folder_id or (
            self.resolve_smart_folder(smart_folder) if smart_folder else None
        )
        if smart_folder and not sid:
            return {
                "ok": False,
                "error": f"Unknown smart folder: {smart_folder}",
                "items": [],
                "total": 0,
                "hint": "Use smart-folder list for paths like Eunbi/images",
            }

        items = self.library.query(
            folder_id=fid,
            smart_folder_id=sid,
            include_descendants=include_descendants,
            search=query or "",
            include_deleted=include_deleted,
        )

        tags_any = {t.lower() for t in (tags or []) if t}
        tags_need = {t.lower() for t in (tags_all or []) if t}
        tags_ex = {t.lower() for t in (exclude_tags or []) if t}
        name_q = (name_contains or "").lower().strip()
        mtype = (media_type or "").lower().strip().lstrip(".")

        filtered: list[Item] = []
        for it in items:
            if untagged and it.tags:
                continue
            if uncategorized and it.folders:
                continue
            tagset = {t.lower() for t in it.tags}
            if tags_any and tagset.isdisjoint(tags_any):
                continue
            if tags_need and not tags_need.issubset(tagset):
                continue
            if tags_ex and not tagset.isdisjoint(tags_ex):
                continue
            if name_q and name_q not in it.name_lower and name_q not in it.display_name.lower():
                continue
            star = 0 if it.star is None else int(it.star)
            if rating is not None and star != int(rating):
                continue
            if rating_min is not None and star < int(rating_min):
                continue
            if rating_max is not None and star > int(rating_max):
                continue
            if mtype:
                if mtype in ("image", "img", "photo"):
                    if not it.is_image:
                        continue
                elif mtype == "video":
                    if not it.is_video:
                        continue
                elif mtype == "audio":
                    if not it.is_audio:
                        continue
                elif it.ext_lower != mtype:
                    continue
            filtered.append(it)

        total = len(filtered)
        if offset:
            filtered = filtered[offset:]
        if limit is not None:
            filtered = filtered[: max(0, int(limit))]

        return {
            "ok": True,
            "total": total,
            "count": len(filtered),
            "offset": offset,
            "limit": limit,
            "items": [_item_to_dict(it, library=self.library) for it in filtered],
            "filters": {
                "query": query,
                "tags": tags,
                "folder": folder or folder_id,
                "smart_folder": smart_folder or smart_folder_id,
                "rating_min": rating_min,
                "rating": rating,
                "type": media_type,
            },
        }

    def get(self, item_id: str) -> dict[str, Any]:
        it = self.library.items_by_id.get(item_id)
        if it is None:
            return {"ok": False, "error": f"Unknown item: {item_id}"}
        return {"ok": True, "item": _item_to_dict(it, library=self.library)}

    # ── Mutate items ──────────────────────────────────────────────────

    def add_tags(self, item_ids: list[str] | str, tags: list[str] | str) -> dict[str, Any]:
        return self._batch_tag(item_ids, tags, add=True)

    def remove_tags(self, item_ids: list[str] | str, tags: list[str] | str) -> dict[str, Any]:
        return self._batch_tag(item_ids, tags, add=False)

    def _batch_tag(
        self, item_ids: list[str] | str, tags: list[str] | str, *, add: bool
    ) -> dict[str, Any]:
        ids = [item_ids] if isinstance(item_ids, str) else list(item_ids)
        tag_list = [tags] if isinstance(tags, str) else list(tags)
        tag_list = [t.strip() for t in tag_list if t and str(t).strip()]
        if not ids or not tag_list:
            return {"ok": False, "error": "item_ids and tags required"}
        try:
            if len(ids) == 1:
                if add:
                    self.library.update_item(ids[0], add_tags=tag_list)
                else:
                    self.library.update_item(ids[0], remove_tags=tag_list)
                ok, errors = 1, []
            else:
                if add:
                    ok, errors = self.library.update_items_batch(ids, add_tags=tag_list)
                else:
                    ok, errors = self.library.update_items_batch(ids, remove_tags=tag_list)
        except WriteError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": errors == [] or ok > 0,
            "updated": ok,
            "errors": errors,
            "tags": tag_list,
            "action": "add" if add else "remove",
        }

    def add_folders(
        self, item_ids: list[str] | str, folders: list[str] | str
    ) -> dict[str, Any]:
        return self._batch_folder(item_ids, folders, add=True)

    def remove_folders(
        self, item_ids: list[str] | str, folders: list[str] | str
    ) -> dict[str, Any]:
        return self._batch_folder(item_ids, folders, add=False)

    def _batch_folder(
        self, item_ids: list[str] | str, folders: list[str] | str, *, add: bool
    ) -> dict[str, Any]:
        ids = [item_ids] if isinstance(item_ids, str) else list(item_ids)
        raw = [folders] if isinstance(folders, str) else list(folders)
        fids: list[str] = []
        unresolved: list[str] = []
        for f in raw:
            rid = self.resolve_folder(str(f))
            if rid:
                fids.append(rid)
            else:
                unresolved.append(str(f))
        if unresolved:
            return {"ok": False, "error": f"Unknown folders: {unresolved}"}
        if not ids or not fids:
            return {"ok": False, "error": "item_ids and folders required"}
        try:
            if len(ids) == 1:
                if add:
                    self.library.update_item(ids[0], add_folders=fids)
                else:
                    self.library.update_item(ids[0], remove_folders=fids)
                ok, errors = 1, []
            else:
                if add:
                    ok, errors = self.library.update_items_batch(ids, add_folders=fids)
                else:
                    ok, errors = self.library.update_items_batch(
                        ids, remove_folders=fids
                    )
        except WriteError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": errors == [] or ok > 0,
            "updated": ok,
            "errors": errors,
            "folder_ids": fids,
            "action": "add" if add else "remove",
        }

    def set_rating(self, item_ids: list[str] | str, rating: int) -> dict[str, Any]:
        """rating 1–5, or 0 to clear."""
        ids = [item_ids] if isinstance(item_ids, str) else list(item_ids)
        star = int(rating)
        if star < 0 or star > 5:
            return {"ok": False, "error": "rating must be 0–5"}
        try:
            if len(ids) == 1:
                self.library.update_item(ids[0], star=star if star else None)
                ok, errors = 1, []
            else:
                ok, errors = self.library.update_items_batch(
                    ids, star=star if star else None
                )
        except WriteError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": ok > 0 or not errors, "updated": ok, "errors": errors, "rating": star}

    def crop(
        self,
        item_id: str,
        *,
        mode: str = "overwrite",
        x: int | None = None,
        y: int | None = None,
        width: int | None = None,
        height: int | None = None,
        aspect: str | None = None,
        anchor: str = "center",
    ) -> dict[str, Any]:
        """
        Crop an image item.

        Parameters
        ----------
        item_id:
            Eagle item id.
        mode:
            ``"overwrite"`` — replace the original media (tags/folders kept).
            ``"new"`` / ``"save-as"`` — create a fresh library item with the crop
            and **no tags / no folders** (same as a new import).
        x, y:
            Top-left of the crop in source pixels. Omit to place via *anchor*.
        width, height:
            Crop size in source pixels. With *aspect*, one side can be omitted.
        aspect:
            Ratio lock: ``"9:16"``, ``"3:4"``, ``"1:1"``, ``"orig"``, ``"free"``.
            Alone → largest centered fit of that ratio.
        anchor:
            Placement when x/y omitted: ``center`` (default), ``top``, ``bottom``,
            ``left``, ``right``, ``top-left``, ``top-right``, ``bottom-left``,
            ``bottom-right``.

        Returns JSON-shaped dict with ``ok``, ``mode``, ``rect``, and ``item``
        (the overwritten or newly created item).
        """
        from crop import (
            apply_crop_to_item,
            resolve_crop_rect,
            save_crop_as_new_item,
        )

        iid = (item_id or "").strip()
        if not iid:
            return {"ok": False, "error": "item id required"}
        item = self.library.items_by_id.get(iid)
        if item is None:
            return {"ok": False, "error": f"Unknown item id: {iid}"}
        if not item.is_image:
            return {"ok": False, "error": "Crop only works on images"}

        mode_l = (mode or "overwrite").strip().lower().replace("_", "-")
        if mode_l in ("overwrite", "save", "original", "in-place", "inplace"):
            mode_l = "overwrite"
        elif mode_l in ("new", "save-as", "saveas", "as-new", "copy"):
            mode_l = "new"
        else:
            return {
                "ok": False,
                "error": f"Unknown mode {mode!r}; use overwrite or new",
            }

        try:
            rect = resolve_crop_rect(
                int(item.width or 0),
                int(item.height or 0),
                x=x,
                y=y,
                width=width,
                height=height,
                aspect=aspect,
                anchor=anchor,
            )
            if mode_l == "overwrite":
                out = apply_crop_to_item(self.library.root, item, rect)
                # Keep map pointer (mutated in place)
                self.library.items_by_id[out.id] = out
            else:
                out = save_crop_as_new_item(self.library.root, item, rect)
                self.library.items_by_id[out.id] = out
                self.library.items.insert(0, out)
            self.library._invalidate_caches()  # noqa: SLF001
        except WriteError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "mode": mode_l,
            "source_id": iid,
            "rect": {"x": rect.x, "y": rect.y, "width": rect.w, "height": rect.h},
            "item": _item_to_dict(out, library=self.library),
        }

    # ── Catalog ───────────────────────────────────────────────────────

    def list_tags(self, *, limit: int | None = None) -> dict[str, Any]:
        tags = self.library.all_tags()
        if limit is not None:
            tags = tags[: int(limit)]
        return {"ok": True, "count": len(tags), "tags": tags}

    def list_folders(self) -> dict[str, Any]:
        rows = []
        for folder, depth in self.library.flatten_folders():
            rows.append(
                {
                    "id": folder.id,
                    "name": folder.name,
                    "path": self.library.folder_paths.get(folder.id, folder.name),
                    "depth": depth,
                    "auto_tags": list(folder.tags),
                }
            )
        return {"ok": True, "count": len(rows), "folders": rows}

    def list_smart_folders(self) -> dict[str, Any]:
        rows = []
        for sf, depth in self.library.flatten_smart_folders():
            rows.append(
                {
                    "id": sf.id,
                    "name": sf.name,
                    "path": self.library.smart_folder_paths.get(sf.id, sf.name),
                    "depth": depth,
                    "parent_id": sf.parent_id,
                    "rule_groups": len(sf.conditions),
                }
            )
        return {"ok": True, "count": len(rows), "smart_folders": rows}

    def get_smart_folder(self, name_or_path_or_id: str) -> dict[str, Any]:
        sid = self.resolve_smart_folder(name_or_path_or_id)
        if not sid:
            return {"ok": False, "error": f"Unknown smart folder: {name_or_path_or_id}"}
        sf = self.library.smart_folders_by_id[sid]
        data = _smart_to_dict(sf, self.library)
        # Also return a sample count
        matches = self.library.query(smart_folder_id=sid, include_deleted=False)
        data["match_count"] = len(matches)
        return {"ok": True, "smart_folder": data}

    def create_smart_folder(
        self,
        name: str,
        *,
        parent: str | None = None,
        tags: list[str] | None = None,
        tags_exclude: list[str] | None = None,
        folder: str | None = None,
        media_type: str | None = None,
        rating: int | None = None,
        rating_min: int | None = None,
        name_contains: str | None = None,
        match: str = "AND",
        description: str = "",
        conditions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Create a smart folder. Either pass raw *conditions* or convenience filters.

        Example: Sofie videos with 3+ stars::

            api.create_smart_folder(
                "Sofie videos 3+",
                parent="Sofie",  # or path
                tags=["sofie"],
                media_type="video",
                rating_min=3,
            )
        """
        parent_id = None
        if parent:
            parent_id = self.resolve_smart_folder(parent)
            if not parent_id:
                return {"ok": False, "error": f"Unknown parent smart folder: {parent}"}

        if conditions is None:
            built = self._conditions_from_filters(
                tags=tags,
                tags_exclude=tags_exclude,
                folder=folder,
                media_type=media_type,
                rating=rating,
                rating_min=rating_min,
                name_contains=name_contains,
                match=match,
            )
            if not built.get("ok"):
                return built
            conditions = built["conditions"]

        try:
            with write_session(self.library.root):
                node = create_smart_folder_node(
                    self.library.root,
                    name=name,
                    conditions=conditions,
                    parent_id=parent_id,
                    description=description,
                )
        except WriteError as exc:
            return {"ok": False, "error": str(exc)}

        self.library.reload_metadata_trees()
        return {
            "ok": True,
            "smart_folder": {
                "id": node["id"],
                "name": node["name"],
                "path": self.library.smart_folder_paths.get(node["id"], node["name"]),
                "parent_id": parent_id,
                "conditions": node["conditions"],
            },
        }

    def _conditions_from_filters(
        self,
        *,
        tags: list[str] | None = None,
        tags_exclude: list[str] | None = None,
        folder: str | None = None,
        media_type: str | None = None,
        rating: int | None = None,
        rating_min: int | None = None,
        name_contains: str | None = None,
        match: str = "AND",
    ) -> dict[str, Any]:
        rules: list[dict[str, Any]] = []
        if tags:
            rules.append({"property": "tags", "method": "union", "value": list(tags)})
        if tags_exclude:
            rules.append(
                {
                    "property": "tags",
                    "method": "identity",
                    "value": list(tags_exclude),
                }
            )
        if folder:
            fid = self.resolve_folder(folder)
            if not fid:
                return {"ok": False, "error": f"Unknown folder: {folder}"}
            rules.append(
                {"property": "folders", "method": "intersection", "value": [fid]}
            )
        if media_type:
            rules.append(
                {
                    "property": "type",
                    "method": "equal",
                    "value": media_type.lower().lstrip("."),
                }
            )
        if rating is not None:
            rules.append(
                {"property": "rating", "method": "equal", "value": str(int(rating))}
            )
        if rating_min is not None:
            rules.append(
                {
                    "property": "rating",
                    "method": "gte",
                    "value": str(int(rating_min)),
                }
            )
        if name_contains:
            rules.append(
                {
                    "property": "name",
                    "method": "contain",
                    "value": name_contains,
                }
            )
        if not rules:
            return {
                "ok": False,
                "error": "Provide filters (tags, type, rating_min, …) or raw conditions",
            }
        return {
            "ok": True,
            "conditions": [
                {
                    "rules": rules,
                    "match": (match or "AND").upper(),
                    "boolean": "TRUE",
                }
            ],
        }

    def update_smart_folder(
        self,
        name_or_path_or_id: str,
        *,
        name: str | None = None,
        parent: str | None | object = _UNSET,
        tags: list[str] | None = None,
        tags_exclude: list[str] | None = None,
        folder: str | None = None,
        media_type: str | None = None,
        rating: int | None = None,
        rating_min: int | None = None,
        name_contains: str | None = None,
        match: str = "AND",
        description: str | None = None,
        conditions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Update name, parent, and/or conditions of an existing smart folder."""
        sid = self.resolve_smart_folder(name_or_path_or_id)
        if not sid:
            return {"ok": False, "error": f"Unknown smart folder: {name_or_path_or_id}"}

        new_conditions = conditions
        has_filters = any(
            [
                tags,
                tags_exclude,
                folder,
                media_type,
                rating is not None,
                rating_min is not None,
                name_contains,
            ]
        )
        if new_conditions is None and has_filters:
            built = self._conditions_from_filters(
                tags=tags,
                tags_exclude=tags_exclude,
                folder=folder,
                media_type=media_type,
                rating=rating,
                rating_min=rating_min,
                name_contains=name_contains,
                match=match,
            )
            if not built.get("ok"):
                return built
            new_conditions = built["conditions"]

        parent_id: str | None | object = _UNSET
        if parent is not _UNSET:
            if parent:
                parent_id = self.resolve_smart_folder(str(parent))
                if not parent_id:
                    return {"ok": False, "error": f"Unknown parent smart folder: {parent}"}
            else:
                parent_id = None

        if (
            name is None
            and parent is _UNSET
            and description is None
            and new_conditions is None
        ):
            return {"ok": False, "error": "Nothing to update"}

        try:
            with write_session(self.library.root):
                node = update_smart_folder_node(
                    self.library.root,
                    sid,
                    name=name,
                    conditions=new_conditions,
                    parent_id=parent_id,
                    description=description,
                )
        except WriteError as exc:
            return {"ok": False, "error": str(exc)}

        self.library.reload_metadata_trees()
        return {
            "ok": True,
            "smart_folder": {
                "id": node["id"],
                "name": node["name"],
                "path": self.library.smart_folder_paths.get(node["id"], node["name"]),
                "parent_id": self.library.smart_folders_by_id[node["id"]].parent_id
                if node["id"] in self.library.smart_folders_by_id
                else None,
                "conditions": node.get("conditions") or [],
            },
        }

    def delete_smart_folder(
        self, name_or_path_or_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        sid = self.resolve_smart_folder(name_or_path_or_id)
        if not sid:
            return {"ok": False, "error": f"Unknown smart folder: {name_or_path_or_id}"}
        sf = self.library.smart_folders_by_id.get(sid)
        child_count = len(sf.children) if sf else 0
        if child_count and not force:
            return {
                "ok": False,
                "error": f"Smart folder has {child_count} child folder(s); pass force=True to delete the subtree",
                "child_count": child_count,
            }
        try:
            with write_session(self.library.root):
                removed = delete_smart_folder_node(self.library.root, sid)
        except WriteError as exc:
            return {"ok": False, "error": str(exc)}

        removed_n = _count_smart_subtree(removed) if isinstance(removed, dict) else 1
        parent_id = sf.parent_id if sf else None
        self.library.reload_metadata_trees()
        return {
            "ok": True,
            "deleted_id": sid,
            "deleted_name": removed.get("name") if isinstance(removed, dict) else sid,
            "deleted_count": removed_n,
            "parent_id": parent_id,
        }


# ── CLI ───────────────────────────────────────────────────────────────


def _json_out(data: Any, *, pretty: bool = True) -> None:
    if pretty:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eagle-api",
        description="JSON API for agents to query and update an Eagle.cool library",
    )
    p.add_argument(
        "--library",
        default=os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)),
        help="Path to .library directory",
    )
    p.add_argument("--compact", action="store_true", help="Compact JSON")
    # Shared flags also on subcommands so `search --compact` works
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--compact", action="store_true", help=argparse.SUPPRESS)
    shared.add_argument(
        "--library",
        default=os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)),
        help=argparse.SUPPRESS,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", parents=[shared], help="Search assets")
    s.add_argument("-q", "--query", default="", help="Free-text name/tags search")
    s.add_argument("--tag", action="append", default=[], help="Tag (any); repeatable")
    s.add_argument("--tags", default="", help="Comma-separated tags (any)")
    s.add_argument("--tags-all", default="", help="Comma-separated tags (all required)")
    s.add_argument("--exclude-tag", action="append", default=[], dest="exclude_tags")
    s.add_argument("--folder", default="", help="Category/folder name, path, or id")
    s.add_argument("--smart-folder", default="", help="Smart folder name or path e.g. Eunbi/images")
    s.add_argument("--name", default="", dest="name_contains")
    s.add_argument("--rating", type=int, default=None)
    s.add_argument("--rating-min", type=int, default=None)
    s.add_argument("--rating-max", type=int, default=None)
    s.add_argument("--type", default="", dest="media_type", help="image|video|audio|ext")
    s.add_argument("--untagged", action="store_true")
    s.add_argument("--uncategorized", action="store_true")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--ids-only", action="store_true")

    g = sub.add_parser("get", parents=[shared], help="Get one item by id")
    g.add_argument("id")

    t = sub.add_parser("tag", parents=[shared], help="Add/remove tags")
    t.add_argument("action", choices=("add", "remove"))
    t.add_argument("ids", help="Item id or comma-separated ids")
    t.add_argument("tags", help="Tag or comma-separated tags")

    f = sub.add_parser("folder", parents=[shared], help="Add/remove folder membership (categories)")
    f.add_argument("action", choices=("add", "remove"))
    f.add_argument("ids", help="Item id or comma-separated ids")
    f.add_argument("folders", help="Folder name/path/id or comma-separated")

    r = sub.add_parser("rate", parents=[shared], help="Set star rating 0–5")
    r.add_argument("ids", help="Item id or comma-separated ids")
    r.add_argument("rating", type=int)

    c = sub.add_parser(
        "crop",
        parents=[shared],
        help="Crop image: overwrite original or save as new untagged item",
    )
    c.add_argument("id", help="Item id")
    c.add_argument(
        "--mode",
        default="overwrite",
        choices=("overwrite", "new", "save-as"),
        help="overwrite | new (save-as: no tags/folders)",
    )
    c.add_argument("--x", type=int, default=None)
    c.add_argument("--y", type=int, default=None)
    c.add_argument("--width", type=int, default=None)
    c.add_argument("--height", type=int, default=None)
    c.add_argument(
        "--aspect",
        default="",
        help="9:16, 3:4, 1:1, 16:9, 2:3, 3:2, 4:3, orig, free",
    )
    c.add_argument("--anchor", default="center")

    sub.add_parser("tags", parents=[shared], help="List all tags")
    sub.add_parser("folders", parents=[shared], help="List folders/categories")
    sub.add_parser("reload", parents=[shared], help="Reload library from disk")

    sf = sub.add_parser("smart-folder", parents=[shared], help="List/show/create smart folders")
    sfs = sf.add_subparsers(dest="sf_cmd", required=True)
    sfs.add_parser("list", parents=[shared], help="List smart folders")
    sfs_show = sfs.add_parser("show", parents=[shared], help="Show one smart folder + match count")
    sfs_show.add_argument("path", help="Name, path (Eunbi/images), or id")
    sfs_c = sfs.add_parser("create", parents=[shared], help="Create smart folder from filters")
    sfs_c.add_argument("--name", required=True)
    sfs_c.add_argument("--parent", default="", help="Parent smart folder name/path/id")
    sfs_c.add_argument("--tag", action="append", default=[], dest="tags")
    sfs_c.add_argument("--tags", default="", dest="tags_csv")
    sfs_c.add_argument("--exclude-tag", action="append", default=[], dest="exclude_tags")
    sfs_c.add_argument("--folder", default="")
    sfs_c.add_argument("--type", default="", dest="media_type")
    sfs_c.add_argument("--rating", type=int, default=None)
    sfs_c.add_argument("--rating-min", type=int, default=None)
    sfs_c.add_argument("--name-contains", default="")
    sfs_c.add_argument("--match", default="AND", choices=("AND", "OR"))
    sfs_c.add_argument("--description", default="")
    sfs_c.add_argument(
        "--conditions-json",
        default="",
        help="Raw conditions JSON array (overrides convenience filters)",
    )
    sfs_u = sfs.add_parser("update", parents=[shared], help="Update smart folder")
    sfs_u.add_argument("path", help="Name, path, or id to update")
    sfs_u.add_argument("--name", default="", help="New name")
    sfs_u.add_argument(
        "--parent",
        default=None,
        help="New parent (name/path/id). Empty string moves to root",
    )
    sfs_u.add_argument("--tag", action="append", default=[], dest="tags")
    sfs_u.add_argument("--tags", default="", dest="tags_csv")
    sfs_u.add_argument("--exclude-tag", action="append", default=[], dest="exclude_tags")
    sfs_u.add_argument("--folder", default="")
    sfs_u.add_argument("--type", default="", dest="media_type")
    sfs_u.add_argument("--rating", type=int, default=None)
    sfs_u.add_argument("--rating-min", type=int, default=None)
    sfs_u.add_argument("--name-contains", default="")
    sfs_u.add_argument("--match", default="AND", choices=("AND", "OR"))
    sfs_u.add_argument("--description", default=None)
    sfs_u.add_argument(
        "--conditions-json",
        default="",
        help="Raw conditions JSON array (replaces existing conditions)",
    )
    sfs_d = sfs.add_parser("delete", parents=[shared], help="Delete smart folder")
    sfs_d.add_argument("path", help="Name, path, or id")
    sfs_d.add_argument(
        "--force",
        action="store_true",
        help="Delete even if the folder has children",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # Allow `eagle-api search --compact` as well as `eagle-api --compact search`
    try:
        args = parser.parse_intermixed_args(argv)
    except TypeError:
        args = parser.parse_args(argv)
    pretty = not args.compact
    try:
        api = EagleAPI(args.library)
    except Exception as exc:  # noqa: BLE001
        _json_out({"ok": False, "error": str(exc)}, pretty=pretty)
        return 1

    try:
        if args.cmd == "reload":
            _json_out(api.reload(), pretty=pretty)
            return 0

        if args.cmd == "search":
            tags = list(args.tag) + _split_csv(args.tags)
            result = api.search(
                query=args.query,
                tags=tags or None,
                tags_all=_split_csv(args.tags_all) or None,
                exclude_tags=list(args.exclude_tags) or None,
                folder=args.folder or None,
                smart_folder=args.smart_folder or None,
                name_contains=args.name_contains or None,
                rating=args.rating,
                rating_min=args.rating_min,
                rating_max=args.rating_max,
                media_type=args.media_type or None,
                untagged=args.untagged,
                uncategorized=args.uncategorized,
                limit=args.limit,
                offset=args.offset,
            )
            if args.ids_only and result.get("ok"):
                result = {
                    "ok": True,
                    "total": result["total"],
                    "ids": [it["id"] for it in result["items"]],
                }
            _json_out(result, pretty=pretty)
            return 0 if result.get("ok") else 2

        if args.cmd == "get":
            result = api.get(args.id)
            _json_out(result, pretty=pretty)
            return 0 if result.get("ok") else 2

        if args.cmd == "tag":
            ids = _split_csv(args.ids)
            tags = _split_csv(args.tags)
            if args.action == "add":
                result = api.add_tags(ids, tags)
            else:
                result = api.remove_tags(ids, tags)
            _json_out(result, pretty=pretty)
            return 0 if result.get("ok") else 2

        if args.cmd == "folder":
            ids = _split_csv(args.ids)
            folders = _split_csv(args.folders)
            if args.action == "add":
                result = api.add_folders(ids, folders)
            else:
                result = api.remove_folders(ids, folders)
            _json_out(result, pretty=pretty)
            return 0 if result.get("ok") else 2

        if args.cmd == "rate":
            result = api.set_rating(_split_csv(args.ids), args.rating)
            _json_out(result, pretty=pretty)
            return 0 if result.get("ok") else 2

        if args.cmd == "crop":
            mode = args.mode
            if mode == "save-as":
                mode = "new"
            result = api.crop(
                args.id,
                mode=mode,
                x=args.x,
                y=args.y,
                width=args.width,
                height=args.height,
                aspect=args.aspect or None,
                anchor=args.anchor,
            )
            _json_out(result, pretty=pretty)
            return 0 if result.get("ok") else 2

        if args.cmd == "tags":
            _json_out(api.list_tags(), pretty=pretty)
            return 0

        if args.cmd == "folders":
            _json_out(api.list_folders(), pretty=pretty)
            return 0

        if args.cmd == "smart-folder":
            if args.sf_cmd == "list":
                _json_out(api.list_smart_folders(), pretty=pretty)
                return 0
            if args.sf_cmd == "show":
                result = api.get_smart_folder(args.path)
                _json_out(result, pretty=pretty)
                return 0 if result.get("ok") else 2
            if args.sf_cmd == "create":
                conditions = None
                if args.conditions_json:
                    conditions = json.loads(args.conditions_json)
                tags = list(args.tags) + _split_csv(args.tags_csv)
                result = api.create_smart_folder(
                    args.name,
                    parent=args.parent or None,
                    tags=tags or None,
                    tags_exclude=list(args.exclude_tags) or None,
                    folder=args.folder or None,
                    media_type=args.media_type or None,
                    rating=args.rating,
                    rating_min=args.rating_min,
                    name_contains=args.name_contains or None,
                    match=args.match,
                    description=args.description,
                    conditions=conditions,
                )
                _json_out(result, pretty=pretty)
                return 0 if result.get("ok") else 2
            if args.sf_cmd == "update":
                conditions = None
                if args.conditions_json:
                    conditions = json.loads(args.conditions_json)
                tags = list(args.tags) + _split_csv(args.tags_csv)
                parent = _UNSET
                if args.parent is not None:
                    parent = args.parent or None
                result = api.update_smart_folder(
                    args.path,
                    name=args.name or None,
                    parent=parent,
                    tags=tags or None,
                    tags_exclude=list(args.exclude_tags) or None,
                    folder=args.folder or None,
                    media_type=args.media_type or None,
                    rating=args.rating,
                    rating_min=args.rating_min,
                    name_contains=args.name_contains or None,
                    match=args.match,
                    description=args.description,
                    conditions=conditions,
                )
                _json_out(result, pretty=pretty)
                return 0 if result.get("ok") else 2
            if args.sf_cmd == "delete":
                result = api.delete_smart_folder(args.path, force=bool(args.force))
                _json_out(result, pretty=pretty)
                return 0 if result.get("ok") else 2

        _json_out({"ok": False, "error": f"Unknown command: {args.cmd}"}, pretty=pretty)
        return 1
    except WriteError as exc:
        _json_out({"ok": False, "error": str(exc)}, pretty=pretty)
        return 2
    except Exception as exc:  # noqa: BLE001
        _json_out({"ok": False, "error": str(exc)}, pretty=pretty)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
