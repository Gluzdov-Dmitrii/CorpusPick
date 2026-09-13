from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from corpuspick.core import Session
from corpuspick.content_similarity import (fingerprint, evidence, content_order, text_fingerprint,
                                           semantic_layout, visual_similarity, TOPIC_VERSION)


class ContentTests(unittest.TestCase):
    def test_hdbscan_separates_synthetic_document_topics(self):
        themes = [
            'коммерческое предложение поставка оборудование цена стоимость заказчик условия договор срок оплаты',
            'база данных геометрические характеристики испытания образцов материалы результаты таблица реестр',
            'служебная записка совещание поручение исполнение ответственный решение срок контроль',
        ]
        docs = []
        variants = ['спецификация', 'приложение', 'комплект', 'описание', 'согласование']
        for group, theme in enumerate(themes):
            for index in range(5):
                variant = ' ' + variants[index]
                content = {'sha256': f'{group}-{index}'}
                content.update(text_fingerprint([(theme + variant) * 8]))
                docs.append({'path': f'{group}-{index}.pdf', 'size': 1000 + index, 'content': content})
        labels, visual = semantic_layout(docs, threading.Event())
        topic_labels = [{labels[f'{group}-{index}.pdf'][0] for index in range(5)} for group in range(3)]
        self.assertTrue(all(len(group) == 1 and -1 not in group for group in topic_labels), topic_labels)
        self.assertEqual(len(set.union(*topic_labels)), 3)
        self.assertEqual(visual['clusters'], 3)
        self.assertEqual(visual['clustered'], 15)
        ranks, groups, notes = content_order(docs, threading.Event())
        grouped = [{groups[f'{group}-{index}.pdf'] for index in range(5)} for group in range(3)]
        self.assertTrue(all(len(group) == 1 for group in grouped))
        self.assertEqual(len(set.union(*grouped)), 3)
        self.assertTrue(all('HDBSCAN' in notes[document['path']] for document in docs))
        self.assertEqual(len(ranks), 15)

    def test_existing_fingerprint_adds_only_bounded_topic_pass(self):
        self.put('a.txt', ('искусственный тематический документ ' * 20).encode('utf-8'))
        session = Session(self.root, self.base / 'state', read_only=True)
        try:
            session.open_catalog()
            document = session.state['documents'][0]
            document['content'] = {'version': 1, 'sha256': 'cached', 'bytes': document['size'],
                                   'binary': [], 'chunks': [], 'words': 20,
                                   'text_sha256': 'cached-text', 'text': []}
            calls = []
            def count(worker, path, cancel):
                calls.append(worker.target.__name__)
                return {'topic_version': TOPIC_VERSION, 'topic_words': 20,
                        'topic': text_fingerprint(['искусственный тематический документ ' * 20])['topic']}
            with patch('corpuspick.stats_worker.StatsWorker.count', count):
                session.group_similar()
            self.assertEqual(calls, ['topic_worker'])
        finally:
            session.close()

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
        self.assertTrue(all('Только тип и размер' in note for note in notes.values()))

    def test_large_skip_and_cache_reset_preserve_structure_and_hash(self):
        self.put('a.zip', b'synthetic archive')
        session = Session(self.root, self.base / 'state', read_only=True)
        try:
            # A lowered threshold exercises the real scan without allocating GB files.
            with patch('corpuspick.content_similarity.MAX_CONTENT_BYTES', 4), \
                 patch('corpuspick.stats_worker.StatsWorker.count', side_effect=AssertionError('Must not read large file')):
                result = session.group_similar()
                self.assertIn('Только тип и размер', result[2]['a.zip'])
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
        _, groups, _ = content_order(first, threading.Event())
        self.assertEqual(groups[first[0]['path']], groups[first[1]['path']])
        self.assertNotEqual(groups[first[0]['path']], groups[first[2]['path']])
        second = [dict(d, path=f'renamed/{9-i}.anything') for i, d in enumerate(first)]
        _, renamed, _ = content_order(list(reversed(second)), threading.Event())
        self.assertEqual([groups[d['path']] for d in first], [renamed[d['path']] for d in second])
        unknown = [{'path': 'one/report.doc', 'size': 100},
                   {'path': 'two/report.doc', 'size': 102, 'content': {'failed': True}}]
        _, groups, notes = content_order(unknown, threading.Event())
        self.assertEqual(len(set(groups.values())), 1)
        self.assertTrue(all('Резервная группа' in note for note in notes.values()))

    def test_residual_name_grouping_only_uses_same_type_and_broad_size_band(self):
        docs = [
            {'path': '01 КП поставка насосов.pdf', 'size': 1_000_000,
             'content': {'sha256': 'a', 'bytes': 1_000_000, 'chunks': []}},
            {'path': 'КП поставка насосного оборудования.pdf', 'size': 1_450_000,
             'content': {'sha256': 'b', 'bytes': 1_450_000, 'chunks': []}},
            {'path': 'КП поставка насосов.xlsx', 'size': 1_100_000,
             'content': {'sha256': 'c', 'bytes': 1_100_000, 'chunks': []}},
            {'path': 'КП поставка насосов.pdf', 'size': 3_100_000,
             'content': {'sha256': 'd', 'bytes': 3_100_000, 'chunks': []}},
            {'path': 'База данных испытаний.pdf', 'size': 1_050_000,
             'content': {'sha256': 'e', 'bytes': 1_050_000, 'chunks': []}},
        ]
        _, groups, notes = content_order(docs, threading.Event())
        self.assertEqual(groups[docs[0]['path']], groups[docs[1]['path']])
        self.assertNotEqual(groups[docs[0]['path']], groups[docs[2]['path']])
        self.assertNotEqual(groups[docs[0]['path']], groups[docs[3]['path']])
        self.assertNotEqual(groups[docs[0]['path']], groups[docs[4]['path']])
        self.assertIn('Резервная группа', notes[docs[0]['path']])

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
            ranks, groups, _ = content_order(docs, threading.Event())
        self.assertEqual(groups['258'], groups['259'])
        self.assertTrue(all(groups[str(i)] != groups['258'] for i in range(258)))
        self.assertLess(max(ranks['258'], ranks['259']), min(ranks[str(i)] for i in range(258)))

    def test_unmatched_files_are_last_then_ordered_by_format_and_size(self):
        sizes = [('first.pdf', 56591), ('second.pdf', 56585), ('sheet.xlsx', 94537),
                 ('third.pdf', 56451), ('far.pdf', 120000)]
        docs = [{'path': path, 'size': size,
                 'content': {'sha256': path, 'bytes': size, 'chunks': []}}
                for path, size in sizes]
        docs += [{'path': 'copy-a.bin', 'size': 200, 'content': {'sha256': 'same', 'bytes': 200}},
                 {'path': 'copy-b.bin', 'size': 200, 'content': {'sha256': 'same', 'bytes': 200}}]
        ranks, groups, notes = content_order(docs, threading.Event())
        self.assertLess(max(ranks['copy-a.bin'], ranks['copy-b.bin']),
                        min(ranks[path] for path, _ in sizes))
        ordered = [path for path, _ in sorted(ranks.items(), key=lambda pair: pair[1])]
        self.assertEqual(ordered[2:], ['third.pdf', 'second.pdf', 'first.pdf', 'far.pdf', 'sheet.xlsx'])
        self.assertEqual(groups['third.pdf'], groups['first.pdf'])
        self.assertNotEqual(groups['third.pdf'], groups['far.pdf'])
        self.assertTrue(all('расположен в конце' in notes[path] for path, _ in sizes))

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
            self.assertTrue(all('выше порога не найдено' in result[2][path.name] for path in (a, b)))
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

    def test_image_only_pdf_uses_visual_page_fingerprint_without_ocr(self):
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (500, 700), 'white')
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((40, 30, 460, 120), fill='black')
        for row in range(8):
            drawing.rectangle((55, 180 + row * 45, 420 - row * 7, 193 + row * 45), fill='black')
        first_path, second_path = self.root / 'scan-a.pdf', self.root / 'scan-b.pdf'
        image.save(first_path, 'PDF', resolution=150, quality=82)
        image.save(second_path, 'PDF', resolution=150, quality=96)
        first, second = fingerprint(first_path), fingerprint(second_path)
        self.assertEqual(first['words'], 0)
        self.assertTrue(first['visual'])
        self.assertGreaterEqual(visual_similarity(first, second), .88)
        self.assertIn('Визуально близкие страницы PDF без OCR', evidence(first, second))
        other = Image.new('RGB', (500, 700), 'white')
        other_drawing = ImageDraw.Draw(other)
        other_drawing.ellipse((80, 120, 420, 580), fill='black')
        other_path = self.root / 'scan-c.pdf'
        other.save(other_path, 'PDF', resolution=150)
        third = fingerprint(other_path)
        documents = [
            {'path': path.name, 'size': path.stat().st_size, 'content': signature}
            for path, signature in ((first_path, first), (second_path, second), (other_path, third))
        ]
        _, groups, notes = content_order(documents, threading.Event())
        self.assertEqual(groups['scan-a.pdf'], groups['scan-b.pdf'])
        self.assertNotEqual(groups['scan-a.pdf'], groups['scan-c.pdf'])
        self.assertIn('Визуально близкие страницы PDF без OCR', notes['scan-a.pdf'])
