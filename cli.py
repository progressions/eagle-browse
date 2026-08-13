#!/usr/bin/env python3
"""
eagle-api — CLI for the Eagle.cool library (humans + agents).

Human-readable tables by default; pass --json for machine-readable output.

  eagle-api search --tag eunbi --rating-min 3
  eagle-api search --smart-folder Eunbi/images --json
  eagle-api tag add <id> sofie
  eagle-api crop <id> --aspect 9:16 --mode new
  eagle-api smart-folder create --name "Sofie videos 3+" --tag sofie --type video --rating-min 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from api import EagleAPI, _UNSET, build_parser as _api_build_parser
from library import DEFAULT_LIBRARY
from write import WriteError


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _emit_json(data: Any, *, compact: bool = False) -> None:
    if compact:
        print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _stars(n: int | None) -> str:
    if not n:
        return "—"
    return "★" * int(n) + "☆" * (5 - int(n))


def _fmt_search(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "search failed")
        return
    total = data.get("total", 0)
    items = data.get("items") or []
    print(f"{len(items)} shown · {total} total")
    if not items:
        return
    # columns: stars name tags folders path
    for it in items:
        star = _stars(it.get("star") or it.get("rating"))
        tags = ",".join(it.get("tags") or []) or "—"
        folders = ",".join(it.get("folder_names") or []) or "—"
        name = it.get("display_name") or it.get("name") or "?"
        print(f"{star}  {name}")
        print(f"    id {it.get('id')}  type {it.get('ext')}  tags [{tags}]")
        print(f"    folders [{folders}]")
        print(f"    {it.get('path')}")


def _fmt_get(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "not found")
        return
    it = data["item"]
    print(it.get("display_name"))
    print(f"  id:       {it.get('id')}")
    print(f"  rating:   {_stars(it.get('star'))}")
    print(f"  tags:     {', '.join(it.get('tags') or []) or '—'}")
    print(f"  folders:  {', '.join(it.get('folder_names') or []) or '—'}")
    print(f"  size:     {it.get('width')}×{it.get('height')}  {it.get('size')} bytes")
    if it.get("duration"):
        print(f"  duration: {it.get('duration')}s")
    print(f"  path:     {it.get('path')}")


def _fmt_ids(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        return
    for i in data.get("ids") or []:
        print(i)
    print(f"# {data.get('total', 0)} total", file=sys.stderr)


def _fmt_ok_action(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        if data.get("errors"):
            for e in data["errors"]:
                print(f"  {e}", file=sys.stderr)
        return
    parts = [f"updated {data.get('updated', '?')}"]
    if data.get("action"):
        parts.append(data["action"])
    if data.get("tags"):
        parts.append("tags=" + ",".join(data["tags"]))
    if data.get("folder_ids"):
        parts.append("folders=" + ",".join(data["folder_ids"]))
    if "rating" in data:
        parts.append(f"rating={data['rating']}")
    print("ok · " + " · ".join(parts))


def _fmt_tags(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        return
    tags = data.get("tags") or []
    print(f"{len(tags)} tags")
    # multi-column-ish
    width = max((len(t) for t in tags), default=8)
    col_w = min(width + 2, 28)
    cols = max(1, 100 // col_w)
    for i, t in enumerate(tags):
        end = "\n" if (i + 1) % cols == 0 else ""
        print(f"{t:<{col_w}}", end=end)
    if tags and len(tags) % cols:
        print()


def _fmt_folders(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        return
    for f in data.get("folders") or []:
        pad = "  " * int(f.get("depth") or 0)
        auto = f.get("auto_tags") or []
        auto_s = f"  🏷 {', '.join(auto)}" if auto else ""
        print(f"{pad}{f.get('name')}  ({f.get('id')}){auto_s}")
        if f.get("path") and f.get("depth", 0) > 0:
            pass  # name is enough in tree


def _fmt_smart_list(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        return
    print(f"{data.get('count', 0)} smart folders")
    for s in data.get("smart_folders") or []:
        pad = "  " * int(s.get("depth") or 0)
        print(f"{pad}{s.get('path')}  [{s.get('id')}]")


def _fmt_smart_show(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        return
    sf = data["smart_folder"]
    print(sf.get("path") or sf.get("name"))
    print(f"  id:      {sf.get('id')}")
    print(f"  parent:  {sf.get('parent_id') or '—'}")
    print(f"  matches: {sf.get('match_count', '?')}")
    print(f"  children: {len(sf.get('children') or [])}")
    print("  conditions:")
    print(json.dumps(sf.get("conditions"), indent=4, ensure_ascii=False))
    for c in sf.get("children") or []:
        print(f"  · {c.get('path')}")


def _fmt_smart_create(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        return
    sf = data["smart_folder"]
    print(f"created · {sf.get('path')}  [{sf.get('id')}]")
    print("reload Eagle Browse (r) to see it in the sidebar")


def _fmt_smart_update(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        return
    sf = data["smart_folder"]
    print(f"updated · {sf.get('path')}  [{sf.get('id')}]")


def _fmt_smart_delete(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        return
    n = data.get("deleted_count") or 1
    name = data.get("deleted_name") or data.get("deleted_id")
    extra = f" · {n} folders" if n > 1 else ""
    print(f"deleted · {name}{extra}")


def _fmt_smart_move(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        return
    sf = data["smart_folder"]
    print(f"moved · {sf.get('path')}  [{sf.get('id')}]")


def _fmt_crop(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "crop failed")
        return
    mode = data.get("mode")
    rect = data.get("rect") or {}
    it = data.get("item") or {}
    print(f"crop · {mode}")
    print(
        f"  rect:  {rect.get('width')}×{rect.get('height')} "
        f"@ ({rect.get('x')}, {rect.get('y')})"
    )
    print(f"  id:    {it.get('id')}")
    print(f"  size:  {it.get('width')}×{it.get('height')}")
    print(f"  tags:  {', '.join(it.get('tags') or []) or '—'}")
    print(f"  path:  {it.get('path')}")
    if mode == "new":
        print(f"  source: {data.get('source_id')} (unchanged)")


def _fmt_reload(data: dict[str, Any]) -> None:
    if not data.get("ok"):
        _err(data.get("error") or "failed")
        return
    print(
        f"loaded {data.get('items')} items · "
        f"{data.get('folders')} folders · "
        f"{data.get('smart_folders')} smart folders"
    )
    print(data.get("library"))


def build_parser() -> argparse.ArgumentParser:
    """CLI parser: api parser + human/json flags."""
    # Reuse api command structure by building our own with clearer help
    p = argparse.ArgumentParser(
        prog="eagle-api",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Query and update an Eagle.cool library (CLI for humans and agents).",
        epilog="""
