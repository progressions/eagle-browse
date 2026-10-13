"""Escape closes viewer/group layers without following viewer history backwards."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import EagleBrowseWindow, _ViewLoc


class GroupEscapeTest(unittest.TestCase):
    def setUp(self):
        self.parent = _ViewLoc('videos', None, None, None, True, None)
        self.group = _ViewLoc(None, None, 'set', 'set:a', True, None)
        self.win = SimpleNamespace(
            _handling_escape=False, _last_escape_mono=0, _special_view='set',
            _set_parent_view=self.parent, _close_open_dialog=Mock(return_value=False),
            is_viewer_open=Mock(return_value=False), close_inline_viewer=Mock(),
            _view_loc=Mock(return_value=self.group), _apply_view_loc=Mock(),
            _record_view_change=Mock(), _leave_set_view=Mock(), nav_back=Mock(),
        )
        self.win._escape_set_view = lambda: EagleBrowseWindow._escape_set_view(self.win)

    def test_video_then_group_escape_returns_to_parent_not_video_history(self):
        self.win._nav_back = [self.parent, self.group, self.group._replace(viewer_id='video')]
        self.win.is_viewer_open.return_value = True
        self.assertTrue(EagleBrowseWindow._handle_escape(self.win))
        self.win.close_inline_viewer.assert_called_once()
        self.win._apply_view_loc.assert_not_called()
        self.win.is_viewer_open.return_value = False
        self.win._last_escape_mono = 0
        self.assertTrue(EagleBrowseWindow._handle_escape(self.win))
        self.win._apply_view_loc.assert_called_once_with(self.parent)
        self.win.nav_back.assert_not_called()
        self.win._record_view_change.assert_called_once_with(self.group)

    def test_dialog_closes_before_group(self):
        self.win._close_open_dialog.return_value = True
        self.assertTrue(EagleBrowseWindow._handle_escape(self.win))
        self.win._apply_view_loc.assert_not_called()

    def test_missing_parent_falls_back_to_library(self):
        self.win._set_parent_view = None
        self.win._escape_set_view()
        self.win._leave_set_view.assert_called_once()

    def test_open_group_remembers_origin_without_its_viewer(self):
        self.win._view_loc.return_value = self.parent._replace(viewer_id='old-video')
        self.win._rebuild_set_counts = Mock()
        self.win.folder_list = Mock()
        self.win._unlock_sidebar_nav = Mock()
        self.win.refresh_items = Mock()
        self.win._set_counts = {}
        self.win._toast = Mock()
        with patch('app.GLib.idle_add'):
            EagleBrowseWindow.open_set_view(self.win, 'set:a')
        self.assertEqual(self.win._set_parent_view, self.parent)
        # Opening another group while in the set keeps the outer parent.
        self.win._view_loc.return_value = self.group
        with patch('app.GLib.idle_add'):
            EagleBrowseWindow.open_set_view(self.win, 'set:b')
        self.assertEqual(self.win._set_parent_view, self.parent)
