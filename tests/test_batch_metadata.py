"""Batch writes preserve metadata safety without rewriting the shared index per item."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from library import EagleLibrary, Folder, Item
import write


def make_library(root, count=3):
    library = EagleLibrary(root)
    for number in range(count):
        iid = f'M{number:012d}'
        item_dir = root / 'images' / f'{iid}.info'
        item_dir.mkdir(parents=True)
        data = dict(id=iid, name=iid, ext='png', folders=[], tags=[],
                    annotation='', modificationTime=1, lastModified=1)
        (item_dir / 'metadata.json').write_text(json.dumps(data))
        item = Item(id=iid, name=iid, ext='png', tags=[], folders=[],
                    path=item_dir / f'{iid}.png', thumb=None, is_deleted=False,
                    size=1, width=1, height=1, annotation='', modification_time=1,
                    item_dir=item_dir, name_lower=iid.lower(), ext_lower='png')
        library.items.append(item)
        library.items_by_id[iid] = item
    (root / 'mtime.json').write_text(json.dumps({it.id: 1 for it in library.items}))
    return library


class BatchMetadataTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.library = make_library(self.root)
        self.ids = list(self.library.items_by_id)

    def metadata(self, iid):
        return json.loads((self.library.items_by_id[iid].item_dir / 'metadata.json').read_text())

    def index(self):
        return json.loads((self.root / 'mtime.json').read_text())

    def test_one_index_write_and_backup_under_lock(self):
        original = write.atomic_write_json
        def save(path, data):
            self.assertTrue((self.root / write.LOCK_FILENAME).exists())
            original(path, data)
        with patch('write.atomic_write_json', side_effect=save) as atomic, \
             patch('write.backup_file', wraps=write.backup_file) as backup:
            self.assertEqual(self.library.update_items_batch(self.ids, add_folders=['f']), (3, []))
        self.assertEqual(sum(c.args[0] == self.root / 'mtime.json' for c in atomic.call_args_list), 1)
        self.assertEqual(sum(c.args[1] == self.root / 'mtime.json' for c in backup.call_args_list), 1)
        self.assertEqual(atomic.call_count, 4)
        self.assertEqual(backup.call_count, 4)
        for iid in self.ids:
            self.assertEqual(self.index()[iid], self.metadata(iid)['modificationTime'])
            self.assertEqual(self.metadata(iid)['folders'], ['f'])
        self.assertFalse((self.root / write.LOCK_FILENAME).exists())
        self.assertEqual(len(list((self.root / write.BACKUP_DIRNAME).rglob('*.bak'))), 4)

    def test_noop_does_not_save_backup_or_change_timestamps(self):
        self.library.update_items_batch(self.ids, add_folders=['f'], add_tags=['tag'], star=3, annotation='note')
        before = [self.metadata(iid) for iid in self.ids]
        with patch('write.atomic_write_json') as atomic, patch('write.backup_file') as backup:
            self.assertEqual(self.library.update_items_batch(
                self.ids, add_folders=['f'], remove_folders=['absent'], add_tags=['tag'],
                remove_tags=['absent'], star=3, annotation='note'), (3, []))
        atomic.assert_not_called()
        backup.assert_not_called()
        self.assertEqual(before, [self.metadata(iid) for iid in self.ids])

    def test_mixed_membership_preserves_other_folders_and_inherited_auto_tags(self):
        self.library.folders_by_id = {
            'parent': Folder(id='parent', name='Parent', tags=['parent-tag']),
            'f': Folder(id='f', name='Folder', parent_id='parent', tags=['child-tag']),
        }
        self.library.update_item(self.ids[0], add_folders=['f', 'other'])
        # Existing membership must not re-add a deliberately removed auto-tag.
        self.library.update_item(self.ids[0], remove_tags=['child-tag'])
        with patch('write.save_item_metadata', wraps=write.save_item_metadata) as save:
            self.assertEqual(self.library.update_items_batch(self.ids, add_folders=['f']), (3, []))
        self.assertEqual(save.call_count, 2)
        self.assertEqual(self.metadata(self.ids[0])['folders'], ['f', 'other'])
        self.assertEqual(self.metadata(self.ids[0])['tags'], ['parent-tag'])
        for iid in self.ids[1:]:
            self.assertEqual(self.metadata(iid)['tags'], ['parent-tag', 'child-tag'])
        self.library.update_items_batch(self.ids, remove_folders=['f'])
        self.assertEqual(self.metadata(self.ids[1])['tags'], ['parent-tag', 'child-tag'])
        self.assertEqual(self.metadata(self.ids[0])['folders'], ['other'])

    def test_partial_failure_indexes_only_successes_and_reports_progress(self):
        original = write.atomic_write_json
        failed_path = self.library.items[1].item_dir / 'metadata.json'
        def save(path, data):
            if path == failed_path:
                raise OSError('disk full')
            original(path, data)
        progress = Mock()
        with patch('write.atomic_write_json', side_effect=save):
            ok, errors = self.library.update_items_batch(self.ids, add_folders=['f'], progress=progress)
        self.assertEqual(ok, 2)
        self.assertEqual(len(errors), 1)
        self.assertIn('disk full', errors[0])
        self.assertEqual(self.index()[self.ids[1]], 1)
        self.assertEqual(self.library.items[1].folders, [])
        self.assertEqual([c.args for c in progress.call_args_list], [(1, 3), (2, 3), (3, 3)])
        self.assertEqual([it.id for it in self.library.query(folder_id='f')], [self.ids[0], self.ids[2]])

    def test_interruption_flushes_completed_entries_before_unlocking(self):
        def interrupted(done, total):
            raise RuntimeError('stop after first item')
        with self.assertRaisesRegex(RuntimeError, 'stop after first'):
            self.library.update_items_batch(self.ids, add_tags=['tag'], progress=interrupted)
        self.assertEqual(self.index()[self.ids[0]], self.metadata(self.ids[0])['modificationTime'])
        self.assertEqual(self.index()[self.ids[1]], 1)
        self.assertFalse((self.root / write.LOCK_FILENAME).exists())

    def test_lock_failure_and_unknown_id(self):
        with write.write_session(self.root):
            ok, errors = self.library.update_items_batch(self.ids, add_tags=['tag'])
        self.assertEqual(ok, 0)
        self.assertIn('locked', errors[0])
        ok, errors = self.library.update_items_batch(['unknown', self.ids[0]], add_tags=['tag'])
        self.assertEqual(ok, 1)
        self.assertIn('Unknown item', errors[0])

    def test_optional_or_invalid_index_stays_untouched(self):
        path = self.root / 'mtime.json'
        for content in (None, '{bad', '[]'):
            with self.subTest(content=content):
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(content)
                self.assertEqual(self.library.update_items_batch(self.ids, annotation=str(content)), (3, []))
                self.assertEqual(path.read_text() if path.exists() else None, content)

    def test_index_failure_remains_best_effort(self):
        original = write.atomic_write_json
        def save(path, data):
            if path == self.root / 'mtime.json':
                raise OSError('index unavailable')
            original(path, data)
        with patch('write.atomic_write_json', side_effect=save):
            self.assertEqual(self.library.update_items_batch(self.ids, add_tags=['tag']), (3, []))
        self.assertEqual(self.metadata(self.ids[0])['tags'], ['tag'])
        self.assertFalse((self.root / write.LOCK_FILENAME).exists())

    def test_single_item_still_updates_index_and_uses_disk_for_noop_check(self):
        item = self.library.items[0]
        item.folders = ['stale']
        self.library.update_item(item.id, add_folders=['stale'])
        self.assertEqual(self.metadata(item.id)['folders'], ['stale'])
        self.assertEqual(self.index()[item.id], item.modification_time)
        item.folders = []
        with patch('write.save_item_metadata') as save:
            self.library.update_item(item.id, add_folders=['stale'])
        save.assert_not_called()
        self.assertEqual(item.folder_set, frozenset(['stale']))

    def test_delete_restore_each_flush_once(self):
        for deleted in (True, False):
            with patch('write._touch_mtime_index_batch', wraps=write._touch_mtime_index_batch) as touch:
                self.assertEqual(self.library.set_items_deleted(self.ids, deleted=deleted), (self.ids, []))
            touch.assert_called_once()
            self.assertEqual(len(touch.call_args.args[1]), 3)

    def test_empty_and_all_failed_batches_do_not_touch_index(self):
        with patch('write._touch_mtime_index_batch') as touch:
            self.assertEqual(self.library.update_items_batch([], add_tags=['tag']), (0, []))
            self.assertEqual(self.library.update_items_batch(['unknown'], add_tags=['tag'])[0], 0)
        touch.assert_not_called()

    def test_query_during_batch_is_invalidated_by_later_items(self):
        seen = []
        def progress(done, total):
            seen.append(len(self.library.query(folder_id='f')))
        self.library.update_items_batch(self.ids, add_folders=['f'], progress=progress)
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(len(self.library.query(folder_id='f')), 3)