examples:
  eagle-api search --tag eunbi --rating-min 3
  eagle-api search --smart-folder Eunbi/images --type video --limit 20
  eagle-api search --folder Eunbi --json
  eagle-api get MXXXXXXXXXXXX
  eagle-api tag add MXXXXXXXXXXXX sofie,raw
  eagle-api folder add MXXXXXXXXXXXX Eunbi
  eagle-api rate MXXXXXXXXXXXX 4
  eagle-api crop MXXXXXXXXXXXX --aspect 9:16 --mode new
  eagle-api crop MXXXXXXXXXXXX --width 1080 --height 1440 --anchor top --mode overwrite
  eagle-api smart-folder list
  eagle-api smart-folder show Eunbi/images
  eagle-api smart-folder create --name "Sofie videos 3+" --tag sofie --type video --rating-min 3
  eagle-api smart-folder update "Sofie videos 3+" --rating-min 4
  eagle-api smart-folder delete "Sofie videos 3+"

environment:
  EAGLE_LIBRARY   default library path
""",
    )
    p.add_argument(
        "--library",
        default=os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)),
        help="Path to .library directory",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON (for agents)",
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="Compact JSON (implies --json)",
    )

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
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
    s.add_argument("--folder", default="", help="Category name, path, or id")
    s.add_argument(
        "--smart-folder",
        default="",
        help='Smart folder name or path, e.g. "Eunbi/images"',
    )
    s.add_argument("--name", default="", dest="name_contains", help="Name contains")
    s.add_argument("--rating", type=int, default=None, help="Exact stars 1–5")
    s.add_argument("--rating-min", type=int, default=None)
    s.add_argument("--rating-max", type=int, default=None)
    s.add_argument("--type", default="", dest="media_type", help="image|video|audio|ext")
    s.add_argument("--untagged", action="store_true")
    s.add_argument("--uncategorized", action="store_true")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--ids-only", action="store_true", help="Print only item ids")

    g = sub.add_parser("get", parents=[shared], help="Show one item by id")
    g.add_argument("id")

    t = sub.add_parser("tag", parents=[shared], help="Add or remove tags")
    t.add_argument("action", choices=("add", "remove"))
    t.add_argument("ids", help="Item id or comma-separated ids")
    t.add_argument("tags", help="Tag or comma-separated tags")

    f = sub.add_parser("folder", parents=[shared], help="Add/remove categories")
    f.add_argument("action", choices=("add", "remove"))
    f.add_argument("ids", help="Item id or comma-separated ids")
    f.add_argument("folders", help="Folder name/path/id or comma-separated")

    r = sub.add_parser("rate", parents=[shared], help="Set rating 0–5 (0 clears)")
    r.add_argument("ids", help="Item id or comma-separated ids")
    r.add_argument("rating", type=int)

    c = sub.add_parser(
        "crop",
        parents=[shared],
        help="Crop an image (overwrite original or save as new untagged item)",
    )
    c.add_argument("id", help="Item id")
    c.add_argument(
        "--mode",
        default="overwrite",
        choices=("overwrite", "new", "save-as"),
        help="overwrite = replace original; new/save-as = fresh item, no tags/folders",
    )
    c.add_argument("--x", type=int, default=None, help="Crop left (source px)")
    c.add_argument("--y", type=int, default=None, help="Crop top (source px)")
    c.add_argument("--width", type=int, default=None, help="Crop width (source px)")
    c.add_argument("--height", type=int, default=None, help="Crop height (source px)")
    c.add_argument(
        "--aspect",
        default="",
        help="Aspect lock: 9:16, 3:4, 1:1, 16:9, 2:3, 3:2, 4:3, orig, free",
    )
    c.add_argument(
        "--anchor",
        default="center",
        help="Placement when --x/--y omitted (center, top, bottom, left, right, "
        "top-left, top-right, bottom-left, bottom-right)",
    )

    sub.add_parser("tags", parents=[shared], help="List all tags")
    sub.add_parser("folders", parents=[shared], help="List categories (tree)")
    sub.add_parser("reload", parents=[shared], help="Reload library from disk")

    sf = sub.add_parser("smart-folder", parents=[shared], help="Smart folders")
    sfs = sf.add_subparsers(dest="sf_cmd", required=True)
    sfs.add_parser("list", parents=[shared], help="List smart folders")
    sfs_show = sfs.add_parser("show", parents=[shared], help="Show smart folder")
    sfs_show.add_argument("path", help="Name, path (Eunbi/images), or id")
    sfs_c = sfs.add_parser("create", parents=[shared], help="Create smart folder")
    sfs_c.add_argument("--name", required=True)
    sfs_c.add_argument("--parent", default="", help="Parent smart folder")
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
        help="Raw conditions JSON (overrides filters)",
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
        help="Raw conditions JSON (replaces existing conditions)",
    )
    sfs_d = sfs.add_parser("delete", parents=[shared], help="Delete smart folder")
    sfs_d.add_argument("path", help="Name, path, or id")
    sfs_d.add_argument(
        "--force",
        action="store_true",
        help="Delete even if the folder has children",
    )
    sfs_m = sfs.add_parser("move", parents=[shared], help="Reorder a smart folder")
    sfs_m.add_argument("path", help="Name, path, or id to move")
    sfs_m.add_argument("--before", default="", help="Place immediately before this folder")
    sfs_m.add_argument("--after", default="", help="Place immediately after this folder")
    sfs_m.add_argument(
        "--first",
        action="store_true",
        help="Move to the start of the root list",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_intermixed_args(argv)
    except TypeError:
        args = parser.parse_args(argv)

    as_json = bool(args.json or args.compact)
    compact = bool(args.compact)

    try:
        api = EagleAPI(args.library)
    except Exception as exc:  # noqa: BLE001
        if as_json:
            _emit_json({"ok": False, "error": str(exc)}, compact=compact)
        else:
            _err(str(exc))
        return 1

    def out(data: dict[str, Any], human) -> int:
        if as_json:
            _emit_json(data, compact=compact)
        else:
            human(data)
        return 0 if data.get("ok", True) else 2

    try:
        if args.cmd == "reload":
            return out(api.reload(), _fmt_reload)

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
                return out(result, _fmt_ids)
            return out(result, _fmt_search)

        if args.cmd == "get":
            return out(api.get(args.id), _fmt_get)

        if args.cmd == "tag":
            ids = _split_csv(args.ids)
            tags = _split_csv(args.tags)
            if args.action == "add":
                result = api.add_tags(ids, tags)
            else:
                result = api.remove_tags(ids, tags)
            return out(result, _fmt_ok_action)

        if args.cmd == "folder":
            ids = _split_csv(args.ids)
            folders = _split_csv(args.folders)
            if args.action == "add":
                result = api.add_folders(ids, folders)
            else:
                result = api.remove_folders(ids, folders)
            return out(result, _fmt_ok_action)

        if args.cmd == "rate":
            result = api.set_rating(_split_csv(args.ids), args.rating)
            return out(result, _fmt_ok_action)

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
            return out(result, _fmt_crop)

        if args.cmd == "tags":
            return out(api.list_tags(), _fmt_tags)

        if args.cmd == "folders":
            return out(api.list_folders(), _fmt_folders)

        if args.cmd == "smart-folder":
            if args.sf_cmd == "list":
                return out(api.list_smart_folders(), _fmt_smart_list)
            if args.sf_cmd == "show":
                return out(api.get_smart_folder(args.path), _fmt_smart_show)
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
                return out(result, _fmt_smart_create)
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
                return out(result, _fmt_smart_update)
            if args.sf_cmd == "delete":
                result = api.delete_smart_folder(args.path, force=bool(args.force))
                return out(result, _fmt_smart_delete)
            if args.sf_cmd == "move":
                result = api.move_smart_folder(
                    args.path,
                    before=args.before or None,
                    after=args.after or None,
                    first=bool(args.first),
                )
                return out(result, _fmt_smart_move)

        if as_json:
            _emit_json({"ok": False, "error": f"Unknown command: {args.cmd}"}, compact=compact)
        else:
            _err(f"Unknown command: {args.cmd}")
        return 1

    except WriteError as exc:
        if as_json:
            _emit_json({"ok": False, "error": str(exc)}, compact=compact)
        else:
            _err(str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001
        if as_json:
            _emit_json({"ok": False, "error": str(exc)}, compact=compact)
        else:
            _err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
