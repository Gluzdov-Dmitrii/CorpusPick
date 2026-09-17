"""First-page previews with a persistent local PNG cache."""
from __future__ import annotations

import os
import re
import hashlib
import json
import shutil
import subprocess
from .process_job import run_owned_command
from .office_profile import create_profile, pdf_filter, OFFICE_CONVERSION_TIMEOUT, OFFICE_MEMORY_LIMIT
import tempfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile



IMAGE_SUFFIXES = {
    '.bmp', '.gif', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp',
}
TEXT_SUFFIXES = {
    '.csv', '.htm', '.html', '.ini', '.json', '.log', '.md', '.py',
    '.sql', '.text', '.tsv', '.txt', '.xml', '.yaml', '.yml',
}
OOXML_SUFFIXES = {'.docx', '.pptx', '.xlsx'}
OFFICE_SUFFIXES = OOXML_SUFFIXES | {'.doc', '.ppt', '.rtf', '.xls'}
TRANSIENT_FILE_PREFIXES = ('~$',)
MAX_TEXT_BYTES = 96 * 1024
MAX_TEXT_CHARS = 16_000
MAX_TEXT_LINES = 220
IMAGE_CACHE_VERSION = 1
IMAGE_CACHE_FINGERPRINT_BYTES = 64 * 1024
PDF_PREVIEW_DPI = 120
PDF_PREVIEW_MAX_PIXELS = 5_000_000
OFFICE_RENDER_FAILURE_LIMIT = 3
_LIBREOFFICE_UNAVAILABLE = False
_LIBREOFFICE_FAILURES = 0
_SOFFICE_UNKNOWN = object()
_SOFFICE_COMMAND = _SOFFICE_UNKNOWN
_REUSE_OFFICE = False
_OFFICE_SESSION = None


def close_office_session():
    global _OFFICE_SESSION
    if _OFFICE_SESSION is not None:
        _OFFICE_SESSION.close()
        _OFFICE_SESSION = None


def warm_office_session():
    global _OFFICE_SESSION
    from .office_session import OfficeSession
    if _OFFICE_SESSION is None:
        office = _find_soffice()
        if not office:
            return False
        _OFFICE_SESSION = OfficeSession(office)
    try:
        _OFFICE_SESSION.start()
        return True
    except Exception:
        close_office_session()
        return False


def _cache_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
    root = base / "CorpusPick" / "preview-cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_file(parts: list[str]) -> Path:
    raw = "|".join(parts)
    return _cache_root() / (hashlib.sha256(raw.encode("utf-8")).hexdigest() + ".png")


def _image_cache_path(path: Path, variant: str, info=None) -> Path:
    """Legacy exact-path key kept so previews rendered by older builds still load."""
    info = info or path.stat()
    return _cache_file([
        str(IMAGE_CACHE_VERSION), variant, os.path.normcase(str(path.resolve())),
        str(info.st_dev), str(info.st_ino), str(info.st_size), str(info.st_mtime_ns),
    ])


def _stable_path_image_cache_path(path: Path, variant: str, info=None) -> Path:
    info = info or path.stat()
    return _cache_file([
        str(IMAGE_CACHE_VERSION), 'stable-path-v1', variant,
        os.path.normcase(str(path.resolve())), str(info.st_size), str(info.st_mtime_ns),
    ])


def _file_content_fingerprint(path: Path, info=None) -> str:
    info = info or path.stat()
    digest = hashlib.sha256()
    digest.update(str(info.st_size).encode('ascii'))
    digest.update(b'\0')
    with path.open('rb') as stream:
        digest.update(stream.read(IMAGE_CACHE_FINGERPRINT_BYTES))
        if info.st_size > IMAGE_CACHE_FINGERPRINT_BYTES:
            stream.seek(max(0, info.st_size - IMAGE_CACHE_FINGERPRINT_BYTES))
            digest.update(b'\0')
            digest.update(stream.read(IMAGE_CACHE_FINGERPRINT_BYTES))
    return digest.hexdigest()


def _content_image_cache_path(path: Path, variant: str, info=None) -> Path:
    info = info or path.stat()
    return _cache_file([
        str(IMAGE_CACHE_VERSION), 'content-v1', variant, path.suffix.lower(),
        str(info.st_size), _file_content_fingerprint(path, info),
    ])


def _unique_paths(paths):
    seen = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            yield path


def _write_image_cache_paths(path: Path, variant: str) -> list[Path]:
    info = path.stat()
    paths = [_stable_path_image_cache_path(path, variant, info)]
    try:
        paths.append(_content_image_cache_path(path, variant, info))
    except OSError:
        pass
    return list(_unique_paths(paths))


