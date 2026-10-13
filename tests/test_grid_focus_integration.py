"""Opt-in full-window GTK regression, using only a temporary Eagle library.

Run with EAGLE_GTK_INTEGRATION=1 and a GTK display (Broadway works headlessly).
"""
import json
import os
import time
import unittest
from unittest.mock import patch

from app import Adw, EagleBrowseWindow, Gio, GLib
from library import EagleLibrary
from test_external_refresh import TempLibrary


@unittest.skipUnless(os.environ.get('EAGLE_GTK_INTEGRATION') == '1',
                     'set EAGLE_GTK_INTEGRATION=1 with a GTK display')
class GridFocusIntegrationTest(unittest.TestCase):
    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(.005)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.pump(.02)
        self.assertTrue(predicate(), 'GTK window did not reach expected state')

    def test_repeated_rating_keeps_successor_focused_and_scrolled(self):
        tmp = TempLibrary(150)
        self.addCleanup(tmp.cleanup)
        meta = tmp.root / 'metadata.json'
        data = json.loads(meta.read_text())
        data['smartFolders'][0]['conditions'] = [{'rules': [
            {'property': 'rating', 'method': 'unequal', 'value': '1'}]}]
        meta.write_text(json.dumps(data))
        app = Adw.Application(application_id='local.eagle.GridFocusTest',
                              flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        # Never touch the user's saved UI state or running-GUI marker.
        with patch('app.mark_gui_running'), patch('app.mark_gui_stopped'), \
                patch.object(EagleBrowseWindow, '_load_sidebar_state'), \
                patch.object(EagleBrowseWindow, '_save_sidebar_state'), \
                patch.object(EagleBrowseWindow, '_start_duration_backfill'):
            w = EagleBrowseWindow(app, EagleLibrary(tmp.root))
            try:
                w.present()
                self.wait_for(lambda: len(w._items) == 150)
                w._collapse_groups = False
                w.current_smart_folder_id = 'sf-ready'
                w.refresh_items(reset_selection=True)
                self.pump(1)
                w._select_index(70, from_click=True)
                self.pump(2)
                original_scroll = w._grid_scroll_value()
                self.assertGreater(original_scroll, 100)
                for rating in (4, 1, 1):
                    with self.subTest(rating=rating):
                        target = w.selected_item.id
                        expected = w._items[71].id if rating == 1 else target
                        w.set_rating(rating)
                        self.wait_for(lambda: not w._metadata_batch_busy)
                        # The bug appeared after the initial selection/scroll
                        # restore had already passed: let GTK layout settle.
                        self.pump(2)
                        self.assertEqual(w.selected_item.id, expected)
                        self.assertEqual(w.selection.get_selected(), 70)
                        self.assertEqual(w._marked, {expected})
                        self.assertAlmostEqual(w._grid_scroll_value(), original_scroll, delta=2)
                        self.assertEqual(w.library.items_by_id[target].star, rating)
                expected = w._items[71].id
                w.move_selection(1)
                self.pump(1)
                self.assertEqual(w.selected_item.id, expected)
                self.assertEqual(w.selection.get_selected(), 71)
            finally:
                w._shutdown_background()
                w.destroy()
                self.pump(.1)

    def test_intake_category_then_tag_keeps_selection_after_focus_returns(self):
        for collapsed, count, grouped in ((False, 1, False), (False, 2, False),
                                          (True, 1, True), (True, 1, False)):
            with self.subTest(collapsed=collapsed, count=count, grouped=grouped):
                tmp = TempLibrary(1 if collapsed and not grouped else 3)
                self.addCleanup(tmp.cleanup)
                meta = tmp.root / 'metadata.json'
                data = json.loads(meta.read_text())
                data['folders'] = [{'id': 'sofie', 'name': 'Sofie', 'children': []}]
                meta.write_text(json.dumps(data))
                if grouped:
                    for iid in ('ITEM0', 'ITEM1'):
                        tmp.external_edit(iid, tags=['set:pair'])
                app = Adw.Application(application_id=f'local.eagle.IntakeSelectionTest.c{int(collapsed)}n{count}g{int(grouped)}',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
                app.register(None)
                with patch('app.mark_gui_running'), patch('app.mark_gui_stopped'), \
                        patch.object(EagleBrowseWindow, '_load_sidebar_state'), \
                        patch.object(EagleBrowseWindow, '_save_sidebar_state'), \
                        patch.object(EagleBrowseWindow, '_start_duration_backfill'), \
                        patch('picker.TogglePicker') as picker, \
                        patch('picker.load_recent', return_value=[]):
                    w = EagleBrowseWindow(app, EagleLibrary(tmp.root))
                    try:
                        w.present()
                        self.wait_for(lambda: bool(w._items))
                        w._collapse_groups = collapsed
                        w.current_smart_folder_id = None
                        w.current_folder_id = None
                        w._special_view = 'uncategorized'
                        w.refresh_items(reset_selection=True)
                        self.pump(.5)
                        idx = next(i for i, it in enumerate(w._items)
                                   if ('set:pair' in it.tags if grouped else it.id == 'ITEM0'))
                        w._select_index(idx, from_click=True)
                        if count == 2:
                            idx = next(i for i, it in enumerate(w._items) if it.id == 'ITEM1')
                            w._select_index(idx, ctrl=True, from_click=True)
                        original_marks = set(w._marked)
                        targets = {it.id for it in w._metadata_edit_items()}
                        w.edit_folders_dialog()
                        folder_callbacks = picker.call_args.kwargs
                        w._set_grid_focus(False)
                        folder_callbacks['on_toggle']('Sofie', True)
                        self.wait_for(lambda: not w._metadata_batch_busy)
                        self.pump(.5)
                        folder_callbacks['on_close']()
                        self.pump(1)
                        self.assertEqual(w._marked, original_marks)
                        self.assertTrue(targets.isdisjoint(it.id for it in w._items))
                        w.edit_tags_dialog()
                        tag_callbacks = picker.call_args.kwargs
                        tag_callbacks['on_toggle']('published-x', True)
                        self.wait_for(lambda: not w._metadata_batch_busy)
                        tag_callbacks['on_close']()
                        self.pump(1)
                        self.assertEqual(w._marked, original_marks)
                        reloaded = EagleLibrary(tmp.root)
                        reloaded.load()
                        self.assertEqual(
                            {it.id for it in reloaded.items if 'published-x' in it.tags},
                            targets,
                        )
                        if w._items:
                            w._select_index(0, from_click=True)
                            self.assertEqual(w._marked, {w._items[0].id})
                    finally:
                        w._shutdown_background()
                        w.destroy()
                        self.pump(.1)
