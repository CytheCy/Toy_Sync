import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from toy_sync.engine import Engine, Journal, pair, scan, validate


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a, self.b = self.root / 'a', self.root / 'b'
        self.a.mkdir()
        self.b.mkdir()
        self.state = self.root / 'state'
        self.engine = Engine(self.state)
        self.p = pair('Test', self.a, self.b)

    def put(self, root, name, text):
        path = root / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(text)

    def run_sync(self):
        return self.engine.run(self.p)

    def test_merge_update_deletion(self):
        self.put(self.a, 'a', 'first')
        self.put(self.b, 'b', 'second')
        self.run_sync()
        self.assertEqual(scan(self.a), scan(self.b))
        self.put(self.b, 'a', 'edited on right')
        self.run_sync()
        self.assertEqual((self.a / 'a').read_text(), 'edited on right')
        (self.a / 'b').unlink()
        self.run_sync()
        self.assertFalse((self.b / 'b').exists())
        self.assertEqual(self.run_sync(), '0 file change(s)')

    def test_conflicts_keep_both(self):
        self.put(self.a, 'file', 'left')
        self.put(self.b, 'file', 'right')
        self.run_sync()
        for root in (self.a, self.b):
            self.assertEqual({p.read_text() for p in root.iterdir()}, {'left', 'right'})
        self.assertEqual(self.run_sync(), '0 file change(s)')
        self.put(self.a, 'file', 'new left')
        self.put(self.b, 'file', 'new right')
        self.run_sync()
        self.assertEqual(scan(self.a), scan(self.b))
        self.assertIn('new right', {p.read_text() for p in self.a.iterdir()})

    def test_edit_beats_deletion(self):
        self.put(self.a, 'file', 'original')
        self.run_sync()
        (self.a / 'file').unlink()
        self.put(self.b, 'file', 'changed instead')
        self.run_sync()
        self.assertEqual((self.a / 'file').read_text(), 'changed instead')

    def test_one_way_keeps_destination_only(self):
        self.p['mode'] = 'one-way'
        self.put(self.a, 'file', 'source')
        self.put(self.b, 'extra', 'keep')
        self.run_sync()
        self.assertFalse((self.a / 'extra').exists())
        (self.a / 'file').unlink()
        self.run_sync()
        self.assertTrue((self.b / 'file').exists())
        self.assertTrue((self.b / 'extra').exists())

    def test_backup_retention_is_scoped(self):
        self.p.update(mode='backup', keep=2)
        unrelated = self.b / 'backup_old'
        unrelated.mkdir()
        for n in range(4):
            self.put(self.a, 'file', str(n))
            self.run_sync()
        snapshots = list((self.b / ('Toy_Sync-' + self.p['id'])).iterdir())
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(unrelated.exists())
        self.assertIn('3', {(p / 'file').read_text() for p in snapshots})

    def test_missing_root_does_not_propagate_deletions(self):
        self.put(self.a, 'file', 'keep')
        self.run_sync()
        (self.a / 'file').unlink()
        self.a.rmdir()
        with self.assertRaises(RuntimeError):
            self.run_sync()
        self.assertTrue((self.b / 'file').exists())

    def test_links_rejected(self):
        (self.a / 'link').symlink_to(self.b, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.run_sync()

    def test_empty_folders(self):
        (self.a / 'empty').mkdir()
        self.run_sync()
        self.assertTrue((self.b / 'empty').is_dir())

    def test_overlap_rejected(self):
        self.p['target'] = str(self.a / 'nested')
        with self.assertRaises(ValueError):
            validate(self.p)

    def test_external_change_is_not_swallowed_by_baseline(self):
        self.put(self.a, 'x', 'original')
        self.run_sync()
        self.put(self.a, 'x', 'trigger copy')
        from toy_sync.engine import copy_file
        def mutate(src, dst, expected):
            copy_file(src, dst, expected)
            self.put(self.b, 'external', 'created during sync')
        with patch('toy_sync.engine.copy_file', side_effect=mutate):
            self.run_sync()
        self.run_sync()
        self.assertEqual((self.a / 'external').read_text(), 'created during sync')

    def test_log_expiry(self):
        journal = Journal(self.root / 'log')
        journal.add('current')
        with sqlite3.connect(journal.path) as db:
            db.execute('INSERT INTO activity VALUES (?, ?, ?)', (time.time() - 90000, 'INFO', 'old'))
        journal.prune()
        self.assertEqual([r[2] for r in journal.rows()], ['current'])


if __name__ == '__main__':
    unittest.main()
