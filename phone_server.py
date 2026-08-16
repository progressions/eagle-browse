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
from datetime import datetime, timezone
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
    """IPv4 addresses on this machine that phones on the LAN can reach.

    Boot order matters: systemd can start us before Wi‑Fi has an address.
    Try several methods so a later retry (see main) can pick the IP up.
    """
    ips: list[str] = []

    def _add(ip: str, *, front: bool = False) -> None:
        if not ip or ip.startswith("127.") or ip.startswith("169.254."):
            return
        if ip in ips:
            return
        if front:
            ips.insert(0, ip)
        else:
            ips.append(ip)

    # 1) Hostname A records (often empty until /etc/hosts or DNS knows us)
    try:
        out = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in out:
            _add(info[4][0])
    except OSError:
        pass

    # 2) UDP connect trick — works once a default route exists
    for dest in (("8.8.8.8", 80), ("1.1.1.1", 80), ("192.168.40.1", 80)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect(dest)
            _add(s.getsockname()[0], front=True)
            s.close()
            break
        except OSError:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass

    # 3) Enumerate interfaces via `ip` (reliable on Omarchy once link is up)
    if not ips and shutil.which("ip"):
        try:
            out = subprocess.check_output(
                ["ip", "-4", "-o", "addr", "show", "scope", "global"],
                text=True,
                timeout=2,
            )
            for line in out.splitlines():
                # "2: wlp3s0    inet 192.168.40.126/24 brd ..."
                parts = line.split()
                if "inet" in parts:
                    i = parts.index("inet")
                    if i + 1 < len(parts):
                        _add(parts[i + 1].split("/")[0])
        except (OSError, subprocess.SubprocessError):
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
    # id → (tries, next_retry_unix) for incomplete .info folders
    pending: dict = {}
    _persist_timer: threading.Timer | None = None
    _persist_dirty: bool = False
    _PENDING_MAX = 200
    _PENDING_MAX_TRIES = 8
    _PENDING_BACKOFF = (2.0, 5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0)

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
            cls.pending = {}
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
            if not iid:
                continue
            with cls.lock:
                already = iid in cls.items_by_id
            if already:
                cls.pending.pop(iid, None)
                continue
            item_dir = cls.library_root / "images" / f"{iid}.info"
            item = _item_from_dir(item_dir)
            if item is None or item.is_deleted:
                cls._note_pending(iid)
                continue
            row = item_to_row(item)
            with cls.lock:
                if item.id in cls.items_by_id:
                    cls.pending.pop(item.id, None)
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
                cls.pending.pop(item.id, None)
            added += 1
        if added:
            cls.schedule_persist()
        return added

    @classmethod
    def _note_pending(cls, iid: str) -> None:
        tries, _nxt = cls.pending.get(iid, (0, 0.0))
        tries += 1
        if tries > cls._PENDING_MAX_TRIES:
            cls.pending.pop(iid, None)
            return
        if iid not in cls.pending and len(cls.pending) >= cls._PENDING_MAX:
            return
        delay = cls._PENDING_BACKOFF[min(tries, len(cls._PENDING_BACKOFF)) - 1]
        cls.pending[iid] = (tries, time.time() + delay)

    @classmethod
    def retry_pending(cls) -> int:
        if not cls.pending:
            return 0
        now = time.time()
        due = [iid for iid, (_tries, nxt) in list(cls.pending.items()) if nxt <= now]
        if not due:
            return 0
        return cls.ingest_item_ids(due)

    @classmethod
    def scan_unknown_ids(cls) -> list[str]:
        """Ids on disk that are not in the in-memory catalog."""
        images = cls.library_root / "images"
        with cls.lock:
            known = set(cls.items_by_id)
        unknown: list[str] = []
        try:
            for p in images.iterdir():
                if not p.is_dir() or not p.name.endswith(".info"):
                    continue
                iid = p.name.removesuffix(".info")
                if iid and iid not in known:
                    unknown.append(iid)
        except OSError:
            return []
        return unknown

    @classmethod
    def schedule_persist(cls) -> None:
        """Debounce writes of phone-index.json (full catalog is a few MB)."""
        def fire() -> None:
            with cls.lock:
                cls._persist_timer = None
                dirty = cls._persist_dirty
                cls._persist_dirty = False
            if dirty:
                cls.persist_catalog()
            with cls.lock:
                again = cls._persist_dirty
            if again:
                cls.schedule_persist()

        with cls.lock:
            cls._persist_dirty = True
            if cls._persist_timer is not None:
                return
            timer = threading.Timer(2.0, fire)
            timer.daemon = True
            cls._persist_timer = timer
        timer.start()

    @classmethod
    def persist_catalog(cls) -> None:
        """Write the live catalog back to phone-index.json."""
        with cls.lock:
            catalog = cls.catalog
            if not catalog:
                return
            snap = dict(catalog)
            snap["items"] = list(catalog.get("items") or [])
            snap["item_count"] = len(snap["items"])
            snap["updated_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            path = cls.index_path
        try:
            text = json.dumps(snap, separators=(",", ":"), ensure_ascii=False)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            print(f"  persist phone-index.json failed: {exc}", flush=True)


def _watch_inbox_signal(handler: type[PhoneBrowseHandler]) -> None:
    """Poll the inbox signal file and ingest new item ids into the catalog.

    last_ts starts at 0 so a signal written while we were down is still
    ingested. Missing disk ids are scanned on a slow timer (one walker).
    """
    path = handler.library_root / INBOX_SIGNAL_FILENAME
    last_ts = 0.0
    last_disk_scan = 0.0
    images_mtime = 0.0
    scan_lock = threading.Lock()
    scanning = False

    def kick_disk_scan() -> None:
        nonlocal scanning
        if not scan_lock.acquire(blocking=False):
            return
        if scanning:
            scan_lock.release()
            return
        scanning = True
        scan_lock.release()

        def work() -> None:
            nonlocal scanning
            try:
                unknown = handler.scan_unknown_ids()
                if unknown:
                    n = handler.ingest_item_ids(unknown)
                    if n:
                        print(
                            f"Ingested {n} item(s) from images/ into phone catalog",
                            flush=True,
                        )
            finally:
                with scan_lock:
                    scanning = False

        threading.Thread(target=work, name="phone-disk-scan", daemon=True).start()

    # First pass: current signal (if any) + disk gap-fill, then loop.
    kick_disk_scan()

    while True:
        time.sleep(1.0)
        now = time.time()
        n_pending = handler.retry_pending()
        if n_pending:
            print(f"Ingested {n_pending} pending item(s) into phone catalog", flush=True)

        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict):
                try:
                    ts = float(raw.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                ids = [str(i) for i in (raw.get("ids") or []) if i]
                if ts > last_ts and ids:
                    last_ts = ts
                    n = handler.ingest_item_ids(ids)
                    if n:
                        print(
                            f"Ingested {n} new item(s) into phone catalog",
                            flush=True,
                        )

        images = handler.library_root / "images"
        try:
            mt = images.stat().st_mtime
        except OSError:
            mt = images_mtime
        if mt != images_mtime or (now - last_disk_scan) >= 30.0:
            images_mtime = mt
            last_disk_scan = now
            kick_disk_scan()


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
    # Mutable so the deferred-mDNS thread can update it after boot networking.
    pub_state: dict = {"publisher": None}

    def _try_publish_mdns(ip_list: list[str]) -> bool:
        if args.no_mdns or not ip_list:
            return False
        if pub_state["publisher"] is not None:
            return True
        publisher = MdnsPublisher(mdns_host, ip_list[0], args.port)
        if publisher.start():
            pub_state["publisher"] = publisher
            print(
                f"  mDNS ready: http://{mdns_host}:{args.port}/  ({ip_list[0]})",
                flush=True,
            )
            return True
        return False

    def _log(*parts: object) -> None:
        print(*parts, flush=True)

    _log()
    _log("Eagle phone browse (local)")
    _log(f"  library: {library_root}")
    _log(f"  UI:      {PHONE_WEB}")
    _log(f"  bind:    http://{args.host}:{args.port}/")
    _log("  open on phone (same Wi‑Fi):")
    if ips:
        _try_publish_mdns(ips)
        if pub_state["publisher"] is not None:
            _log(f"    http://{mdns_host}:{args.port}/")
        for ip in ips:
            _log(f"    http://{ip}:{args.port}/  (IP fallback)")
    else:
        _log("    (LAN IP not up yet — will retry mDNS in background)")
        _log(f"    http://{mdns_host}:{args.port}/  (once Wi‑Fi is up)")
    _log("  Ctrl+C to stop")
    _log()

    # Boot race: Wi‑Fi often comes up after this service. Retry IP + mDNS.
    def _deferred_mdns() -> None:
        if args.no_mdns:
            return
        for delay in (2, 5, 10, 20, 30, 60, 120):
            if pub_state["publisher"] is not None:
                return
            time.sleep(delay)
            found = _lan_ips()
            if not found:
                print(f"  mDNS retry: still no LAN IP (waited +{delay}s)", flush=True)
                continue
            print(f"  mDNS retry: found {found[0]} — publishing…", flush=True)
            if _try_publish_mdns(found):
                return
        if pub_state["publisher"] is None:
            print(
                "  mDNS: gave up — use the IP URL from `ip -4 addr` on this machine",
                flush=True,
            )

    if not args.no_mdns and pub_state["publisher"] is None:
        threading.Thread(
            target=_deferred_mdns, name="phone-mdns-retry", daemon=True
        ).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        publisher = pub_state.get("publisher")
        if publisher is not None:
            publisher.stop()
        PhoneBrowseHandler.persist_catalog()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
