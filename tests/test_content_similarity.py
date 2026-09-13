from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from corpuspick.core import Session
from corpuspick.content_similarity import fingerprint, evidence, content_order


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'files'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_one_byte_change_and_insertion_small_and_large(self):
        for size in (4096, 32768, 150000):
            data = random.Random(123).randbytes(size)
            first = fingerprint(self.put('a.bin', data))
            edited = fingerprint(self.put('b.bin', data[:1000] + bytes([data[1000] ^ 1]) + data[1001:]))
            inserted = fingerprint(self.put('c.bin', data[:1000] + b'X' + data[1000:]))
            self.assertNotEqual(first['sha256'], edited['sha256'])
            self.assertIn('Близкие байты', evidence(first, edited))
            self.assertIn('Близкие байты', evidence(first, inserted))
            unrelated = fingerprint(self.put('d.bin', random.Random(456).randbytes(size)))
            self.assertIsNone(evidence(first, unrelated))

    def docx(self, name, text, compression):
        path = self.root / name
        with ZipFile(path, 'w', compression=compression) as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:body></w:document>')
        return path

    def test_repacked_docx_and_cross_format_text_different_names(self):
        text = ' '.join(f'Искусственный образец номер {i} содержит показатель {i*i}' for i in range(80))
        a = self.docx('report.docx', text, ZIP_DEFLATED)
        b = self.docx('xyz.docx', text.replace('показатель 49', 'показатель 50'), ZIP_STORED)
        c = self.put('other.txt', text.encode('utf-8'))
        first, second, third = map(fingerprint, (a, b, c))
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertIn('Близкий текст', evidence(first, second))
        self.assertIn('Близкий текст', evidence(first, third))
        docs = [{'path': path.name, 'size': path.stat().st_size, 'content': fp} for path, fp in zip((a,b,c), (first,second,third))]
        _, groups, notes = content_order(docs, threading.Event())
        self.assertEqual(len(set(groups.values())), 1)
        self.assertTrue(all('текст' in note for note in notes.values()))

    def test_session_cache_invalidation_and_exact_delete_separation(self):
        data = random.Random(23).randbytes(4096)
        a = self.put('unrelated-name.bin', data)
        b = self.put('completely-different.bin', data[:500] + b'ABC' + data[503:])
        session = Session(self.root, self.base / 'state', read_only=True)
        try:
            result = session.group_similar()
            self.assertEqual(len(set(result[1].values())), 1)
            self.assertEqual(session.duplicate_plan(), [])
            with patch('corpuspick.stats_worker.StatsWorker.count', side_effect=AssertionError('Use cached signature')):
                session.group_similar()
            b.write_bytes(random.Random(22).randbytes(4096))
            result = session.group_similar()
            self.assertEqual(len(set(result[1].values())), 2)
            self.assertEqual(a.read_bytes(), data)
            session.cancel.set()
            self.assertIsNone(session.group_similar())
        finally:
            session.close()

    def test_corrupt_docx_keeps_binary_but_not_fake_text(self):
        result = fingerprint(self.put('broken.docx', b'not a zip archive' * 100))
        self.assertTrue(result['text_failed'])
        self.assertTrue(result['sha256'])
        self.assertNotIn('text_sha256', result)

    def test_stop_keeps_finished_signatures_after_reopen(self):
        self.put('a.bin', random.Random(1).randbytes(4000))
        self.put('b.bin', random.Random(2).randbytes(4000))
        session = Session(self.root, self.base / 'state', read_only=True)
        try:
            def stop_after_first(_):
                if any(d.get('content') for d in session.state['documents']):
                    session.cancel.set()
            self.assertIsNone(session.group_similar(stop_after_first))
            self.assertEqual(sum(bool(d.get('content')) for d in session.state['documents']), 1)
            session.close()
            session = Session(self.root, self.base / 'state', read_only=True)
            from corpuspick.stats_worker import StatsWorker
            original = StatsWorker.count
            calls = []
            def count(worker, path, cancel):
                calls.append(path)
                return original(worker, path, cancel)
            with patch.object(StatsWorker, 'count', count):
                session.group_similar()
            self.assertEqual(len(calls), 1)
        finally:
            session.close()

    def test_pdf_text_and_image_only_no_fake_text(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(600, 800)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        text = 'Synthetic report with twelve or more words for testing the local content comparison engine'
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 10 Tf 10 700 Td (' + text + ') Tj ET').encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
        path = self.root / 'report.pdf'
        writer.write(path)
        pdf = fingerprint(path)
        txt = fingerprint(self.put('unrelated.txt', text.encode()))
        self.assertIn('Близкий текст', evidence(pdf, txt))
        blank = PdfWriter()
        blank.add_blank_page(600, 800)
        blank.write(self.root / 'blank.pdf')
        self.assertEqual(fingerprint(self.root / 'blank.pdf')['words'], 0)
