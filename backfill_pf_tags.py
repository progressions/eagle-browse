#!/usr/bin/env python3
"""Backfill Eagle ``pf:<id>`` tags from ``image-<id>-…`` filenames (Fizzy #482).

Usage:
  python3 backfill_pf_tags.py --dry-run
  python3 backfill_pf_tags.py
  python3 backfill_pf_tags.py --library /path/to/Eunbi.library --limit 20

Also available as: ``eagle-api backfill-pf [--dry-run]``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import DEFAULT_LIBRARY  # noqa: E402
from promptforge_stamp import history_id_from_name, stamp_metadata  # noqa: E402
from write import WriteError, load_item_metadata, save_item_metadata, write_session  # noqa: E402


def resolve_library(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = __import__("os").environ.get("EAGLE_LIBRARY")
    if env:
        return Path(env).expanduser().resolve()
    return Path(DEFAULT_LIBRARY).expanduser().resolve()


def iter_item_dirs(library_root: Path):
    images = library_root / "images"
    if not images.is_dir():
        return
    for item_dir in images.iterdir():
        if item_dir.is_dir() and item_dir.name.endswith(".info"):
            yield item_dir


def backfill(
    library_root: Path,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    scanned = 0
    candidates = 0
    would_change = 0
    updated = 0
    skipped = 0
    errors = 0

    pending: list[tuple[Path, dict]] = []

    for item_dir in iter_item_dirs(library_root):
        scanned += 1
        meta_path = item_dir / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            with meta_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            errors += 1
            continue
        if not isinstance(data, dict) or data.get("isDeleted"):
            continue
        name = str(data.get("name") or "")
        if history_id_from_name(name) is None:
            continue
        candidates += 1
        before_tags = list(data.get("tags") or [])
        before_ann = data.get("annotation") or ""
        # Work on a copy so dry-run does not mutate the loaded dict used only for compare.
        probe = {
            "name": name,
            "tags": list(before_tags),
            "annotation": before_ann,
        }
        if not stamp_metadata(probe, name=name):
            skipped += 1
            continue
        would_change += 1
        pending.append((item_dir, probe))
        if limit is not None and would_change >= limit:
            break

    if dry_run:
        for item_dir, probe in pending:
            hid = history_id_from_name(probe.get("name"))
            print(
                f"would stamp {item_dir.name.removesuffix('.info')}  "
                f"name={probe.get('name')!r}  pf:{hid}"
            )
        return {
            "scanned": scanned,
            "candidates": candidates,
            "would_change": would_change,
            "updated": 0,
            "skipped": skipped,
            "errors": errors,
            "dry_run": 1,
        }

    if pending:
        try:
            with write_session(library_root):
                for item_dir, _probe in pending:
                    try:
                        data = load_item_metadata(item_dir)
                        if not stamp_metadata(data):
                            skipped += 1
                            continue
                        save_item_metadata(library_root, item_dir, data)
                        updated += 1
                        print(
                            f"stamped {data.get('id')}  name={data.get('name')!r}  "
                            f"pf:{history_id_from_name(str(data.get('name') or ''))}"
                        )
                    except WriteError as exc:
                        errors += 1
                        print(f"error {item_dir.name}: {exc}", file=sys.stderr)
                    except Exception as exc:  # noqa: BLE001
                        errors += 1
                        print(f"error {item_dir.name}: {exc}", file=sys.stderr)
        except WriteError as exc:
            print(f"error: library locked / write failed: {exc}", file=sys.stderr)
            return {
                "scanned": scanned,
                "candidates": candidates,
                "would_change": would_change,
                "updated": updated,
                "skipped": skipped,
                "errors": errors + 1,
                "dry_run": 0,
            }

    return {
        "scanned": scanned,
        "candidates": candidates,
        "would_change": would_change,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "dry_run": 0,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--library",
        default="",
        help=f"Eagle library root (default: {DEFAULT_LIBRARY})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change; do not write",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max items to stamp (after filtering to image-<id>- names needing tags)",
    )
    args = p.parse_args(argv)
    library = resolve_library(args.library or None)
    if not library.is_dir():
        print(f"error: library not found: {library}", file=sys.stderr)
        return 2
    stats = backfill(library, dry_run=args.dry_run, limit=args.limit)
    print(
        "summary: "
        f"scanned={stats['scanned']} candidates={stats['candidates']} "
        f"would_change={stats['would_change']} updated={stats['updated']} "
        f"skipped={stats['skipped']} errors={stats['errors']} "
        f"dry_run={bool(stats['dry_run'])}"
    )
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
