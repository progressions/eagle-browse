"""Collapsed groups expand only tag/folder targets, retaining mixed-value deltas."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import EagleBrowseWindow
from library import EagleLibrary


def item(i, tags=(), folders=(), deleted=False):
    return SimpleNamespace(id=str(i), tags=list(tags), folders=list(folders),
                           is_deleted=deleted)


class GroupMetadataEditsTest(unittest.TestCase):
    def setUp(self):
        self.a = item(1, ['set:a', 'shared', 'mixed'], ['common', 'partial'])
        self.b = item(2, ['set:a', 'shared'], ['common'])
        self.c = item(3, ['set:b', 'shared'], ['common'])
        self.d = item(4, ['set:b', 'shared'], ['common'])
        self.single = item(5, ['shared'], ['common'])
        self.deleted = item(6, ['set:a'], deleted=True)
        library = EagleLibrary('/tmp/unused-group-metadata')
        library.items = [self.a, self.b, self.c, self.d, self.single, self.deleted]
        library.folder_paths = {'common': 'Common', 'partial': 'Partial'}
        library.all_tags = Mock(return_value=['shared', 'mixed'])
        library.update_items_batch = Mock(return_value=(5, []))
        library.auto_tags_for_folders = Mock(return_value=[])
        self.win = SimpleNamespace(
            _collapse_groups=True, _special_view=None, _metadata_batch_busy=False,
            is_viewer_open=Mock(return_value=False), library=library,
            _effective_hand_off_items=Mock(return_value=[self.a, self.c, self.single]),
            _toast=Mock(), refresh_items=Mock(), _refresh_special_counts=Mock(),
            _grid_scroll_value=Mock(return_value=0),
        )
        self.win._metadata_edit_items = lambda: EagleBrowseWindow._metadata_edit_items(self.win)

        def run_batch(ids, *, description, picker, on_done, **changes):
            result = self.win.library.update_items_batch(ids, **changes)
            on_done(*result)
            return True
        self.win._run_metadata_batch = run_batch

    def test_expands_groups_beyond_visible_selection_and_deduplicates(self):
        self.win._effective_hand_off_items.return_value.append(self.b)
        self.assertEqual([i.id for i in self.win._metadata_edit_items()], ['1', '2', '3', '4', '5'])

    def test_individual_views_do_not_expand(self):
        for attr, value in [('_collapse_groups', False), ('_special_view', 'set')]:
            with self.subTest(attr=attr), patch.object(self.win, attr, value):
                self.assertEqual(self.win._metadata_edit_items(), [self.a, self.c, self.single])
        self.win.is_viewer_open.return_value = True
        self.assertEqual(self.win._metadata_edit_items(), [self.a, self.c, self.single])

    def test_empty_selection(self):
        self.win._effective_hand_off_items.return_value = []
        self.assertEqual(self.win._metadata_edit_items(), [])

    def test_overlapping_sets_count_member_once(self):
        self.b.tags.append('set:b')
        self.assertEqual([i.id for i in self.win._metadata_edit_items()], ['1', '2', '3', '4', '5'])

    def test_editors_show_mixed_values_and_write_only_toggled_value(self):
        for method, active, partial, value, add, remove in [
            ('edit_tags_dialog', {'shared'}, {'mixed'}, 'mixed', 'add_tags', 'remove_tags'),
            ('edit_folders_dialog', {'Common'}, {'Partial'}, 'partial', 'add_folders', 'remove_folders'),
        ]:
            with self.subTest(method=method), patch('picker.TogglePicker') as picker, patch('picker.load_recent', return_value=[]):
                self.win.library.update_items_batch.reset_mock()
                getattr(EagleBrowseWindow, method)(self.win)
                options = picker.call_args.kwargs
                self.assertEqual(options['active'], active)
                self.assertEqual(options['partial'], partial)
                self.assertTrue(options['subtitle'].startswith('5 item(s)'))
                self.win.library.update_items_batch.assert_not_called()
                label = 'Partial' if method == 'edit_folders_dialog' else value
                self.assertFalse(options['on_toggle'](label, True))
                self.win.library.update_items_batch.assert_called_once_with(
                    ['1', '2', '3', '4', '5'], **{add: [value]})
                self.win.library.update_items_batch.reset_mock()
                self.assertFalse(options['on_toggle'](label, False))
                self.win.library.update_items_batch.assert_called_once_with(
                    ['1', '2', '3', '4', '5'], **{remove: [value]})
