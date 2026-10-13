"""Real GTK dialog exercise with a temporary library (opt-in display)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app import EagleBrowseWindow, Gtk
from library import EagleLibrary


@unittest.skipUnless(os.environ.get('EAGLE_GTK_INTEGRATION') == '1', 'requires GTK display')
class CreateFolderDialogTest(unittest.TestCase):
    def test_cancel_validation_and_create(self):
        Gtk.init()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'metadata.json').write_text('{"folders": []}')
            win = Gtk.Window()
            win.library = EagleLibrary(root)
            win.library.load()
            win._remember_dialog = lambda dialog: setattr(win, 'dialog', dialog)
            win._repopulate_sidebar_keep_selection = Mock()
            win._toast = Mock()
            try:
                EagleBrowseWindow.create_folder_dialog(win)
                box = win.dialog.get_child()
                entry = box.get_first_child().get_next_sibling()
                buttons = box.get_last_child()
                create = buttons.get_last_child()
                self.assertFalse(create.get_sensitive())
                entry.set_text('Cancelled')
                buttons.get_first_child().emit('clicked')
                self.assertEqual(json.loads((root / 'metadata.json').read_text())['folders'], [])

                win.library.create_folder('Existing')
                EagleBrowseWindow.create_folder_dialog(win)
                box = win.dialog.get_child()
                entry = box.get_first_child().get_next_sibling()
                create = box.get_last_child().get_last_child()
                entry.set_text('existing')
                create.emit('clicked')
                self.assertIn('already exists', entry.get_next_sibling().get_text())
                self.assertTrue(win.dialog.get_visible())
                entry.set_text('Summer outfits')
                self.assertTrue(create.get_sensitive())
                create.emit('clicked')
                self.assertIn('Summer outfits', win.library.folder_paths.values())
                win._repopulate_sidebar_keep_selection.assert_called_once()
                self.assertTrue(win._folders_section_expanded)
            finally:
                win.dialog.destroy()
                win.destroy()


if __name__ == '__main__':
    unittest.main()
