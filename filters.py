"""View filters for Eagle Browse (tags, folders, type, dimensions, duration)."""

from __future__ import annotations

from dataclasses import dataclass, field

from library import Item


@dataclass
class ViewFilters:
    """Active filters for the current grid view."""

    tags_include: set[str] = field(default_factory=set)
    tags_exclude: set[str] = field(default_factory=set)
    # folder ids
    folders_include: set[str] = field(default_factory=set)
    folders_exclude: set[str] = field(default_factory=set)
    # "image"|"video"|"audio"|ext
    types_include: set[str] = field(default_factory=set)
    types_exclude: set[str] = field(default_factory=set)
    width_min: int | None = None
    width_max: int | None = None
    height_min: int | None = None
    height_max: int | None = None
    duration_min: float | None = None
    duration_max: float | None = None

    def clear(self) -> None:
        self.tags_include.clear()
        self.tags_exclude.clear()
        self.folders_include.clear()
        self.folders_exclude.clear()
        self.types_include.clear()
        self.types_exclude.clear()
        self.width_min = self.width_max = None
        self.height_min = self.height_max = None
        self.duration_min = self.duration_max = None

    def active(self) -> bool:
        return bool(
            self.tags_include
            or self.tags_exclude
            or self.folders_include
            or self.folders_exclude
            or self.types_include
            or self.types_exclude
            or self.width_min is not None
            or self.width_max is not None
            or self.height_min is not None
            or self.height_max is not None
            or self.duration_min is not None
            or self.duration_max is not None
        )

    def summary_parts(self) -> list[str]:
        parts: list[str] = []
        for t in sorted(self.tags_include):
            parts.append(f"+tag:{t}")
        for t in sorted(self.tags_exclude):
            parts.append(f"-tag:{t}")
        for f in sorted(self.folders_include):
            parts.append(f"+folder:{f}")
        for f in sorted(self.folders_exclude):
            parts.append(f"-folder:{f}")
        for t in sorted(self.types_include):
            parts.append(f"+type:{t}")
        for t in sorted(self.types_exclude):
            parts.append(f"-type:{t}")
        if self.width_min is not None:
            parts.append(f"w≥{self.width_min}")
        if self.width_max is not None:
            parts.append(f"w≤{self.width_max}")
        if self.height_min is not None:
            parts.append(f"h≥{self.height_min}")
        if self.height_max is not None:
            parts.append(f"h≤{self.height_max}")
        if self.duration_min is not None:
            parts.append(f"dur≥{self.duration_min:g}s")
        if self.duration_max is not None:
            parts.append(f"dur≤{self.duration_max:g}s")
        return parts


def _matches_type_set(item: Item, filters: set[str]) -> bool:
    """True if item matches any filter key in the set."""
    if not filters:
        return False
    ext = item.ext_lower.lstrip(".")
    for f in filters:
        key = f.lower().lstrip(".")
        if key == "image" and item.is_image:
            return True
        if key == "video" and item.is_video:
            return True
        if key == "audio" and item.is_audio:
            return True
        if key == ext:
            return True
    return False


def item_matches_view_filters(item: Item, vf: ViewFilters) -> bool:
    # Tags: must have all includes; must have none of excludes
    if vf.tags_include and not vf.tags_include.issubset(item.tag_set):
        return False
    if vf.tags_exclude and not item.tag_set.isdisjoint(vf.tags_exclude):
        return False

    # Folders: must be in all included folders? Usually OR for include is better for
    # "show items in A or B". AND for include is strict. Eagle multi-folder is membership
    # in each — for filter "in folder X" one include is enough; multiple includes = OR.
    if vf.folders_include and item.folder_set.isdisjoint(vf.folders_include):
        return False
    if vf.folders_exclude and not item.folder_set.isdisjoint(vf.folders_exclude):
        return False

    # Types: include = must match at least one; exclude = must not match any
    if vf.types_include and not _matches_type_set(item, vf.types_include):
        return False
    if vf.types_exclude and _matches_type_set(item, vf.types_exclude):
        return False

    w, h = item.width, item.height
    if vf.width_min is not None and w < vf.width_min:
        return False
    if vf.width_max is not None and (w == 0 or w > vf.width_max):
        return False
    if vf.height_min is not None and h < vf.height_min:
        return False
    if vf.height_max is not None and (h == 0 or h > vf.height_max):
        return False

    if vf.duration_min is not None or vf.duration_max is not None:
        d = item.duration
        if d is None:
            return False
        if vf.duration_min is not None and d < vf.duration_min:
            return False
        if vf.duration_max is not None and d > vf.duration_max:
            return False

    return True
