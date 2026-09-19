"""Bounded local OCR/NER/layout extraction for document embeddings."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import unicodedata

OCR_VERSION = 2
OCR_DPI = 120
OCR_MODEL_NAME = 'rapidocr-cyrillic-v5'
OCR_MODEL_FILE = 'cyrillic_PP-OCRv5_rec_mobile.onnx'
OCR_MODEL_URL = ('https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/'
                 'onnx/PP-OCRv5/rec/cyrillic_PP-OCRv5_rec_mobile.onnx')
OCR_MODEL_SIZE = 8_074_092
OCR_MODEL_SHA256 = '90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711'
LAYOUT_DIMENSIONS = 384


def ocr_model_directory(data_root):
    return Path(data_root) / 'models' / OCR_MODEL_NAME


def ocr_model_path(data_root):
    return ocr_model_directory(data_root) / OCR_MODEL_FILE


def ocr_model_ready(data_root):
    path = ocr_model_path(data_root)
    try:
        return path.is_file() and path.stat().st_size == OCR_MODEL_SIZE
    except OSError:
        return False


def ensure_ocr_model(data_root, cancel=None):
    from .embeddings import _download
    path = ocr_model_path(data_root)
    if not ocr_model_ready(data_root):
        try:
            _download(OCR_MODEL_URL, path, OCR_MODEL_SIZE, OCR_MODEL_SHA256, cancel)
        except Exception as exc:
            from .core import Cancelled
            if isinstance(exc, Cancelled):
                raise
            raise ValueError('Не удалось скачать локальную Cyrillic OCR-модель.') from None
    return path


def _create_ocr(model_path, provider):
    from rapidocr import EngineType, LangRec, ModelType, OCRVersion, RapidOCR
    params = {
        'Rec.engine_type': EngineType.ONNXRUNTIME,
        'Rec.lang_type': LangRec.CYRILLIC,
        'Rec.model_type': ModelType.MOBILE,
        'Rec.ocr_version': OCRVersion.PPOCRV5,
        'Rec.model_path': str(model_path),
        'Global.log_level': 'error',
    }
    if provider == 'DmlExecutionProvider':
        params['EngineConfig.onnxruntime.use_dml'] = True
    elif provider == 'CUDAExecutionProvider':
        params['EngineConfig.onnxruntime.use_cuda'] = True
    return RapidOCR(params=params)


class _OcrEngine:
    def __init__(self, model_path):
        import onnxruntime as ort
        available = ort.get_available_providers()
        self.model_path = model_path
        self.provider = next((name for name in ('CUDAExecutionProvider', 'DmlExecutionProvider')
                              if name in available), 'CPUExecutionProvider')
        try:
            self.engine = _create_ocr(model_path, self.provider)
        except Exception:
            self.provider = 'CPUExecutionProvider'
            self.engine = _create_ocr(model_path, self.provider)

    def __call__(self, image):
        try:
            return self.engine(image)
        except Exception:
            if self.provider == 'CPUExecutionProvider':
                raise
            self.provider = 'CPUExecutionProvider'
            self.engine = _create_ocr(self.model_path, self.provider)
            return self.engine(image)


def load_ocr(model_path):
    return _OcrEngine(model_path)


def load_ner():
    from natasha import NewsEmbedding, NewsNERTagger, Segmenter
    embedding = NewsEmbedding()
    return Segmenter(), NewsNERTagger(embedding)


_DATE = re.compile(
    r'(?<!\d)(?:\d{1,2}[.\-/]\d{1,2}[.\-/](?:\d{2}|\d{4})|'
    r'(?:19|20)\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}|(?:19|20)\d{2})(?!\d)')
_MONTH_DATE = re.compile(
    r'(?<!\w)\d{1,2}\s+(?:январ[ья]|феврал[ья]|марта|апрел[ья]|мая|июн[ья]|июл[ья]|'
    r'августа|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])\s+(?:19|20)\d{2}(?:\s*г(?:ода|\.)?)?', re.I)


def anonymize_entities(text, ner=None):
    """Replace entity values while retaining their semantic types."""
    value = unicodedata.normalize('NFKC', text)
    counts = Counter()
    replacements = []
    if ner is not None and value.strip():
        try:
            from natasha import Doc
            segmenter, tagger = ner
            document = Doc(value[:24_000])
            document.segment(segmenter)
            document.tag_ner(tagger)
            names = {'PER': ' персона ', 'ORG': ' организация ', 'LOC': ' место '}
            for span in document.spans:
                if span.type in names:
                    replacements.append((span.start, span.stop, names[span.type]))
                    counts[span.type.casefold()] += 1
        except Exception:
            replacements = []
    for start, stop, replacement in reversed(replacements):
        value = value[:start] + replacement + value[stop:]
    value, found = _MONTH_DATE.subn(' дата ', value)
    counts['date'] += found
    value, found = _DATE.subn(' дата ', value)
    counts['date'] += found
    value, found = re.subn(r'(?<!\w)(?:(?:№|no\.?)\s*)?\d+(?:[.,:/\-]\d+)*(?!\w)',
                           ' число ', value, flags=re.I)
    counts['number'] += found
    value = re.sub(r'\s+', ' ', value.casefold().replace('ё', 'е')).strip()
    return value, dict(counts)


def _box_layout(image, boxes):
    """128 numeric layout features: text occupancy, projections, coarse ink."""
    import cv2
    import numpy as np
    height, width = image.shape[:2]
    occupancy = np.zeros((8, 8), dtype=np.float32)
    for box in boxes or []:
        points = np.asarray(box, dtype=np.float32).reshape(-1, 2)
        if not len(points):
            continue
        x0, y0 = points.min(axis=0)
        x1, y1 = points.max(axis=0)
        left, right = np.clip([int(x0 / max(width, 1) * 8), int(x1 / max(width, 1) * 8)], 0, 7)
        top, bottom = np.clip([int(y0 / max(height, 1) * 8), int(y1 / max(height, 1) * 8)], 0, 7)
        occupancy[top:bottom + 1, left:right + 1] += 1
    occupancy = np.log1p(occupancy)
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    ink = (gray < 235).astype(np.float32)
    rows = cv2.resize(ink.mean(axis=1)[:, None], (1, 16), interpolation=cv2.INTER_AREA).ravel()
    columns = cv2.resize(ink.mean(axis=0)[None, :], (16, 1), interpolation=cv2.INTER_AREA).ravel()
    blurred = cv2.GaussianBlur(ink, (0, 0), sigmaX=max(2, width / 80), sigmaY=max(2, height / 80))
    coarse = cv2.resize(blurred, (8, 4), interpolation=cv2.INTER_AREA).ravel()
    vector = np.concatenate((occupancy.ravel(), rows, columns, coarse)).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def _native_blocks(page, scale_x=1.0, scale_y=1.0):
    text, boxes = [], []
    for block in page.get_text('blocks', sort=True):
        if len(block) >= 5 and str(block[4]).strip():
            text.append(str(block[4]))
            x0, y0, x1, y1 = map(float, block[:4])
            x0, x1, y0, y1 = x0 * scale_x, x1 * scale_x, y0 * scale_y, y1 * scale_y
            boxes.append([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
    return '\n'.join(text), boxes


def sample_pdf(path, ocr_engine):
    """Read only the first PDF page and return no persistent text."""
    import numpy as np
    import pymupdf
    document = pymupdf.open(path)
    try:
        if not document.page_count:
            return {'texts': [], 'layout': np.zeros(LAYOUT_DIMENSIONS, dtype=np.float32),
                    'sampled_pages': 0, 'ocr_pages': 0, 'native_pages': 0}
        slots = [0]
        cache, texts, layouts = {}, [], []
        ocr_pages = native_pages = 0
        for index in slots:
            if index not in cache:
                page = document[index]
                pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csRGB, alpha=False, annots=False)
                image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
                text, boxes = _native_blocks(page, pixmap.width / page.rect.width,
                                             pixmap.height / page.rect.height)
                if len(re.findall(r'[^\W_]+', text)) >= 20:
                    source = 'native'
                elif ocr_engine is not None:
                    result = ocr_engine(image)
                    text = '\n'.join(result.txts or [])
                    boxes = list(result.boxes) if result.boxes is not None else []
                    source = 'ocr'
                else:
                    text, boxes, source = '', [], 'unavailable'
                cache[index] = (text[:12_000], _box_layout(image, boxes), source)
            text, layout, source = cache[index]
            texts.append(text)
            layouts.append(layout)
            if source == 'ocr':
                ocr_pages += 1
            else:
                native_pages += 1
        # Keep the vector schema compatible; unused page slots contain no signal.
        layout = np.zeros(LAYOUT_DIMENSIONS, dtype=np.float32)
        layout[:128] = layouts[0]
        norm = float(np.linalg.norm(layout))
        if norm:
            layout /= norm
        return {'texts': texts, 'layout': layout, 'sampled_pages': len(set(slots)),
                'ocr_pages': len({index for index in set(slots) if cache[index][2] == 'ocr'}),
                'native_pages': len({index for index in set(slots) if cache[index][2] == 'native'}),
                'ocr_failed': any(cache[index][2] == 'unavailable' for index in set(slots)),
                'page_count': document.page_count}
    finally:
        document.close()
