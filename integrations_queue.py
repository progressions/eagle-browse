"""Queue PromptForge integrations from Eagle Browse (upscale / bust / wardrobe / edit)."""

from __future__ import annotations

import json
import os
import re
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
EDIT_PATH = "/api/v1/edit"
OFFLINE_TOAST = "PromptForge not answering"
NO_PROMPT_LINKED_TOAST = "No PromptForge prompt linked"

BUST_ENGINES = ("klein", "qwen", "krea2")
WARDROBE_ENGINES = ("qwen", "krea2", "klein")
# Edit API expects UI labels (PF may map flux→klein server-side).
EDIT_ENGINES = ("qwen", "flux", "krea")
DEFAULT_BUST_ENGINE = "klein"
DEFAULT_WARDROBE_ENGINE = "qwen"
DEFAULT_EDIT_ENGINE = "qwen"
# Flat-lay (#510): wardrobe-flatlay skill recipe — QIE-2511 @ 1.0, 9:16 wood pad.
FLAT_LAY_W = 864
FLAT_LAY_H = 1536
DEFAULT_FLAT_LAY_PROMPT = """Extract the clothing and create a flat mockup.
Create a flat lay wardrobe sheet from the outfit shown in the reference image.
Remove any human figure completely. Extract each individual clothing item and
accessory and arrange them separately on a clean light tan wood surface.
Do not add any printed labels, handwritten cards, tags, captions, or text of any kind.

Include a large physical photograph (Polaroid or print) of the original
reference so the viewer can see the outfit worn.

Style: clean editorial flat lay, light tan wood background, soft shadows,
ultra-realistic product photography, 9:16 vertical, photorealistic. No text."""

EUNBI_FLAT_LAY_EXTRAS = (
    "Always include on the display: a black velvet choker collar with a small "
    "silver heart pendant, and a cherry blossom hair pin."
)

NON_EUNBI_FLAT_LAY_EXTRAS = (
    "Do not add a black velvet choker, a cherry blossom hair pin, a pink phone, "
    "or any item that is not in the reference."
)

# Continuity stamps (#483): pf:<id> / pf-<id> tags, annotation, image-<id>- filename.
_PF_TAG_RE = re.compile(r"^(?:pf:|pf-)(\d+)$", re.IGNORECASE)
_PF_ANNOTATION_RE = re.compile(r"promptforge:(\d+)", re.IGNORECASE)
# Same spirit as PromptForge QueueLive.parse_image_history_id.
_IMAGE_HISTORY_RE = re.compile(r"(?:^|[/\\_])image-(\d+)(?:[-_.]|$)", re.IGNORECASE)


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


def resolve_promptforge_history_id(item: Any) -> int | None:
    """Resolve a PromptForge prompt_histories id from an Eagle library Item.

    Order: pf:/pf- tag → annotation ``promptforge:<id>`` → ``image-<id>-`` in
    name/path (QueueLive.parse_image_history_id spirit).
    """
    tags = list(getattr(item, "tags", None) or [])
    tag_set = getattr(item, "tag_set", None)
    if tag_set:
        for t in tag_set:
            if t not in tags:
                tags.append(t)
    for raw in tags:
        m = _PF_TAG_RE.match(str(raw).strip())
        if m:
            return int(m.group(1))

    annotation = getattr(item, "annotation", None) or ""
    m = _PF_ANNOTATION_RE.search(str(annotation))
    if m:
        return int(m.group(1))

    texts: list[str] = []
    name = getattr(item, "name", None)
    if name:
        texts.append(str(name))
    display = getattr(item, "display_name", None)
    if display:
        texts.append(str(display))
    path = getattr(item, "path", None)
    if path is not None:
        texts.append(str(path))
        try:
            texts.append(path.name)
        except Exception:  # noqa: BLE001
            pass

    seen: set[str] = set()
    for text in texts:
        if not text or text in seen:
            continue
        seen.add(text)
        m = _IMAGE_HISTORY_RE.search(text)
        if m:
            return int(m.group(1))
    return None


def promptforge_base_url() -> str:
    bases = candidate_bases()
    if bases:
        return bases[0]
    return "http://promptforge.local:4000"


def promptforge_build_url(history_id: int) -> str:
    return f"{promptforge_base_url()}/build?id={int(history_id)}"


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