def _find_cached_image_path(path: Path, variant: str, content=True) -> Path | None:
    info = path.stat()
    for cached in _unique_paths([
            _stable_path_image_cache_path(path, variant, info),
            _image_cache_path(path, variant, info)]):
        if cached.exists():
            return cached
    if not content:
        return None
    try:
        cached = _content_image_cache_path(path, variant, info)
    except OSError:
        return None
    if cached.exists():
        return cached
    return None


def _write_cache_file(destination: Path, data: bytes) -> None:
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, destination)


def _store_cached_image_family(path: Path, variant: str, data: bytes, source: Path | None = None) -> None:
    root = None
    for destination in _write_image_cache_paths(path, variant):
        try:
            if source is not None and destination == source:
                continue
            if source is not None and destination.exists():
                continue
            _write_cache_file(destination, data)
            root = destination.parent
        except OSError:
            continue
    # Keep completed prerenders on disk; automatic eviction caused repeated conversion.


def _cached_image(path: Path, variant: str, title: str, content=True) -> dict | None:
    try:
        cached = _find_cached_image_path(path, variant, content=content)
        if cached is None:
            return None
        data = cached.read_bytes()
        if cached != _stable_path_image_cache_path(path, variant, path.stat()):
            _store_cached_image_family(path, variant, data, source=cached)
        return {'kind': 'image', 'title': title, 'data': data}
    except OSError:
        return None


def _cache_variants(path: Path) -> list[tuple[str, str]]:
    if transient_preview_file(path):
        return []
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        return [('pdf-120dpi', 'PDF · первая страница')]
    if suffix in IMAGE_SUFFIXES:
        return [('image-thumbnail', 'Изображение · первая страница/кадр')]
    variants = []
    if suffix in ('.doc', '.docx', '.rtf'):
        variants.append(('word-com-page1', f'{suffix.upper()[1:]} · первая страница через Word'))
    if suffix in ('.ppt', '.pptx'):
        variants.append(('powerpoint-com-slide1', f'{suffix.upper()[1:]} · первый слайд через PowerPoint'))
    if suffix in OFFICE_SUFFIXES:
        variants.append(('libreoffice-page1', f'{suffix.upper()[1:]} · первая страница через LibreOffice'))
    return variants


def preview_cache_candidate(path: Path | str) -> bool:
    return bool(_cache_variants(Path(path)))


def parallel_preview_candidate(path: Path | str) -> bool:
    path = Path(path)
    return not transient_preview_file(path) and path.suffix.lower() in IMAGE_SUFFIXES | {'.pdf'}


def office_preview_candidate(path: Path | str) -> bool:
    path = Path(path)
    return not transient_preview_file(path) and path.suffix.lower() in OFFICE_SUFFIXES


def transient_preview_file(path: Path | str) -> bool:
    return Path(path).name.startswith(TRANSIENT_FILE_PREFIXES)


def cached_preview_result(path: Path | str) -> dict | None:
    path = Path(path)
    for content in (False, True):
        for variant, title in _cache_variants(path):
            cached = _cached_image(path, variant, title, content=content)
            if cached:
                return cached
    return None


def cached_preview_available(path: Path | str) -> bool:
    path = Path(path)
    for content in (False, True):
        for variant, _title in _cache_variants(path):
            try:
                if _find_cached_image_path(path, variant, content=content) is not None:
                    return True
            except OSError:
                continue
    return False


def _store_cached_image(path: Path, variant: str, data: bytes) -> None:
    try:
        _store_cached_image_family(path, variant, data)
    except OSError:
        pass


def image_cache_size() -> int:
    """Count only owned cache files; never follow links or read image contents."""
    total = 0
    with os.scandir(_cache_root()) as entries:
        for entry in entries:
            if re.fullmatch(r'[0-9a-f]{64}\.(png|tmp)', entry.name) and entry.is_file(follow_symlinks=False):
                try:
                    total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    return total


def clear_image_cache() -> int:
    """Delete only direct, recognized cache files, never directories or links."""
    failed = 0
    root = _cache_root().resolve()
    with os.scandir(root) as entries:
        for entry in entries:
            if not re.fullmatch(r'[0-9a-f]{64}\.(png|tmp)', entry.name):
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            target = Path(entry.path)
            if target.resolve().parent != root:
                continue
            try:
                target.unlink(missing_ok=True)
            except OSError:
                failed += 1
    return failed


