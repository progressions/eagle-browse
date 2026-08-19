"""Asset sets — families of related stills and videos, joined by a set: tag."""

from __future__ import annotations

from typing import Any, Iterable

SET_PREFIX = "set:"


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
