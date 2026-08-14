"""Interactive image crop for Eagle Browse.

GTK dialog with moveable/resizable crop overlay, width×height fields, and
common aspect-ratio presets (3:4, 9:16, etc.). Apply overwrites the media
file in place (with backup), updates item metadata, and regenerates the
thumbnail.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from pixbuf_io import pixbuf_from_path
from write import WriteError, backup_file, load_item_metadata, save_item_metadata, write_session


def _load_pixbuf(path: Path | str) -> GdkPixbuf.Pixbuf:
    """Load *path* from bytes. GdkPixbuf's filename cache is stale after overwrite."""
    pb = pixbuf_from_path(path)
    if pb is None:
        raise WriteError(f"Cannot load image: {path}")
    return pb

# (id, label, aspect_w, aspect_h) — aspect None means free
ASPECT_PRESETS: list[tuple[str, str, int | None, int | None]] = [
    ("free", "Free", None, None),
    ("orig", "Orig", None, None),  # filled from image at open
    ("1:1", "1:1", 1, 1),
    ("3:4", "3:4", 3, 4),
    ("4:3", "4:3", 4, 3),
    ("9:16", "9:16", 9, 16),
    ("16:9", "16:9", 16, 9),
    ("2:3", "2:3", 2, 3),
    ("3:2", "3:2", 3, 2),
]

HANDLE_HIT_PX = 12  # display-space hit radius for corners/edges
MIN_CROP = 8  # minimum crop edge in source pixels


@dataclass(slots=True)
class CropRect:
    x: int
    y: int
    w: int
    h: int

    def clamp_to(self, img_w: int, img_h: int) -> CropRect:
        w = max(MIN_CROP, min(self.w, img_w))
        h = max(MIN_CROP, min(self.h, img_h))
        x = max(0, min(self.x, img_w - w))
        y = max(0, min(self.y, img_h - h))
        return CropRect(x, y, w, h)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h


def aspect_ratio(aw: int, ah: int) -> float:
    return aw / ah if ah else 1.0


def max_rect_for_aspect(img_w: int, img_h: int, aw: int, ah: int) -> CropRect:
    """Largest centered crop of aspect aw:ah that fits in the image."""
    r = aspect_ratio(aw, ah)
    # Fit width first
    w = img_w
    h = int(round(w / r))
    if h > img_h:
        h = img_h
        w = int(round(h * r))
    w = max(MIN_CROP, min(w, img_w))
    h = max(MIN_CROP, min(h, img_h))
    x = (img_w - w) // 2
    y = (img_h - h) // 2
    return CropRect(x, y, w, h).clamp_to(img_w, img_h)


def sized_rect(
    img_w: int,
    img_h: int,
    cw: int,
    ch: int,
    *,
    center_on: CropRect | None = None,
) -> CropRect:
    """Build a crop rect of size cw×ch, centered on existing rect (or image)."""
    cw = max(MIN_CROP, min(int(cw), img_w))
    ch = max(MIN_CROP, min(int(ch), img_h))
    if center_on is not None:
        cx = center_on.x + center_on.w // 2
        cy = center_on.y + center_on.h // 2
    else:
        cx, cy = img_w // 2, img_h // 2
    x = cx - cw // 2
    y = cy - ch // 2
    return CropRect(x, y, cw, ch).clamp_to(img_w, img_h)


def parse_aspect(
    aspect: str | None,
    img_w: int,
    img_h: int,
) -> tuple[int, int] | None:
    """
    Parse an aspect string into (aw, ah).

    Accepts ``"9:16"``, ``"3:4"``, ``"1:1"``, ``"orig"`` / ``"original"``,
    or ``"free"`` / empty / None (returns None = free).
    """
    if aspect is None:
        return None
    a = str(aspect).strip().lower()
    if not a or a in ("free", "none", "any"):
        return None
    if a in ("orig", "original"):
        if img_w <= 0 or img_h <= 0:
            raise WriteError("Cannot use aspect=orig without known image size")
        return int(img_w), int(img_h)
    if ":" in a:
        left, right = a.split(":", 1)
        try:
            aw, ah = int(left.strip()), int(right.strip())
        except ValueError as exc:
            raise WriteError(f"Invalid aspect ratio: {aspect!r}") from exc
        if aw <= 0 or ah <= 0:
            raise WriteError(f"Aspect parts must be positive: {aspect!r}")
        return aw, ah
    raise WriteError(
        f"Invalid aspect {aspect!r}; use e.g. 9:16, 3:4, 1:1, orig, or free"
    )


