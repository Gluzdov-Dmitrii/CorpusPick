"""Document composition, never document previews or content logs."""
import logging
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .core import native

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'



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
            return {'pages': pages, 'pages_estimated': True,
                    'figures': sum(1 for _ in root.iter(W + 'drawing')) + sum(1 for _ in root.iter(W + 'pict')),
                    'tables': sum(1 for _ in root.iter(W + 'tbl')),
                    'info': 'DOCX: страницы из сохранённых свойств (могут устареть); рисунки — графические объекты основного текста. Колонтитулы не входят.'}
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
                    'info': 'PDF: точное число страниц. Рисунки и таблицы PDF не считаются.'}
    if suffix == '.doc':
        try:
            import olefile
        except ImportError:
            return {'info': 'Для DOC установите requirements.txt'}
        with olefile.OleFileIO(native(path)) as document:
            props = document.getproperties('\x05SummaryInformation') if document.exists('\x05SummaryInformation') else {}
            pages = props.get(14)
            return {'pages': pages if isinstance(pages, int) and pages > 0 else None, 'pages_estimated': True,
                    'info': 'DOC: сохранённые страницы (могут устареть); точную статистику можно уточнить через LibreOffice.'}
    return {'info': 'Подсчёт состава для этого формата не поддерживается'}
