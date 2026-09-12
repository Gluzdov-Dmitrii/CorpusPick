import csv
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from corpuspick.core import Session, Cancelled, digest
from corpuspick.manifest import read_manifest, write_manifest
from corpuspick.__main__ import totals_text


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'backup'
        self.root.mkdir()
        self.session = Session(self.root, self.base / 'state')

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def put(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_backup_export_import_flat_reopen(self):
        a = self.put('reports/2025/a.txt', 'report')
        self.put('letters/b.txt', 'letter')
        (self.root / 'empty').mkdir()
        before = [(str(p.relative_to(self.root)), p.stat().st_mtime_ns, digest(p)) for p in self.root.rglob('*') if p.is_file()]
        self.session.read_only = True
        self.session.open_catalog()
        manifest = self.base / 'structure.csv'
        self.session.export_structure(manifest)
        after = [(str(p.relative_to(self.root)), p.stat().st_mtime_ns, digest(p)) for p in self.root.rglob('*') if p.is_file()]
        self.assertEqual(before, after)
        rows, dirs = read_manifest(manifest)
        self.assertIn('empty', dirs)
        flat = self.base / 'flat'
        flat.mkdir()
        (flat / 'renamed.txt').write_bytes(a.read_bytes())
        (flat / 'b.txt').write_bytes((self.root / 'letters/b.txt').read_bytes())
        second = Session(flat, self.base / 'state')
        try:
            result = second.import_structure(manifest)
            self.assertEqual(result['matched'], 2)
            d = next(d for d in second.state['documents'] if d['path'] == 'renamed.txt')
            self.assertEqual(d['origin'], str(Path('reports/2025/a.txt')))
            second.close()
            second = Session(flat, self.base / 'state')
            self.assertEqual(next(d for d in second.scan() if d['path'] == 'renamed.txt')['origin'], str(Path('reports/2025/a.txt')))
        finally:
            second.close()

    def test_identical_hashes_keep_all_origins(self):
        self.put('one/a.txt', 'same')
        self.put('two/b.txt', 'same')
        manifest = self.base / 'structure.csv'
        self.session.export_structure(manifest)
        flat = self.base / 'flat'
        flat.mkdir()
        (flat / 'unknown.txt').write_text('same')
        second = Session(flat, self.base / 'state')
        try:
            result = second.import_structure(manifest)
            self.assertEqual(result['ambiguous'], 1)
            d = second.state['documents'][0]
            self.assertEqual(set(d['origins']), {str(Path('one/a.txt')), str(Path('two/b.txt'))})
        finally:
            second.close()

    def test_readonly_blocks_all_mutations_and_unlock(self):
        self.put('a.txt', 'one')
        self.session.read_only = True
        with patch.object(self.session, 'unlock', side_effect=AssertionError('must not unlock')):
            self.session.open_catalog()
        for action in (self.session.flatten, self.session.remove_empty_directories, self.session.unlock,
                       self.session.cancel_pending, lambda: self.session.trash_documents([])):
            with self.assertRaises(ValueError):
                action()
        with self.assertRaises(ValueError):
            self.session.export_structure(self.root / 'bad.csv')
        self.assertFalse((self.root / 'bad.csv').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows ADS')
    def test_automatic_unlock_except_backup(self):
        path = self.put('a.txt', 'one')
        ads = str(path) + ':Zone.Identifier'
        with open(ads, 'w') as stream:
            stream.write('ZoneId=3')
        self.session.read_only = True
        self.session.open_catalog()
        with open(ads) as stream:
            self.assertEqual(stream.read(), 'ZoneId=3')
        self.session.read_only = False
        self.session.open_catalog()
        self.assertFalse(os.path.exists(ads))

    def test_cached_hashes_and_bounded_full_journal_writes(self):
        for i in range(40):
            self.put(f'nested/{i}.txt', str(i))
        self.session.scan()
        with patch('corpuspick.core.digest', side_effect=AssertionError('no repeated hashes')), patch.object(self.session, 'save', wraps=self.session.save) as save:
            result = self.session.flatten()
            self.assertEqual(result['moved'], 40)
            self.assertLessEqual(save.call_count, 5)
        rows, _ = read_manifest(self.session.csv_path)
        self.assertEqual(len(rows), 40)
        self.assertTrue(all(r['origins'][0].startswith('nested') for r in rows))

    def test_stop_resume_with_csv_and_no_origin_loss(self):
        for i in range(4):
            self.put(f'nested/{i}.txt', str(i))
        self.session.scan()
        from corpuspick.core import move_exclusive
        moved = 0
        def stop_after_two(src, dst):
            nonlocal moved
            self.assertTrue(self.session.csv_path.exists())
            move_exclusive(src, dst)
            moved += 1
            if moved == 2:
                self.session.cancel.set()
        with patch('corpuspick.core.move_exclusive', stop_after_two):
            result = self.session.flatten()
        self.assertTrue(result['stopped'])
        self.assertEqual(result['moved'], 2)
        self.assertEqual(len(read_manifest(self.session.csv_path)[0]), 4)
        self.session.close()
        self.session = Session(self.root, self.base / 'state')
        self.assertEqual(self.session.flatten()['moved'], 2)
        self.assertTrue(all(d['origin'].startswith('nested') for d in self.session.state['documents']))

    def test_csv_failure_prevents_move(self):
        path = self.put('nested/a.txt', 'one')
        with patch('corpuspick.manifest.write_manifest', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.session.flatten()
        self.assertTrue(path.exists())
        self.assertFalse((self.root / 'a.txt').exists())

    def test_crash_before_append_is_recoverable(self):
        self.put('nested/a.txt', 'one')
        with patch.object(self.session, 'log_move', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                self.session.flatten()
        self.session.close()
        self.session = Session(self.root, self.base / 'state')
        self.session.flatten()
        self.assertEqual(self.session.state['documents'][0]['origin'], str(Path('nested/a.txt')))

    def test_csv_formula_path_roundtrip(self):
        self.put('=formula.txt', 'one')
        self.put("'quoted.txt", 'two')
        target = self.base / 'structure.csv'
        self.session.export_structure(target)
        rows, _ = read_manifest(target)
        self.assertEqual({r['origins'][0] for r in rows}, {'=formula.txt', "'quoted.txt"})
        with target.open(encoding='utf-8-sig') as stream:
            raw = list(csv.DictReader(stream, delimiter=';'))
        self.assertTrue(all(r['original_path'].startswith("'") for r in raw))

    def test_manifest_rejects_traversal_before_matching(self):
        target = self.base / 'invalid.csv'
        write_manifest(target, [{'path': 'x.txt', 'origin': '../escape', 'hash': '', 'size': 1}], [])
        with self.assertRaises(ValueError):
            self.session.import_structure(target)

    def test_totals_selection_and_partial_marker(self):
        word = {'path': 'a.docx', 'size': 10, 'stats': {'pages': 2, 'pages_estimated': True, 'figures': 3, 'tables': 1}}
        pdf = {'path': 'b.pdf', 'size': 20, 'stats': {'pages': 4}}
        self.assertIn('Рис.: >3', totals_text([word, pdf]))
        self.assertIn('Стр.: ≈6', totals_text([word, pdf]))
        self.assertIn('Рис.: 3', totals_text([word], selected=True))
        self.assertNotIn('Прил.', totals_text([word]))

    def test_digest_stop_inside_file(self):
        path = self.put('large.txt', 'synthetic')
        self.session.cancel.set()
        with self.assertRaises(Cancelled):
            digest(path, self.session.cancel)