def _limited_text(value: str) -> str:
    value = value.replace('\r\n', '\n').replace('\r', '\n').strip()
    lines = value.splitlines()[:MAX_TEXT_LINES]
    text = '\n'.join(lines)
    if len(value.splitlines()) > MAX_TEXT_LINES:
        text += '\n\n…'
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rstrip() + '\n\n…'
    return text or 'Текстовый preview пуст.'


def _read_text(path: Path) -> dict:
    with path.open('rb') as stream:
        data = stream.read(MAX_TEXT_BYTES)
    for encoding in ('utf-8-sig', 'utf-8', 'cp1251', 'cp866', 'latin-1'):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode('utf-8', errors='replace')
    return {'kind': 'text', 'title': 'Текст · начало файла', 'text': _limited_text(text)}


def _png_from_pillow_image(image) -> bytes:
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image)
    if getattr(image, 'is_animated', False):
        image.seek(0)
    if image.mode not in ('RGB', 'RGBA'):
        image = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
    image.thumbnail((1500, 2100), Image.Resampling.LANCZOS)
    stream = BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def _render_image(path: Path) -> dict:
    from PIL import Image

    title = 'Изображение · первая страница/кадр'
    cached = _cached_image(path, 'image-thumbnail', title)
    if cached:
        return cached
    with Image.open(path) as image:
        data = _png_from_pillow_image(image)
        _store_cached_image(path, 'image-thumbnail', data)
        return {'kind': 'image', 'title': title, 'data': data}


def _render_pdf(path: Path) -> dict:
    title = 'PDF · первая страница'
    cached = _cached_image(path, 'pdf-120dpi', title)
    if cached:
        return cached
    try:
        import fitz
    except Exception:
        return {'kind': 'message', 'title': 'PDF preview',
                'text': 'Для preview PDF нужен PyMuPDF из requirements.txt.'}
    document = fitz.open(path)
    try:
        if document.page_count < 1:
            return {'kind': 'message', 'title': 'PDF preview', 'text': 'В PDF нет страниц.'}
        page = document.load_page(0)
        pixmap = page.get_pixmap(matrix=_pdf_preview_matrix(page), alpha=False)
        data = pixmap.tobytes('png')
        _store_cached_image(path, 'pdf-120dpi', data)
        return {'kind': 'image', 'title': title, 'data': data}
    finally:
        document.close()


def _render_pdf_uncached(path: Path, title: str) -> dict:
    try:
        import fitz
    except Exception:
        return {'kind': 'message', 'title': title, 'text': 'Для preview PDF нужен PyMuPDF из requirements.txt.'}
    document = fitz.open(path)
    try:
        if document.page_count < 1:
            return {'kind': 'message', 'title': title, 'text': 'В PDF нет страниц.'}
        page = document.load_page(0)
        pixmap = page.get_pixmap(matrix=_pdf_preview_matrix(page), alpha=False)
        return {'kind': 'image', 'title': title, 'data': pixmap.tobytes('png')}
    finally:
        document.close()


def _pdf_preview_matrix(page):
    import fitz

    zoom = PDF_PREVIEW_DPI / 72
    width = max(1, float(page.rect.width) * zoom)
    height = max(1, float(page.rect.height) * zoom)
    pixels = width * height
    if pixels > PDF_PREVIEW_MAX_PIXELS:
        zoom *= (PDF_PREVIEW_MAX_PIXELS / pixels) ** 0.5
    return fitz.Matrix(zoom, zoom)


def _xml_root(archive: ZipFile, name: str):
    try:
        return ElementTree.fromstring(archive.read(name))
    except KeyError:
        return None


def _docx_text(path: Path) -> dict:
    lines = []
    with ZipFile(path) as archive:
        root = _xml_root(archive, 'word/document.xml')
        if root is not None:
            for paragraph in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
                parts = []
                for element in paragraph.iter():
                    tag = element.tag
                    if tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t':
                        parts.append(element.text or '')
                    elif tag == '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tab':
                        parts.append('\t')
                    elif tag in ('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}br',
                                 '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}cr'):
                        parts.append('\n')
                line = ''.join(parts).strip()
                if line:
                    lines.append(line)
                if len(lines) >= MAX_TEXT_LINES:
                    break
    return {'kind': 'text', 'title': 'DOCX · быстрый текстовый preview',
            'text': _limited_text('\n'.join(lines))}


def _slide_number(name: str) -> int:
    match = re.search(r'slide(\d+)\.xml$', name)
    return int(match.group(1)) if match else 0


