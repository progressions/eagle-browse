"""View filters for Eagle Browse (tags, folders, type, dimensions, duration, stars, dates)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dt_time
from datetime import date as date_cls
from typing import Any

from library import Item

# Star-rating comparison: equal / at least / at most
RATING_OP_EQ = "eq"
RATING_OP_GTE = "gte"
RATING_OP_LTE = "lte"
RATING_OPS = (RATING_OP_EQ, RATING_OP_GTE, RATING_OP_LTE)
RATING_OP_SYMBOLS = {
    RATING_OP_EQ: "=",
    RATING_OP_GTE: "≥",
    RATING_OP_LTE: "≤",
}


def rating_chip_label(op: str, stars: int) -> str:
    """Chip / summary text, e.g. ★=3 or ★≥4."""
    symbol = RATING_OP_SYMBOLS.get(op, "=")
    return f"★{symbol}{int(stars)}"


def item_star_value(item: Item) -> int:
    """Eagle stores stars as item.star; unrated is treated as 0."""
    return 0 if item.star is None else int(item.star)


def rating_matches(star: int, op: str, target: int) -> bool:
    if op == RATING_OP_GTE:
        return star >= target
    if op == RATING_OP_LTE:
        return star <= target
    return star == target


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
    # Inclusive local calendar days, YYYY-MM-DD. Created = Eagle file mtime;
    # added = library btime.
    created_from: str | None = None
    created_to: str | None = None
    added_from: str | None = None
    added_to: str | None = None
    # Star rating: 1–5 plus = / ≥ / ≤. None = no rating filter.
    rating: int | None = None
    rating_op: str = RATING_OP_EQ

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
        self.created_from = self.created_to = None
        self.added_from = self.added_to = None
        self.rating = None
        self.rating_op = RATING_OP_EQ

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
            or self.created_from is not None
            or self.created_to is not None
            or self.added_from is not None
            or self.added_to is not None
            or self.rating is not None
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
        if self.created_from is not None:
            parts.append(f"created≥{self.created_from}")
        if self.created_to is not None:
            parts.append(f"created≤{self.created_to}")
        if self.added_from is not None:
            parts.append(f"added≥{self.added_from}")
        if self.added_to is not None:
            parts.append(f"added≤{self.added_to}")
        if self.rating is not None:
            parts.append(rating_chip_label(self.rating_op, self.rating))
        return parts


def parse_filter_date(text: str) -> date_cls | None:
    """YYYY-MM-DD, YYYY/MM/DD, or M/D/YYYY. None if empty or invalid."""
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def format_filter_date(d: date_cls) -> str:
    return d.isoformat()


def day_start_ms(d: date_cls) -> int:
    return int(datetime.combine(d, dt_time.min).timestamp() * 1000)


def day_end_ms(d: date_cls) -> int:
    return int(datetime.combine(d, dt_time(23, 59, 59, 999000)).timestamp() * 1000)


def item_created_ms(item: Item) -> int:
    """Original file time (Eagle mtime), then added time, then modificationTime."""
    for raw in (
        getattr(item, "created_time", 0),
        item.btime,
        item.modification_time,
    ):
        t = int(raw or 0)
        if t > 0:
            return t
    return 0


def item_added_ms(item: Item) -> int:
    """When the item entered this library (Eagle btime)."""
    t = int(item.btime or 0)
    if t <= 0:
        t = int(item.modification_time or 0)
    return t


def timestamp_matches_rule(ms: int, method: str, value: Any) -> bool:
    """Smart-folder date rule. *ms* is item created or added time."""
    method = (method or "").lower()
    if ms <= 0:
        return False
    if method == "within":
        days = _within_days(value)
        if days is None:
            return False
        start = datetime.combine(
            datetime.now().date() - timedelta(days=days - 1),
            dt_time.min,
        )
        return ms >= int(start.timestamp() * 1000)
    day, extra = _rule_date_values(value)
    if day is None:
        return False
    lo, hi = day_start_ms(day), day_end_ms(day)
    if method in ("equal", "on"):
        return lo <= ms <= hi
    if method in ("unequal", "not"):
        return not (lo <= ms <= hi)
    if method in ("gte", "ge", "after", "gt", "greater"):
        # after = strictly after that calendar day
        if method in ("gt", "greater", "after"):
            return ms > hi
        return ms >= lo
    if method in ("lte", "le", "before", "lt", "less"):
        if method in ("lt", "less", "before"):
            return ms < lo
        return ms <= hi
    if method in ("between", "range") and extra is not None:
        return lo <= ms <= day_end_ms(extra)
    return False


def _within_days(value: Any) -> int | None:
    if isinstance(value, list) and value:
        value = value[0]
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def _rule_date_values(value: Any) -> tuple[date_cls | None, date_cls | None]:
    if isinstance(value, list):
        first = value[0] if value else None
        second = value[1] if len(value) > 1 else None
        d1 = _coerce_rule_day(first)
        d2 = _coerce_rule_day(second)
        return d1, d2
    return _coerce_rule_day(value), None


def _coerce_rule_day(value: Any) -> date_cls | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ms = int(value)
        if ms > 10_000_000_000:  # epoch ms vs seconds
            ms = ms // 1000
        try:
            return datetime.fromtimestamp(ms).date()
        except (OSError, OverflowError, ValueError):
            return None
    return parse_filter_date(str(value))


def _ms_in_date_range(ms: int, from_s: str | None, to_s: str | None) -> bool:
    if from_s is None and to_s is None:
        return True
    if ms <= 0:
        return False
    if from_s:
        d = parse_filter_date(from_s)
        if d is not None and ms < day_start_ms(d):
            return False
    if to_s:
        d = parse_filter_date(to_s)
        if d is not None and ms > day_end_ms(d):
            return False
    return True


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
    if vf.tags_include:
        inc = {t.lower() for t in vf.tags_include}
        if not inc.issubset(item.tag_set):
            return False
    if vf.tags_exclude:
        exc = {t.lower() for t in vf.tags_exclude}
        if not item.tag_set.isdisjoint(exc):
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

    if vf.created_from or vf.created_to:
        if not _ms_in_date_range(
            item_created_ms(item), vf.created_from, vf.created_to
        ):
            return False
    if vf.added_from or vf.added_to:
        if not _ms_in_date_range(item_added_ms(item), vf.added_from, vf.added_to):
            return False

    if vf.rating is not None:
        if not rating_matches(item_star_value(item), vf.rating_op, int(vf.rating)):
            return False

    return True
