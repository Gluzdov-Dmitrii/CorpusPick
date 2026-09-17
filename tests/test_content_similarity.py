from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from corpuspick.core import Session
from corpuspick.content_similarity import (fingerprint, evidence, content_order, text_fingerprint,
                                           metadata_order, semantic_layout, visual_similarity,
                                           weak_label_candidates)
from corpuspick.embeddings import EMBEDDING_VERSION, metadata_features, pack_embedding


class ContentTests(unittest.TestCase):
    def test_renamed_pdf_refresh_sends_cached_vectors_without_ocr(self):
        self.put('scan.pdf', b'Synthetic placeholder, must never be parsed')
        session = Session(self.root, self.base / 'state', read_only=True)
        try:
            session.scan(hash_files=False)
            vector = [0.] * 384
            vector[0] = 1
            from corpuspick.embeddings import metadata_key
            session.state['documents'][0]['similarity'] = {
                'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(vector),
                'metadata_key': metadata_key(['scan.pdf']), 'ocr_pages': 3, 'embedding_source': 'ocr+metadata'}
            (self.root / 'scan.pdf').rename(self.root / 'Акты_scan.pdf')
            requests = []
            def refresh(worker, path, cancel, request=None):
                requests.append(request)
                self.assertIsNone(request['path'])
                self.assertEqual(request['cached_similarity']['ocr_pages'], 3)
                return dict(request['cached_similarity'], metadata_key=metadata_key(request['metadata_paths']))
            with patch('corpuspick.stats_worker.StatsWorker.count', refresh), \
                    patch('corpuspick.embeddings.ensure_model', return_value=self.base / 'model'), \
                    patch('corpuspick.ocr.ensure_ocr_model', side_effect=AssertionError('No OCR model needed')):
                result = session.group_similar()
                session.group_similar()
            self.assertEqual(len(requests), 1)
            self.assertIn('Акты_scan.pdf', result[1])
            self.assertEqual(session.state['documents'][0]['similarity']['ocr_pages'], 3)
        finally:
            session.close()

    def test_large_clustering_rejected_before_distance_allocation(self):
        vector = [0.] * 384
        vector[0] = 1
        document = {'path': 'synthetic.pdf', 'similarity': {
            'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(vector)}}
        with patch('numpy.zeros', side_effect=AssertionError('No dense allocation')):
            with self.assertRaisesRegex(ValueError, 'памяти'):
                metadata_order([document] * 6000, threading.Event())

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
                vector = [0.] * 384
                vector[group] = 1
                vector[10 + index] = .08
                content.update(embedding_version=EMBEDDING_VERSION, embedding=pack_embedding(vector))
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
        self.assertTrue(all('SVM-класс' in notes[document['path']] for document in docs))
        self.assertEqual(len(ranks), 15)

    def test_svm_uses_frequent_labels_handles_overlap_and_builds_other_class(self):
        def item(path, theme, index, group):
            content = {'sha256': path}
            content.update(text_fingerprint([(theme + f' вариант{index} приложение{index} ') * 8]))
            vector = [0.] * 384
            vector[group] = 1
            vector[20 + index] = .08
            content.update(embedding_version=EMBEDDING_VERSION, embedding=pack_embedding(vector))
            return {'path': path, 'size': 1000 + index, 'content': content}

        acts = 'акт приемка выполненные работы комиссия результат '
        agreements = 'дополнительное соглашение договор условия срок сторона '
        diplomas = 'диплом исследование университет кафедра работа защита '
        docs = []
        for index in range(5):
            docs.append(item(f'a{index}/АКТ Приемки {index}.pdf', acts, index, 0))
            docs.append(item(f'd{index}/дс соглашение {index}.pdf', agreements, 10 + index, 1))
        people = ['Иванов Сергей', 'Петров Антон', 'Сидоров Павел',
                  'Орлов Илья', 'Волков Олег', 'Смирнов Роман']
        for index, person in enumerate(people):
            docs.append(item(f'p{index}/{person}.pdf', diplomas, 20 + index, 2))
        docs.append(item('mixed/акт приемки дс соглашение спорный.pdf', acts, 40, 0))

        weak, frequencies = weak_label_candidates(docs)
        self.assertEqual(set(frequencies), {'акт_приемки', 'дс_соглашение'})
        self.assertEqual(weak['a0/АКТ Приемки 0.pdf'], ['акт_приемки'])
        self.assertEqual(weak['mixed/акт приемки дс соглашение спорный.pdf'],
                         ['акт_приемки', 'дс_соглашение'])
        labels, summary = semantic_layout(docs, threading.Event())
        self.assertEqual(labels['mixed/акт приемки дс соглашение спорный.pdf'][2], 'акт_приемки')
        self.assertEqual({labels[f'p{index}/{person}.pdf'][2] for index, person in enumerate(people)},
                         {'other_1'})
        self.assertEqual(summary['ambiguous'], 1)
        self.assertEqual(summary['clusters'], 3)

    def test_hyphenated_weak_label_is_one_class(self):
        docs = [{'path': f'flat/alpha{index}.pdf',
                 'origin': f'Технологии/ноу-хау/person{index}.pdf'} for index in range(5)]
        docs += [{'path': f'flat/beta{index}.pdf',
                  'origin': f'Прочее{index}/different{index}.pdf'} for index in range(5)]
        weak, frequencies = weak_label_candidates(docs)
        self.assertIn('ноу_хау', frequencies)
        self.assertEqual(weak[docs[0]['path']], ['ноу_хау'])

    def test_metadata_mode_does_not_read_existing_content(self):
        self.put('a.txt', ('искусственный тематический документ ' * 20).encode('utf-8'))
        session = Session(self.root, self.base / 'state', read_only=True)
        try:
            session.open_catalog()
            document = session.state['documents'][0]
            document['content'] = {'version': 1, 'sha256': 'cached', 'bytes': document['size'],
                                   'binary': [], 'chunks': [], 'words': 20,
                                   'text_sha256': 'cached-text', 'text': []}
            calls = []
            def count(worker, path, cancel, request=None):
                calls.append(worker.target.__name__)
                vector = [0.] * 384
                vector[0] = 1
                return {'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(vector)}
            with patch('corpuspick.stats_worker.StatsWorker.count', count), \
                 patch('corpuspick.embeddings.ensure_model', return_value=self.base / 'model'):
                session.group_similar()
            self.assertEqual(calls, ['embedding_worker'])
            refreshed = session.state['documents'][0]
            self.assertEqual(refreshed['content']['sha256'], 'cached')
            self.assertIn('similarity', refreshed)
        finally:
            session.close()

    def test_metadata_entities_replace_person_date_and_number_values(self):
        features = metadata_features([r'Отчеты\Иванов Сергей\Акт от 12.03.2024 № 15.pdf'])
        self.assertEqual(features['entity_counts'], {'person': 1, 'date': 1, 'number': 1})
        rendered = ' '.join((features['title'], features['folders']))
        self.assertNotIn('иванов', rendered)
        self.assertNotIn('сергей', rendered)
        self.assertNotIn('2024', rendered)
        self.assertIn('персона', rendered)
        self.assertNotIn('дата', rendered)
        self.assertNotIn('номер', features['entities'])

    def test_metadata_order_uses_only_embeddings_and_leaves_singletons_separate(self):
        def doc(path, vector, **entities):
            return {'path': path, 'size': 100,
                    'similarity': {'embedding_version': EMBEDDING_VERSION,
                                   'embedding': pack_embedding(vector),
                                   'metadata_entities': entities}}
        a, b, c = [0.] * 384, [0.] * 384, [0.] * 384
        a[0], b[0], b[1], c[2] = 1, .99, .05, 1
        documents = [doc('Акт Иванов 2023.pdf', a, person=1, date=1),
                     doc('Акт Петров 2024.docx', b, person=1, date=1),
                     doc('Письмо.xlsx', c)]
        _, groups, notes = metadata_order(documents, threading.Event())
        self.assertEqual(groups[documents[0]['path']], groups[documents[1]['path']])
        self.assertNotEqual(groups[documents[0]['path']], groups[documents[2]['path']])
        self.assertIn('ФИО', notes[documents[0]['path']])
        self.assertIn('OCR-текст не сохраняется', notes[documents[0]['path']])

    def test_layout_can_join_reports_with_different_text(self):
        semantic_a, semantic_b, semantic_c, lexical_a, lexical_b, lexical_c = ([0.] * 384 for _ in range(6))
        semantic_a[0] = 1
        semantic_b[0], semantic_b[1] = .65, .76
        semantic_c[2] = 1
        lexical_a[10], lexical_b[11], lexical_c[12] = 1, 1, 1
        layout_same, layout_other = [0.] * 384, [0.] * 384
        layout_same[20], layout_other[21] = 1, 1
        def doc(path, semantic, lexical, layout):
            return {'path': path, 'similarity': {
                'embedding_version': EMBEDDING_VERSION,
                'embedding': pack_embedding(semantic),
                'metadata_lexical': pack_embedding(lexical),
                'layout_embedding': pack_embedding(layout),
                'sampled_pages': 3}}
        documents = [doc('report-a.pdf', semantic_a, lexical_a, layout_same),
                     doc('report-b.pdf', semantic_b, lexical_b, layout_same),
                     doc('different-layout.pdf', semantic_c, lexical_c, layout_other)]
        _, groups, _ = metadata_order(documents, threading.Event())
        self.assertEqual(groups['report-a.pdf'], groups['report-b.pdf'])
        self.assertNotEqual(groups['report-a.pdf'], groups['different-layout.pdf'])

    def test_grouping_threshold_controls_granularity(self):
        first, second = [0.] * 384, [0.] * 384
        first[0] = 1
        second[0], second[1] = .75, (1 - .75 ** 2) ** .5
        documents = [
            {'path': 'first.pdf', 'similarity': {
                'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(first)}},
            {'path': 'second.pdf', 'similarity': {
                'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(second)}},
        ]
        _, strict_groups, _ = metadata_order(documents, threading.Event(), distance_threshold=.20)
        _, broad_groups, notes = metadata_order(documents, threading.Event(), distance_threshold=.30)
        self.assertNotEqual(strict_groups['first.pdf'], strict_groups['second.pdf'])
        self.assertEqual(broad_groups['first.pdf'], broad_groups['second.pdf'])
        self.assertIn('порог близости: 70%', notes['first.pdf'])

    def test_regroup_uses_cached_vectors_without_scan(self):
        self.put('cached.pdf', b'synthetic')
        session = Session(self.root, self.base / 'state', read_only=True)
        try:
            session.open_catalog()
            vector = [0.] * 384
            vector[0] = 1
            session.state['documents'][0]['similarity'] = {
                'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(vector)}
            with patch.object(session, 'scan', side_effect=AssertionError('regroup must not scan')):
                result = session.regroup_similar(.42)
            self.assertIn('cached.pdf', result[1])
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
            vector = [0.] * 384
            vector[0] = 1
            def metadata_count(worker, path, cancel, request=None):
                self.assertIsNotNone(request)
                return {'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(vector)}
            # Metadata mode handles every size without opening document bytes.
            with patch('corpuspick.content_similarity.MAX_CONTENT_BYTES', 4), \
                 patch('corpuspick.stats_worker.StatsWorker.count', metadata_count), \
                 patch('corpuspick.embeddings.ensure_model', return_value=self.base / 'model'):
                result = session.group_similar()
                self.assertIn('оставлен отдельно', result[2]['a.zip'])
            doc = session.state['documents'][0]
            doc.update(content={'sha256': 'synthetic'}, hash='keep', origin='original/a.zip', stats={'pages': 2})
            session.clear_similarity_cache()
            session.close()
            session = Session(self.root, self.base / 'state', read_only=True)
            doc = session.state['documents'][0]
            self.assertEqual(doc['content'], {'sha256': 'synthetic'})
            self.assertNotIn('similarity', doc)
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
            vector = [0.] * 384
            vector[0] = 1
            def metadata_count(worker, path, cancel, request=None):
                return {'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(vector)}
            with patch('corpuspick.stats_worker.StatsWorker.count', metadata_count), \
                 patch('corpuspick.embeddings.ensure_model', return_value=self.base / 'model'):
                result = session.group_similar()
            self.assertEqual(len(set(result[1].values())), 1)
            self.assertEqual(session.duplicate_plan(), [])
            with patch('corpuspick.stats_worker.StatsWorker.count', side_effect=AssertionError('Use cached signature')):
                session.group_similar()
            b.write_bytes(random.Random(22).randbytes(4096))
            calls = []
            def recount(worker, path, cancel, request=None):
                calls.append(path)
                return {'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(vector)}
            with patch('corpuspick.stats_worker.StatsWorker.count', recount), \
                 patch('corpuspick.embeddings.ensure_model', return_value=self.base / 'model'):
                result = session.group_similar()
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(set(result[1].values())), 1)
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
            vector = [0.] * 384
            vector[0] = 1
            def metadata_count(worker, path, cancel, request=None):
                return {'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(vector)}
            def stop_after_first(_):
                if any(d.get('similarity') for d in session.state['documents']):
                    session.cancel.set()
            with patch('corpuspick.stats_worker.StatsWorker.count', metadata_count), \
                 patch('corpuspick.embeddings.ensure_model', return_value=self.base / 'model'):
                self.assertIsNone(session.group_similar(stop_after_first))
            self.assertEqual(sum(bool(d.get('similarity')) for d in session.state['documents']), 1)
            session.close()
            session = Session(self.root, self.base / 'state', read_only=True)
            calls = []
            def count(worker, path, cancel, request=None):
                calls.append(path)
                return {'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(vector)}
            with patch('corpuspick.stats_worker.StatsWorker.count', count), \
                 patch('corpuspick.embeddings.ensure_model', return_value=self.base / 'model'):
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