def _pptx_text(path: Path) -> dict:
    with ZipFile(path) as archive:
        slides = sorted((name for name in archive.namelist()
                         if name.startswith('ppt/slides/slide') and name.endswith('.xml')),
                        key=_slide_number)
        if not slides:
            return {'kind': 'message', 'title': 'PPTX preview', 'text': 'В презентации нет слайдов.'}
        root = ElementTree.fromstring(archive.read(slides[0]))
    lines = [element.text.strip() for element in root.iter()
             if element.tag.endswith('}t') and element.text and element.text.strip()]
    return {'kind': 'text', 'title': 'PPTX · первый слайд',
            'text': _limited_text('\n'.join(lines))}


def _shared_strings(archive: ZipFile) -> list[str]:
    root = _xml_root(archive, 'xl/sharedStrings.xml')
    if root is None:
        return []
    strings = []
    for item in root.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si'):
        strings.append(''.join(element.text or '' for element in item.iter()
                               if element.tag.endswith('}t')).strip())
    return strings


def _xlsx_text(path: Path) -> dict:
    with ZipFile(path) as archive:
        sheets = sorted(name for name in archive.namelist()
                        if name.startswith('xl/worksheets/sheet') and name.endswith('.xml'))
        if not sheets:
            return {'kind': 'message', 'title': 'XLSX preview', 'text': 'В книге нет листов.'}
        shared = _shared_strings(archive)
        root = ElementTree.fromstring(archive.read(sheets[0]))
    lines = []
    namespace = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    for row in root.iter(namespace + 'row'):
        values = []
        for cell in list(row)[:16]:
            if not cell.tag.endswith('}c'):
                continue
            cell_type = cell.attrib.get('t')
            value = ''
            if cell_type == 'inlineStr':
                value = ''.join(element.text or '' for element in cell.iter() if element.tag.endswith('}t'))
            else:
                node = cell.find(namespace + 'v')
                if node is not None and node.text is not None:
                    value = node.text
                    if cell_type == 's':
                        try:
                            value = shared[int(value)]
                        except (IndexError, ValueError):
                            pass
            values.append(value)
        if any(value for value in values):
            lines.append('\t'.join(values))
        if len(lines) >= 60:
            break
    return {'kind': 'text', 'title': 'XLSX · первый лист',
            'text': _limited_text('\n'.join(lines))}


def _office_text(path: Path) -> dict:
    if path.suffix.lower() == '.docx':
        return _docx_text(path)
    if path.suffix.lower() == '.pptx':
        return _pptx_text(path)
    if path.suffix.lower() == '.xlsx':
        return _xlsx_text(path)
    return {'kind': 'message', 'title': 'Office preview',
            'text': 'Для старых DOC/XLS/PPT нужен установленный LibreOffice для рендера первой страницы.'}


def _command_status(process) -> dict:
    output = getattr(process, 'stdout', b'') or b''
    if isinstance(output, str):
        text = output
    else:
        text = output.decode('utf-8-sig', errors='ignore')
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text.splitlines()[-1])
    except (json.JSONDecodeError, TypeError):
        return {}


def _record_libreoffice_result(success: bool, count: bool = True) -> None:
    global _LIBREOFFICE_UNAVAILABLE, _LIBREOFFICE_FAILURES
    if success:
        _LIBREOFFICE_FAILURES = 0
        return
    if not count:
        return
    _LIBREOFFICE_FAILURES += 1
    if _LIBREOFFICE_FAILURES >= OFFICE_RENDER_FAILURE_LIMIT:
        _LIBREOFFICE_UNAVAILABLE = True


def _render_word_with_com(path: Path) -> dict | None:
    # COM activation is brokered outside our process job and can outlive us.
    # Reuse existing renders, but never start Office or change its user settings.
    return _cached_image(path, 'word-com-page1', f'{path.suffix.upper()[1:]} · первая страница через Word')


def _render_powerpoint_with_com(path: Path) -> dict | None:
    return _cached_image(path, 'powerpoint-com-slide1', f'{path.suffix.upper()[1:]} · первый слайд через PowerPoint')


