"""Category creation, validation, metadata preservation and persistent filing."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from library import EagleLibrary
from write import WriteError


class CreateFolderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.meta = self.root / 'metadata.json'
        self.meta.write_text(json.dumps({'folders': [], 'smartFolders': [], 'custom': 42}))
        item = self.root / 'images' / 'ITEM.info'
        item.mkdir(parents=True)
        (item / 'photo.jpg').write_bytes(b'image')
        (item / 'metadata.json').write_text(json.dumps(
            {'id': 'ITEM', 'name': 'photo', 'ext': 'jpg', 'folders': [], 'tags': []}))
        self.library = EagleLibrary(self.root)
        self.library.load()

    def test_create_file_and_reload(self):
        folder = self.library.create_folder('  Summer outfits  ')
        self.assertEqual(self.library.folder_paths[folder.id], 'Summer outfits')
        self.assertEqual(self.library.update_items_batch(['ITEM'], add_folders=[folder.id]), (1, []))
        fresh = EagleLibrary(self.root)
        fresh.load()
        self.assertEqual(fresh.folder_paths[folder.id], 'Summer outfits')
        self.assertIn(folder.id, fresh.items_by_id['ITEM'].folders)
        self.assertEqual(json.loads(self.meta.read_text())['custom'], 42)
        self.assertTrue(list((self.root / 'backup/eagle-browse-writes').rglob('metadata.json.*.bak')))

    def test_empty_and_duplicate_names_do_not_write(self):
        self.library.create_folder('Summer')
        before = self.meta.read_bytes()
        for name in (' ', '', ' summer ', 'SUMMER'):
            with self.subTest(name=name), self.assertRaises(WriteError):
                self.library.create_folder(name)
            self.assertEqual(self.meta.read_bytes(), before)

    def test_creation_reads_latest_metadata(self):
        meta = json.loads(self.meta.read_text())
        meta['folders'].append({'id': 'EXTERNAL', 'name': 'External', 'children': []})
        self.meta.write_text(json.dumps(meta))
        self.library.create_folder('New')
        self.assertEqual(set(self.library.folder_paths.values()), {'External', 'New'})

    def test_write_failure_does_not_add_phantom_folder(self):
        before = self.meta.read_bytes()
        with patch('write.atomic_write_json', side_effect=OSError('read only')):
            with self.assertRaises(OSError):
                self.library.create_folder('New')
        self.assertEqual(self.library.folders, [])
        self.assertEqual(self.meta.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
