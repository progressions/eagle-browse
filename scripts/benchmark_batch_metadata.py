#!/usr/bin/env python3
"""Time folder assignments on disposable metadata, with real backups and fsync.

Example: python scripts/benchmark_batch_metadata.py --directory ~/.cache
Use --source /path/to/other/checkout to compare the same fixture across revisions.
No existing Eagle library is opened or modified.
"""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--directory', type=Path, default=None,
                        help='Temporary parent; choose a disk filesystem to measure fsync costs')
    parser.add_argument('--items', type=int, default=100)
    parser.add_argument('--index-entries', type=int, default=14516)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.items < 1 or args.index_entries < args.items or args.repeats < 1:
        parser.error('Require 1 <= items <= index-entries and repeats >= 1')
    sys.path.insert(0, str(args.source.resolve()))
    from library import EagleLibrary, Item

    for run in range(1, args.repeats + 1):
        with tempfile.TemporaryDirectory(prefix='eagle-batch-bench-', dir=args.directory) as directory:
            root = Path(directory)
            library = EagleLibrary(root)
            for number in range(args.items):
                iid = f'M{number:012d}'
                item_dir = root / 'images' / f'{iid}.info'
                item_dir.mkdir(parents=True)
                data = dict(id=iid, name=iid, ext='png', folders=[], tags=[],
                            annotation='', modificationTime=1, lastModified=1)
                (item_dir / 'metadata.json').write_text(json.dumps(data))
                item = Item(id=iid, name=iid, ext='png', tags=[], folders=[],
                            path=item_dir / f'{iid}.png', thumb=None, is_deleted=False,
                            size=1, width=1, height=1, annotation='', modification_time=1,
                            item_dir=item_dir, name_lower=iid.lower(), ext_lower='png')
                library.items.append(item)
                library.items_by_id[iid] = item
            index = {f'M{n:012d}': 1700000000000 for n in range(args.index_entries)}
            (root / 'mtime.json').write_text(json.dumps(index))
            ids = list(library.items_by_id)
            for action in ('add-folder', 'repeat-same-folder'):
                started = time.perf_counter()
                ok, errors = library.update_items_batch(ids, add_folders=['bench-folder'])
                elapsed = time.perf_counter() - started
                if ok != args.items or errors:
                    raise RuntimeError((ok, errors))
                print(json.dumps(dict(run=run, action=action, items=args.items,
                                      index_entries=args.index_entries, seconds=round(elapsed, 4))),
                      flush=True)


if __name__ == '__main__':
    main()
