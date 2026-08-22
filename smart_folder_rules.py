"""Editor model for Eagle smart-folder conditions.

Maps the in-app rule editor (groups of rating / tags / categories) to the
on-disk Eagle ``conditions`` JSON and back. Unknown rules stay on the group
as ``OtherRule`` so a save does not drop type/name/identity/etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from filters import (
    RATING_OP_EQ,
    RATING_OP_GTE,
    RATING_OP_LTE,
    format_filter_date,
    parse_filter_date,
    rating_chip_label,
)

GROUP_ALL = "all"
GROUP_ANY = "any"
GROUP_NONE = "none"
GROUP_MODES = (GROUP_ALL, GROUP_ANY, GROUP_NONE)
GROUP_LABELS = {
    GROUP_ALL: "All are true",
    GROUP_ANY: "Any are true",
    GROUP_NONE: "None are true",
}

SET_ALL = "all"
SET_ANY = "any"
SET_MODES = (SET_ALL, SET_ANY)
SET_LABELS = {
    SET_ALL: "all are present",
    SET_ANY: "any are present",
}

# Eagle match/boolean → editor group mode. NAND (AND + FALSE) is not in the UI.
_GROUP_FROM_EAGLE = {
    ("AND", "TRUE"): GROUP_ALL,
    ("OR", "TRUE"): GROUP_ANY,
    ("OR", "FALSE"): GROUP_NONE,
}
_GROUP_TO_EAGLE = {
    GROUP_ALL: ("AND", "TRUE"),
    GROUP_ANY: ("OR", "TRUE"),
    GROUP_NONE: ("OR", "FALSE"),
}

_RATING_FROM_EAGLE = {
    "equal": RATING_OP_EQ,
    "gte": RATING_OP_GTE,
    "ge": RATING_OP_GTE,
    "lte": RATING_OP_LTE,
    "le": RATING_OP_LTE,
}
_RATING_TO_EAGLE = {
    RATING_OP_EQ: "equal",
    RATING_OP_GTE: "gte",
    RATING_OP_LTE: "lte",
}

_ANY_METHODS = frozenset({"union", "intersection"})
_ALL_METHODS = frozenset({"subset", "contain", "all"})

DATE_KIND_CREATED = "created"
DATE_KIND_ADDED = "added"
DATE_KIND_LABELS = {
    DATE_KIND_CREATED: "Created",
    DATE_KIND_ADDED: "Added",
}
DATE_PROP = {
    DATE_KIND_CREATED: "createTime",
    DATE_KIND_ADDED: "btime",
}
DATE_PROP_FROM_EAGLE = {
    "createtime": DATE_KIND_CREATED,
    "mtime": DATE_KIND_CREATED,
    "btime": DATE_KIND_ADDED,
    "importtime": DATE_KIND_ADDED,
    "addedtime": DATE_KIND_ADDED,
}

DATE_OP_GTE = "gte"
DATE_OP_LTE = "lte"
DATE_OP_EQ = "eq"
DATE_OP_WITHIN = "within"
DATE_OPS = (DATE_OP_GTE, DATE_OP_LTE, DATE_OP_EQ, DATE_OP_WITHIN)
DATE_OP_LABELS = {
    DATE_OP_GTE: "on or after",
    DATE_OP_LTE: "on or before",
    DATE_OP_EQ: "on",
    DATE_OP_WITHIN: "last N days",
}
_DATE_FROM_EAGLE = {
    "gte": DATE_OP_GTE,
    "ge": DATE_OP_GTE,
    "after": DATE_OP_GTE,
    "lte": DATE_OP_LTE,
    "le": DATE_OP_LTE,
    "before": DATE_OP_LTE,
    "equal": DATE_OP_EQ,
    "on": DATE_OP_EQ,
    "within": DATE_OP_WITHIN,
}
_DATE_TO_EAGLE = {
    DATE_OP_GTE: "gte",
    DATE_OP_LTE: "lte",
    DATE_OP_EQ: "equal",
    DATE_OP_WITHIN: "within",
}


@dataclass
class RatingRule:
    op: str = RATING_OP_GTE
    stars: int = 3


@dataclass
class TagsRule:
    mode: str = SET_ANY
    tags: list[str] = field(default_factory=list)


@dataclass
class CategoriesRule:
    mode: str = SET_ANY
    folder_ids: list[str] = field(default_factory=list)


@dataclass
class DateRule:
    """Created-at (Eagle createTime) or added-at (library btime)."""

    kind: str = DATE_KIND_CREATED
    op: str = DATE_OP_GTE
    day: str = ""  # YYYY-MM-DD for after/before/on
    days: int = 7  # last N days for within


@dataclass
class OtherRule:
    """An Eagle rule the editor does not own. Written back unchanged."""

    raw: dict[str, Any]


EditorRule = RatingRule | TagsRule | CategoriesRule | DateRule | OtherRule


@dataclass
class EditorGroup:
    mode: str = GROUP_ALL
    rules: list[EditorRule] = field(default_factory=list)


@dataclass
class UnrecognizedGroup:
    """A condition group whose match/boolean the editor cannot represent."""

    raw: dict[str, Any]


@dataclass
class EditorSpec:
    name: str = ""
    parent_id: str | None = None
    groups: list[EditorGroup] = field(default_factory=list)
    other_groups: list[UnrecognizedGroup] = field(default_factory=list)

    def has_any_rule(self) -> bool:
        if self.other_groups:
            return True
        for g in self.groups:
            if g.rules:
                return True
        return False


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    s = str(value)
    return [s] if s else []


def decode_rule(raw: dict[str, Any]) -> EditorRule:
    """Turn one Eagle rule dict into an editor rule, or OtherRule."""
    if not isinstance(raw, dict):
        return OtherRule(raw={"property": "", "method": "", "value": raw})
    prop = str(raw.get("property") or "").lower()
    method = str(raw.get("method") or "").lower()
    value = raw.get("value")

    if prop == "rating" and method in _RATING_FROM_EAGLE:
        try:
            stars = int(value)
        except (TypeError, ValueError):
            return OtherRule(raw=dict(raw))
        if stars < 0 or stars > 5:
            return OtherRule(raw=dict(raw))
        return RatingRule(op=_RATING_FROM_EAGLE[method], stars=stars)

    if prop == "tags":
        tags = _str_list(value)
        if method in _ANY_METHODS:
            return TagsRule(mode=SET_ANY, tags=tags)
        if method in _ALL_METHODS:
            return TagsRule(mode=SET_ALL, tags=tags)
        return OtherRule(raw=dict(raw))

    if prop == "folders":
        ids = _str_list(value)
        if method in _ANY_METHODS:
            return CategoriesRule(mode=SET_ANY, folder_ids=ids)
        if method in _ALL_METHODS:
            return CategoriesRule(mode=SET_ALL, folder_ids=ids)
        return OtherRule(raw=dict(raw))

    kind = DATE_PROP_FROM_EAGLE.get(prop)
    if kind is not None and method in _DATE_FROM_EAGLE:
        op = _DATE_FROM_EAGLE[method]
        if op == DATE_OP_WITHIN:
            days = 7
            raw_days = value[0] if isinstance(value, list) and value else value
            try:
                days = max(1, int(raw_days))
            except (TypeError, ValueError):
                days = 7
            return DateRule(kind=kind, op=op, days=days)
        day = ""
        raw_day = value[0] if isinstance(value, list) and value else value
        parsed = parse_filter_date(str(raw_day or ""))
        if parsed is None and isinstance(raw_day, (int, float)):
            from filters import _coerce_rule_day

            parsed = _coerce_rule_day(raw_day)
        if parsed is not None:
            day = format_filter_date(parsed)
        return DateRule(kind=kind, op=op, day=day)

    return OtherRule(raw=dict(raw))


def encode_rule(rule: EditorRule) -> dict[str, Any] | None:
    """Eagle rule dict, or None if the rule is empty and should be skipped."""
    if isinstance(rule, OtherRule):
        return dict(rule.raw)
    if isinstance(rule, RatingRule):
        stars = max(0, min(5, int(rule.stars)))
        method = _RATING_TO_EAGLE.get(rule.op, "gte")
        return {"property": "rating", "method": method, "value": str(stars)}
    if isinstance(rule, TagsRule):
        tags = [t for t in rule.tags if t]
        if not tags:
            return None
        method = "subset" if rule.mode == SET_ALL else "union"
        return {"property": "tags", "method": method, "value": tags}
    if isinstance(rule, CategoriesRule):
        ids = [i for i in rule.folder_ids if i]
        if not ids:
            return None
        method = "subset" if rule.mode == SET_ALL else "intersection"
        return {"property": "folders", "method": method, "value": ids}
    if isinstance(rule, DateRule):
        prop = DATE_PROP.get(rule.kind, "createTime")
        method = _DATE_TO_EAGLE.get(rule.op, "gte")
        if rule.op == DATE_OP_WITHIN:
            return {
                "property": prop,
                "method": "within",
                "value": [max(1, int(rule.days or 1))],
            }
        day = parse_filter_date(rule.day or "")
        if day is None:
            return None
        return {
            "property": prop,
            "method": method,
            "value": format_filter_date(day),
        }
    return None


def decode_conditions(conditions: Iterable[dict[str, Any]] | None) -> tuple[
    list[EditorGroup], list[UnrecognizedGroup]
]:
    groups: list[EditorGroup] = []
    other: list[UnrecognizedGroup] = []
    for raw in conditions or []:
        if not isinstance(raw, dict):
            continue
        match = str(raw.get("match") or "AND").upper()
        boolean = str(raw.get("boolean") or "TRUE").upper()
        mode = _GROUP_FROM_EAGLE.get((match, boolean))
        if mode is None:
            other.append(UnrecognizedGroup(raw=dict(raw)))
            continue
        rules = [
            decode_rule(r)
            for r in (raw.get("rules") or [])
            if isinstance(r, dict)
        ]
        groups.append(EditorGroup(mode=mode, rules=rules))
    return groups, other


def encode_conditions(
    groups: Iterable[EditorGroup],
    other_groups: Iterable[UnrecognizedGroup] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in groups:
        match, boolean = _GROUP_TO_EAGLE.get(g.mode, ("AND", "TRUE"))
        rules: list[dict[str, Any]] = []
        for rule in g.rules:
            encoded = encode_rule(rule)
            if encoded is not None:
                rules.append(encoded)
        if not rules:
            continue
        out.append({"rules": rules, "match": match, "boolean": boolean})
    for og in other_groups or []:
        if isinstance(og.raw, dict):
            out.append(dict(og.raw))
    return out


def spec_from_folder(
    *,
    name: str,
    parent_id: str | None,
    conditions: list[dict[str, Any]] | None,
) -> EditorSpec:
    groups, other = decode_conditions(conditions)
    if not groups and not other:
        groups = [EditorGroup(mode=GROUP_ALL, rules=[])]
    return EditorSpec(
        name=name or "",
        parent_id=parent_id,
        groups=groups,
        other_groups=other,
    )


def empty_spec(*, parent_id: str | None = None) -> EditorSpec:
    return EditorSpec(
        name="",
        parent_id=parent_id,
        groups=[EditorGroup(mode=GROUP_ALL, rules=[])],
    )


def rule_summary(
    rule: EditorRule,
    *,
    folder_paths: dict[str, str] | None = None,
) -> str:
    """One-line label for a rule row."""
    paths = folder_paths or {}
    if isinstance(rule, RatingRule):
        return rating_chip_label(rule.op, rule.stars)
    if isinstance(rule, TagsRule):
        mode = SET_LABELS.get(rule.mode, rule.mode)
        names = ", ".join(rule.tags) if rule.tags else "(none)"
        return f"Tags · {mode}: {names}"
    if isinstance(rule, CategoriesRule):
        mode = SET_LABELS.get(rule.mode, rule.mode)
        names = ", ".join(paths.get(i, i) for i in rule.folder_ids) or "(none)"
        return f"Categories · {mode}: {names}"
    if isinstance(rule, DateRule):
        label = DATE_KIND_LABELS.get(rule.kind, rule.kind)
        if rule.op == DATE_OP_WITHIN:
            n = max(1, int(rule.days or 1))
            unit = "day" if n == 1 else "days"
            return f"{label} · last {n} {unit}"
        op = DATE_OP_LABELS.get(rule.op, rule.op)
        day = rule.day or "(date)"
        return f"{label} · {op} {day}"
    raw = rule.raw
    prop = raw.get("property") or "?"
    method = raw.get("method") or "?"
    value = raw.get("value")
    if isinstance(value, list):
        shown = ", ".join(str(v) for v in value[:6])
        if len(value) > 6:
            shown += "…"
    else:
        shown = str(value)
    return f"{prop} {method} {shown}"


def group_mode_label(mode: str) -> str:
    return GROUP_LABELS.get(mode, mode)


def summarize_conditions(
    conditions: Iterable[dict[str, Any]] | None,
    *,
    folder_paths: dict[str, str] | None = None,
) -> list[str]:
    """Human lines for inherited / other-group display."""
    groups, other = decode_conditions(conditions)
    lines: list[str] = []
    for i, g in enumerate(groups, start=1):
        head = f"Group {i} · {group_mode_label(g.mode)}"
        if not g.rules:
            lines.append(head + " (empty)")
            continue
        lines.append(head)
        for rule in g.rules:
            lines.append("  " + rule_summary(rule, folder_paths=folder_paths))
    for og in other:
        lines.append("Unrecognized group (kept as-is)")
        for r in og.raw.get("rules") or []:
            if isinstance(r, dict):
                lines.append("  " + rule_summary(OtherRule(raw=r), folder_paths=folder_paths))
    return lines
