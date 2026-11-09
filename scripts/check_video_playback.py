#!/usr/bin/env python3
"""Desktop smoke test: python scripts/check_video_playback.py VIDEO [CYCLES].

Uses only the supplied video and a temporary window, never an Eagle library.
A crash, backend error, or playback that does not advance fails the check.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsk_compat import configure_gsk_renderer

configure_gsk_renderer()
import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk

path = Path(sys.argv[1]).resolve()
if not path.is_file():
    raise SystemExit(f'Missing video: {path}')
cycles = int(sys.argv[2]) if len(sys.argv) > 2 else 12
app = Gtk.Application(application_id='local.eagle.VideoSmoke',
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
state = {'cycle': 0, 'failed': False}


def activate(application):
    window = Gtk.ApplicationWindow(application=application, title='Eagle video playback check')
    window.set_default_size(640, 480)
    video = Gtk.Video()
    window.set_child(video)
    window.present()

    def open_video():
        media = Gtk.MediaFile.new_for_filename(str(path))
        media.set_muted(True)
        video.set_media_stream(media)
        media.play()

        def check():
            error = media.get_error()
            timestamp = media.get_timestamp()
            if error or timestamp <= 0:
                print(f'FAIL: error={error}, timestamp={timestamp}', flush=True)
                state['failed'] = True
                application.quit()
                return False
            media.pause()
            video.set_media_stream(None)
            media.clear()
            state['cycle'] += 1
            print(f"PASS cycle {state['cycle']}: timestamp={timestamp}", flush=True)
            if state['cycle'] >= cycles:
                window.destroy()
                application.quit()
            else:
                GLib.timeout_add(100, open_video)
            return False

        GLib.timeout_add(2500, check)
        return False

    open_video()


app.connect('activate', activate)
app.run([])
raise SystemExit(1 if state['failed'] or state['cycle'] != cycles else 0)
