#!/usr/bin/env python3
"""Build a synthetic Eagle library for performance / regression fixtures.

Does not use private library data. Writes under a temp dir (or --out).

Examples:

  python3 scripts/synth_catalog.py --count 5000
  python3 scripts/synth_catalog.py --count 20000 --out /tmp/synth.library --bench
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

from library import EagleLibrary


def write_synth_library(root: Path, count: int) -> None:
    """Create *count* tiny stills under images/<id>.info/."""
    root.mkdir(parents=True, exist_ok=True)
    images = root / "images"
    images.mkdir(exist_ok=True)
    (root / "metadata.json").write_text(
        json.dumps(
            {"folders": [], "smartFolders": [], "tagsGroups": [], "modificationTime": 1},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # 1x1 PNG
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    for i in range(count):
        item_id = f"SYN{i:06d}"
        item_dir = images / f"{item_id}.info"
        item_dir.mkdir(exist_ok=True)
        media = item_dir / f"{item_id}.png"
        media.write_bytes(png)
        meta = {
            "id": item_id,
            "name": item_id,
            "ext": "png",
            "size": len(png),
            "width": 1,
            "height": 1,
            "tags": ["synth", f"bucket-{i % 50}"],
            "folders": [],
            "isDeleted": False,
            "annotation": "",
            "modificationTime": 1_700_000_000_000 + i,
            "btime": 1_700_000_000_000 + i,
            "mtime": 1_700_000_000_000 + i,
        }
        (item_dir / "metadata.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )


def bench_queries(library: EagleLibrary, repeats: int = 5) -> dict[str, float]:
    """Time cold vs warm query() on the synthetic catalog."""
    library._invalidate_caches()  # noqa: SLF001
    t0 = time.perf_counter()
    cold = library.query(include_deleted=False)
    cold_s = time.perf_counter() - t0
    warm_times: list[float] = []
    for _ in range(repeats):
        t1 = time.perf_counter()
        warm = library.query(include_deleted=False)
        warm_times.append(time.perf_counter() - t1)
        if warm is not cold:
            # Still correct if invalidate happened; identity expected when warm.
            pass
    return {
        "items": float(len(cold)),
        "cold_s": cold_s,
        "warm_mean_s": sum(warm_times) / len(warm_times),
        "warm_min_s": min(warm_times),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000, help="Number of items")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Library root (default: temp dir, deleted unless --keep)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Do not delete the temp library when --out is omitted",
    )
    parser.add_argument(
        "--bench",
        action="store_true",
        help="Load the library and print cold/warm query timings",
    )
    args = parser.parse_args(argv)

    cleanup = False
    if args.out is None:
        root = Path(tempfile.mkdtemp(prefix="eagle-synth-"))
        cleanup = not args.keep
    else:
        root = args.out.expanduser().resolve()

    write_synth_library(root, max(0, int(args.count)))
    print(f"synth library: {root} ({args.count} items)")

    if args.bench:
        library = EagleLibrary(root)
        library.load()
        stats = bench_queries(library)
        print(
            f"bench items={int(stats['items'])} "
            f"cold={stats['cold_s']*1000:.1f}ms "
            f"warm_mean={stats['warm_mean_s']*1000:.2f}ms "
            f"warm_min={stats['warm_min_s']*1000:.2f}ms"
        )

    if cleanup:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
