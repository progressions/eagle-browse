"""Interactive audio crop for Eagle Browse.

Pick a start–end segment to keep. Save overwrites the original (with backup),
Save as writes a new untagged library item, Cancel writes nothing.
"""

from __future__ import annotations

import array
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from write import (
    WriteError,
    atomic_write_json,
    backup_file,
    load_item_metadata,
    save_item_metadata,
    write_session,
)

MIN_CROP_S = 0.05
HANDLE_HIT_PX = 10


def format_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m, s = divmod(seconds, 60.0)
    if m >= 60:
        h, m = divmod(m, 60.0)
        return f"{int(h):d}:{int(m):02d}:{s:05.2f}"
    return f"{int(m):d}:{s:05.2f}"


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            timeout=30,
        )
        data = json.loads(out.decode("utf-8", "replace"))
        return float((data.get("format") or {}).get("duration") or 0)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        raise WriteError(f"Could not read audio duration: {exc}") from exc


def extract_peaks(path: Path, buckets: int = 480) -> list[float]:
    """Peak envelope for the waveform. Empty list if ffmpeg fails."""
    buckets = max(32, int(buckets))
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-ac",
        "1",
        "-ar",
        "8000",
        "-f",
        "f32le",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=90, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    raw = proc.stdout or b""
    n = len(raw) // 4
    if n < 2:
        return []
    arr = array.array("f")
    arr.frombytes(raw[: n * 4])
    if sys.byteorder != "little":
        arr.byteswap()
    peaks = [0.0] * buckets
    for i, sample in enumerate(arr):
        b = min(buckets - 1, int(i * buckets / n))
        a = sample if sample >= 0 else -sample
        if a > peaks[b]:
            peaks[b] = a
    mx = max(peaks) or 1.0
    return [p / mx for p in peaks]


_FF_FORMAT = {
    ".mp3": "mp3",
    ".wav": "wav",
    ".m4a": "ipod",
    ".aac": "adts",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".flac": "flac",
    ".aiff": "aiff",
    ".aif": "aiff",
}


def _audio_ext(src: Path, dest: Path) -> str:
    for p in (dest, src):
        ext = p.suffix.lower()
        if ext and ext not in {".part", ".tmp", ".crop-tmp"}:
            return ext
    return ".mp3"