def normalize_edit_engine(raw: str | None) -> str | None:
    """Map edit engine to API labels: qwen | flux | krea."""
    key = (raw or "").strip().lower()
    aliases = {
        "qwen": "qwen",
        "flux": "flux",
        "klein": "flux",
        "flux-klein": "flux",
        "krea": "krea",
        "krea2": "krea",
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


def _http_reason(status: int, body: Any, *, path: str = "") -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if err:
            text = str(err).strip()
            if text:
                return text[:160]
    if status == 404 and path in (WARDROBE_PATH, BUST_PATH, EDIT_PATH):
        return (
            f"PromptForge has no {path} yet — merge/redeploy "
            "(or set PROMPTFORGE_URL to a preview that has it)"
        )
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
    last_http: IntegrationResult | None = None
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
        # 404 on a host that is up but missing the route — try the next base
        # (e.g. production :4000 vs preview with wardrobe/bust).
        last_http = IntegrationResult(
            STATUS_HTTP_ERROR, _http_reason(status, body, path=path)
        )
        if status == 404:
            continue
        return last_http
    return last_http or IntegrationResult(STATUS_OFFLINE, OFFLINE_TOAST)


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


def post_edit(
    item: Any,
    *,
    prompt: str,
    engine: str = DEFAULT_EDIT_ENGINE,
    width: int | None = None,
    height: int | None = None,
    toast_kind: str = "edit",
) -> IntegrationResult:
    """POST /api/v1/edit for the focused still (PromptForge edit queue, #503/#510)."""
    if not getattr(item, "is_image", False):
        return IntegrationResult(STATUS_UNSUPPORTED, f"{toast_kind.capitalize()} is for stills")
    if _file_missing(item):
        return IntegrationResult(STATUS_FILE_MISSING, "File missing")

    text = (prompt or "").strip()
    if not text:
        return IntegrationResult(STATUS_UNSUPPORTED, "Edit prompt required")

    eng = normalize_edit_engine(engine)
    if eng is None:
        return IntegrationResult(STATUS_UNSUPPORTED, "Unknown edit engine")

    payload: dict[str, Any] = {
        "eagle_id": str(item.id),
        "prompt": text,
        "engine": eng,
    }
    if width is not None and height is not None:
        payload["width"] = max(1, int(width))
        payload["height"] = max(1, int(height))
    result = _post_json(EDIT_PATH, payload)
    if result.status == STATUS_OK:
        label = {"qwen": "Qwen", "flux": "Flux", "krea": "Krea"}.get(eng, eng)
        kind = (toast_kind or "edit").strip() or "edit"
        return IntegrationResult(STATUS_OK, f"Queued {kind} ({label}) on Eric")
    return result


def flat_lay_prompt_for(item: Any, prompt: str | None = None) -> str:
    """Default QIE extract prompt; append character extras when using the stock text."""
    custom = (prompt or "").strip()
    if custom:
        return custom
    extras = (
        EUNBI_FLAT_LAY_EXTRAS
        if character_for(item) == "Eunbi"
        else NON_EUNBI_FLAT_LAY_EXTRAS
    )
    return f"{DEFAULT_FLAT_LAY_PROMPT.strip()}\n\n{extras}"


def post_flat_lay(
    item: Any,
    *,
    prompt: str | None = None,
    engine: str = DEFAULT_EDIT_ENGINE,
) -> IntegrationResult:
    """POST /api/v1/edit as wardrobe flat-lay (#510). Qwen + QIE-2511, 9:16 wood pad."""
    text = flat_lay_prompt_for(item, prompt)
    # Flat-lay is the QIE-2511 recipe; always queue as qwen + job=flat-lay so
    # PromptForge attaches QIE-2511-Extract-Outfit and pads 864×1536.
    _ = engine  # UI may still show engines; QIE path is Qwen-only.
    eng = "qwen"
    if _file_missing(item):
        return IntegrationResult(STATUS_FILE_MISSING, "File missing")
    if not getattr(item, "is_image", False):
        return IntegrationResult(STATUS_UNSUPPORTED, "Flat-lay is for stills")

    payload: dict[str, Any] = {
        "eagle_id": str(item.id),
        "prompt": text,
        "engine": eng,
        "job": "flat-lay",
        "character": character_for(item),
        "width": FLAT_LAY_W,
        "height": FLAT_LAY_H,
    }
    result = _post_json(EDIT_PATH, payload)
    if result.status == STATUS_OK:
        return IntegrationResult(STATUS_OK, "Queued flat-lay (Qwen + QIE-2511) on Eric")
    return result
