"""Document composition, never document previews or content logs."""
import json
import logging
import os
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .core import native

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
APPENDIX = re.compile(r'^\s*(?:приложение|appendix)\s+(?:[А-ЯЁA-Z]|\d+)(?:\s*[:.\-–—]\s*.*)?\s*$', re.I)


def xml_part(archive, name):
    if archive.getinfo(name).file_size > 32_000_000:
        raise ValueError('XML too large')
    return ET.fromstring(archive.read(name))


def document_stats(path):
    suffix = path.suffix.lower()
    if Path(path).name.startswith('~$'):
        return {'info': 'Временный файл Office'}
    if suffix == '.docx':
        with ZipFile(native(path)) as archive:
            root = xml_part(archive, 'word/document.xml')
            # DrawingML / VML alternatives represent the same drawing: choose one.
            mc = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
            for alternate in root.iter(mc + 'AlternateContent'):
                choices = list(alternate)
                for child in choices[1:]:
                    alternate.remove(child)
            pages = None
            if 'docProps/app.xml' in archive.namelist():
                props = xml_part(archive, 'docProps/app.xml')
                for node in props:
                    if node.tag.endswith('}Pages') and node.text and node.text.isdigit():
                        pages = int(node.text) or None
            paragraphs = [''.join(t.text or '' for t in p.iter(W + 't')).strip() for p in root.iter(W + 'p')]
            return {'pages': pages, 'pages_estimated': True,
                    'figures': sum(1 for _ in root.iter(W + 'drawing')) + sum(1 for _ in root.iter(W + 'pict')),
                    'tables': sum(1 for _ in root.iter(W + 'tbl')),
                    'appendices': sum(bool(APPENDIX.fullmatch(p)) for p in paragraphs),
                    'appendices_estimated': True,
                    'info': 'DOCX: страницы из сохранённых свойств (могут устареть); рисунки — графические объекты основного текста; приложения ≈ отдельные заголовки «Приложение А/1». Колонтитулы не входят.'}
    if suffix == '.pdf':
        try:
            from pypdf import PdfReader
        except ImportError:
            return {'info': 'Для PDF установите requirements.txt; DOCX работает без пакетов'}
        # Third-party parser warnings may include sensitive PDF metadata.
        logging.getLogger('pypdf').setLevel(logging.CRITICAL)
        with open(native(path), 'rb') as stream:
            reader = PdfReader(stream, strict=False)
            if reader.is_encrypted and not reader.decrypt(''):
                return {'info': 'PDF защищён паролем'}
            return {'pages': len(reader.pages),
                    'info': 'PDF: точное число страниц. Рисунки, таблицы и приложения не определяются: требуется анализ вёрстки/OCR.'}
    if suffix == '.doc':
        return {'info': 'Для старого DOC: Инструменты → Подсчитать через Word'}
    return {'info': 'Подсчёт состава для этого формата не поддерживается'}


def word_stats(path):
    if os.name != 'nt':
        return {'info': 'Подсчёт через Word доступен в Windows'}
    env = os.environ.copy()
    env['CORPUSPICK_DOCUMENT'] = str(path.resolve())
    script = Path(__file__).with_name('word_stats.ps1')
    try:
        process = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                  '-File', str(script)], env=env, capture_output=True, timeout=150,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        if process.returncode:
            return {'info': 'Word недоступен или не смог открыть документ'}
        result = json.loads(process.stdout.decode('utf-8-sig').strip())
        if 'pages' in result:
            result['info'] = 'Word: страницы после перевёрстки; рисунки — InlineShapes + Shapes; таблицы основного текста; приложения ≈ отдельные заголовки. Файл не сохранялся.'
            result['appendices_estimated'] = True
        return result
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return {'info': 'Word не ответил за отведённое время или недоступен'}
