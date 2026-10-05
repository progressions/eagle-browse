"""Local image handoff and mixed-selection routing without launching editors."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import EagleBrowseWindow as Window


class PhotosuiteHandoffTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.image = self.root / 'an image; $name.png'
        self.image.touch()
        self.win = SimpleNamespace(_toast=Mock())

    def test_paths_remain_separate_arguments(self):
        with patch('app.shutil.which', return_value='/usr/bin/photosuite'),                 patch('app._spawn_detached', return_value=True) as spawn:
            Window.open_images_in_photosuite(self.win, [SimpleNamespace(path=self.image)])
        spawn.assert_called_once_with(['/usr/bin/photosuite', str(self.image)])

    def test_home_bin_fallback_and_missing_file(self):
        (self.root / 'bin').mkdir()
        exe = self.root / 'bin/photosuite'
        exe.touch()
        exe.chmod(0o755)
        with patch('app.shutil.which', return_value=None),                 patch('app.Path.home', return_value=self.root),                 patch('app._spawn_detached', return_value=True) as spawn:
            Window.open_images_in_photosuite(self.win, [
                SimpleNamespace(path=self.image),
                SimpleNamespace(path=self.root / 'missing.png'),
            ])
        spawn.assert_called_once_with([str(exe), str(self.image)])
        self.assertIn('1 missing', self.win._toast.call_args.args[0])

    def test_unavailable_app_and_launch_failure_are_reported(self):
        with patch('app.shutil.which', return_value=None),                 patch('app.Path.home', return_value=self.root),                 patch('app._spawn_detached') as spawn:
            Window.open_images_in_photosuite(self.win, [SimpleNamespace(path=self.image)])
        spawn.assert_not_called()
        self.assertIn('not found', self.win._toast.call_args.args[0])
        with patch('app.shutil.which', return_value='photosuite'),                 patch('app._spawn_detached', return_value=False):
            Window.open_images_in_photosuite(self.win, [SimpleNamespace(path=self.image)])
        self.win._toast.assert_called_with('Could not open PhotoSuite')

    def test_mixed_selection_routes_each_type(self):
        image = SimpleNamespace(is_image=True, is_video=False, is_audio=False)
        video = SimpleNamespace(is_image=False, is_video=True, is_audio=False)
        audio = SimpleNamespace(is_image=False, is_video=False, is_audio=True)
        self.win._effective_hand_off_items = lambda: [image, video, audio]
        self.win.open_images_in_photosuite = Mock()
        self.win.open_selected_in_clip_editor = Mock()
        Window.open_selected_in_editor(self.win)
        self.win.open_images_in_photosuite.assert_called_once_with([image])
        self.win.open_selected_in_clip_editor.assert_called_once_with(items=[video, audio])
