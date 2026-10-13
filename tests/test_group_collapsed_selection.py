"""Grouping collapsed thumbnails must not split the sets they represent."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app import EagleBrowseWindow
from library import EagleLibrary
from write import apply_tags


def item(i, tag=None, deleted=False):
    return SimpleNamespace(id=str(i), tags=['keep'] + ([tag] if tag else []),
                           is_deleted=deleted)


class GroupCollapsedSelectionTest(unittest.TestCase):
    def window(self, selected, members, collapsed=True):
        library = EagleLibrary('/tmp/unused-group-selection')
        library.items = members
        win = SimpleNamespace(
            library=library, _collapse_groups=collapsed, _special_view=None,
            is_viewer_open=Mock(return_value=False),
            _effective_hand_off_items=Mock(return_value=selected), _toast=Mock(),
            _rebuild_set_counts=Mock(),
        )
        win._metadata_edit_items = lambda: EagleBrowseWindow._metadata_edit_items(win)
        def batch(ids, *, description, on_done, **changes):
            self.assertEqual(len(ids), len(set(ids)))
            for member in members:
                if member.id in ids:
                    data = {'tags': list(member.tags)}
                    apply_tags(data, **changes)
                    member.tags = data['tags']
            on_done(len(ids), [])
        win._run_metadata_batch = Mock(side_effect=batch)
        return win

    def test_combines_21_and_5_members_in_either_display_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                a = [item(f'a{i}', 'set:a') for i in range(21)]
                b = [item(f'b{i}', 'set:b') for i in range(5)]
                deleted = item('deleted', 'set:b', deleted=True)
                selected = [a[-1], b[-1]]
                if reverse:
                    selected.reverse()
                win = self.window(selected, a + b + [deleted])
                EagleBrowseWindow.group_selection_into_set(win)
                target = 'set:b' if reverse else 'set:a'
                self.assertTrue(all(m.tags == ['keep', target] for m in a + b))
                self.assertEqual(deleted.tags, ['keep', 'set:b'])
                win._rebuild_set_counts.assert_called_once_with(force=True)

    def test_group_plus_ungrouped_asset_and_overlapping_selection(self):
        a, b, single = item('a', 'set:a'), item('b', 'set:a'), item('single')
        win = self.window([single, a, b], [a, b, single])
        EagleBrowseWindow.group_selection_into_set(win)
        self.assertTrue(all(m.tags == ['keep', 'set:a'] for m in [a, b, single]))
        self.assertEqual(win._run_metadata_batch.call_args.args[0], ['single'])

    def test_expanded_view_moves_only_selected_asset(self):
        a, b, c = item('a', 'set:a'), item('b', 'set:b'), item('c', 'set:b')
        win = self.window([a, b], [a, b, c], collapsed=False)
        EagleBrowseWindow.group_selection_into_set(win)
        self.assertEqual(b.tags, ['keep', 'set:a'])
        self.assertEqual(c.tags, ['keep', 'set:b'])

    def test_one_collapsed_group_does_not_write(self):
        a, b = item('a', 'set:a'), item('b', 'set:a')
        win = self.window([a], [a, b])
        EagleBrowseWindow.group_selection_into_set(win)
        win._run_metadata_batch.assert_not_called()
        win._toast.assert_called_once_with('Already grouped')
