#!/usr/bin/env python3
"""Local LAN server for browsing an Eagle library on a phone.

Binds 0.0.0.0 so other devices on the same Wi‑Fi can open the UI.
No deploy, no Dropbox OAuth for this path — serves files from the local library.

Usage:
  ./phone_server.py
  ./phone_server.py --port 8787
  ./phone_server.py /path/to/Eunbi.library

Then on your phone (same network):
  http://eagle.local:8787/

(mDNS name requires avahi-daemon; falls back to printing the LAN IP.)

Rebuild index first (or let the server build on start if missing):
  ./build_phone_index.py
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from library import DEFAULT_LIBRARY, _item_from_dir, _resolve_media_paths  # noqa: E402
from write import INBOX_SIGNAL_FILENAME  # noqa: E402

PHONE_WEB = Path(__file__).resolve().parent / "phone_web"
INDEX_NAME = "phone-index.json"
DEFAULT_PORT = 8787
DEFAULT_MDNS_NAME = "eagle"  # → eagle.local


def _lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        out = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in out:
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    # Fallback: UDP trick for primary outbound interface
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in ips and not ip.startswith("127."):
            ips.insert(0, ip)
    except OSError:
        pass
    return ips


def _normalize_mdns_host(name: str) -> str:
    """'eagle' or 'eagle.local' → 'eagle.local'."""
    name = (name or "").strip().lower().rstrip(".")
    if not name:
        name = DEFAULT_MDNS_NAME
    if name.endswith(".local"):
        return name
    return f"{name}.local"


class MdnsPublisher:
    """Publish a friendly *.local hostname via avahi-publish (IPv4 A record)."""

    def __init__(self, hostname: str, ip: str, port: int):
        self.hostname = _normalize_mdns_host(hostname)
        self.ip = ip
        self.port = port
        self._procs: list[subprocess.Popen] = []

    def start(self) -> bool:
        if not shutil.which("avahi-publish"):
            print("  mDNS: avahi-publish not found — use the IP URL", flush=True)
            return False
        # -R: allow replacing a stale record if we crashed last time
        addr_cmd = ["avahi-publish", "-a", "-R", self.hostname, self.ip]
        # Optional service browse entry (Bonjour-friendly label)
        short = self.hostname.removesuffix(".local")
        svc_cmd = [
            "avahi-publish",
            "-s",
            short,
            "_http._tcp",
            str(self.port),
            f"path=/",
        ]
        ok = False
        for cmd in (addr_cmd, svc_cmd):
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                # Give avahi a moment to fail fast if name is taken badly
                time.sleep(0.15)
                if proc.poll() is not None:
                    err = (proc.stderr.read() if proc.stderr else "") or f"exit {proc.returncode}"
                    cmd_short = " ".join(cmd[:3])
                    print(f"  mDNS: failed ({cmd_short}…): {err.strip()}", flush=True)
                    continue
                self._procs.append(proc)
                ok = True
            except OSError as e:
                print(f"  mDNS: could not start avahi-publish: {e}", flush=True)
        if ok:
            print(f"  mDNS: {self.hostname} → {self.ip}", flush=True)
        return ok

    def stop(self) -> None:
        for proc in self._procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._procs.clear()


class PhoneBrowseHandler(SimpleHTTPRequestHandler):
    # Set on the class by main()
    library_root: Path = Path()
    index_path: Path = Path()
    catalog: dict | None = None
    items_by_id: dict = {}
    lock = threading.Lock()
    recent_imports: list = []  # (ts, row)
    updates_ts: float = 0.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PHONE_WEB), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self) -> None:
        # Phone browsers on LAN; allow nothing fancy needed for same-origin.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", "/index.html"):
            return self._serve_file(PHONE_WEB / "index.html", "text/html; charset=utf-8")

        if path.startswith("/api/"):
            return self._api(path, parse_qs(parsed.query))

        if path.startswith("/thumb/"):
            return self._media(path[len("/thumb/") :], thumb=True)
        if path.startswith("/media/"):
            return self._media(path[len("/media/") :], thumb=False)

        # Static assets from phone_web/
        return super().do_GET()

    def _api(self, path: str, qs: dict) -> None:
        if path == "/api/health":
            return self._json(
                {
                    "ok": True,
                    "library": str(self.library_root),
                    "item_count": (self.catalog or {}).get("item_count"),
                    "built_at": (self.catalog or {}).get("built_at"),
                    "updated_at": self.updates_ts,
                }
            )

        if path == "/api/updates":
            try:
                since = float((qs.get("since") or ["0"])[0])
            except (TypeError, ValueError):
                since = 0.0
            with self.lock:
                items = [row for ts, row in self.recent_imports if ts > since]
                ts = self.updates_ts or time.time()
            return self._json({"ts": ts, "items": items})

        if path == "/api/catalog":
            if not self.catalog:
                return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "index not loaded")
            # Full catalog (folders + items). ~few MB; fine on LAN.
            with self.lock:
                catalog = dict(self.catalog)
                catalog["items"] = list(self.catalog.get("items") or [])
            return self._json(catalog)

        if path == "/api/meta":
            # Lightweight: folders, tags, characters — no items
            if not self.catalog:
                return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "index not loaded")
            c = self.catalog
            return self._json(
                {
                    "version": c.get("version"),
                    "built_at": c.get("built_at"),
                    "item_count": c.get("item_count"),
                    "characters": c.get("characters") or {},
                    "folders": c.get("folders") or [],
                    "tags": c.get("tags") or [],
                    "tag_counts": c.get("tag_counts") or {},
                }
            )

        if path == "/api/reload":
            try:
                self.__class__.load_catalog(rebuild=False)
                return self._json({"ok": True, "item_count": self.catalog.get("item_count")})
            except Exception as e:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

        if path == "/api/rebuild":
            try:
                self.__class__.load_catalog(rebuild=True)
                return self._json({"ok": True, "item_count": self.catalog.get("item_count")})
            except Exception as e:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

        return self._error(HTTPStatus.NOT_FOUND, "unknown api")

    def _media(self, item_id: str, *, thumb: bool) -> None:
        item_id = item_id.strip().strip("/")
        if not item_id or "/" in item_id or ".." in item_id:
            return self._error(HTTPStatus.BAD_REQUEST, "bad id")

        item_dir = self.library_root / "images" / f"{item_id}.info"
        if not item_dir.is_dir():
            return self._error(HTTPStatus.NOT_FOUND, "item not found")

        meta_path = item_dir / "metadata.json"
        name, ext = item_id, ""
        if meta_path.is_file():
            try:
                raw = json.loads(meta_path.read_text(encoding="utf-8"))
                name = raw.get("name") or name
                ext = (raw.get("ext") or "").lstrip(".")
            except (OSError, json.JSONDecodeError):
                pass

        original, thumb_path = _resolve_media_paths(item_dir, name, ext)
        target = thumb_path if thumb else original
        if thumb and target is None:
            target = original  # fall back to full if no thumb
        if target is None or not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "file missing")

        # Stay inside the library
        try:
            target.resolve().relative_to(self.library_root.resolve())
        except ValueError:
            return self._error(HTTPStatus.FORBIDDEN, "path escape")

        return self._send_file_bytes(target)

    def _send_file_bytes(self, path: Path) -> None:
        ctype, _ = mimetypes.guess_type(str(path))
        if not ctype:
            ctype = "application/octet-stream"
        size = path.stat().st_size
        range_header = self.headers.get("Range")

        if range_header and range_header.startswith("bytes="):
            # Simple single-range for mobile video
            try:
                spec = range_header[6:].strip()
                start_s, _, end_s = spec.partition("-")
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
                end = min(end, size - 1)
                if start < 0 or start > end:
                    raise ValueError("bad range")
            except ValueError:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with path.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with path.open("rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "missing")
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj: dict) -> None:
        data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: HTTPStatus, msg: str) -> None:
        data = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @classmethod
    def load_catalog(cls, *, rebuild: bool = False) -> None:
        from build_phone_index import build_index, DEFAULT_NAME  # local import

        with cls.lock:
            idx = cls.index_path
            if rebuild or not idx.is_file():
                print(f"{'Rebuilding' if rebuild else 'Building'} index → {idx} …", flush=True)
                catalog = build_index(cls.library_root)
                text = json.dumps(catalog, separators=(",", ":"), ensure_ascii=False)
                tmp = idx.with_suffix(idx.suffix + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(idx)
            else:
                print(f"Loading index {idx} …", flush=True)
                catalog = json.loads(idx.read_text(encoding="utf-8"))

            cls.catalog = catalog
            cls.items_by_id = {row["id"]: row for row in catalog.get("items") or []}
            cls.recent_imports = []
            cls.updates_ts = time.time()
            print(
                f"Catalog ready: {catalog.get('item_count')} items "
                f"(built {catalog.get('built_at')})",
                flush=True,
            )

    @classmethod
    def ingest_item_ids(cls, item_ids: list[str]) -> int:
        """Add newly imported items to the in-memory catalog. Returns count added."""
        from build_phone_index import item_to_row

        added = 0
        now = time.time()
        for iid in item_ids:
            if not iid or iid in cls.items_by_id:
                continue
            item_dir = cls.library_root / "images" / f"{iid}.info"
            item = _item_from_dir(item_dir)
            if item is None or item.is_deleted:
                continue
            row = item_to_row(item)
            with cls.lock:
                if item.id in cls.items_by_id:
                    continue
                cls.items_by_id[item.id] = row
                if cls.catalog is not None:
                    items = cls.catalog.setdefault("items", [])
                    items.insert(0, row)
                    cls.catalog["item_count"] = len(items)
                cls.recent_imports.append((now, row))
                if len(cls.recent_imports) > 200:
                    cls.recent_imports = cls.recent_imports[-200:]
                cls.updates_ts = now
            added += 1
        return added


def _watch_inbox_signal(handler: type[PhoneBrowseHandler]) -> None:
    """Poll the inbox signal file and ingest new item ids into the catalog."""
    path = handler.library_root / INBOX_SIGNAL_FILENAME
    last_ts = 0.0
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            last_ts = float(raw.get("ts") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            last_ts = 0.0
    while True:
        time.sleep(1.0)
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        try:
            ts = float(raw.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= last_ts:
            continue
        last_ts = ts
        ids = [str(i) for i in (raw.get("ids") or []) if i]
        if not ids:
            continue
        n = handler.ingest_item_ids(ids)
        if n:
            print(f"Ingested {n} new item(s) into phone catalog", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Eagle phone browse — local LAN server")
    parser.add_argument(
        "library",
        nargs="?",
        default=os.environ.get("EAGLE_LIBRARY", str(DEFAULT_LIBRARY)),
        help="Path to *.library",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0 for LAN)")
    parser.add_argument(
        "--index",
        default=None,
        help=f"Index JSON path (default: <library>/{INDEX_NAME})",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force rebuild phone-index.json on start",
    )
    parser.add_argument(
        "--mdns-name",
        default=os.environ.get("EAGLE_PHONE_MDNS", DEFAULT_MDNS_NAME),
        help=f"mDNS hostname without/with .local (default: {DEFAULT_MDNS_NAME} → eagle.local)",
    )
    parser.add_argument(
        "--no-mdns",
        action="store_true",
        help="Do not publish a *.local name via Avahi",
    )
    args = parser.parse_args()

    library_root = Path(args.library).expanduser().resolve()
    if not library_root.is_dir():
        print(f"Library not found: {library_root}", file=sys.stderr)
        return 1
    if not PHONE_WEB.is_dir():
        print(f"UI folder missing: {PHONE_WEB}", file=sys.stderr)
        return 1

    PhoneBrowseHandler.library_root = library_root
    PhoneBrowseHandler.index_path = (
        Path(args.index).expanduser().resolve()
        if args.index
        else library_root / INDEX_NAME
    )
    PhoneBrowseHandler.load_catalog(rebuild=args.rebuild)
    threading.Thread(
        target=_watch_inbox_signal,
        args=(PhoneBrowseHandler,),
        name="phone-inbox-watch",
        daemon=True,
    ).start()

    server = ThreadingHTTPServer((args.host, args.port), PhoneBrowseHandler)
    ips = _lan_ips()
    mdns_host = _normalize_mdns_host(args.mdns_name)
    publisher: MdnsPublisher | None = None

    print()
    print("Eagle phone browse (local)")
    print(f"  library: {library_root}")
    print(f"  UI:      {PHONE_WEB}")
    print(f"  bind:    http://{args.host}:{args.port}/")
    print("  open on phone (same Wi‑Fi):")
    if not args.no_mdns and ips:
        publisher = MdnsPublisher(mdns_host, ips[0], args.port)
        if publisher.start():
            print(f"    http://{mdns_host}:{args.port}/")
        else:
            publisher = None
    # Always print IP fallback(s)
    for ip in ips:
        print(f"    http://{ip}:{args.port}/  (IP fallback)")
    if not ips and args.no_mdns:
        print("    (no LAN IP detected)")
    print("  Ctrl+C to stop")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if publisher:
            publisher.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