def write_audio_segment(src: Path, dest: Path, start: float, end: float) -> None:
    """Write [start, end) of *src* to *dest* via ffmpeg."""
    start = max(0.0, float(start))
    end = max(start + MIN_CROP_S, float(end))
    duration = end - start
    ext = _audio_ext(src, dest)
    # ffmpeg picks the muxer from the filename. A trailing .part / .crop-tmp
    # makes it fail with "Unable to choose an output format".
    work = dest.with_name(f".{dest.name}.ff{ext}")
    if work.exists():
        work.unlink()
    fmt = _FF_FORMAT.get(ext)
    last_err = ""

    def run(codec: list[str]) -> None:
        nonlocal last_err
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            *codec,
        ]
        if fmt:
            cmd.extend(["-f", fmt])
        cmd.append(str(work))
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300,
            check=False,
            text=True,
        )
        if proc.returncode != 0:
            last_err = (proc.stderr or proc.stdout or "").strip()
            raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stderr)

    try:
        try:
            run(["-map", "0:a:0", "-c", "copy"])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if ext == ".mp3":
                run(["-c:a", "libmp3lame", "-q:a", "2"])
            elif ext in (".m4a", ".aac"):
                run(["-c:a", "aac", "-b:a", "192k"])
            elif ext in (".ogg", ".oga"):
                run(["-c:a", "libvorbis", "-q:a", "5"])
            elif ext == ".flac":
                run(["-c:a", "flac"])
            elif ext in (".wav", ".aiff", ".aif"):
                run(["-c:a", "pcm_s16le"])
            else:
                run(["-c:a", "libmp3lame", "-q:a", "2"])
        if not work.is_file() or work.stat().st_size <= 0:
            raise WriteError("ffmpeg produced no audio")
        os.replace(work, dest)
    except FileNotFoundError as exc:
        raise WriteError("ffmpeg is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = last_err.splitlines()[-1] if last_err else "encode failed"
        raise WriteError(f"ffmpeg could not crop this audio · {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WriteError("ffmpeg timed out cropping audio") from exc
    finally:
        if work.exists():
            try:
                work.unlink()
            except OSError:
                pass


def _validate_audio_item(item: Any) -> tuple[Path, Path]:
    path = Path(getattr(item, "path", "") or "")
    item_dir = getattr(item, "item_dir", None)
    if not getattr(item, "is_audio", False):
        raise WriteError("Not an audio item")
    if not path.is_file():
        raise WriteError(f"Audio file missing: {path}")
    if item_dir is None:
        raise WriteError("No item directory")
    return path, Path(item_dir)


def apply_audio_crop_to_item(
    library_root: Path,
    item: Any,
    start: float,
    end: float,
) -> Any:
    """Overwrite the item's audio with [start, end). Mutates and returns *item*."""
    path, item_dir = _validate_audio_item(item)
    total = float(getattr(item, "duration", 0) or 0) or probe_duration(path)
    start = max(0.0, min(float(start), total - MIN_CROP_S))
    end = max(start + MIN_CROP_S, min(float(end), total))
    if start <= 0.001 and end >= total - 0.001:
        raise WriteError("Crop matches the full file — nothing to do")

    with write_session(library_root):
        backup_file(library_root, path)
        tmp = path.with_name(f".{path.stem}.cropped{path.suffix}")
        try:
            write_audio_segment(path, tmp, start, end)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        new_size = path.stat().st_size
        new_dur = probe_duration(path)
        data = load_item_metadata(item_dir)
        data["size"] = new_size
        data["duration"] = new_dur
        save_item_metadata(library_root, item_dir, data)

    item.size = new_size
    item.duration = new_dur
    item.modification_time = int(data.get("modificationTime") or item.modification_time)
    return item


def save_audio_crop_as_new_item(
    library_root: Path,
    item: Any,
    start: float,
    end: float,
) -> Any:
    """Write [start, end) as a new untagged / uncategorized library item."""
    from import_media import _now_ms, _unique_item_dir
    from library import Item

    path, _item_dir = _validate_audio_item(item)
    total = float(getattr(item, "duration", 0) or 0) or probe_duration(path)
    start = max(0.0, min(float(start), total - MIN_CROP_S))
    end = max(start + MIN_CROP_S, min(float(end), total))
    ext = (item.ext or path.suffix.lstrip(".")).lstrip(".")
    name = str(item.name or path.stem).replace("/", "-").replace("\\", "-") or "audio"
    name = f"{name}-crop"

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
            write_audio_segment(path, dest_media, start, end)
        except Exception:
            try:
                if dest_media.is_file():
                    dest_media.unlink()
                item_dir.rmdir()
            except OSError:
                pass
            raise

        new_size = dest_media.stat().st_size
        new_dur = probe_duration(dest_media)
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
            "lastModified": now,
            "palettes": [],
            "duration": new_dur,
        }
        atomic_write_json(item_dir / "metadata.json", meta)

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
        thumb=None,
        is_deleted=False,
        size=new_size,
        width=0,
        height=0,
        annotation="",
        modification_time=now,
        btime=now,
        star=None,
        duration=new_dur,
        item_dir=item_dir.resolve(),
        tag_set=frozenset(),
        folder_set=frozenset(),
        name_lower=name.lower(),
        ext_lower=ext.lower(),
    )


