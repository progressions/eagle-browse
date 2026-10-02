"""Metadata refreshes preserve the cursor position in uncollapsed smart folders."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app import EagleBrowseWindow as Window, Gio, Gtk, ItemObject
from library import EagleLibrary
from test_external_refresh import TempLibrary


class EditSelectionTest(unittest.TestCase):
    def setUp(self):
        tmp = TempLibrary()
        self.addCleanup(tmp.cleanup)
        for i in range(3):
            tmp.external_edit(f'ITEM{i}', tags=['ready'])
        lib = EagleLibrary(tmp.root)
        lib.load()
        lib.smart_folders_by_id['sf-ready'].inherited_conditions.append({
            'match': 'AND', 'boolean': 'TRUE',
            'rules': [{'property': 'rating', 'method': 'unequal', 'value': '1'}],
        })
        items = sorted(lib.items, key=lambda it: it.id)
        store = Gio.ListStore(item_type=ItemObject)
        for it in items:
            store.append(ItemObject(it))
        selection = Gtk.SingleSelection(model=store, autoselect=False, can_unselect=True)
        selection.set_selected(1)
        self.callbacks = []
        w = self.win = SimpleNamespace(
            library=lib, store=store, selection=selection,
            _shutdown=threading.Event(), _query_gen=0,
            current_folder_id=None, current_smart_folder_id='sf-ready',
            _special_view=None, _collapse_groups=False, include_descendants=True,
            _filter_text='', _view_filters=SimpleNamespace(active=lambda: False),
            _scope_label=lambda: 'Eunbi/images', status_left=Mock(),
            _marked={'ITEM1'}, selected_item=items[1], _items=items,
            _all_items=items, _last_focus_idx=1, _keep_grid_unselected=False,
            _grid_has_focus=True, _smart_counts={}, _special_counts={},
            _grid_scroll_value=lambda: 120, _cancel_scroll_restore=Mock(),
            _sort_items=lambda found: sorted(found, key=lambda it: it.id),
            _rebuild_set_counts=Mock(), _update_smart_count_label=Mock(),
            _rebuild_scope_text=Mock(), _refresh_status=Mock(),
            _update_path_label=Mock(), _rebuild_filter_chips=Mock(),
            _restore_grid_scroll=Mock(), _scroll_grid_to_top=Mock(),
            _set_grid_scroll_value=Mock(), update_inspector=Mock(),
            _ui_idle=lambda fn: self.callbacks.append(fn),
            _query_worker=SimpleNamespace(submit=lambda fn: fn(threading.Event())),
        )
        selection.connect('notify::selected-item',
                          lambda sel, pspec: Window._on_grid_selection(w, sel, pspec))

    def refresh(self, **kwargs):
        Window.refresh_items(self.win, revalidate=False, **kwargs)
        while self.callbacks:
            self.callbacks.pop(0)()

    def assert_current(self, iid):
        self.assertEqual(self.win.selected_item.id, iid)
        self.assertEqual(self.win.selection.get_selected_item().item.id, iid)
        self.assertIn(iid, self.win._marked)

    def test_one_star_edit_selects_next_at_same_position(self):
        w = self.win
        w.library.update_item('ITEM1', star=1)
        self.refresh()
        self.assert_current('ITEM2')
        self.assertEqual(w._marked, {'ITEM2'})
        self.assertEqual(w.selection.get_selected(), 1)
        self.assertNotIn('ITEM1', [it.id for it in w._items])
        w._update_smart_count_label.assert_called_with('sf-ready', 2)
        w._restore_grid_scroll.assert_called_with(120)
        self.refresh()  # automatic follow-up must not rewind
        self.assert_current('ITEM2')

    def test_tag_edit_selects_next_and_navigation_still_resets(self):
        self.win.library.update_item('ITEM1', set_tags=[])
        self.refresh()
        self.assert_current('ITEM2')
        self.assertEqual(self.win._marked, {'ITEM2'})
        self.refresh(reset_selection=True)
        self.assert_current('ITEM0')

    def test_last_item_falls_back_to_previous(self):
        w = self.win
        w.selection.set_selected(2)
        w._marked = {'ITEM2'}
        w.library.update_item('ITEM2', star=1)
        self.refresh()
        self.assert_current('ITEM1')
        self.assertEqual(w._marked, {'ITEM1'})

    def test_multiple_marks_are_preserved_for_batch_actions(self):
        w = self.win
        w._marked = {'ITEM0', 'ITEM1'}
        w.library.update_item('ITEM1', star=1)
        self.refresh()
        self.assertEqual(w._marked, {'ITEM0', 'ITEM1'})
        self.assertEqual(w.selected_item.id, 'ITEM0')

    def test_scroll_during_query_is_not_rewound(self):
        w = self.win
        Window.refresh_items(w, revalidate=False)
        w._grid_scroll_value = lambda: 480
        while self.callbacks:
            self.callbacks.pop(0)()
        w._restore_grid_scroll.assert_called_with(480)

    def test_empty_result_clears_single_edit_target(self):
        for it in self.win.library.items:
            self.win.library.update_item(it.id, star=1)
        self.refresh()
        self.assertIsNone(self.win.selected_item)
        self.assertFalse(self.win._marked)
        self.assertEqual(self.win.store.get_n_items(), 0)

    def test_matching_edit_preserves_current(self):
        self.win.library.update_item('ITEM1', star=4, add_tags=['extra'])
        self.refresh()
        self.assert_current('ITEM1')

    def test_focus_loss_to_rating_control_keeps_logical_current_item(self):
        w = self.win
        Window._set_grid_focus(w, False)
        self.assertEqual(w.selected_item.id, 'ITEM1')
        w.library.update_item('ITEM1', star=4)
        self.refresh()
        self.assertEqual(w.selected_item.id, 'ITEM1')
        Window._set_grid_focus(w, True)
        self.assert_current('ITEM1')

    def test_navigation_during_query_is_not_rewound(self):
        w = self.win
        Window.refresh_items(w, revalidate=False)
        w.selection.set_selected(2)
        w._marked = {'ITEM2'}
        w._last_focus_idx = 2
        while self.callbacks:
            self.callbacks.pop(0)()
        self.assert_current('ITEM2')

    def test_explicit_unselection_survives_refresh(self):
        w = self.win
        w._keep_grid_unselected = True
        w._marked.clear()
        w.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        self.refresh()
        self.assertIsNone(w.selected_item)
        self.assertFalse(w._marked)

    def test_deleted_item_is_not_retained(self):
        self.win.library.items_by_id['ITEM1'].is_deleted = True
        self.refresh()
        self.assertNotIn('ITEM1', [it.id for it in self.win._items])

    def test_ordinary_refresh_reuses_smart_folder_cache(self):
        w = self.win
        first = w.library.query(smart_folder_id='sf-ready')
        generation = w.library._cache_generation
        self.refresh()
        self.refresh()
        self.assertIs(w.library.query(smart_folder_id='sf-ready'), first)
        self.assertEqual(w.library._cache_generation, generation)
        self.assert_current('ITEM1')

    def test_rating_edit_invalidates_cached_view_before_refresh(self):
        w = self.win
        first = w.library.query(smart_folder_id='sf-ready')
        w.library.update_item('ITEM1', star=1)
        self.refresh()
        self.assertNotIn('ITEM1', [it.id for it in w._items])
        self.assertIsNot(w.library.query(smart_folder_id='sf-ready'), first)
        self.assert_current('ITEM2')
