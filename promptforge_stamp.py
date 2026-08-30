"""Stamp PromptForge history ids onto Eagle stills (Fizzy #482).

Build stills use Comfy filename prefix ``image-<history_id>-…``. On import we
add Eagle tag ``pf:<id>`` and an annotation line ``promptforge:<id>`` so Browse
can open Build without parsing the filename every time.
"""

from __future__ import annotations

import re
from typing import Any

# Basename must start with image-<digits>- (Build/Comfy prefix). Mid-name
# "…-image-1-…" / grok-image-<n> must not match.
PF_HISTORY_IN_NAME = re.compile(
    r"^image-(\d+)(?:[-_.]|$)",
    re.IGNORECASE,
)
PF_ANNOTATION_LINE = re.compile(r"(?m)^promptforge:(\d+)\s*$")


def history_id_from_name(name: str | None) -> int | None:
    if not name:
        return None
    m = PF_HISTORY_IN_NAME.search(str(name))
    if not m:
        return None
    return int(m.group(1))


def pf_tag(history_id: int) -> str:
    return f"pf:{int(history_id)}"


def annotation_line(history_id: int) -> str:
    return f"promptforge:{int(history_id)}"


def merge_annotation(existing: str | None, history_id: int) -> str:
    """Append ``promptforge:<id>`` if missing. Leave an existing line alone."""
    text = (existing or "").replace("\r\n", "\n").replace("\r", "\n")
    if PF_ANNOTATION_LINE.search(text):
        return text
    line = annotation_line(history_id)
    if text.strip():
        return text.rstrip() + "\n" + line
    return line


def stamp_metadata(meta: dict[str, Any], *, name: str | None = None) -> bool:
    """Mutate *meta* tags/annotation from the item name. True if changed."""
    from write import canonicalize_tags

    hid = history_id_from_name(name if name is not None else meta.get("name"))
    if hid is None:
        return False

    changed = False
    tag = pf_tag(hid)
    tags = canonicalize_tags(meta.get("tags") or [])
    if tag not in tags:
        tags.append(tag)
        meta["tags"] = tags
        changed = True

    new_ann = merge_annotation(meta.get("annotation"), hid)
    if new_ann != (meta.get("annotation") or ""):
        meta["annotation"] = new_ann
        changed = True
    return changed


def apply_stamp_to_item(library: Any, item: Any) -> bool:
    """Add missing ``pf:<id>`` / annotation on an in-memory library item."""
    if item is None:
        return False
    name = getattr(item, "name", "") or ""
    hid = history_id_from_name(name)
    if hid is None:
        return False

    from write import WriteError, canonicalize_tags

    tag = pf_tag(hid)
    tags = canonicalize_tags(getattr(item, "tags", None) or [])
    need_tag = tag not in tags
    new_ann = merge_annotation(getattr(item, "annotation", None), hid)
    need_ann = new_ann != (getattr(item, "annotation", None) or "")
    if not need_tag and not need_ann:
        return False

    kwargs: dict[str, Any] = {}
    if need_tag:
        kwargs["add_tags"] = [tag]
    if need_ann:
        kwargs["annotation"] = new_ann

    try:
        library.update_item(item.id, **kwargs)
    except WriteError:
        return False
    return True
