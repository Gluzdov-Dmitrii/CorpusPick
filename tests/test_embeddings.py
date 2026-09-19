import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from corpuspick.core import Session
from corpuspick.embeddings import embedding_worker, model_directory, model_ready, unpack_embedding
from corpuspick.ocr import ocr_model_path, ocr_model_ready
from corpuspick.stats_worker import StatsWorker


class EmbeddingTests(unittest.TestCase):
    def test_three_page_cache_cannot_be_reused(self):
        from corpuspick.embeddings import reusable_similarity, EMBEDDING_VERSION
        for version in (4, 5):
            self.assertFalse(reusable_similarity({'embedding_version': version, 'embedding': 'old'}))
        self.assertTrue(reusable_similarity({'embedding_version': EMBEDDING_VERSION, 'embedding': 'current'}))

    def test_split_content_refresh_matches_full_encode_without_extraction(self):
        import hashlib
        import numpy as np
        from corpuspick.embeddings import _encode_document, _refresh_metadata
        def encode(tokenizer, model, passages, weights, return_chunks=False):
            chunks = []
            for passage in passages:
                rng = np.random.default_rng(int.from_bytes(hashlib.sha256(passage.encode()).digest()[:8], 'little'))
                chunk = rng.normal(size=384).astype('f4')
                chunks.append(chunk / np.linalg.norm(chunk))
            chunks = np.stack(chunks)
            vector = (chunks * np.asarray(weights)[:, None]).sum(axis=0)
            vector /= np.linalg.norm(vector)
            return (vector, chunks) if return_chunks else vector
        with patch('corpuspick.embeddings._encode_passages', side_effect=encode), \
                patch('corpuspick.embeddings._sample_text', return_value=['Synthetic report body', 'Second excerpt']):
            original = _encode_document(Path('synthetic.txt'), None, None, ['report.txt'])
            expected = _encode_document(Path('synthetic.txt'), None, None, ['Акты_report.txt'])
            with patch('corpuspick.embeddings._sample_text', side_effect=AssertionError('No extraction')), \
                    patch('corpuspick.ocr.sample_pdf', side_effect=AssertionError('No OCR')):
                updated = _refresh_metadata(original, None, None, ['Акты_report.txt'])
                again = _refresh_metadata(updated, None, None, ['Акты_report.txt'])
            np.testing.assert_allclose(unpack_embedding(updated['embedding']),
                                       unpack_embedding(expected['embedding']), atol=.001)
            self.assertEqual(updated['embedding'], again['embedding'])
            self.assertEqual(original['content_embedding'], updated['content_embedding'])
            legacy = dict(original)
            legacy.pop('content_embedding')
            legacy.pop('content_embedding_norm')
            upgraded = _refresh_metadata(legacy, None, None, ['Акты_report.txt'])
            self.assertTrue(upgraded['content_embedding_legacy'])
            self.assertEqual(upgraded['content_embedding'], original['embedding'])
            again = _refresh_metadata(upgraded, None, None, ['Акты_report.txt'])
            self.assertEqual(upgraded['embedding'], again['embedding'])

    def test_numeric_metadata_does_not_change_model_inputs(self):
        from corpuspick.embeddings import metadata_features, _lexical_embedding
        import numpy as np
        baseline = metadata_features(['Акты/Акт приемки.pdf'])
        for path in ['001_Акты/123_Акт приемки.pdf', 'Акты/№ 123 Акт приемки.pdf',
                     'Акты/123Акт приемки2025.pdf', 'Акты/Акт приемки 12.03.2024.pdf',
                     'Акты/Акт приемки 12 марта 2024.pdf', 'Акты/１２３_Акт приемки.pdf']:
            with self.subTest(path=path):
                features = metadata_features([path])
                for field in ('title', 'folders', 'entities'):
                    self.assertEqual(features[field], baseline[field])
                np.testing.assert_array_equal(_lexical_embedding(features), _lexical_embedding(baseline))

    @unittest.skipUnless(os.environ.get('CORPUSPICK_TEST_E5') == '1',
                         'Optional local multilingual-e5 integration')
    def test_local_int8_model_in_isolated_worker(self):
        data_root = Path(os.environ.get('LOCALAPPDATA', '')) / 'CorpusPick'
        self.assertTrue(model_ready(data_root), 'Download the pinned model through CorpusPick first')
        with tempfile.TemporaryDirectory(prefix='CorpusPick-e5-') as directory:
            path = Path(directory) / 'synthetic.txt'
            with StatsWorker(timeout=120, target=embedding_worker,
                             init_args=(str(model_directory(data_root)),)) as worker:
                result = worker.count(path, threading.Event(),
                                      request={'metadata_paths': ['Акты/акт приемки 12.03.2024 № 7.pdf']})
                empty = Path(directory) / 'placeholder'
                metadata_result = worker.count(
                    empty, threading.Event(),
                    request={'metadata_paths': ['ДИПЛОМЫ/Иванов Сергей.pdf']})
            vector = unpack_embedding(result['embedding'])
            self.assertEqual(len(vector), 384)
            self.assertAlmostEqual(float((vector @ vector) ** .5), 1, places=3)
            self.assertEqual(result['embedding_source'], 'metadata')
            self.assertGreaterEqual(result['embedding_chunks'], 2)
            self.assertEqual(metadata_result['embedding_source'], 'metadata')
            self.assertEqual(metadata_result['metadata_entities']['person'], 1)
            self.assertEqual(len(unpack_embedding(metadata_result['embedding'])), 384)

    @unittest.skipUnless(os.environ.get('CORPUSPICK_TEST_E5') == '1',
                         'Optional local multilingual-e5 integration')
    def test_end_to_end_metadata_embeddings_separate_synthetic_topics(self):
        installed_root = Path(os.environ.get('LOCALAPPDATA', '')) / 'CorpusPick'
        installed = model_directory(installed_root)
        self.assertTrue(model_ready(installed_root))
        with tempfile.TemporaryDirectory(prefix='CorpusPick-e5-session-') as directory:
            base = Path(directory)
            root = base / 'documents'
            root.mkdir()
            for index in range(5):
                (root / f'АКТ приемки {index}.txt').write_text(
                    ('акт приемки выполненных работ комиссия результат оборудование ' + str(index) + ' ') * 30,
                    encoding='utf-8')
                (root / f'ДС соглашение {index}.txt').write_text(
                    ('дополнительное соглашение договор стороны условия срок оплата ' + str(index) + ' ') * 30,
                    encoding='utf-8')
            session = Session(root, base / 'state', read_only=True)
            try:
                with patch('corpuspick.embeddings.ensure_model', return_value=installed):
                    _, groups, notes = session.group_similar()
                acts = {groups[f'АКТ приемки {index}.txt'] for index in range(5)}
                agreements = {groups[f'ДС соглашение {index}.txt'] for index in range(5)}
                self.assertEqual(len(acts), 1)
                self.assertEqual(len(agreements), 1)
                self.assertNotEqual(acts, agreements)
                self.assertTrue(all('OCR-текст не сохраняется' in note for note in notes.values()))
                before = next(d for d in session.state['documents'] if d['path'] == 'АКТ приемки 0.txt')['similarity']
                (root / 'АКТ приемки 0.txt').rename(root / 'Общие_АКТ приемки 0.txt')
                with patch('corpuspick.embeddings.ensure_model', return_value=installed):
                    session.group_similar()
                after = next(d for d in session.state['documents'] if d['path'] == 'Общие_АКТ приемки 0.txt')['similarity']
                self.assertEqual(before['content_embedding'], after['content_embedding'])
                self.assertNotEqual(before['metadata_key'], after['metadata_key'])
                self.assertFalse(after['content_embedding_legacy'])
            finally:
                session.close()

    @unittest.skipUnless(os.environ.get('CORPUSPICK_TEST_E5') == '1' and
                         os.environ.get('CORPUSPICK_TEST_OCR') == '1',
                         'Optional full OCR + NER + E5 worker integration')
    def test_scanned_pdf_returns_only_numeric_features(self):
        from PIL import Image, ImageDraw, ImageFont
        import pymupdf

        data_root = Path(os.environ.get('LOCALAPPDATA', '')) / 'CorpusPick'
        self.assertTrue(model_ready(data_root))
        self.assertTrue(ocr_model_ready(data_root))
        font_path = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / 'arial.ttf'
        if not font_path.is_file():
            self.skipTest('Synthetic Cyrillic font unavailable')
        with tempfile.TemporaryDirectory(prefix='CorpusPick-full-ocr-') as directory:
            image = Image.new('RGB', (1200, 1600), 'white')
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype(str(font_path), 52)
            draw.text((80, 150), 'АКТ ПРИЕМКИ Иванов Сергей 12.03.2024', fill='black', font=font)
            png, pdf = Path(directory) / 'page.png', Path(directory) / 'scan.pdf'
            image.save(png)
            document = pymupdf.open()
            page = document.new_page(width=600, height=800)
            page.insert_image(page.rect, filename=str(png))
            document.save(pdf)
            document.close()
            with StatsWorker(timeout=180, target=embedding_worker,
                             init_args=(str(model_directory(data_root)), str(ocr_model_path(data_root)))) as worker:
                result = worker.count(pdf, threading.Event(), request={
                    'path': str(pdf), 'metadata_paths': ['Акты/Акт приемки 2024.pdf']})
            self.assertEqual(result['embedding_source'], 'ocr+metadata')
            self.assertEqual(result['ocr_pages'], 1)
            self.assertTrue(result['layout_embedding'])
            self.assertIn(result['ocr_provider'], ('CPUExecutionProvider', 'DmlExecutionProvider',
                                                   'CUDAExecutionProvider'))
            self.assertEqual(len(unpack_embedding(result['embedding'])), 384)
            self.assertNotIn('texts', result)


if __name__ == '__main__':
    unittest.main()
