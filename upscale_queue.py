"""Queue a still or video upscale on PromptForge from Eagle Browse."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

TIMEOUT_S = 2.0
QUEUE_PATH = "/api/v1/video_queue"
DEFAULT_BASES = (
    "http://promptforge.local:4000",
    "http://ginger.local:4000",
    "http://192.168.40.126:4000",
)

TAG_QUEUED = "upscaling"
TAG_DONE = "upscaled"
TAG_NEED = "needs-upscale"
TAG_SOFIE = "sofie"

STATUS_OK = "ok"
STATUS_OFFLINE = "offline"
STATUS_HTTP_ERROR = "http_error"
STATUS_ALREADY = "already"
STATUS_FILE_MISSING = "file_missing"
STATUS_UNSUPPORTED = "unsupported"

OFFLINE_TOAST = "PromptForge not answering"


@dataclass(frozen=True)
class UpscaleJob:
    kind: str  # still | video
    type: str
    host: str
    prefix: str
    prompt_text: str
    character: str
    success_toast: str


@dataclass(frozen=True)
class UpscaleResult:
    status: str
    toast: str
    job: UpscaleJob | None = None


def _tag_set(item: Any) -> set[str]:
    raw = getattr(item, "tag_set", None)
    if raw:
        return {str(t).strip().lower() for t in raw}
    tags = getattr(item, "tags", None) or []
    return {str(t).strip().lower() for t in tags}


def classify(item: Any) -> UpscaleJob | None:
    tags = _tag_set(item)
    character = "Sofie" if TAG_SOFIE in tags else "Eunbi"
    item_id = str(getattr(item, "id", "") or "")
    if getattr(item, "is_image", False):
        return UpscaleJob(
            kind="still",
            type="rtp-2x",
            host="jack",
            prefix=f"rtp-2x-{item_id}",
            prompt_text="2x upscale",
            character=character,
            success_toast="Queued 2× on Jack",
        )
    if getattr(item, "is_video", False):
        return UpscaleJob(
            kind="video",
            type="seedvr2",
            host="eric",
            prefix=f"rtp-sv2-{item_id}",
            prompt_text="seedvr2 upscale",
            character=character,
            success_toast="Queued SeedVR2 on Eric",
        )
    return None


def already_reason(item: Any) -> str | None:
    tags = _tag_set(item)
    if TAG_QUEUED in tags:
        return "Already queued"
    if TAG_DONE in tags:
        return "Already upscaled"
    return None


def candidate_bases() -> list[str]:
    env = (os.environ.get("PROMPTFORGE_URL") or "").strip().rstrip("/")
    if env:
        return [env]
    return [b.rstrip("/") for b in DEFAULT_BASES]


def _parse_body(raw: str) -> Any:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _http_reason(status: int, body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if err:
            text = str(err).strip()
            if text:
                return text[:160]
    return f"PromptForge HTTP {status}"


def _post(url: str, payload: dict[str, Any], *, token: str | None) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return int(getattr(resp, "status", 200) or 200), _parse_body(raw)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        return int(exc.code), _parse_body(raw)


def post_upscale(item: Any) -> UpscaleResult:
    """POST one upscale job. Safe to call from a worker thread."""
    path = getattr(item, "path", None)
    try:
        missing = path is None or not path.is_file()
    except Exception:  # noqa: BLE001
        missing = True
    if missing:
        return UpscaleResult(STATUS_FILE_MISSING, "File missing")

    prior = already_reason(item)
    if prior:
        return UpscaleResult(STATUS_ALREADY, prior)

    job = classify(item)
    if job is None:
        return UpscaleResult(STATUS_UNSUPPORTED, "Upscale is for stills and videos")

    payload = {
        "type": job.type,
        "host": job.host,
        "character": job.character,
        "slug": job.prefix,
        "status": "queued",
        "eagle_id": str(item.id),
        "image_path": str(item.path),
        "prefix": job.prefix,
        "prompt_text": job.prompt_text,
        "seconds": 5,
        "size": 864,
        "count": 1,
    }
    token = (os.environ.get("PROMPTFORGE_API_TOKEN") or "").strip() or None
    for base in candidate_bases():
        url = f"{base}{QUEUE_PATH}"
        try:
            status, body = _post(url, payload, token=None)
            if status == 401 and token:
                status, body = _post(url, payload, token=token)
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionRefusedError,
            socket.gaierror,
            socket.timeout,
            OSError,
        ):
            continue
        if 200 <= status < 300:
            return UpscaleResult(STATUS_OK, job.success_toast, job)
        return UpscaleResult(STATUS_HTTP_ERROR, _http_reason(status, body), job)
    return UpscaleResult(STATUS_OFFLINE, OFFLINE_TOAST, job)
