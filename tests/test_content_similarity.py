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
    def test_large_files_group_only_by_size_and_extension_without_chain(self):
        from corpuspick.content_similarity import MAX_CONTENT_BYTES, size_only
        self.assertFalse(size_only({'size': MAX_CONTENT_BYTES}))
        self.assertTrue(size_only({'size': MAX_CONTENT_BYTES + 1}))
        docs = [{'path': name, 'size': size * 1024 * 1024} for name, size in
                [('a.rst', 200), ('unrelated.RST', 208), ('a.zip', 200), ('b.rst', 216)]]
        _, groups, notes = content_order(docs, threading.Event())
        self.assertEqual(groups['a.rst'], groups['unrelated.RST'])
        self.assertNotEqual(groups['a.rst'], groups['a.zip'])
        self.assertNotEqual(groups['a.rst'], groups['b.rst'])
        self.assertTrue(all('Только размер' in note for note in notes.values()))

    def test_large_skip_and_cache_reset_preserve_structure_and_hash(self):
        self.put('a.zip', b'synthetic archive')
        session = Session(self.root, self.base / 'state', read_only=True)
        try:
            # A lowered threshold exercises the real scan without allocating GB files.
            with patch('corpuspick.content_similarity.MAX_CONTENT_BYTES', 4), \
                 patch('corpuspick.stats_worker.StatsWorker.count', side_effect=AssertionError('Must not read large file')):
                result = session.group_similar()
                self.assertIn('Только размер', result[2]['a.zip'])
            doc = session.state['documents'][0]
            doc.update(content={'sha256': 'synthetic'}, hash='keep', origin='original/a.zip', stats={'pages': 2})
            session.clear_similarity_cache()
            session.close()
            session = Session(self.root, self.base / 'state', read_only=True)
            doc = session.state['documents'][0]
            self.assertNotIn('content', doc)
            self.assertEqual(doc['origin'], 'original/a.zip')
            self.assertEqual(doc['hash'], 'keep')
            self.assertEqual(doc['stats'], {'pages': 2})
        finally:
            session.close()

    def test_names_paths_and_input_order_cannot_change_groups(self):
        data = random.Random(81).randbytes(4096)
        fps = [fingerprint(self.put('a.bin', data)),
               fingerprint(self.put('b.bin', data[:900] + b'xyz' + data[903:])),
               fingerprint(self.put('c.bin', random.Random(91).randbytes(4096)))]
        first = [{'path': name, 'content': fp} for name, fp in zip(
            ('folder/report.doc', 'different/unknown.pdf', 'other/report.doc'), fps)]
        with patch('corpuspick.similarity.similar_order', side_effect=AssertionError('No filename grouping')):
            _, groups, _ = content_order(first, threading.Event())
        self.assertEqual(groups[first[0]['path']], groups[first[1]['path']])
        self.assertNotEqual(groups[first[0]['path']], groups[first[2]['path']])
        second = [dict(d, path=f'renamed/{9-i}.anything') for i, d in enumerate(first)]
        _, renamed, _ = content_order(list(reversed(second)), threading.Event())
        self.assertEqual([groups[d['path']] for d in first], [renamed[d['path']] for d in second])
        unknown = [{'path': 'one/report.doc'}, {'path': 'two/report.doc', 'content': {'failed': True}}]
        _, groups, _ = content_order(unknown, threading.Event())
        self.assertEqual(len(set(groups.values())), 2)

    def test_complete_link_merges_strongest_pair_before_borderline_file(self):
        # A-B=.9, A-C=.97, B-C below threshold: filenames/input order must not
        # let borderline B take A away from the stronger A-C pair.
        docs = [{'path': name, 'content': {'sha256': key, 'chunks': ['shared']}} for name, key in
                [('a.doc', 'A'), ('b.doc', 'B'), ('z.doc', 'C')]]
        def match(a, b):
            keys = frozenset((a['sha256'], b['sha256']))
            score = {frozenset(('A', 'B')): .9, frozenset(('A', 'C')): .97}.get(keys)
            return (score, 'test content match') if score else None
        with patch('corpuspick.content_similarity.content_match', match):
            _, groups, _ = content_order(docs, threading.Event())
            self.assertEqual(groups['a.doc'], groups['z.doc'])
            self.assertNotEqual(groups['a.doc'], groups['b.doc'])

    def test_optimized_chunks_match_previous_cache(self):
        from corpuspick.content_similarity import Sketch, GEAR, binary_fingerprint
        for data in (b'', b'x' * 9000, bytes(range(256)) * 200,
                     random.Random(31).randbytes(1024 * 1024 + 9000)):
            sketch, chunk, rolling = Sketch(), bytearray(), 0
            for byte in data:
                chunk.append(byte)
                rolling = ((rolling << 1) + GEAR[byte]) & 0xffffffffffffffff
                if len(chunk) >= 512 and ((rolling & 1023) == 0 or len(chunk) >= 8192):
                    sketch.add(chunk)
                    chunk.clear()
                    rolling = 0
            if chunk:
                sketch.add(chunk)
            actual = binary_fingerprint(self.put('synthetic.bin', data))
            self.assertEqual(actual['chunks'], sketch.result())

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

    def test_candidates_beyond_256_are_compared(self):
        docs = [{'path': str(i), 'content': {'sha256': f'{i:04d}', 'chunks': ['shared']}}
                for i in range(260)]
        def match(a, b):
            return (.95, 'test') if {a['sha256'], b['sha256']} == {'0258', '0259'} else None
        with patch('corpuspick.content_similarity.content_match', side_effect=match):
            _, groups, _ = content_order(docs, threading.Event())
        self.assertEqual(groups['258'], groups['259'])
        self.assertEqual(len(set(groups.values())), 259)

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
