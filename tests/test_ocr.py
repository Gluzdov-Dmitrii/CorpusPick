import os
from pathlib import Path
import tempfile
import unittest

import numpy as np

from corpuspick.ocr import (_box_layout, anonymize_entities, load_ner, load_ocr,
                            ocr_model_path, ocr_model_ready, sample_pdf)


class OcrNerTests(unittest.TestCase):
    def test_local_ner_replaces_person_location_date_and_number(self):
        clean, counts = anonymize_entities(
            'Акт подписал Иванов Сергей в Москве 12 марта 2024 года, документ № 57.', load_ner())
        self.assertNotIn('иванов', clean)
        self.assertNotIn('сергей', clean)
        self.assertNotIn('москве', clean)
        self.assertNotIn('2024', clean)
        self.assertIn('персона', clean)
        self.assertIn('место', clean)
        self.assertIn('дата', clean)
        self.assertGreaterEqual(counts.get('per', 0), 1)
        self.assertGreaterEqual(counts.get('date', 0), 1)

    def test_layout_embedding_uses_geometry_not_recognized_words(self):
        image = np.full((800, 600, 3), 255, dtype=np.uint8)
        image[100:110, 80:520] = 0
        image[300:310, 80:360] = 0
        boxes = [[[80, 90], [520, 90], [520, 120], [80, 120]],
                 [[80, 290], [360, 290], [360, 320], [80, 320]]]
        first = _box_layout(image, boxes)
        second = _box_layout(image.copy(), boxes)
        shifted = _box_layout(image, [[[300, 600], [550, 600], [550, 650], [300, 650]]])
        self.assertEqual(first.shape, (128,))
        self.assertAlmostEqual(float(first @ second), 1, places=5)
        self.assertLess(float(first @ shifted), .9)

    def test_pdf_with_text_layer_skips_ocr_but_keeps_layout(self):
        import pymupdf
        with tempfile.TemporaryDirectory(prefix='CorpusPick-native-pdf-') as directory:
            path = Path(directory) / 'native.pdf'
            document = pymupdf.open()
            page = document.new_page(width=600, height=800)
            text = ' '.join(f'synthetic report field {index}' for index in range(12))
            page.insert_textbox((60, 80, 540, 500), text, fontsize=14)
            document.save(path)
            document.close()
            result = sample_pdf(path, None)
        self.assertEqual(result['sampled_pages'], 1)
        self.assertEqual(result['native_pages'], 1)
        self.assertEqual(result['ocr_pages'], 0)
        self.assertFalse(result['ocr_failed'])
        self.assertEqual(result['layout'].shape, (384,))

    @unittest.skipUnless(os.environ.get('CORPUSPICK_TEST_OCR') == '1',
                         'Optional local Cyrillic OCR integration')
    def test_scanned_russian_pdf_first_middle_last(self):
        from PIL import Image, ImageDraw, ImageFont
        import pymupdf

        data_root = Path(os.environ.get('LOCALAPPDATA', '')) / 'CorpusPick'
        self.assertTrue(ocr_model_ready(data_root))
        font_path = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / 'arial.ttf'
        if not font_path.is_file():
            self.skipTest('Synthetic Cyrillic font unavailable')
        with tempfile.TemporaryDirectory(prefix='CorpusPick-ocr-') as directory:
            image = Image.new('RGB', (1200, 1600), 'white')
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype(str(font_path), 54)
            for line, text in enumerate(('АКТ ПРИЕМКИ ВЫПОЛНЕННЫХ РАБОТ',
                                         'Иванов Сергей 12 марта 2024 года',
                                         'Комиссия приняла оборудование')):
                draw.text((90, 160 + line * 100), text, fill='black', font=font)
            png = Path(directory) / 'scan.png'
            image.save(png)
            pdf = Path(directory) / 'scan.pdf'
            document = pymupdf.open()
            page = document.new_page(width=600, height=800)
            page.insert_image(page.rect, filename=str(png))
            document.save(pdf)
            document.close()
            engine = load_ocr(ocr_model_path(data_root))
            result = sample_pdf(pdf, engine)
            recognized = ' '.join(result['texts']).casefold()
            self.assertEqual(result['sampled_pages'], 1)
            self.assertEqual(result['ocr_pages'], 1)
            self.assertIn('акт', recognized)
            self.assertEqual(result['layout'].shape, (384,))
            self.assertIn(engine.provider, ('CPUExecutionProvider', 'DmlExecutionProvider',
                                            'CUDAExecutionProvider'))


if __name__ == '__main__':
    unittest.main()