class AudioCropWindow(Gtk.Window):
    """Modal editor: keep a start–end slice of one audio item."""

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
            title=f"Crop audio · {getattr(item, 'display_name', item.name)}",
            transient_for=parent,
            modal=True,
            default_width=720,
            default_height=280,
        )
        self._item = item
        if library_root is not None:
            self._library_root = Path(library_root)
        elif item.item_dir is not None:
            self._library_root = Path(item.item_dir).parent.parent
        else:
            self._library_root = Path(".")
        self._on_done = on_done
        self._on_close = on_close
        self._closing = False
        self._path = Path(item.path)
        self._duration = float(item.duration or 0) or probe_duration(self._path)
        if self._duration <= MIN_CROP_S:
            raise WriteError("Audio is too short to crop")
        self._start = 0.0
        self._end = self._duration
        self._peaks: list[float] = []
        self._syncing = False
        self._drag: str = ""  # "" | "start" | "end" | "move"
        self._drag_origin = 0.0
        self._drag_start0 = 0.0
        self._drag_end0 = 0.0
        # External player — Gtk.MediaFile/GStreamer aborted the process
        # (SIGSEGV / SIGABRT in gst_pad_push) when seeking an audio file.
        self._preview_proc: subprocess.Popen[bytes] | None = None
        self._play_watch = 0

        self._build()
        self.connect("close-request", self._on_close_request)
        if hasattr(parent, "_remember_dialog"):
            parent._remember_dialog(self)  # type: ignore[attr-defined]
        GLib.idle_add(self._load_peaks)

    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(10)
        bar.set_margin_bottom(6)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        root.append(bar)

        bar.append(Gtk.Label(label="Start", xalign=0))
        adj_s = Gtk.Adjustment(
            lower=0.0,
            upper=max(0.0, self._duration - MIN_CROP_S),
            step_increment=0.05,
            page_increment=1.0,
            value=0.0,
        )
        self.spin_start = Gtk.SpinButton(adjustment=adj_s, digits=2, numeric=True)
        self.spin_start.set_width_chars(7)
        self.spin_start.connect("value-changed", self._on_start_spin)
        bar.append(self.spin_start)

        bar.append(Gtk.Label(label="End", xalign=0))
        adj_e = Gtk.Adjustment(
            lower=MIN_CROP_S,
            upper=self._duration,
            step_increment=0.05,
            page_increment=1.0,
            value=self._duration,
        )
        self.spin_end = Gtk.SpinButton(adjustment=adj_e, digits=2, numeric=True)
        self.spin_end.set_width_chars(7)
        self.spin_end.connect("value-changed", self._on_end_spin)
        bar.append(self.spin_end)

        self.time_lbl = Gtk.Label(xalign=0)
        self.time_lbl.add_css_class("dim-label")
        self.time_lbl.set_hexpand(True)
        bar.append(self.time_lbl)

        self.play_btn = Gtk.Button(label="Play selection")
        self.play_btn.connect("clicked", lambda *_: self._toggle_play())
        bar.append(self.play_btn)

        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_content_height(120)
        self.area.set_draw_func(self._draw)
        self.area.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.area.set_margin_start(12)
        self.area.set_margin_end(12)
        root.append(self.area)

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
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.area.add_controller(drag)

        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        foot.set_margin_top(8)
        foot.set_margin_bottom(12)
        foot.set_margin_start(12)
        foot.set_margin_end(12)
        foot.set_halign(Gtk.Align.END)
        root.append(foot)

        hint = Gtk.Label(
            label="Drag the handles or type start/end · Space plays the selection · Enter saves · Shift+Enter saves as · Esc cancels"
        )
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        hint.set_hexpand(True)
        hint.set_xalign(0)
        foot.append(hint)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self._close())
        foot.append(cancel)
        save_as = Gtk.Button(label="Save as")
        save_as.set_tooltip_text(
            "Create a new library item with this segment (no tags, no folders)"
        )
        save_as.connect("clicked", lambda *_: self._save_as())
        foot.append(save_as)
        save = Gtk.Button(label="Save")
        save.set_tooltip_text("Overwrite the original audio with this segment")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save_original())
        foot.append(save)

        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        self._refresh_time_label()

    def _load_peaks(self) -> bool:
        path = self._path

        def work() -> None:
            peaks = extract_peaks(path)

            def apply() -> bool:
                if self._closing:
                    return False
                self._peaks = peaks
                self.area.queue_draw()
                return False

            GLib.idle_add(apply)

        import threading

        threading.Thread(target=work, name="eagle-audio-peaks", daemon=True).start()
        return False

    def _refresh_time_label(self) -> None:
        kept = max(0.0, self._end - self._start)
        self.time_lbl.set_text(
            f"{format_time(self._start)} – {format_time(self._end)}   "
            f"keep {format_time(kept)} of {format_time(self._duration)}"
        )

    def _set_range(self, start: float, end: float) -> None:
        start = max(0.0, min(start, self._duration - MIN_CROP_S))
        end = max(start + MIN_CROP_S, min(end, self._duration))
        self._start = start
        self._end = end
        self._syncing = True
        try:
            self.spin_start.set_value(start)
            self.spin_end.set_value(end)
        finally:
            self._syncing = False
        self._refresh_time_label()
        self.area.queue_draw()

    def _on_start_spin(self, spin: Gtk.SpinButton) -> None:
        if self._syncing:
            return
        self._set_range(spin.get_value(), self._end)

    def _on_end_spin(self, spin: Gtk.SpinButton) -> None:
        if self._syncing:
            return
        self._set_range(self._start, spin.get_value())

    def _x_to_time(self, x: float) -> float:
        w = max(1.0, float(self.area.get_width()))
        return max(0.0, min(self._duration, (x / w) * self._duration))

    def _time_to_x(self, t: float) -> float:
        w = max(1.0, float(self.area.get_width()))
        if self._duration <= 0:
            return 0.0
        return (t / self._duration) * w

    def _hit(self, x: float) -> str:
        xs = self._time_to_x(self._start)
        xe = self._time_to_x(self._end)
        if abs(x - xs) <= HANDLE_HIT_PX:
            return "start"
        if abs(x - xe) <= HANDLE_HIT_PX:
            return "end"
        if xs < x < xe:
            return "move"
        return "start" if abs(x - xs) < abs(x - xe) else "end"

    def _on_press(self, _g, _n: int, x: float, _y: float) -> None:
        self._drag = self._hit(x)
        self._drag_origin = x
        self._drag_start0 = self._start
        self._drag_end0 = self._end
        if self._drag in ("start", "end"):
            t = self._x_to_time(x)
            if self._drag == "start":
                self._set_range(t, self._end)
            else:
                self._set_range(self._start, t)

    def _on_motion(self, _c, x: float, _y: float) -> None:
        hit = self._drag or self._hit(x)
        name = "ew-resize" if hit in ("start", "end") else "grabbing" if hit == "move" else "pointer"
        self.area.set_cursor(Gdk.Cursor.new_from_name(name))

    def _on_drag_update(self, _g, dx: float, _dy: float) -> None:
        if not self._drag:
            return
        w = max(1.0, float(self.area.get_width()))
        dt = (dx / w) * self._duration
        if self._drag == "start":
            self._set_range(self._drag_start0 + dt, self._drag_end0)
        elif self._drag == "end":
            self._set_range(self._drag_start0, self._drag_end0 + dt)
        else:
            span = self._drag_end0 - self._drag_start0
            start = self._drag_start0 + dt
            start = max(0.0, min(start, self._duration - span))
            self._set_range(start, start + span)

    def _on_drag_end(self, *_a) -> None:
        self._drag = ""

    def _on_release(self, *_a) -> None:
        self._drag = ""

    def _draw(self, _area, cr, width: int, height: int) -> None:
        cr.set_source_rgb(0.12, 0.12, 0.13)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        mid = height / 2.0
        amp = max(8.0, height * 0.38)
        peaks = self._peaks
        if peaks:
            n = len(peaks)
            cr.set_source_rgb(0.45, 0.48, 0.52)
            cr.set_line_width(1.0)
            for i, p in enumerate(peaks):
                x = (i + 0.5) * width / n
                h = p * amp
                cr.move_to(x, mid - h)
                cr.line_to(x, mid + h)
            cr.stroke()
        else:
            cr.set_source_rgb(0.3, 0.3, 0.32)
            cr.set_line_width(2)
            cr.move_to(0, mid)
            cr.line_to(width, mid)
            cr.stroke()

        xs = self._time_to_x(self._start)
        xe = self._time_to_x(self._end)
        cr.set_source_rgba(0.25, 0.55, 0.95, 0.22)
        cr.rectangle(xs, 0, max(1.0, xe - xs), height)
        cr.fill()
        cr.set_source_rgb(0.35, 0.65, 1.0)
        cr.set_line_width(2)
        cr.move_to(xs, 0)
        cr.line_to(xs, height)
        cr.move_to(xe, 0)
        cr.line_to(xe, height)
        cr.stroke()
        # Handle caps
        for x in (xs, xe):
            cr.rectangle(x - 3, 0, 6, 10)
            cr.rectangle(x - 3, height - 10, 6, 10)
            cr.fill()

    def _preview_cmd(self) -> list[str] | None:
        start = max(0.0, self._start)
        dur = max(MIN_CROP_S, self._end - self._start)
        path = str(self._path)
        if shutil.which("ffplay"):
            return [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{dur:.3f}",
                path,
            ]
        if shutil.which("mpv"):
            return [
                "mpv",
                "--no-video",
                "--really-quiet",
                f"--start={start:.3f}",
                f"--length={dur:.3f}",
                path,
            ]
        return None

    def _toggle_play(self) -> None:
        if self._preview_proc is not None and self._preview_proc.poll() is None:
            self._stop_preview()
            return
        self._stop_preview()
        cmd = self._preview_cmd()
        if cmd is None:
            self._error_toast("Need ffplay or mpv to preview")
            return
        try:
            self._preview_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._error_toast(f"Could not play: {exc}")
            self._preview_proc = None
            return
        self.play_btn.set_label("Stop")
        self._play_watch = GLib.timeout_add(80, self._watch_play)

    def _watch_play(self) -> bool:
        proc = self._preview_proc
        if self._closing or proc is None or proc.poll() is not None:
            self._stop_preview()
            return False
        return True

    def _stop_preview(self) -> None:
        if self._play_watch:
            try:
                GLib.source_remove(self._play_watch)
            except Exception:  # noqa: BLE001
                pass
            self._play_watch = 0
        proc = self._preview_proc
        self._preview_proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            self.play_btn.set_label("Play selection")
        except Exception:  # noqa: BLE001
            pass

    def _on_key(self, _c, keyval: int, _kc: int, state: Gdk.ModifierType) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._close()
            return True
        if keyval == Gdk.KEY_space:
            focus = self.get_focus()
            if not isinstance(focus, Gtk.SpinButton):
                self._toggle_play()
                return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._save_as()
            else:
                self._save_original()
            return True
        return False

    def _save_original(self) -> None:
        self._stop_preview()
        try:
            apply_audio_crop_to_item(
                self._library_root, self._item, self._start, self._end
            )
        except WriteError as exc:
            self._error_toast(str(exc))
            return
        if self._on_done:
            self._on_done("overwrite", self._item)
        self._close()

    def _save_as(self) -> None:
        self._stop_preview()
        try:
            new_item = save_audio_crop_as_new_item(
                self._library_root, self._item, self._start, self._end
            )
        except WriteError as exc:
            self._error_toast(str(exc))
            return
        if self._on_done:
            self._on_done("new", new_item)
        self._close()

    def _error_toast(self, text: str) -> None:
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
        self._stop_preview()
        if getattr(self.get_transient_for(), "_open_dialog", None) is self:
            try:
                self.get_transient_for()._open_dialog = None  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        self.destroy()
        if self._on_close:
            try:
                self._on_close()
            except Exception:  # noqa: BLE001
                pass


def open_audio_crop_dialog(
    parent: Gtk.Window,
    item: Any,
    *,
    library_root: Path | None = None,
    on_done: Callable[[str, Any], None] | None = None,
    on_close: Callable[[], None] | None = None,
) -> AudioCropWindow | None:
    if not getattr(item, "is_audio", False):
        if hasattr(parent, "_toast"):
            parent._toast("Not an audio file")  # noqa: SLF001
        return None
    path = getattr(item, "path", None)
    if path is None or not Path(path).is_file():
        if hasattr(parent, "_toast"):
            parent._toast("Audio file missing")  # noqa: SLF001
        return None
    try:
        win = AudioCropWindow(
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