def _find_soffice() -> str | None:
    global _SOFFICE_COMMAND
    if _SOFFICE_COMMAND is not _SOFFICE_UNKNOWN:
        return _SOFFICE_COMMAND
    # Dedicated extracted runtime: no system registration or user's Office profile.
    bundled = Path(__file__).resolve().parent.parent / 'apps' / 'LibreOffice' / 'program' / 'soffice.com'
    if bundled.is_file():
        _SOFFICE_COMMAND = str(bundled)
        return _SOFFICE_COMMAND
    for command in ('soffice', 'libreoffice'):
        found = shutil.which(command)
        if found:
            _SOFFICE_COMMAND = found
            return found
    if os.name == 'nt':
        for candidate in (
            Path(os.environ.get('PROGRAMFILES', '')) / 'LibreOffice/program/soffice.exe',
            Path(os.environ.get('PROGRAMFILES(X86)', '')) / 'LibreOffice/program/soffice.exe',
        ):
            if candidate.exists():
                console = candidate.with_suffix('.com')
                _SOFFICE_COMMAND = str(console if console.is_file() else candidate)
                return _SOFFICE_COMMAND
    _SOFFICE_COMMAND = None
    return None


def _render_office_with_libreoffice(path: Path) -> dict | None:
    title = f'{path.suffix.upper()[1:]} · первая страница через LibreOffice'
    cached = _cached_image(path, 'libreoffice-page1', title)
    if cached:
        return cached
    if _LIBREOFFICE_UNAVAILABLE:
        return None
    soffice = _find_soffice()
    if not soffice:
        return None
    with tempfile.TemporaryDirectory(prefix='corpuspick-preview-') as directory:
        output = Path(directory)
        source = output / ('input' + path.suffix.lower())
        # Converters see only a disposable copy, never open/lock the original.
        shutil.copyfile(path, source)
        try:
            if _REUSE_OFFICE:
                if not warm_office_session():
                    _record_libreoffice_result(False)
                    return None
                _OFFICE_SESSION.render(source, output / 'input.pdf', pdf_filter(path.suffix.lower()).split(':')[1])
            else:
                profile = output / 'profile'
                create_profile(profile)
                command = [
                    soffice, f'-env:UserInstallation={profile.resolve().as_uri()}', '--headless', '--nologo',
                    '--nodefault', '--nofirststartwizard', '--norestore',
                    '--convert-to', pdf_filter(path.suffix.lower()), '--outdir', str(output), str(source),
                ]
                run_owned_command(command, timeout=OFFICE_CONVERSION_TIMEOUT, memory_limit=OFFICE_MEMORY_LIMIT)
        except (OSError, subprocess.TimeoutExpired):
            _record_libreoffice_result(False)
            return None
        pdfs = sorted(output.glob('*.pdf'), key=lambda item: item.stat().st_mtime_ns, reverse=True)
        if not pdfs:
            _record_libreoffice_result(False, count=False)
            return None
        result = _render_pdf_uncached(pdfs[0], title)
        if result.get('kind') == 'image':
            _store_cached_image(path, 'libreoffice-page1', result['data'])
            _record_libreoffice_result(True)
        else:
            _record_libreoffice_result(False)
        return result


def preview_document(path: Path) -> dict:
    if transient_preview_file(path):
        return {'kind': 'message', 'title': 'Preview',
                'text': 'Временный служебный файл Office пропущен.'}
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        return _render_pdf(path)
    if suffix in IMAGE_SUFFIXES:
        return _render_image(path)
    if suffix in TEXT_SUFFIXES:
        return _read_text(path)
    if suffix in OFFICE_SUFFIXES:
        # Check all path-key variants before legacy lookups fingerprint the source.
        cached = cached_preview_result(path)
        if cached:
            return cached
        visual = _render_word_with_com(path)
        if visual:
            return visual
        visual = _render_powerpoint_with_com(path)
        if visual:
            return visual
        visual = _render_office_with_libreoffice(path)
        if visual:
            return visual
        try:
            return _office_text(path)
        except (BadZipFile, KeyError, ElementTree.ParseError, OSError):
            return {'kind': 'message', 'title': 'Office preview',
                    'text': 'Быстрый preview этого Office-файла недоступен.'}
    return {'kind': 'message', 'title': 'Preview',
            'text': 'Для этого формата быстрый preview пока не поддерживается.'}


def preview_worker(connection):
    global _REUSE_OFFICE
    _REUSE_OFFICE = os.name == 'nt'
    import sys
    with open(os.devnull, 'w') as quiet:
        sys.stdout = sys.stderr = quiet
        try:
            while True:
                path = connection.recv()
                if path is None:
                    break
                try:
                    if isinstance(path, dict) and path.get('warmup'):
                        result = {'ready': warm_office_session()}
                    else:
                        result = preview_document(Path(path))
                except Exception:
                    result = {'kind': 'message', 'title': 'Preview',
                              'text': 'Preview недоступен: файл повреждён, защищён или занят.'}
                connection.send(result)
        except (EOFError, BrokenPipeError):
            pass
        finally:
            close_office_session()
            connection.close()