def place_rect(
    img_w: int,
    img_h: int,
    cw: int,
    ch: int,
    *,
    x: int | None = None,
    y: int | None = None,
    anchor: str = "center",
) -> CropRect:
    """Place a cw×ch crop inside the image using explicit x/y or an anchor."""
    cw = max(MIN_CROP, min(int(cw), img_w))
    ch = max(MIN_CROP, min(int(ch), img_h))
    if x is not None and y is not None:
        return CropRect(int(x), int(y), cw, ch).clamp_to(img_w, img_h)

    a = (anchor or "center").strip().lower().replace("_", "-")
    if a in ("center", "c", "middle"):
        px, py = (img_w - cw) // 2, (img_h - ch) // 2
    elif a in ("top", "north", "n"):
        px, py = (img_w - cw) // 2, 0
    elif a in ("bottom", "south", "s"):
        px, py = (img_w - cw) // 2, img_h - ch
    elif a in ("left", "west", "w"):
        px, py = 0, (img_h - ch) // 2
    elif a in ("right", "east", "e"):
        px, py = img_w - cw, (img_h - ch) // 2
    elif a in ("top-left", "nw", "north-west", "northwest"):
        px, py = 0, 0
    elif a in ("top-right", "ne", "north-east", "northeast"):
        px, py = img_w - cw, 0
    elif a in ("bottom-left", "sw", "south-west", "southwest"):
        px, py = 0, img_h - ch
    elif a in ("bottom-right", "se", "south-east", "southeast"):
        px, py = img_w - cw, img_h - ch
    else:
        raise WriteError(
            f"Unknown anchor {anchor!r}; use center, top, bottom, left, right, "
            "top-left, top-right, bottom-left, bottom-right"
        )
    return CropRect(px, py, cw, ch).clamp_to(img_w, img_h)


def resolve_crop_rect(
    img_w: int,
    img_h: int,
    *,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    aspect: str | None = None,
    anchor: str = "center",
) -> CropRect:
    """
    Build a crop rect for agent/CLI use.

    Spec sources (combined):
    - ``aspect`` — lock ratio (``9:16``, ``3:4``, ``orig``, …)
    - ``width`` / ``height`` — crop size in source pixels
    - ``x`` / ``y`` — top-left (else placed by ``anchor``, default center)

    If only ``aspect`` is given, uses the largest fit of that ratio.
    If only size is given (no aspect), uses free aspect at that size.
    """
    if img_w <= 0 or img_h <= 0:
        raise WriteError("Image has no usable dimensions")

    pair = parse_aspect(aspect, img_w, img_h)
    has_size = width is not None or height is not None

    if pair is not None:
        aw, ah = pair
        if has_size:
            cw, ch = fit_size_to_aspect(img_w, img_h, width, height, aw, ah)
        else:
            r = max_rect_for_aspect(img_w, img_h, aw, ah)
            cw, ch = r.w, r.h
    elif has_size:
        if width is None or height is None:
            raise WriteError(
                "Provide both --width and --height, or pass --aspect so the "
                "missing side can be derived"
            )
        cw = max(MIN_CROP, min(int(width), img_w))
        ch = max(MIN_CROP, min(int(height), img_h))
    elif x is not None and y is not None:
        # x,y alone with no size → remaining to bottom-right
        cw = max(MIN_CROP, img_w - int(x))
        ch = max(MIN_CROP, img_h - int(y))
    else:
        raise WriteError(
            "Crop needs a region: pass --aspect and/or --width/--height "
            "(and optional --x/--y), e.g. --aspect 9:16 or --width 1080 --height 1920"
        )

    return place_rect(img_w, img_h, cw, ch, x=x, y=y, anchor=anchor)


def fit_size_to_aspect(
    img_w: int,
    img_h: int,
    width: int | None,
    height: int | None,
    aw: int,
    ah: int,
) -> tuple[int, int]:
    """
    Resolve a width/height pair under a locked aspect, clamped to the image.

    Prefer the explicitly provided dimension(s); if both given, prefer width
    and derive height (unless height alone is set).
    """
    r = aspect_ratio(aw, ah)
    if width is not None and height is None:
        w = max(MIN_CROP, min(int(width), img_w))
        h = int(round(w / r))
        if h > img_h:
            h = img_h
            w = int(round(h * r))
        return max(MIN_CROP, w), max(MIN_CROP, h)
    if height is not None and width is None:
        h = max(MIN_CROP, min(int(height), img_h))
        w = int(round(h * r))
        if w > img_w:
            w = img_w
            h = int(round(w / r))
        return max(MIN_CROP, w), max(MIN_CROP, h)
    # both or neither
    if width is not None and height is not None:
        w = max(MIN_CROP, min(int(width), img_w))
        h = int(round(w / r))
        if h > img_h or h != int(height):
            # try height-driven if closer, else clamp
            h2 = max(MIN_CROP, min(int(height), img_h))
            w2 = int(round(h2 * r))
            if w2 <= img_w and abs(w2 - int(width)) + abs(h2 - int(height)) < abs(
                w - int(width)
            ) + abs(h - int(height)):
                return max(MIN_CROP, w2), max(MIN_CROP, h2)
        if h > img_h:
            h = img_h
            w = int(round(h * r))
        return max(MIN_CROP, min(w, img_w)), max(MIN_CROP, min(h, img_h))
    # neither — max fit
    rect = max_rect_for_aspect(img_w, img_h, aw, ah)
    return rect.w, rect.h


Handle = str  # "move" | "nw"|"ne"|"sw"|"se"|"n"|"s"|"e"|"w" | ""


