"""Asset sets — families of related stills and videos, joined by a set: tag."""

from __future__ import annotations

import re
from typing import Any, Iterable

SET_PREFIX = "set:"

# Eagle item ids are M + 12 chars from 0-9A-Z (import_media / write).
EAGLE_ID_RE = re.compile(r"(?<![0-9A-Za-z])(M[0-9A-Za-z]{12})(?![0-9A-Za-z])", re.I)


def is_set_tag(tag: str) -> bool:
    return bool(tag) and tag.startswith(SET_PREFIX)


def mint_set_tag(item_id: str) -> str:
    """`set:` plus the item id, stored lowercase like every other tag."""
    from write import normalize_tag

    return normalize_tag(f"{SET_PREFIX}{item_id}")


def set_tags_of(item: Any) -> list[str]:
    return [t for t in (getattr(item, "tags", None) or []) if is_set_tag(t)]


def set_tag_of(item: Any) -> str | None:
    tags = set_tags_of(item)
    return tags[0] if tags else None


def ensure_set_tag(library_root: Any, source: Any) -> str:
    """Return source's set: tag, writing it onto source if it had none.

    Call while the library write lock is already held (inside write_session).
    Updates source.tags / tag_set in memory when the sidecar write succeeds.
    """
    from pathlib import Path

    from write import apply_tags, load_item_metadata, save_item_metadata

    tag = set_tag_of(source)
    if not tag:
        tag = mint_set_tag(str(getattr(source, "id", "") or ""))
    if not tag:
        return tag
    tag_set = getattr(source, "tag_set", None) or frozenset(
        getattr(source, "tags", None) or []
    )
    if tag in tag_set:
        return tag
    item_dir = getattr(source, "item_dir", None)
    if item_dir is None:
        return tag
    data = load_item_metadata(Path(item_dir))
    apply_tags(data, add_tags=[tag])
    save_item_metadata(Path(library_root), Path(item_dir), data)
    source.tags = list(data.get("tags") or [])
    source.tag_set = frozenset(source.tags)
    return tag


def items_with_set_tag(items: Iterable[Any], tag: str) -> list[Any]:
    want = (tag or "").strip().lower()
    if not want:
        return []
    out: list[Any] = []
    for it in items:
        if getattr(it, "is_deleted", False):
            continue
        tag_set = getattr(it, "tag_set", None)
        if tag_set is not None:
            if want in tag_set:
                out.append(it)
            continue
        if want in (getattr(it, "tags", None) or []):
            out.append(it)
    return out


def eagle_ids_in_text(text: str) -> list[str]:
    """Unique Eagle ids found in a filename or other string, uppercase."""
    seen: list[str] = []
    for m in EAGLE_ID_RE.finditer(text or ""):
        iid = m.group(1).upper()
        if iid not in seen:
            seen.append(iid)
    return seen


def _item_by_id(library: Any, iid: str) -> Any | None:
    by_id = getattr(library, "items_by_id", None) or {}
    it = by_id.get(iid)
    if it is not None:
        return it
    want = iid.upper()
    for key, val in by_id.items():
        if str(key).upper() == want:
            return val
    return None


def join_into_set(library: Any, source: Any, *children: Any) -> str | None:
    """Put source + children on one set: tag. Reuses source's tag, or mints.

    Same rule as the inspector Group button / in-app derivatives.
    Returns the tag, or None if the write failed.
    """
    from write import WriteError

    if source is None:
        return None
    kids = [c for c in children if c is not None and getattr(c, "id", None)]
    tag = set_tag_of(source)
    ids: list[str] = []
    source_tags = getattr(source, "tag_set", None) or frozenset()
    if tag is None:
        tag = mint_set_tag(source.id)
        if tag not in source_tags:
            ids.append(source.id)
    elif tag not in source_tags:
        ids.append(source.id)
    for child in kids:
        child_tags = getattr(child, "tag_set", None) or frozenset()
        if tag not in child_tags:
            ids.append(child.id)
    ids = list(dict.fromkeys(ids))
    if not ids:
        return tag
    try:
        library.update_items_batch(ids, add_tags=[tag])
    except WriteError:
        return None
    return tag


def join_imported_by_name(library: Any, new_item: Any) -> str | None:
    """If the new item's name contains an existing Eagle id, join that set.

    When the new item is a video and more than one id is in the name, prefer
    an image (the still being animated) over another video.
    """
    if new_item is None:
        return None
    name = getattr(new_item, "name", "") or ""
    self_id = getattr(new_item, "id", "")
    candidates: list[Any] = []
    for iid in eagle_ids_in_text(name):
        if iid == self_id:
            continue
        it = _item_by_id(library, iid)
        if it is None or getattr(it, "is_deleted", False):
            continue
        candidates.append(it)
    if not candidates:
        return None
    source = candidates[0]
    if getattr(new_item, "is_video", False):
        for it in candidates:
            if getattr(it, "is_image", False):
                source = it
                break
    return join_into_set(library, source, new_item)
