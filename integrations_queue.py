"""Queue PromptForge integrations from Eagle Browse (upscale / bust / wardrobe)."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from upscale_queue import (  # noqa: F401 — re-export for callers
    STATUS_ALREADY,
    STATUS_FILE_MISSING,
    STATUS_HTTP_ERROR,
    STATUS_OFFLINE,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    TAG_SOFIE,
    UpscaleResult,
    already_reason,
    candidate_bases,
    post_upscale,
)

TIMEOUT_S = 5.0
WARDROBE_PATH = "/api/v1/wardrobe"
BUST_PATH = "/api/v1/bust"
OFFLINE_TOAST = "PromptForge not answering"

BUST_ENGINES = ("klein", "qwen", "krea2")
WARDROBE_ENGINES = ("qwen", "krea2", "klein")
DEFAULT_BUST_ENGINE = "klein"
DEFAULT_WARDROBE_ENGINE = "qwen"


@dataclass(frozen=True)
class IntegrationResult:
    status: str
    toast: str


def _tag_set(item: Any) -> set[str]:
    raw = getattr(item, "tag_set", None)
    if raw:
        return {str(t).strip().lower() for t in raw}
    tags = getattr(item, "tags", None) or []
    return {str(t).strip().lower() for t in tags}


def character_for(item: Any) -> str:
    return "Sofie" if TAG_SOFIE in _tag_set(item) else "Eunbi"


def normalize_bust_engine(raw: str | None) -> str | None:
    key = (raw or "").strip().lower()
    aliases = {
        "klein": "klein",
        "flux": "klein",
        "flux-klein": "klein",
        "qwen": "qwen",
        "krea": "krea2",
        "krea2": "krea2",
    }
    return aliases.get(key)


def normalize_wardrobe_engine(raw: str | None) -> str | None:
    key = (raw or "").strip().lower()
    aliases = {
        "klein": "klein",
        "flux": "klein",
        "flux-klein": "klein",
        "qwen": "qwen",
        "krea": "krea2",
        "krea2": "krea2",
    }
    return aliases.get(key)


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


def _post_json(path: str, payload: dict[str, Any]) -> IntegrationResult:
    token = (os.environ.get("PROMPTFORGE_API_TOKEN") or "").strip() or None
    for base in candidate_bases():
        url = f"{base}{path}"
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
            return IntegrationResult(STATUS_OK, "Queued on Eric")
        return IntegrationResult(STATUS_HTTP_ERROR, _http_reason(status, body))
    return IntegrationResult(STATUS_OFFLINE, OFFLINE_TOAST)


def _file_missing(item: Any) -> bool:
    path = getattr(item, "path", None)
    try:
        return path is None or not path.is_file()
    except Exception:  # noqa: BLE001
        return True


def post_bust_enhance(
    item: Any,
    *,
    engine: str = DEFAULT_BUST_ENGINE,
    count: int = 2,
) -> IntegrationResult:
    """POST /api/v1/bust for the focused still."""
    if not getattr(item, "is_image", False):
        return IntegrationResult(STATUS_UNSUPPORTED, "Enhance bust is for stills")
    if _file_missing(item):
        return IntegrationResult(STATUS_FILE_MISSING, "File missing")

    eng = normalize_bust_engine(engine)
    if eng is None:
        return IntegrationResult(STATUS_UNSUPPORTED, "Unknown bust engine")

    payload = {
        "eagle_id": str(item.id),
        "engine": eng,
        "character": character_for(item),
        "count": max(1, min(int(count), 8)),
    }
    result = _post_json(BUST_PATH, payload)
    if result.status == STATUS_OK:
        label = {"klein": "Flux Klein", "qwen": "Qwen", "krea2": "Krea 2"}.get(eng, eng)
        return IntegrationResult(STATUS_OK, f"Queued bust ({label}) on Eric")
    return result


def post_wardrobe_apply(
    item: Any,
    *,
    wardrobe_eagle_id: str,
    engine: str = DEFAULT_WARDROBE_ENGINE,
    count: int = 1,
) -> IntegrationResult:
    """POST /api/v1/wardrobe mode=apply for the focused still + wardrobe Eagle id."""
    if not getattr(item, "is_image", False):
        return IntegrationResult(STATUS_UNSUPPORTED, "Add wardrobe is for stills")
    if _file_missing(item):
        return IntegrationResult(STATUS_FILE_MISSING, "File missing")

    ward = (wardrobe_eagle_id or "").strip()
    if not ward:
        return IntegrationResult(STATUS_UNSUPPORTED, "Wardrobe Eagle id required")

    eng = normalize_wardrobe_engine(engine)
    if eng is None:
        return IntegrationResult(STATUS_UNSUPPORTED, "Unknown wardrobe engine")

    payload = {
        "mode": "apply",
        "person_eagle_id": str(item.id),
        "wardrobe_eagle_id": ward,
        "engine": eng,
        "character": character_for(item),
        "count": max(1, min(int(count), 8)),
    }
    result = _post_json(WARDROBE_PATH, payload)
    if result.status == STATUS_OK:
        label = {"klein": "Flux Klein", "qwen": "Qwen", "krea2": "Krea 2"}.get(eng, eng)
        return IntegrationResult(STATUS_OK, f"Queued wardrobe ({label}) on Eric")
    return result
