"""Collapse preference survives restarts without querying during state restore."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import EagleBrowseWindow


def window():
    win = SimpleNamespace(
        _library_ready=True, _collapse_groups=False, _smart_expanded=set(),
        _folders_section_expanded=False, current_smart_folder_id=None,
        current_folder_id=None, _special_view=None,
        library=SimpleNamespace(smart_folders_by_id={}, folders_by_id={}),
        is_viewer_open=Mock(return_value=False), refresh_items=Mock(),
    )
    win._save_sidebar_state = lambda: EagleBrowseWindow._save_sidebar_state(win)
    win.collapse_groups_btn = Mock()
    def set_active(active):
        button = Mock()
        button.get_active.return_value = active
        EagleBrowseWindow._on_collapse_groups_toggled(win, button)
    win.collapse_groups_btn.set_active.side_effect = set_active
    return win


class CollapseGroupsStateTest(unittest.TestCase):
    def test_toggle_saves_both_states_and_new_window_restores_without_refresh(self):
        with tempfile.TemporaryDirectory() as d, patch('app._UI_STATE_PATH', Path(d) / 'state.json'):
            for active in (True, False):
                win = window()
                win._collapse_groups = not active
                win.collapse_groups_btn.set_active(active)
                restored = window()
                EagleBrowseWindow._load_sidebar_state(restored)
                self.assertEqual(restored._collapse_groups, active)
                restored.collapse_groups_btn.set_active.assert_called_once_with(active)
                restored.refresh_items.assert_not_called()
                win.refresh_items.assert_called_once_with(reset_selection=True)

    def test_old_or_invalid_preference_defaults_off(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            with patch('app._UI_STATE_PATH', path):
                for data in ({}, {'collapse_groups': 'false'}, {'collapse_groups': 1}):
                    path.write_text(json.dumps(data))
                    win = window()
                    EagleBrowseWindow._load_sidebar_state(win)
                    self.assertFalse(win._collapse_groups)
                    self.assertEqual(json.loads(path.read_text()), data)

    def test_missing_or_corrupt_state_keeps_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            with patch('app._UI_STATE_PATH', path):
                win = window()
                EagleBrowseWindow._load_sidebar_state(win)
                self.assertFalse(win._collapse_groups)
                path.write_text('{broken')
                EagleBrowseWindow._load_sidebar_state(win)
                self.assertFalse(win._collapse_groups)
