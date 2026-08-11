#!/usr/bin/env python3
"""Build a compact Eagle library index for the phone web browser.

Writes phone-index.json (default: inside the library root). The web app and
phone_server use this to filter by tags/folders without scanning 25k item
dirs on every request. Media files stay on disk / Dropbox; only metadata is
in the index.

Usage:
  ./build_phone_index.py
  ./build_phone_index.py /path/to/Eunbi.library
  EAGLE_LIBRARY=... ./build_phone_index.py -o /tmp/phone-index.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running from any cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from library import DEFAULT_LIBRARY, EagleLibrary  # noqa: E402

INDEX_VERSION = 1
DEFAULT_NAME = "phone-index.json"

# Well-known character folders in this library (also resolved by name).
CHARACTER_NAMES = ("Eunbi", "Sofie")


def _folder_tree(nodes) -> list[dict]:
    out = []
    for f in nodes:
        out.append(
            {
                "id": f.id,
                "name": f.name,
                "tags": list(f.tags),
                "children": _folder_tree(f.children),
            }
        )
    return out


def _character_folder_ids(lib: EagleLibrary) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in CHARACTER_NAMES:
        for fid, folder in lib.folders_by_id.items():
            if folder.name == name:
                found[name.lower()] = fid
                break
    return found


def build_index(library_path: Path) -> dict:
    lib = EagleLibrary(library_path)
    t0 = time.perf_counter()
    lib.load()
    load_s = time.perf_counter() - t0

    items = []
    for item in lib.items:
        if item.is_deleted:
            continue
        row = {
            "id": item.id,
            "name": item.name,
            "ext": item.ext,
            "tags": item.tags,
            "folders": item.folders,
            "mtime": item.modification_time,
            "w": item.width,
            "h": item.height,
            "size": item.size,
            "has_thumb": item.thumb is not None,
        }
        if item.star is not None:
            row["star"] = item.star
        if item.duration is not None:
            row["duration"] = item.duration
        items.append(row)

    # Newest first (same as EagleLibrary.load)
    items.sort(key=lambda r: r.get("mtime") or 0, reverse=True)

    tag_counts: dict[str, int] = {}
    for row in items:
        for t in row["tags"]:
            tag_counts[t] = tag_counts.get(t, 0) + 1

    catalog = {
        "version": INDEX_VERSION,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "library": str(library_path.resolve()),
        "item_count": len(items),
        "load_seconds": round(load_s, 2),
        "characters": _character_folder_ids(lib),
        "folders": _folder_tree(lib.folders),
        "tags": sorted(tag_counts.keys(), key=lambda t: (-tag_counts[t], t.lower())),
        "tag_counts": tag_counts,
        "items": items,
    }
    return catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="Build phone-index.json for Eagle phone browse")
    parser.add_argument(
        "library",
        nargs="?",
        default=os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)),
        help="Path to *.library (default: EAGLE_LIBRARY or Eunbi.library)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help=f"Output path (default: <library>/{DEFAULT_NAME})",
    )
    args = parser.parse_args()

    library_path = Path(args.library).expanduser().resolve()
    if not library_path.is_dir():
        print(f"Library not found: {library_path}", file=sys.stderr)
        return 1

    out_path = Path(args.output).expanduser() if args.output else library_path / DEFAULT_NAME
    print(f"Loading {library_path} …", flush=True)
    catalog = build_index(library_path)
    text = json.dumps(catalog, separators=(",", ":"), ensure_ascii=False)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out_path)

    mb = out_path.stat().st_size / (1024 * 1024)
    print(
        f"Wrote {out_path}  ({catalog['item_count']} items, "
        f"{mb:.1f} MB, load {catalog['load_seconds']}s)"
    )
    chars = catalog.get("characters") or {}
    if chars:
        print("Character folders:", ", ".join(f"{k}={v}" for k, v in chars.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