def hit_test(
    img_x: float,
    img_y: float,
    rect: CropRect,
    *,
    scale: float,
) -> Handle:
    """Hit-test in image coordinates; handle size accounts for display scale."""
    if scale <= 0:
        return ""
    pad = HANDLE_HIT_PX / scale
    x0, y0, x1, y1 = rect.x, rect.y, rect.x + rect.w, rect.y + rect.h
    near_l = abs(img_x - x0) <= pad
    near_r = abs(img_x - x1) <= pad
    near_t = abs(img_y - y0) <= pad
    near_b = abs(img_y - y1) <= pad
    inside_x = x0 - pad <= img_x <= x1 + pad
    inside_y = y0 - pad <= img_y <= y1 + pad
    if near_t and near_l:
        return "nw"
    if near_t and near_r:
        return "ne"
    if near_b and near_l:
        return "sw"
    if near_b and near_r:
        return "se"
    if near_t and inside_x:
        return "n"
    if near_b and inside_x:
        return "s"
    if near_l and inside_y:
        return "w"
    if near_r and inside_y:
        return "e"
    if rect.x <= img_x <= rect.x + rect.w and rect.y <= img_y <= rect.y + rect.h:
        return "move"
    return ""


def resize_rect(
    rect: CropRect,
    handle: Handle,
    img_x: float,
    img_y: float,
    img_w: int,
    img_h: int,
    *,
    aspect: float | None,
    anchor: CropRect | None = None,
) -> CropRect:
    """
    Resize from a drag on ``handle``. ``aspect`` is w/h when locked.
    ``anchor`` is the rect at drag start (fixed opposite corner/edge).
    """
    start = anchor or rect
    x0, y0, x1, y1 = start.x, start.y, start.x + start.w, start.y + start.h

    # Free-form first
    if handle == "nw":
        x0, y0 = int(round(img_x)), int(round(img_y))
    elif handle == "ne":
        x1, y0 = int(round(img_x)), int(round(img_y))
    elif handle == "sw":
        x0, y1 = int(round(img_x)), int(round(img_y))
    elif handle == "se":
        x1, y1 = int(round(img_x)), int(round(img_y))
    elif handle == "n":
        y0 = int(round(img_y))
    elif handle == "s":
        y1 = int(round(img_y))
    elif handle == "w":
        x0 = int(round(img_x))
    elif handle == "e":
        x1 = int(round(img_x))
    else:
        return rect.clamp_to(img_w, img_h)

    # Normalize so x0<x1, y0<y1
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0
    w = max(MIN_CROP, x1 - x0)
    h = max(MIN_CROP, y1 - y0)

    if aspect and aspect > 0:
        # Keep the opposite corner fixed from drag start
        if handle in ("se", "e", "s"):
            # anchor top-left of start
            ax, ay = start.x, start.y
            if handle == "e":
                w = max(MIN_CROP, int(round(img_x)) - ax)
                h = int(round(w / aspect))
            elif handle == "s":
                h = max(MIN_CROP, int(round(img_y)) - ay)
                w = int(round(h * aspect))
            else:
                w = max(MIN_CROP, int(round(img_x)) - ax)
                h = int(round(w / aspect))
                # if pointer is more vertical, prefer height
                h2 = max(MIN_CROP, int(round(img_y)) - ay)
                w2 = int(round(h2 * aspect))
                if abs(h2 - h) < abs(w2 - w):
                    h, w = h2, w2
            x0, y0 = ax, ay
        elif handle in ("nw",):
            ax, ay = start.x + start.w, start.y + start.h  # bottom-right fixed
            w = max(MIN_CROP, ax - int(round(img_x)))
            h = int(round(w / aspect))
            x0, y0 = ax - w, ay - h
        elif handle in ("ne",):
            ax, ay = start.x, start.y + start.h  # bottom-left fixed
            w = max(MIN_CROP, int(round(img_x)) - ax)
            h = int(round(w / aspect))
            x0, y0 = ax, ay - h
        elif handle in ("sw",):
            ax, ay = start.x + start.w, start.y  # top-right fixed
            w = max(MIN_CROP, ax - int(round(img_x)))
            h = int(round(w / aspect))
            x0, y0 = ax - w, ay
        elif handle == "n":
            ax = start.x + start.w // 2
            ay = start.y + start.h  # bottom center fixed
            h = max(MIN_CROP, ay - int(round(img_y)))
            w = int(round(h * aspect))
            x0, y0 = ax - w // 2, ay - h
        elif handle == "w":
            ax = start.x + start.w  # right edge fixed
            ay = start.y + start.h // 2
            w = max(MIN_CROP, ax - int(round(img_x)))
            h = int(round(w / aspect))
            x0, y0 = ax - w, ay - h // 2
        # Clamp size to image while keeping aspect
        if w > img_w:
            w = img_w
            h = max(MIN_CROP, int(round(w / aspect)))
        if h > img_h:
            h = img_h
            w = max(MIN_CROP, int(round(h * aspect)))
        if w > img_w:
            w = img_w
            h = max(MIN_CROP, int(round(w / aspect)))

    return CropRect(int(x0), int(y0), int(w), int(h)).clamp_to(img_w, img_h)


def _save_cropped_pixbuf(pb: GdkPixbuf.Pixbuf, dest: Path, ext: str) -> None:
    ext_l = (ext or dest.suffix.lstrip(".")).lower()
    tmp = dest.with_suffix(dest.suffix + f".crop-tmp.{os.getpid()}")
    try:
        if ext_l in ("jpg", "jpeg"):
            pb.savev(str(tmp), "jpeg", ["quality"], ["95"])
        elif ext_l == "png":
            pb.savev(str(tmp), "png", [], [])
        elif ext_l == "webp":
            pb.savev(str(tmp), "webp", ["quality"], ["95"])
        elif ext_l == "bmp":
            pb.savev(str(tmp), "bmp", [], [])
        elif ext_l in ("tif", "tiff"):
            pb.savev(str(tmp), "tiff", [], [])
        else:
            # Fall through to ImageMagick with source path handled by caller
            raise WriteError(f"GdkPixbuf cannot save .{ext_l}")
        os.replace(tmp, dest)
    except Exception as exc:
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        raise WriteError(f"Failed to save crop: {exc}") from exc


