"""Worker/UI handoff, repeat edits, partial results and picker lifecycle."""
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import EagleBrowseWindow
from picker import TogglePicker
from shutdown_gate import wrap_idle_callback


class MetadataBatchUITest(unittest.TestCase):
    def setUp(self):
        self.callbacks = queue.Queue()
        self.win = SimpleNamespace(
            _shutdown=threading.Event(), _metadata_batch_busy=False,
            library=SimpleNamespace(update_items_batch=Mock(return_value=(2, []))),
            _toast_overlay=Mock(), _toast=Mock(), refresh_items=Mock(),
            _refresh_special_counts=Mock(), _sync_star_overlays=Mock(),
            _update_path_label=Mock(), update_inspector=Mock(),
        )
        self.win._ui_idle = lambda fn, *args: self.callbacks.put(
            (wrap_idle_callback(self.win._shutdown, fn), args, fn.__name__)
        )
        self.toast_patch = patch('app.Adw.Toast')
        self.toast = self.toast_patch.start().new.return_value
        self.addCleanup(self.toast_patch.stop)
        self.threads = []
        thread_type = threading.Thread
        def thread(**kwargs):
            result = thread_type(**kwargs)
            self.threads.append(result)
            return result
        self.thread_patch = patch('app.threading.Thread', side_effect=thread)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.addCleanup(self.join_threads)

    def join_threads(self):
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def run_batch(self, **kwargs):
        return EagleBrowseWindow._run_metadata_batch(
            self.win, ['a', 'b'], description='Add folder', add_folders=['f'], **kwargs
        )

    def finish(self):
        while True:
            fn, args, name = self.callbacks.get(timeout=5)
            fn(*args)
            if name == 'complete':
                break
        self.join_threads()

    def test_worker_does_not_block_ui_and_rejects_overlapping_batch(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        main_thread = threading.get_ident()
        def update(ids, *, progress, **changes):
            self.assertNotEqual(threading.get_ident(), main_thread)
            started.set()
            if not release.wait(5):
                raise RuntimeError('not released')
            progress(1, 2)
            progress(2, 2)
            return 2, []
        self.win.library.update_items_batch.side_effect = update
        picker, done = Mock(), Mock()
        self.assertTrue(self.run_batch(picker=picker, on_done=done))
        self.assertTrue(started.wait(5))
        self.assertFalse(self.threads[0].daemon)
        self.assertFalse(self.run_batch())
        done.assert_not_called()
        self.win.refresh_items.assert_not_called()
        release.set()
        self.finish()
        self.assertFalse(self.win._metadata_batch_busy)
        picker.set_busy.assert_any_call(True, 'Updating · 2/2')
        picker.set_busy.assert_any_call(False)
        done.assert_called_once_with(2, [])
        self.toast.dismiss.assert_called_once()
        self.win.refresh_items.assert_called_once()

    def test_partial_failure_reports_counts_and_details(self):
        self.win.library.update_items_batch.return_value = (1, ['b: disk full'])
        done = Mock()
        self.run_batch(on_done=done)
        self.finish()
        done.assert_called_once_with(1, ['b: disk full'])
        message = self.win._toast.call_args.args[0]
        self.assertIn('1/2 completed', message)
        self.assertIn('disk full', message)

    def test_unexpected_failure_releases_ui_busy_state(self):
        self.win.library.update_items_batch.side_effect = RuntimeError('failure')
        picker = Mock()
        self.run_batch(picker=picker)
        self.finish()
        self.assertFalse(self.win._metadata_batch_busy)
        picker.set_busy.assert_any_call(False)
        self.assertIn('Update interrupted: failure', self.win._toast.call_args.args[0])

    def test_shutdown_suppresses_callbacks_but_worker_finishes(self):
        started, release, saved = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def update(ids, *, progress, **changes):
            started.set()
            if not release.wait(5):
                raise RuntimeError('not released')
            progress(2, 2)
            saved.set()
            return 2, []
        self.win.library.update_items_batch.side_effect = update
        done = Mock()
        self.run_batch(on_done=done)
        self.assertTrue(started.wait(5))
        self.win._shutdown.set()
        release.set()
        self.finish()
        self.assertTrue(saved.is_set())
        done.assert_not_called()
        self.win.refresh_items.assert_not_called()
        self.toast.set_title.assert_not_called()
        self.assertFalse(self.run_batch())

    def test_progress_is_throttled(self):
        def update(ids, *, progress, **changes):
            for n in range(1, 501):
                progress(n, 500)
            return 2, []
        self.win.library.update_items_batch.side_effect = update
        with patch('app.time.monotonic', return_value=10):
            self.run_batch()
            self.finish()
        self.assertEqual(self.toast.set_title.call_count, 2)


class PickerBatchStateTest(unittest.TestCase):
    def picker(self):
        return SimpleNamespace(
            _closing=False, _busy=False, _active={'f'}, _partial=set(), _all=['f'],
            _excluded=set(), _on_toggle=Mock(), _canonical_value=lambda v: v,
            _finish_toggle_ui=Mock(), note_toggled=Mock(),
        )

    def test_partial_result_is_mixed_instead_of_checked(self):
        picker = self.picker()
        TogglePicker.note_membership(picker, 'f', 1, 2)
        self.assertNotIn('f', picker._active)
        self.assertIn('f', picker._partial)
        picker.note_toggled.assert_not_called()

    def test_uniform_result_updates_checked_state(self):
        picker = self.picker()
        TogglePicker.note_membership(picker, 'f', 2, 2)
        picker.note_toggled.assert_called_once_with('f', True)
        TogglePicker.note_membership(picker, 'f', 0, 2)
        self.assertEqual(picker.note_toggled.call_args.args, ('f', False))

    def test_busy_and_closed_pickers_ignore_toggles(self):
        for field in ('_busy', '_closing'):
            picker = self.picker()
            setattr(picker, field, True)
            TogglePicker._toggle_value(picker, 'f')
            picker._on_toggle.assert_not_called()

    def test_closed_picker_ignores_completion(self):
        picker = self.picker()
        picker._closing = True
        TogglePicker.note_membership(picker, 'f', 1, 2)
        picker._finish_toggle_ui.assert_not_called()
        picker.note_toggled.assert_not_called()
