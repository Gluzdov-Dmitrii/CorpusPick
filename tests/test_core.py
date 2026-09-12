import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from corpuspick.core import Session, digest, native, is_link
from corpuspick.statistics import document_stats


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'corpus'
        self.root.mkdir()
        self.data = self.base / 'state'
        self.session = Session(self.root, self.data)

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def put(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def test_duplicates_and_different_periods(self):
        self.put('a/report.txt', '2025: 15')
        self.put('b/copy.txt', '2025: 15')
        self.put('c/report.txt', '2026: 17')
        docs = self.session.scan()
        self.assertEqual([d['duplicate'] > 0 for d in docs], [True, True, False])
        self.assertTrue(all('score' not in d and 'reviewed' not in d for d in docs))

    def test_collision_origin_mark_and_reopen(self):
        for name in ('same.txt', 'a/same.txt', 'b/same.txt'):
            self.put(name, name)
        self.session.scan()
        relative = str(Path('a/same.txt'))
        self.session.mark(relative)
        self.assertEqual(self.session.flatten()['moved'], 2)
        self.assertEqual(len(list(self.root.glob('*.txt'))), 3)
        self.session.close()
        self.session = Session(self.root, self.data)
        selected = next(d for d in self.session.scan() if d['origin'] == relative)
        self.assertEqual(selected['note'], 'Скорее да')
        self.assertEqual(self.session.remove_empty_directories()['removed'], 2)

    def test_hash_read_error_does_not_block_moves(self):
        self.put('nested/unreadable.txt', 'read blocked but rename allowed')
        self.put('nested/normal.txt', 'normal')
        real_digest = digest
        def fake(path):
            if Path(path).name == 'unreadable.txt':
                raise PermissionError(13, 'synthetic')
            return real_digest(path)
        with patch('corpuspick.core.digest', fake):
            docs = self.session.scan()
            item = next(d for d in docs if Path(d['path']).name == 'unreadable.txt')
            self.assertGreater(item['size'], 0)
            self.assertEqual(item['hash'], '')
            self.assertIn('Нет доступа', item['error'])
            result = self.session.flatten()
        self.assertEqual(result['moved'], 2)
        self.assertFalse(result['errors'])
        self.assertTrue((self.root / 'unreadable.txt').exists())

    def test_busy_file_skipped_others_moved_and_retry(self):
        self.put('folder/busy.txt', 'busy')
        self.put('folder/okay.txt', 'okay')
        from corpuspick.core import move_exclusive
        def fake(src, dst):
            if src.name == 'busy.txt':
                exc = PermissionError(13, 'synthetic')
                exc.winerror = 32
                raise exc
            return move_exclusive(src, dst)
        with patch('corpuspick.core.move_exclusive', fake):
            result = self.session.flatten()
        self.assertEqual(result['moved'], 1)
        self.assertIn('Файл занят', result['errors'][0]['error'])
        self.assertTrue((self.root / 'folder/busy.txt').exists())
        self.assertEqual(self.session.flatten()['moved'], 1)

    def test_explorer_changes_are_rescanned(self):
        path = self.put('folder/file.txt', 'before')
        self.session.scan()
        path.write_text('after')
        self.put('folder/new.txt', 'new')
        self.assertEqual(self.session.flatten()['moved'], 2)
        self.assertEqual((self.root / 'file.txt').read_text(), 'after')

    def test_foreign_destination_never_overwritten(self):
        self.put('folder/file.txt', 'original')
        from corpuspick.core import move_exclusive
        def race(src, dst):
            dst.write_text('foreign')
            return move_exclusive(src, dst)
        with patch('corpuspick.core.move_exclusive', race):
            result = self.session.flatten()
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual((self.root / 'file.txt').read_text(), 'foreign')
        self.assertTrue((self.root / 'folder/file.txt').exists())
        self.session.cancel_pending()
        self.assertEqual(self.session.flatten()['moved'], 1)
        self.assertEqual((self.root / 'file__1.txt').read_text(), 'original')

    def test_resume_after_rename_before_journal_save(self):
        self.put('folder/file.txt', 'synthetic')
        self.session.scan()
        save = self.session.save
        def fail_save():
            if (self.root / 'file.txt').exists():
                raise OSError('interrupted')
            save()
        with patch.object(self.session, 'save', fail_save):
            with self.assertRaises(OSError):
                self.session.flatten()
        self.session.close()
        self.session = Session(self.root, self.data)
        self.assertEqual(self.session.flatten()['moved'], 1)
        self.assertEqual(self.session.scan()[0]['origin'], str(Path('folder/file.txt')))

    def test_legacy_link_journal_recovery(self):
        path = self.put('folder/file.txt', 'synthetic')
        self.session.scan()
        self.session.state['moves'] = [{'source': str(Path('folder/file.txt')), 'destination': 'file.txt',
                                       'origin': 'legacy', 'hash': digest(path), 'done': False}]
        os.link(path, self.root / 'file.txt')
        self.session.save()
        self.assertEqual(self.session.flatten()['moved'], 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.session.scan()[0]['origin'], 'legacy')

    def test_scan_does_not_edit_and_invalidates_stats(self):
        path = self.put('a/file.txt', 'one')
        original = digest(path)
        self.session.scan()
        self.session.state['documents'][0]['stats'] = {'pages': 5}
        self.session.save()
        self.assertEqual(self.session.scan()[0]['stats']['pages'], 5)
        self.assertEqual(digest(path), original)
        path.write_text('two')
        self.assertFalse(self.session.scan()[0]['stats'])
        self.assertEqual(self.session.remove_empty_directories()['removed'], 0)

    def test_docx_structure_and_unknown_not_zero(self):
        path = self.root / 'demo.docx'
        xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><w:body>
        <w:p><w:r><w:t>Приложение А</w:t></w:r></w:p>
        <w:p><w:r><w:t>См. приложение Б</w:t></w:r></w:p>
        <w:tbl/><w:tbl/><mc:AlternateContent><mc:Choice><w:drawing/></mc:Choice><mc:Fallback><w:pict/></mc:Fallback></mc:AlternateContent>
        </w:body></w:document>'''
        with ZipFile(path, 'w') as archive:
            archive.writestr('word/document.xml', xml)
            archive.writestr('docProps/app.xml', '<Properties xmlns="test"><Pages>9</Pages></Properties>')
        stats = document_stats(path)
        self.assertEqual([stats[k] for k in ('pages', 'figures', 'tables', 'appendices')], [9, 1, 2, 1])
        self.assertTrue(stats['pages_estimated'])
        self.assertNotIn('pages', document_stats(self.put('x.bin', 'unknown')))

    def test_pdf_pages(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest('Optional pypdf unavailable')
        path = self.root / 'demo.pdf'
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_blank_page(width=100, height=100)
        with path.open('wb') as stream:
            writer.write(stream)
        self.assertEqual(document_stats(path)['pages'], 2)
        self.assertNotIn('tables', document_stats(path))

    @unittest.skipUnless(os.name == 'nt', 'Windows ADS')
    def test_unlock_recurses_preserves_content_and_other_streams(self):
        path = self.put('a/b/file.txt', 'synthetic')
        with open(str(path) + ':Zone.Identifier', 'w') as stream:
            stream.write('[ZoneTransfer]\nZoneId=3')
        with open(str(path) + ':keep', 'w') as stream:
            stream.write('preserved')
        original = digest(path)
        self.assertEqual(self.session.unlock()['unlocked'], 1)
        self.assertEqual(self.session.unlock()['unchanged'], 1)
        self.assertEqual(digest(path), original)
        with open(str(path) + ':keep') as stream:
            self.assertEqual(stream.read(), 'preserved')

    @unittest.skipUnless(os.name == 'nt', 'Windows rename')
    def test_move_preserves_zone_and_does_not_use_hardlinks(self):
        path = self.put('a/file.txt', 'synthetic')
        with open(str(path) + ':Zone.Identifier', 'w') as stream:
            stream.write('ZoneId=3')
        with patch('os.link', side_effect=AssertionError('must not use hard links')):
            self.assertEqual(self.session.flatten()['moved'], 1)
        with open(str(self.root / 'file.txt') + ':Zone.Identifier') as stream:
            self.assertEqual(stream.read(), 'ZoneId=3')

    def test_cloud_reparse_is_not_a_symlink(self):
        from types import SimpleNamespace
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0, st_reparse_tag=0x9000001A)):
            self.assertFalse(is_link(Path('fake')))
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0, st_reparse_tag=0xA0000003)):
            self.assertTrue(is_link(Path('fake')))

    def test_lock_and_path_escape(self):
        with self.assertRaises(ValueError):
            Session(self.root, self.data)
        with self.assertRaises(ValueError):
            Session(self.root, self.root / 'state')
        with self.assertRaises(ValueError):
            self.session.safe_path('../outside.txt')

    @unittest.skipUnless(os.name == 'nt', 'Windows long paths')
    def test_long_nested_path(self):
        folder = self.root / ('a' * 90) / ('b' * 90) / ('c' * 90)
        Path(native(folder)).mkdir(parents=True)
        Path(native(folder / 'file.txt')).write_text('long path')
        self.assertEqual(self.session.flatten()['moved'], 1)
        self.assertEqual((self.root / 'file.txt').read_text(), 'long path')
        self.assertEqual(self.session.remove_empty_directories()['removed'], 3)


if __name__ == '__main__':
    unittest.main()