def _crop_with_imagemagick(
    src: Path, dest: Path, x: int, y: int, w: int, h: int
) -> None:
    tmp = dest.with_suffix(dest.suffix + f".crop-tmp.{os.getpid()}")
    try:
        subprocess.check_call(
            [
                "convert",
                str(src),
                "-crop",
                f"{w}x{h}+{x}+{y}",
                "+repage",
                str(tmp),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        if not tmp.is_file():
            raise WriteError("ImageMagick crop produced no file")
        os.replace(tmp, dest)
    except Exception as exc:
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        raise WriteError(f"ImageMagick crop failed: {exc}") from exc


def _regenerate_thumbnail(media: Path, item_dir: Path, name: str) -> Path | None:
    """Write ``{name}_thumbnail.png``; returns path or None."""
    from import_media import _make_image_thumbnail

    # Remove old thumbs so stale formats don't linger
    for old in item_dir.glob(f"{name}_thumbnail.*"):
        try:
            old.unlink()
        except OSError:
            pass
    dest = item_dir / f"{name}_thumbnail.png"
    if _make_image_thumbnail(media, dest):
        return dest
    return None


def _validate_image_item(item: Any) -> tuple[Path, Path, CropRect | None]:
    """Return (media_path, item_dir, None). Raises WriteError if not croppable."""
    if not getattr(item, "is_image", False):
        raise WriteError("Crop only works on images")
    path = Path(item.path)
    item_dir = item.item_dir
    if item_dir is None or not Path(item_dir).is_dir():
        raise WriteError("No item directory")
    if not path.is_file():
        raise WriteError(f"Media file missing: {path}")
    return path, Path(item_dir), None


def _clamp_rect_for_item(item: Any, path: Path, rect: CropRect) -> CropRect:
    img_w = int(item.width or 0)
    img_h = int(item.height or 0)
    rect = rect.clamp_to(img_w or 10**9, img_h or 10**9)
    if rect.w < MIN_CROP or rect.h < MIN_CROP:
        raise WriteError("Crop region too small")
    # Prefer actual pixel bounds when readable
    try:
        pb = _load_pixbuf(path)
        rect = rect.clamp_to(int(pb.get_width()), int(pb.get_height()))
    except WriteError:
        pass
    return rect


def _is_full_frame(rect: CropRect, img_w: int, img_h: int) -> bool:
    return (
        img_w > 0
        and img_h > 0
        and rect.x == 0
        and rect.y == 0
        and rect.w == img_w
        and rect.h == img_h
    )


def _write_crop_pixels(
    src: Path,
    dest: Path,
    rect: CropRect,
    ext: str,
) -> tuple[int, int]:
    """
    Crop ``src`` by ``rect`` into ``dest`` (may be the same path).

    Returns actual written (width, height).
    """
    cropped_ok = False
    cropped: GdkPixbuf.Pixbuf | None = None
    try:
        pb = pixbuf_from_path(src)
        if pb is None:
            raise RuntimeError(f"Could not decode {src}")
        full_w, full_h = int(pb.get_width()), int(pb.get_height())
        rect = rect.clamp_to(full_w, full_h)
        sub = pb.new_subpixbuf(rect.x, rect.y, rect.w, rect.h)
        if sub is None:
            raise WriteError("Could not extract crop region")
        cropped = sub.copy()
        try:
            _save_cropped_pixbuf(cropped, dest, ext)
            cropped_ok = True
        except WriteError:
            cropped_ok = False
    except WriteError:
        raise
    except Exception:
        cropped_ok = False

    if not cropped_ok:
        _crop_with_imagemagick(src, dest, rect.x, rect.y, rect.w, rect.h)
        out_pb = pixbuf_from_path(dest)
        if out_pb is not None:
            return int(out_pb.get_width()), int(out_pb.get_height())
        return rect.w, rect.h

    if cropped is not None:
        return int(cropped.get_width()), int(cropped.get_height())
    return rect.w, rect.h


def apply_crop_to_item(
    library_root: Path,
    item: Any,
    rect: CropRect,
) -> Any:
    """
    Crop the item's media file in place and update Eagle metadata.

    ``item`` is a library.Item (duck-typed). Mutates and returns it.
    """
    path, item_dir, _ = _validate_image_item(item)
    rect = _clamp_rect_for_item(item, path, rect)

    img_w = int(item.width or 0)
    img_h = int(item.height or 0)
    if _is_full_frame(rect, img_w, img_h):
        raise WriteError("Crop matches full image — nothing to do")

    ext = item.ext or path.suffix.lstrip(".")
    with write_session(library_root):
        backup_file(library_root, path)
        new_w, new_h = _write_crop_pixels(path, path, rect, ext)
        new_size = path.stat().st_size
        thumb = _regenerate_thumbnail(path, item_dir, item.name)

        data = load_item_metadata(item_dir)
        data["width"] = new_w
        data["height"] = new_h
        data["size"] = new_size
        if "resolutionWidth" in data:
            data["resolutionWidth"] = new_w
        if "resolutionHeight" in data:
            data["resolutionHeight"] = new_h
        save_item_metadata(library_root, item_dir, data)

    item.width = new_w
    item.height = new_h
    item.size = new_size
    item.modification_time = int(data.get("modificationTime") or item.modification_time)
    if thumb is not None:
        item.thumb = thumb
    return item


def save_crop_as_new_item(
    library_root: Path,
    item: Any,
    rect: CropRect,
) -> Any:
    """
    Write the crop as a **new** library item (no tags, no folders).

    Leaves the source item untouched. Returns a new ``library.Item``.
    """
    from import_media import _make_image_thumbnail, _now_ms, _unique_item_dir
    from library import Item
    from write import atomic_write_json

    path, _src_dir, _ = _validate_image_item(item)
    rect = _clamp_rect_for_item(item, path, rect)
    ext = (item.ext or path.suffix.lstrip(".")).lstrip(".")
    name = str(item.name or path.stem).replace("/", "-").replace("\\", "-")
    if not name:
        name = "crop"

    with write_session(library_root):
        images_dir = Path(library_root) / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        iid, item_dir = _unique_item_dir(images_dir)
        try:
            item_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise WriteError(f"Item dir already exists: {item_dir}") from exc

        dest_media = item_dir / f"{name}.{ext}"
        try:
            new_w, new_h = _write_crop_pixels(path, dest_media, rect, ext)
        except Exception:
            # Clean up empty/partial item dir on failure
            try:
                if dest_media.is_file():
                    dest_media.unlink()
                item_dir.rmdir()
            except OSError:
                pass
            raise

        new_size = dest_media.stat().st_size
        thumb_path = item_dir / f"{name}_thumbnail.png"
        if not _make_image_thumbnail(dest_media, thumb_path):
            thumb_path_resolved = None
        else:
            thumb_path_resolved = thumb_path

        now = _now_ms()
        meta: dict[str, Any] = {
            "id": iid,
            "name": name,
            "size": new_size,
            "btime": now,
            "mtime": now,
            "ext": ext,
            "tags": [],
            "folders": [],
            "isDeleted": False,
            "url": "",
            "annotation": "",
            "modificationTime": now,
            "width": new_w,
            "height": new_h,
            "lastModified": now,
            "palettes": [],
        }
        atomic_write_json(item_dir / "metadata.json", meta)

        # mtime index (same as inbox import)
        mtime_path = Path(library_root) / "mtime.json"
        if mtime_path.is_file():
            try:
                with mtime_path.open("r", encoding="utf-8") as f:
                    mt = json.load(f)
                if isinstance(mt, dict):
                    mt[iid] = now
                    backup_file(library_root, mtime_path)
                    atomic_write_json(mtime_path, mt)
            except (OSError, json.JSONDecodeError):
                pass

    return Item(
        id=iid,
        name=name,
        ext=ext,
        tags=[],
        folders=[],
        path=dest_media.resolve(),
        thumb=thumb_path_resolved.resolve() if thumb_path_resolved else None,
        is_deleted=False,
        size=new_size,
        width=new_w,
        height=new_h,
        annotation="",
        modification_time=now,
        btime=now,
        star=None,
        duration=None,
        item_dir=item_dir.resolve(),
        tag_set=frozenset(),
        folder_set=frozenset(),
        name_lower=name.lower(),
        ext_lower=ext.lower(),
    )


# ---------------------------------------------------------------------------
# GTK crop dialog
# ---------------------------------------------------------------------------


class CropWindow(Gtk.Window):
    """Modal crop editor for a single image item."""

    def __init__(
        self,
        parent: Gtk.Window,
        item: Any,
        *,
        library_root: Path | None = None,
        on_done: Callable[[str, Any], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ):
        super().__init__(
            title=f"Crop · {getattr(item, 'display_name', item.name)}",
            transient_for=parent,
            modal=True,
            default_width=960,
            default_height=720,
        )
        self._item = item
        if library_root is not None:
            self._library_root = Path(library_root)
        elif item.item_dir is not None:
            # images/<id>.info → library root
            self._library_root = Path(item.item_dir).parent.parent
        else:
            self._library_root = Path(".")
        # on_done(mode, item) where mode is "overwrite" | "new"
        self._on_done = on_done
        self._on_close = on_close
        self._closing = False

        path = Path(item.path)
        self._pixbuf = _load_pixbuf(path)

        self._img_w = int(self._pixbuf.get_width())
        self._img_h = int(self._pixbuf.get_height())
        # Start with full image; Free aspect
        self._rect = CropRect(0, 0, self._img_w, self._img_h)
        self._aspect: float | None = None  # w/h; None = free
        self._aspect_id = "free"
        self._syncing_spins = False

        # Display transform (updated on draw / resize)
        self._scale = 1.0
        self._ox = 0.0
        self._oy = 0.0
        self._disp_w = 0.0
        self._disp_h = 0.0

        # Drag state
        self._drag_handle: Handle = ""
        self._drag_start_rect: CropRect | None = None
        self._drag_origin_img: tuple[float, float] | None = None

        self._build()
        self.connect("close-request", self._on_close_request)

    # -- UI -----------------------------------------------------------------

    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        # Toolbar: dimensions + ratios
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        root.append(bar)

        bar.append(Gtk.Label(label="W"))
        self.spin_w = Gtk.SpinButton.new_with_range(MIN_CROP, max(MIN_CROP, self._img_w), 1)
        self.spin_w.set_value(self._rect.w)
        self.spin_w.set_width_chars(6)
        self.spin_w.connect("value-changed", self._on_spin_w)
        bar.append(self.spin_w)

        bar.append(Gtk.Label(label="H"))
        self.spin_h = Gtk.SpinButton.new_with_range(MIN_CROP, max(MIN_CROP, self._img_h), 1)
        self.spin_h.set_value(self._rect.h)
        self.spin_h.set_width_chars(6)
        self.spin_h.connect("value-changed", self._on_spin_h)
        bar.append(self.spin_h)

        self.size_lbl = Gtk.Label(label=f"  of {self._img_w}×{self._img_h}")
        self.size_lbl.add_css_class("dim-label")
        bar.append(self.size_lbl)

        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        ratios = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        ratios.set_hexpand(True)
        self._ratio_buttons: dict[str, Gtk.ToggleButton] = {}
        group_leader: Gtk.ToggleButton | None = None
        for pid, label, aw, ah in ASPECT_PRESETS:
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("flat")
            if group_leader is None:
                group_leader = btn
            else:
                btn.set_group(group_leader)
            if pid == "free":
                btn.set_active(True)
            btn.connect("toggled", self._on_ratio_toggled, pid, aw, ah)
            self._ratio_buttons[pid] = btn
            ratios.append(btn)
        bar.append(ratios)

        # Canvas
        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_draw_func(self._draw)
        self.area.set_cursor(Gdk.Cursor.new_from_name("crosshair"))
        root.append(self.area)

        # Pointer
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("pressed", self._on_press)
        click.connect("released", self._on_release)
        self.area.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.area.add_controller(motion)

        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.area.add_controller(drag)

        # Footer actions
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        foot.set_margin_top(8)
        foot.set_margin_bottom(12)
        foot.set_margin_start(12)
        foot.set_margin_end(12)
        foot.set_halign(Gtk.Align.END)
        root.append(foot)

        self.hint = Gtk.Label(
            label="Drag to move · corners/edges resize · Enter saves original · Shift+Enter saves as · Esc cancels"
        )
        self.hint.add_css_class("dim-label")
        self.hint.add_css_class("caption")
        self.hint.set_hexpand(True)
        self.hint.set_xalign(0)
        foot.append(self.hint)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self._close())
        foot.append(cancel)

        save_as = Gtk.Button(label="Save as")
        save_as.set_tooltip_text(
            "Create a new library item with this crop (no tags, no folders)"
        )
        save_as.connect("clicked", lambda *_: self._save_as())
        foot.append(save_as)

        save = Gtk.Button(label="Save")
        save.set_tooltip_text("Overwrite the original image with this crop")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save_original())
        foot.append(save)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

    # -- coordinate mapping -------------------------------------------------

    def _update_transform(self, width: float, height: float) -> None:
        if self._img_w <= 0 or self._img_h <= 0 or width <= 1 or height <= 1:
            self._scale = 1.0
            self._ox = self._oy = 0.0
            self._disp_w = self._disp_h = 0.0
            return
        pad = 8.0
        avail_w = max(1.0, width - pad * 2)
        avail_h = max(1.0, height - pad * 2)
        scale = min(avail_w / self._img_w, avail_h / self._img_h)
        self._scale = scale
        self._disp_w = self._img_w * scale
        self._disp_h = self._img_h * scale
        self._ox = (width - self._disp_w) / 2
        self._oy = (height - self._disp_h) / 2

    def _to_image(self, dx: float, dy: float) -> tuple[float, float]:
        if self._scale <= 0:
            return 0.0, 0.0
        return (dx - self._ox) / self._scale, (dy - self._oy) / self._scale

    def _in_image_display(self, dx: float, dy: float) -> bool:
        return (
            self._ox <= dx <= self._ox + self._disp_w
            and self._oy <= dy <= self._oy + self._disp_h
        )

    # -- draw ---------------------------------------------------------------

    def _draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        self._update_transform(float(width), float(height))
        # Background
        cr.set_source_rgb(0.12, 0.12, 0.12)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        if self._disp_w <= 0 or self._disp_h <= 0:
            return

        # Image
        cr.save()
        cr.translate(self._ox, self._oy)
        cr.scale(self._scale, self._scale)
        Gdk.cairo_set_source_pixbuf(cr, self._pixbuf, 0, 0)
        cr.paint()
        cr.restore()

        # Dim outside crop (four rects in display space)
        rx = self._ox + self._rect.x * self._scale
        ry = self._oy + self._rect.y * self._scale
        rw = self._rect.w * self._scale
        rh = self._rect.h * self._scale
        img_x0, img_y0 = self._ox, self._oy
        img_x1, img_y1 = self._ox + self._disp_w, self._oy + self._disp_h

        cr.set_source_rgba(0, 0, 0, 0.55)
        # top
        cr.rectangle(img_x0, img_y0, self._disp_w, max(0, ry - img_y0))
        cr.fill()
        # bottom
        cr.rectangle(img_x0, ry + rh, self._disp_w, max(0, img_y1 - (ry + rh)))
        cr.fill()
        # left
        cr.rectangle(img_x0, ry, max(0, rx - img_x0), rh)
        cr.fill()
        # right
        cr.rectangle(rx + rw, ry, max(0, img_x1 - (rx + rw)), rh)
        cr.fill()

        # Crop border
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(1.5)
        cr.rectangle(rx + 0.5, ry + 0.5, rw - 1, rh - 1)
        cr.stroke()

        # Rule-of-thirds guides
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.set_line_width(1.0)
        for i in (1, 2):
            cr.move_to(rx + rw * i / 3, ry)
            cr.line_to(rx + rw * i / 3, ry + rh)
            cr.stroke()
            cr.move_to(rx, ry + rh * i / 3)
            cr.line_to(rx + rw, ry + rh * i / 3)
            cr.stroke()

        # Corner/edge handles
        hs = 7.0
        points = [
            (rx, ry),
            (rx + rw, ry),
            (rx, ry + rh),
            (rx + rw, ry + rh),
            (rx + rw / 2, ry),
            (rx + rw / 2, ry + rh),
            (rx, ry + rh / 2),
            (rx + rw, ry + rh / 2),
        ]
        cr.set_source_rgb(1, 1, 1)
        for px, py in points:
            cr.rectangle(px - hs / 2, py - hs / 2, hs, hs)
            cr.fill()
        cr.set_source_rgb(0.15, 0.45, 0.95)
        cr.set_line_width(1.0)
        for px, py in points:
            cr.rectangle(px - hs / 2, py - hs / 2, hs, hs)
            cr.stroke()

        # Size badge
        label = f"{self._rect.w} × {self._rect.h}"
        cr.set_source_rgba(0, 0, 0, 0.65)
        cr.rectangle(rx + 6, ry + 6, 8 + 8 * len(label), 18)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(12)
        cr.move_to(rx + 10, ry + 19)
        cr.show_text(label)

    # -- input --------------------------------------------------------------

    def _cursor_for_handle(self, handle: Handle) -> str:
        return {
            "nw": "nw-resize",
            "ne": "ne-resize",
            "sw": "sw-resize",
            "se": "se-resize",
            "n": "ns-resize",
            "s": "ns-resize",
            "e": "ew-resize",
            "w": "ew-resize",
            "move": "move",
        }.get(handle, "crosshair")

    def _on_motion(self, _c, x: float, y: float) -> None:
        if self._drag_handle:
            return
        ix, iy = self._to_image(x, y)
        handle = hit_test(ix, iy, self._rect, scale=self._scale)
        self.area.set_cursor(Gdk.Cursor.new_from_name(self._cursor_for_handle(handle)))

    def _on_press(self, _g, _n, x: float, y: float) -> None:
        ix, iy = self._to_image(x, y)
        self._drag_handle = hit_test(ix, iy, self._rect, scale=self._scale)
        self._drag_start_rect = CropRect(
            self._rect.x, self._rect.y, self._rect.w, self._rect.h
        )
        self._drag_origin_img = (ix, iy)

    def _on_release(self, *_a) -> None:
        self._drag_handle = ""
        self._drag_start_rect = None
        self._drag_origin_img = None
        self._sync_spins_from_rect()

    def _on_drag_begin(self, _g, _x: float, _y: float) -> None:
        # press already recorded handle
        pass

    def _on_drag_update(self, gesture: Gtk.GestureDrag, ox: float, oy: float) -> None:
        ok, start_x, start_y = gesture.get_start_point()
        if not ok or not self._drag_handle or self._drag_start_rect is None:
            return
        ix, iy = self._to_image(start_x + ox, start_y + oy)
        if self._drag_handle == "move" and self._drag_origin_img is not None:
            dx = ix - self._drag_origin_img[0]
            dy = iy - self._drag_origin_img[1]
            s = self._drag_start_rect
            self._rect = CropRect(
                int(round(s.x + dx)),
                int(round(s.y + dy)),
                s.w,
                s.h,
            ).clamp_to(self._img_w, self._img_h)
        elif self._drag_handle and self._drag_handle != "move":
            self._rect = resize_rect(
                self._rect,
                self._drag_handle,
                ix,
                iy,
                self._img_w,
                self._img_h,
                aspect=self._aspect,
                anchor=self._drag_start_rect,
            )
        self._sync_spins_from_rect()
        self.area.queue_draw()

    def _on_drag_end(self, *_a) -> None:
        self._drag_handle = ""
        self._drag_start_rect = None
        self._drag_origin_img = None
        self._sync_spins_from_rect()
        self.area.queue_draw()

    # -- spins / ratios -----------------------------------------------------

    def _sync_spins_from_rect(self) -> None:
        self._syncing_spins = True
        try:
            self.spin_w.set_value(self._rect.w)
            self.spin_h.set_value(self._rect.h)
        finally:
            self._syncing_spins = False

    def _on_spin_w(self, spin: Gtk.SpinButton) -> None:
        if self._syncing_spins:
            return
        w = int(spin.get_value())
        if self._aspect:
            h = max(MIN_CROP, int(round(w / self._aspect)))
            if h > self._img_h:
                h = self._img_h
                w = max(MIN_CROP, int(round(h * self._aspect)))
            self._rect = sized_rect(self._img_w, self._img_h, w, h, center_on=self._rect)
        else:
            self._rect = sized_rect(
                self._img_w, self._img_h, w, self._rect.h, center_on=self._rect
            )
        self._sync_spins_from_rect()
        self.area.queue_draw()

    def _on_spin_h(self, spin: Gtk.SpinButton) -> None:
        if self._syncing_spins:
            return
        h = int(spin.get_value())
        if self._aspect:
            w = max(MIN_CROP, int(round(h * self._aspect)))
            if w > self._img_w:
                w = self._img_w
                h = max(MIN_CROP, int(round(w / self._aspect)))
            self._rect = sized_rect(self._img_w, self._img_h, w, h, center_on=self._rect)
        else:
            self._rect = sized_rect(
                self._img_w, self._img_h, self._rect.w, h, center_on=self._rect
            )
        self._sync_spins_from_rect()
        self.area.queue_draw()

    def _on_ratio_toggled(
        self,
        btn: Gtk.ToggleButton,
        pid: str,
        aw: int | None,
        ah: int | None,
    ) -> None:
        if not btn.get_active():
            return
        self._aspect_id = pid
        if pid == "free":
            self._aspect = None
            # keep current rect
        elif pid == "orig":
            self._aspect = self._img_w / self._img_h if self._img_h else 1.0
            self._rect = max_rect_for_aspect(self._img_w, self._img_h, self._img_w, self._img_h)
        else:
            assert aw is not None and ah is not None
            self._aspect = aspect_ratio(aw, ah)
            # Keep center; fit largest of this aspect, or shrink current to aspect
            self._rect = max_rect_for_aspect(self._img_w, self._img_h, aw, ah)
        self._sync_spins_from_rect()
        self.area.queue_draw()

    # -- keys / apply / close -----------------------------------------------

    def _on_key(self, _c, keyval: int, _kc: int, state: Gdk.ModifierType) -> bool:
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        step = 10 if shift else 1
        if keyval == Gdk.KEY_Escape:
            self._close()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if shift:
                self._save_as()
            else:
                self._save_original()
            return True
        moved = False
        if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left, Gdk.KEY_h):
            self._rect = CropRect(
                self._rect.x - step, self._rect.y, self._rect.w, self._rect.h
            ).clamp_to(self._img_w, self._img_h)
            moved = True
        elif keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right, Gdk.KEY_l):
            self._rect = CropRect(
                self._rect.x + step, self._rect.y, self._rect.w, self._rect.h
            ).clamp_to(self._img_w, self._img_h)
            moved = True
        elif keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up, Gdk.KEY_k):
            self._rect = CropRect(
                self._rect.x, self._rect.y - step, self._rect.w, self._rect.h
            ).clamp_to(self._img_w, self._img_h)
            moved = True
        elif keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down, Gdk.KEY_j):
            self._rect = CropRect(
                self._rect.x, self._rect.y + step, self._rect.w, self._rect.h
            ).clamp_to(self._img_w, self._img_h)
            moved = True
        if moved:
            self.area.queue_draw()
            return True
        return False

    def _save_original(self) -> None:
        """Overwrite the source media with the crop."""
        item = self._item
        if item.item_dir is None:
            self._error_toast("No item directory")
            return
        try:
            apply_crop_to_item(self._library_root, item, self._rect)
        except WriteError as exc:
            self._error_toast(str(exc))
            return
        if self._on_done:
            self._on_done("overwrite", item)
        self._close()

    def _save_as(self) -> None:
        """Create a new library item from the crop (untagged, uncategorized)."""
        item = self._item
        if item.item_dir is None:
            self._error_toast("No item directory")
            return
        try:
            new_item = save_crop_as_new_item(self._library_root, item, self._rect)
        except WriteError as exc:
            self._error_toast(str(exc))
            return
        if self._on_done:
            self._on_done("new", new_item)
        self._close()

    def _error_toast(self, text: str) -> None:
        # Prefer parent toast if available
        parent = self.get_transient_for()
        if parent is not None and hasattr(parent, "_toast"):
            try:
                parent._toast(text)  # noqa: SLF001
                return
            except Exception:  # noqa: BLE001
                pass
        dialog = Gtk.AlertDialog(message=text)
        dialog.show(self)

    def _on_close_request(self, *_a) -> bool:
        self._close()
        return True

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.destroy()
        if self._on_close:
            try:
                self._on_close()
            except Exception:  # noqa: BLE001
                pass


def open_crop_dialog(
    parent: Gtk.Window,
    item: Any,
    *,
    library_root: Path | None = None,
    on_done: Callable[[str, Any], None] | None = None,
    on_close: Callable[[], None] | None = None,
) -> CropWindow | None:
    """Open the crop dialog; returns None if the item is not a croppable image.

    ``on_done(mode, item)`` is called after a successful save:
    - mode ``"overwrite"`` — ``item`` is the mutated original
    - mode ``"new"`` — ``item`` is the newly created library item
    """
    if not getattr(item, "is_image", False):
        if hasattr(parent, "_toast"):
            parent._toast("Crop works on images only")  # noqa: SLF001
        return None
    path = getattr(item, "path", None)
    if path is None or not Path(path).is_file():
        if hasattr(parent, "_toast"):
            parent._toast("Image file missing")  # noqa: SLF001
        return None
    try:
        win = CropWindow(
            parent,
            item,
            library_root=library_root,
            on_done=on_done,
            on_close=on_close,
        )
    except WriteError as exc:
        if hasattr(parent, "_toast"):
            parent._toast(str(exc))  # noqa: SLF001
        return None
    win.present()
    return win
