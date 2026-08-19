#!/usr/bin/env python3
"""
Headless Eagle inbox watcher — import media without the full GTK app.

Polls the Dropbox inbox, waits for stable file sizes, then imports into the
library. Exact content duplicates follow --dup policy (default: reuse existing
and bump imported-at time).

Usage:
  eagle-inbox-watch                  # run forever
  eagle-inbox-watch --once           # single scan then exit
  eagle-inbox-watch --dup=skip       # leave dups in inbox
  eagle-inbox-watch --dup=queue      # move dups to inbox/.dup-queue
  eagle-inbox-watch --dup=new        # always create a new library item
  eagle-inbox-watch --dup=reuse      # use existing (default)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Allow running from any cwd
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sounds import gui_is_running, play_sound  # noqa: E402
from import_media import (  # noqa: E402
    DEFAULT_INBOX,
    check_zip_complete,
    classify_inbox_files,
    import_file,
    list_inbox_files,
    list_inbox_zips,
    reimport_existing,
    unpack_zip_to_inbox,
)
from library import DEFAULT_LIBRARY, EagleLibrary  # noqa: E402
from write import WriteError, write_session  # noqa: E402

LOG = logging.getLogger("eagle-inbox-watch")
STATE_DIR = Path.home() / ".local" / "state" / "eagle-browse"
DUP_QUEUE_DIRNAME = ".dup-queue"


def _setup_logging(*, verbose: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / "inbox-watch.log"
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    LOG.info("logging to %s", log_path)


def _notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(
                [
                    "notify-send",
                    "-a",
                    "Eagle Inbox",
                    "-u",
                    "low",
                    "-h",
                    "int:suppress-sound:1",
                    title,
                    body,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass


def _stable_ready(
    inbox: Path,
    sizes: dict[str, int],
    *,
    wait_logged: set[str] | None = None,
) -> tuple[list[Path], dict[str, int]]:
    """
    Return files seen twice with the same size (Dropbox-finished) *and*
    that pass media completeness (not a tiny incomplete stub).
    """
    from import_media import check_media_complete

    files = list_inbox_files(inbox)
    current: dict[str, int] = {}
    ready: list[Path] = []
    for p in files:
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz <= 0:
            continue
        current[p.name] = sz
        prev = sizes.get(p.name)
        if prev is not None and prev == sz:
            ok, reason = check_media_complete(p)
            if ok:
                ready.append(p)
                if wait_logged is not None:
                    wait_logged.discard(p.name)
            else:
                # Still downloading or corrupt — keep waiting.
                key = f"{p.name}:{sz}"
                if wait_logged is not None and key not in wait_logged:
                    wait_logged.add(key)
                    # Drop stale keys for this name
                    wait_logged.difference_update(
                        {k for k in list(wait_logged) if k.startswith(p.name + ":") and k != key}
                    )
                    LOG.info("waiting on %s: %s", p.name, reason)
    return ready, current


def _stable_ready_zips(
    inbox: Path,
    sizes: dict[str, int],
    *,
    wait_logged: set[str] | None = None,
) -> tuple[list[Path], dict[str, int]]:
    """Size-stable, readable zips ready to unpack."""
    files = list_inbox_zips(inbox)
    current: dict[str, int] = {}
    ready: list[Path] = []
    for p in files:
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz <= 0:
            continue
        current[p.name] = sz
        prev = sizes.get(p.name)
        if prev is None or prev != sz:
            continue
        ok, reason = check_zip_complete(p)
        if ok:
            ready.append(p)
            if wait_logged is not None:
                wait_logged.discard(p.name)
        elif wait_logged is not None:
            key = f"{p.name}:{sz}"
            if key not in wait_logged:
                wait_logged.add(key)
                wait_logged.difference_update(
                    {k for k in list(wait_logged) if k.startswith(p.name + ":") and k != key}
                )
                LOG.info("waiting on %s: %s", p.name, reason)
    return ready, current


def process_ready_zips(inbox: Path, ready: list[Path]) -> int:
    """Unpack ready zips into *inbox*. Returns number of media files flattened out."""
    total = 0
    for z in ready:
        n, err = unpack_zip_to_inbox(z, inbox)
        if err:
            LOG.warning("unzip failed %s: %s", z.name, err)
            continue
        LOG.info("unzipped %s → %d file(s) in inbox", z.name, n)
        total += n
    return total


def _queue_dup(source: Path, inbox: Path) -> Path | None:
    dest_dir = inbox / DUP_QUEUE_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    if dest.exists():
        stem, suf = source.stem, source.suffix
        n = 2
        while dest.exists():
            dest = dest_dir / f"{stem}_{n}{suf}"
            n += 1
    try:
        shutil.move(str(source), str(dest))
        return dest
    except OSError as exc:
        LOG.warning("could not queue %s: %s", source, exc)
        return None


def process_ready(
    library: EagleLibrary,
    ready: list[Path],
    *,
    dup_policy: str,
    notify: bool,
    sound: bool,
) -> tuple[int, int, int]:
    """
    Import ready files. Returns (new_count, reused_count, fail_count).
    """
    if not ready:
        return 0, 0, 0

    unique, dups = classify_inbox_files(ready, library.items)
    LOG.info(
        "batch: %d ready · %d unique · %d duplicate",
        len(ready),
        len(unique),
        len(dups),
    )

    new_n = 0
    reused_n = 0
    fail_n = 0

    t0 = time.perf_counter()
    new_ids: list[str] = []
    try:
        with write_session(library.root):
            for f in unique:
                r = import_file(
                    library.root,
                    f,
                    move_source=True,
                    hold_lock=True,
                    force_new=True,
                )
                if r.ok:
                    new_n += 1
                    if r.item_id:
                        new_ids.append(r.item_id)
                    LOG.info("imported new %s → %s", f.name, r.item_id)
                elif r.skipped and r.error and str(r.error).startswith("not-ready:"):
                    # Incomplete download; leave in inbox for a later poll.
                    LOG.info("defer %s: %s", f.name, r.error)
                else:
                    fail_n += 1
                    LOG.warning("import failed %s: %s", f.name, r.error)

            for match in dups:
                if dup_policy == "reuse":
                    r = reimport_existing(
                        library.root,
                        match.existing_id,
                        source=match.source,
                        move_source=True,
                        hold_lock=True,
                    )
                    if r.ok:
                        reused_n += 1
                        LOG.info(
                            "reused %s → existing %s",
                            match.source.name,
                            match.existing_id,
                        )
                    else:
                        fail_n += 1
                        LOG.warning("reuse failed %s: %s", match.source.name, r.error)
                elif dup_policy == "new":
                    r = import_file(
                        library.root,
                        match.source,
                        move_source=True,
                        hold_lock=True,
                        force_new=True,
                    )
                    if r.ok:
                        new_n += 1
                        if r.item_id:
                            new_ids.append(r.item_id)
                        LOG.info("imported dup-as-new %s → %s", match.source.name, r.item_id)
                    else:
                        fail_n += 1
                        LOG.warning("import failed %s: %s", match.source.name, r.error)
                elif dup_policy == "queue":
                    dest = _queue_dup(match.source, match.source.parent)
                    if dest:
                        LOG.info("queued duplicate %s → %s", match.source.name, dest)
                    else:
                        fail_n += 1
                else:  # skip
                    LOG.info("skipped duplicate %s (matches %s)", match.source.name, match.existing_id)
    except WriteError as exc:
        LOG.warning("library locked / write error: %s", exc)
        return new_n, reused_n, fail_n + 1

    # Incremental: only the new items, not a 25k-item rescan.
    # ingest_imported also joins a source set when the filename contains
    # an existing Eagle id (animation of a still, upscale-of, etc.).
    for iid in new_ids:
        try:
            library.ingest_imported(iid)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ingest %s after import failed: %s", iid, exc)

    elapsed = time.perf_counter() - t0
    LOG.info(
        "batch done in %.2fs · %d new · %d reused · %d failed",
        elapsed,
        new_n,
        reused_n,
        fail_n,
    )

    total_ok = new_n + reused_n
    gui_up = gui_is_running()
    if total_ok and notify and not gui_up:
        parts = []
        if new_n:
            parts.append(f"{new_n} new")
        if reused_n:
            parts.append(f"{reused_n} existing")
        _notify("Eagle inbox", " · ".join(parts) or f"{total_ok} imported")
    if total_ok and sound and not gui_up:
        # GUI plays the chime when it ingests the new item.
        play_sound("notification", once=True)
    return new_n, reused_n, fail_n


def run_loop(
    *,
    library_path: Path,
    inbox: Path,
    interval: float,
    once: bool,
    dup_policy: str,
    notify: bool,
    sound: bool,
) -> int:
    if not library_path.is_dir():
        LOG.error("library not found: %s", library_path)
        return 1
    if not inbox.is_dir():
        LOG.error("inbox not found: %s", inbox)
        return 1

    library = EagleLibrary(library_path)
    LOG.info("loading library %s …", library_path)
    library.load()
    LOG.info("library ready · %d items · inbox %s · dup=%s", len(library.items), inbox, dup_policy)

    sizes: dict[str, int] = {}
    zip_sizes: dict[str, int] = {}
    wait_logged: set[str] = set()
    while True:
        try:
            ready_zips, zip_sizes = _stable_ready_zips(
                inbox, zip_sizes, wait_logged=wait_logged
            )
            if ready_zips:
                process_ready_zips(inbox, ready_zips)
            ready, sizes = _stable_ready(inbox, sizes, wait_logged=wait_logged)
            # Only import files that are ready (stable size + playable)
            if ready:
                process_ready(
                    library,
                    ready,
                    dup_policy=dup_policy,
                    notify=notify,
                    sound=sound,
                )
        except Exception:  # noqa: BLE001
            LOG.exception("poll error")

        if once:
            return 0
        time.sleep(max(1.0, interval))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Headless Eagle.cool inbox importer")
    p.add_argument(
        "--library",
        default=str(DEFAULT_LIBRARY),
        help="Path to .library directory",
    )
    p.add_argument(
        "--inbox",
        default=str(DEFAULT_INBOX),
        help="Inbox folder to watch",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("EAGLE_INBOX_INTERVAL", "3")),
        help="Poll interval seconds (default 3)",
    )
    p.add_argument(
        "--dup",
        choices=("reuse", "skip", "queue", "new"),
        default=os.environ.get("EAGLE_INBOX_DUP", "reuse"),
        help="Exact-duplicate policy (default: reuse)",
    )
    p.add_argument("--once", action="store_true", help="Single scan then exit")
    p.add_argument("--no-notify", action="store_true", help="Disable desktop notifications")
    p.add_argument("--no-sound", action="store_true", help="Disable import sound")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    _setup_logging(verbose=args.verbose)
    return run_loop(
        library_path=Path(args.library).expanduser(),
        inbox=Path(args.inbox).expanduser(),
        interval=args.interval,
        once=args.once,
        dup_policy=args.dup,
        notify=not args.no_notify,
        sound=not args.no_sound,
    )


if __name__ == "__main__":
    raise SystemExit(main())
