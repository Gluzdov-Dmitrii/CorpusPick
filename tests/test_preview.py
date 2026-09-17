from pathlib import Path
import io
import os
import queue
import tempfile
from threading import Event
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

from corpuspick.__main__ import App
from corpuspick.preview import (
    PDF_PREVIEW_MAX_PIXELS, _image_cache_path, _store_cached_image, cached_preview_available,
    cached_preview_result, office_preview_candidate, parallel_preview_candidate, preview_cache_candidate,
    preview_document)


class PreviewTests(unittest.TestCase):
    def test_preview_loop_starts_only_for_request(self):
        calls = []
        app = App.__new__(App)
        app.preview_closed = False
        app.preview_shutdown = Event()
        app.preview_requests = queue.Queue()
        app.events = queue.Queue()
        app.preview_requests.put((1, 'one', Path('synthetic.docx'), Event()))

        class Worker:
            def __init__(self, **_):
                calls.append('created')
            def __enter__(self):
                return self
            def __exit__(self, *_):
                calls.append('closed')
            def count(self, path, cancel, request=None):
                if request:
                    calls.append('warmup')
                    app.preview_requests.put((1, 'one', Path('synthetic.docx'), Event()))
                    return {'ready': True}
                calls.append(str(path))
                app.preview_requests.put(None)
                return {'kind': 'image', 'data': b'png'}

        with patch('corpuspick.stats_worker.StatsWorker', Worker):
            app.preview_loop()
        self.assertEqual(calls, ['created', 'synthetic.docx', 'closed'])

    def test_text_preview_reads_only_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'notes.md'
            path.write_text('# Заголовок\n' + 'строка\n' * 400, encoding='utf-8')
            result = preview_document(path)
            self.assertEqual(result['kind'], 'text')
            self.assertIn('Заголовок', result['text'])
            self.assertIn('…', result['text'])

    def test_image_preview_returns_png(self):
        try:
            from PIL import Image
        except Exception:
            self.skipTest('Pillow unavailable')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.png'
            Image.new('RGB', (200, 120), 'white').save(path)
            result = preview_document(path)
            self.assertEqual(result['kind'], 'image')
            self.assertTrue(result['data'].startswith(b'\x89PNG'))

    def test_pdf_preview_returns_first_page_png(self):
        try:
            import fitz
        except Exception:
            self.skipTest('PyMuPDF unavailable')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.pdf'
            document = fitz.open()
            page = document.new_page(width=300, height=200)
            page.insert_text((40, 80), 'Synthetic PDF')
            document.save(path)
            document.close()
            result = preview_document(path)
            self.assertEqual(result['kind'], 'image')
            self.assertTrue(result['data'].startswith(b'\x89PNG'))

    def test_huge_pdf_preview_is_pixel_capped(self):
        try:
            import fitz
            from PIL import Image
        except Exception:
            self.skipTest('PyMuPDF or Pillow unavailable')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            path = Path(directory) / 'huge-page.pdf'
            document = fitz.open()
            document.new_page(width=5000, height=5000)
            document.save(path)
            document.close()
            result = preview_document(path)
            self.assertEqual(result['kind'], 'image')
            image = Image.open(io.BytesIO(result['data']))
            self.assertLessEqual(image.width * image.height, int(PDF_PREVIEW_MAX_PIXELS * 1.02))

    def test_preview_cache_helpers_classify_renderable_images(self):
        self.assertTrue(preview_cache_candidate(Path('report.pdf')))
        self.assertTrue(preview_cache_candidate(Path('scan.png')))
        self.assertTrue(preview_cache_candidate(Path('memo.rtf')))
        self.assertTrue(preview_cache_candidate(Path('slides.pptx')))
        self.assertFalse(preview_cache_candidate(Path('notes.txt')))
        self.assertFalse(preview_cache_candidate(Path('archive.zip')))
        self.assertFalse(preview_cache_candidate(Path('~$report.docx')))
        self.assertTrue(parallel_preview_candidate(Path('report.pdf')))
        self.assertTrue(parallel_preview_candidate(Path('scan.jpg')))
        self.assertFalse(parallel_preview_candidate(Path('memo.docx')))
        self.assertFalse(parallel_preview_candidate(Path('~$report.pdf')))
        self.assertTrue(office_preview_candidate(Path('memo.docx')))
        self.assertTrue(office_preview_candidate(Path('slides.pptx')))
        self.assertTrue(office_preview_candidate(Path('book.xlsx')))
        self.assertFalse(office_preview_candidate(Path('report.pdf')))
        self.assertFalse(office_preview_candidate(Path('~$report.docx')))

    def test_cached_preview_result_uses_file_identity(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            path = Path(directory) / 'report.pdf'
            path.write_bytes(b'pdf-v1')
            _store_cached_image(path, 'pdf-120dpi', b'\x89PNG\r\ncached')
            self.assertTrue(cached_preview_available(path))
            result = cached_preview_result(path)
            self.assertEqual(result['kind'], 'image')
            self.assertEqual(result['data'], b'\x89PNG\r\ncached')

            path.write_bytes(b'pdf-v2-changed')
            self.assertFalse(cached_preview_available(path))
            self.assertIsNone(cached_preview_result(path))

    def test_cached_preview_result_survives_rename(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            path = Path(directory) / 'report.pdf'
            path.write_bytes(b'%PDF synthetic same bytes')
            _store_cached_image(path, 'pdf-120dpi', b'\x89PNG\r\ncached')

            renamed = Path(directory) / 'renamed-report.pdf'
            path.rename(renamed)

            self.assertTrue(cached_preview_available(renamed))
            result = cached_preview_result(renamed)
            self.assertEqual(result['kind'], 'image')
            self.assertEqual(result['data'], b'\x89PNG\r\ncached')

    def test_legacy_preview_cache_is_read_and_migrated(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            path = Path(directory) / 'legacy.pdf'
            path.write_bytes(b'%PDF legacy cache')
            legacy = _image_cache_path(path, 'pdf-120dpi')
            legacy.write_bytes(b'\x89PNG\r\nlegacy')

            self.assertTrue(cached_preview_available(path))
            self.assertEqual(cached_preview_result(path)['data'], b'\x89PNG\r\nlegacy')

            renamed = Path(directory) / 'legacy-renamed.pdf'
            path.rename(renamed)
            self.assertEqual(cached_preview_result(renamed)['data'], b'\x89PNG\r\nlegacy')

    def test_libreoffice_cache_is_used_even_after_renderer_disabled(self):
        from corpuspick.preview import _render_office_with_libreoffice
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'LOCALAPPDATA': directory}), \
                patch('corpuspick.preview._LIBREOFFICE_UNAVAILABLE', True), \
                patch('corpuspick.preview._find_soffice',
                      side_effect=AssertionError('Cached image must not probe LibreOffice')):
            path = Path(directory) / 'report.docx'
            path.write_bytes(b'placeholder')
            _store_cached_image(path, 'libreoffice-page1', b'\x89PNG\r\ncached')
            result = _render_office_with_libreoffice(path)
            self.assertEqual(result['kind'], 'image')
            self.assertEqual(result['data'], b'\x89PNG\r\ncached')

    def test_libreoffice_document_failures_do_not_disable_renderer(self):
        import corpuspick.preview as preview
        from corpuspick.preview import OFFICE_RENDER_FAILURE_LIMIT, _render_office_with_libreoffice
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'LOCALAPPDATA': directory}), \
                patch('corpuspick.preview._LIBREOFFICE_UNAVAILABLE', False), \
                patch('corpuspick.preview._LIBREOFFICE_FAILURES', 0), \
                patch('corpuspick.preview._find_soffice', return_value='soffice'), \
                patch('corpuspick.preview.run_owned_command', return_value=SimpleNamespace(returncode=0)) as run:
            path = Path(directory) / 'report.docx'
            path.write_bytes(b'placeholder')
            for _ in range(OFFICE_RENDER_FAILURE_LIMIT + 1):
                self.assertIsNone(_render_office_with_libreoffice(path))
            self.assertFalse(preview._LIBREOFFICE_UNAVAILABLE)
            self.assertEqual(preview._LIBREOFFICE_FAILURES, 0)
            self.assertEqual(run.call_count, OFFICE_RENDER_FAILURE_LIMIT + 1)

    def test_prerender_skips_text_and_counts_existing_disk_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            root = Path(directory)
            pdf = root / 'report.pdf'
            text = root / 'notes.txt'
            pdf.write_bytes(b'pdf')
            text.write_text('text', encoding='utf-8')
            _store_cached_image(pdf, 'pdf-120dpi', b'\x89PNG\r\ncached')
            app = App.__new__(App)
            app.events = queue.Queue()
            app.last_tick = 0
            app.session = SimpleNamespace(
                cancel=Event(),
                checked_document=lambda document: Path(document['path']))
            result = app.prerender_documents([{'path': str(pdf)}, {'path': str(text)}])
            self.assertEqual(result['processed'], 2)
            self.assertEqual(result['images'], 1)
            self.assertEqual(result['cached'], 1)
            self.assertEqual(result['skipped'], 1)
            self.assertEqual(result['errors'], 0)

    def test_prerender_skips_office_temporary_files_without_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            temporary = Path(directory) / '~$report.docx'
            temporary.write_bytes(b'office lock placeholder')
            app = App.__new__(App)
            app.events = queue.Queue()
            app.last_tick = 0
            app.session = SimpleNamespace(
                cancel=Event(),
                checked_document=lambda document: Path(document['path']))
            with patch('corpuspick.stats_worker.StatsWorker',
                       side_effect=AssertionError('Temporary Office file should be skipped')):
                result = app.prerender_documents([{'path': str(temporary)}])
            self.assertEqual(result['processed'], 1)
            self.assertEqual(result['skipped'], 1)
            self.assertEqual(result['errors'], 0)

    def test_preview_document_skips_office_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('corpuspick.preview._render_word_with_com',
                      side_effect=AssertionError('Temporary Office file should not render')):
            temporary = Path(directory) / '~$report.docx'
            temporary.write_bytes(b'office lock placeholder')
            result = preview_document(temporary)
            self.assertEqual(result['kind'], 'message')
            self.assertIn('пропущен', result['text'])

    def test_prerender_processes_uncached_office_sequentially_and_caches(self):
        class RenderingWorker:
            instances = 0
            paths = []

            def __init__(self, *_, **__):
                pass

            def __enter__(self):
                type(self).instances += 1
                return self

            def __exit__(self, *_):
                return None

            def count(self, path, _cancel):
                path = Path(path)
                type(self).paths.append(path)
                suffix = path.suffix.lower()
                if suffix in ('.doc', '.docx', '.rtf'):
                    variant = 'word-com-page1'
                elif suffix in ('.ppt', '.pptx'):
                    variant = 'powerpoint-com-slide1'
                else:
                    variant = 'libreoffice-page1'
                _store_cached_image(path, variant, b'\x89PNG\r\ncached-' + suffix.encode('ascii'))
                return {'kind': 'image', 'title': 'Preview', 'data': b'\x89PNG\r\n'}

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            root = Path(directory)
            docx = root / 'report.docx'
            pptx = root / 'slides.pptx'
            xlsx = root / 'book.xlsx'
            for path in (docx, pptx, xlsx):
                path.write_bytes(b'placeholder')
            app = App.__new__(App)
            app.events = queue.Queue()
            app.last_tick = 0
            app.session = SimpleNamespace(
                cancel=Event(),
                checked_document=lambda document: Path(document['path']))
            RenderingWorker.instances = 0
            RenderingWorker.paths = []
            with patch('corpuspick.stats_worker.StatsWorker', RenderingWorker):
                result = app.prerender_documents(
                    [{'path': str(docx)}, {'path': str(pptx)}, {'path': str(xlsx)}])
            self.assertEqual(result['processed'], 3)
            self.assertEqual(result['images'], 3)
            self.assertEqual(result['skipped'], 0)
            self.assertEqual(result['errors'], 0)
            self.assertEqual(RenderingWorker.instances, 1)
            self.assertEqual(RenderingWorker.paths, [docx, pptx, xlsx])
            self.assertEqual(cached_preview_result(docx)['data'], b'\x89PNG\r\ncached-.docx')
            self.assertEqual(cached_preview_result(pptx)['data'], b'\x89PNG\r\ncached-.pptx')
            self.assertEqual(cached_preview_result(xlsx)['data'], b'\x89PNG\r\ncached-.xlsx')

    def test_prerender_prioritizes_office_and_can_stop_before_pdf(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            root = Path(directory)
            pdf = root / 'first.pdf'
            docx = root / 'later.docx'
            pdf.write_bytes(b'%PDF synthetic')
            docx.write_bytes(b'placeholder')
            cancel = Event()

            class StopAfterOfficeWorker:
                paths = []

                def __init__(self, *_, **__):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return None

                def count(self, path, _cancel):
                    path = Path(path)
                    type(self).paths.append(path)
                    _store_cached_image(path, 'word-com-page1', b'\x89PNG\r\ncached-docx')
                    cancel.set()
                    return {'kind': 'image', 'title': 'Preview', 'data': b'\x89PNG\r\n'}

            cache_checks = []

            def fake_cached_available(path):
                cache_checks.append(Path(path))
                return False

            app = App.__new__(App)
            app.events = queue.Queue()
            app.last_tick = 0
            app.session = SimpleNamespace(
                cancel=cancel,
                checked_document=lambda document: Path(document['path']))
            StopAfterOfficeWorker.paths = []
            with patch('corpuspick.preview.cached_preview_available', side_effect=fake_cached_available), \
                    patch('corpuspick.stats_worker.StatsWorker', StopAfterOfficeWorker):
                result = app.prerender_documents([{'path': str(pdf)}, {'path': str(docx)}])

            self.assertEqual(StopAfterOfficeWorker.paths, [docx])
            self.assertEqual(cache_checks, [docx, docx])
            self.assertEqual(result['processed'], 1)
            self.assertTrue(result['stopped'])
            self.assertEqual(cached_preview_result(docx)['data'], b'\x89PNG\r\ncached-docx')

    def test_prerender_continues_after_file_errors(self):
        class FailingWorker:
            def __init__(self, *_, **__):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def count(self, _path, _cancel):
                return {'kind': 'message', 'title': 'Preview', 'text': 'synthetic failure'}

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}), \
                patch('corpuspick.stats_worker.StatsWorker', FailingWorker), \
                patch('corpuspick.__main__.PRERENDER_MAX_WORKERS', 1):
            docs = []
            for index in range(20):
                path = Path(directory) / f'{index}.pdf'
                path.write_bytes(b'%PDF synthetic')
                docs.append({'path': str(path)})
            app = App.__new__(App)
            app.events = queue.Queue()
            app.last_tick = 0
            app.session = SimpleNamespace(
                cancel=Event(),
                checked_document=lambda document: Path(document['path']))
            result = app.prerender_documents(docs)
            self.assertFalse(result['stopped'])
            self.assertEqual(result['processed'], len(docs))
            self.assertEqual(result['errors'], len(docs))

    def test_prerender_recycles_worker_processes(self):
        class RenderingWorker:
            instances = 0

            def __init__(self, *_, **__):
                pass

            def __enter__(self):
                type(self).instances += 1
                return self

            def __exit__(self, *_):
                return None

            def count(self, path, _cancel):
                _store_cached_image(Path(path), 'pdf-120dpi', b'\x89PNG\r\ncached')
                return {'kind': 'image', 'title': 'Preview', 'data': b'\x89PNG\r\n'}

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}), \
                patch('corpuspick.stats_worker.StatsWorker', RenderingWorker), \
                patch('corpuspick.__main__.PRERENDER_MAX_WORKERS', 1), \
                patch('corpuspick.__main__.PRERENDER_WORKER_RECYCLE_AFTER', 2):
            docs = []
            for index in range(5):
                path = Path(directory) / f'{index}.pdf'
                path.write_bytes(b'%PDF synthetic' + str(index).encode())
                docs.append({'path': str(path)})
            app = App.__new__(App)
            app.events = queue.Queue()
            app.last_tick = 0
            app.session = SimpleNamespace(
                cancel=Event(),
                checked_document=lambda document: Path(document['path']))
            RenderingWorker.instances = 0
            result = app.prerender_documents(docs)
            self.assertEqual(result['processed'], len(docs))
            self.assertEqual(result['images'], len(docs))
            self.assertEqual(RenderingWorker.instances, 3)

    def test_poll_survives_preview_ui_error_and_reschedules(self):
        class FakeWindow:
            def __init__(self):
                self.after_args = None

            def after(self, *args):
                self.after_args = args

        class FakeStatus:
            def __init__(self):
                self.value = ''

            def set(self, value):
                self.value = value

        app = App.__new__(App)
        app.events = queue.Queue()
        app.events.put(('preview', 1, 'key', {'kind': 'image'}))
        app.preview_closed = False
        app.window = FakeWindow()
        app.status = FakeStatus()
        app.finish_preview = lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic ui failure'))
        app.poll()
        self.assertEqual(app.window.after_args[0], 100)
        self.assertIn('Ошибка интерфейса', app.status.value)

    def test_progress_status_includes_current_file_name_and_format(self):
        app = App.__new__(App)
        app.operation = 'Пререндер preview'
        self.assertEqual(
            app.progress_status(7, Path('nested/report.pdf')),
            'Пререндер preview… обработано 7 · report.pdf · PDF')
        self.assertEqual(
            app.progress_status(1, Path('README')),
            'Пререндер preview… обработано 1 · README · без расширения')

        app.events = queue.Queue()
        app.last_tick = 999999999
        app.session = SimpleNamespace(state={'documents': []})
        app.tick(2, Path('nested/slide.pptx'), force=True)
        event = app.events.get_nowait()
        self.assertEqual(event[0], 'progress')
        self.assertEqual(event[1], 2)
        self.assertTrue(event[2].endswith('slide.pptx'))

    def test_ooxml_fallbacks_extract_first_content(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('corpuspick.preview._render_word_with_com', return_value=None), \
                patch('corpuspick.preview._render_powerpoint_with_com', return_value=None), \
                patch('corpuspick.preview._render_office_with_libreoffice', return_value=None):
            root = Path(directory)
            docx = root / 'doc.docx'
            with ZipFile(docx, 'w') as archive:
                archive.writestr('word/document.xml',
                                 '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                                 '<w:body><w:p><w:r><w:t>Первый абзац</w:t></w:r></w:p></w:body></w:document>')
            pptx = root / 'slides.pptx'
            with ZipFile(pptx, 'w') as archive:
                archive.writestr('ppt/slides/slide1.xml',
                                 '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                                 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                                 '<p:cSld><p:spTree><a:t>Первый слайд</a:t></p:spTree></p:cSld></p:sld>')
            xlsx = root / 'book.xlsx'
            with ZipFile(xlsx, 'w') as archive:
                archive.writestr('xl/sharedStrings.xml',
                                 '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                                 '<si><t>Первая ячейка</t></si></sst>')
                archive.writestr('xl/worksheets/sheet1.xml',
                                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                                 '<sheetData><row><c t="s"><v>0</v></c></row></sheetData></worksheet>')
            self.assertIn('Первый абзац', preview_document(docx)['text'])
            self.assertIn('Первый слайд', preview_document(pptx)['text'])
            self.assertIn('Первая ячейка', preview_document(xlsx)['text'])

    def test_word_com_preview_precedes_libreoffice_for_word_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'doc.docx'
            path.write_bytes(b'placeholder')
            rendered = {'kind': 'image', 'title': 'DOCX · первая страница через Word', 'data': b'\x89PNG\r\n'}
            with patch('corpuspick.preview._render_word_with_com', return_value=rendered) as word, \
                 patch('corpuspick.preview._render_office_with_libreoffice',
                       side_effect=AssertionError('LibreOffice should not run after Word success')):
                self.assertIs(preview_document(path), rendered)
                word.assert_called_once_with(path)

    def test_rtf_uses_word_com_preview_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'memo.rtf'
            path.write_text(r'{\rtf1 Synthetic}', encoding='ascii')
            rendered = {'kind': 'image', 'title': 'RTF · первая страница через Word', 'data': b'\x89PNG\r\n'}
            with patch('corpuspick.preview._render_word_with_com', return_value=rendered) as word, \
                 patch('corpuspick.preview._read_text',
                       side_effect=AssertionError('RTF should not be shown as raw text')):
                self.assertIs(preview_document(path), rendered)
                word.assert_called_once_with(path)

    def test_powerpoint_com_preview_precedes_libreoffice_for_pptx(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'slides.pptx'
            path.write_bytes(b'placeholder')
            rendered = {'kind': 'image', 'title': 'PPTX · первый слайд через PowerPoint', 'data': b'\x89PNG\r\n'}
            with patch('corpuspick.preview._render_word_with_com', return_value=None), \
                 patch('corpuspick.preview._render_powerpoint_with_com', return_value=rendered) as powerpoint, \
                 patch('corpuspick.preview._render_office_with_libreoffice',
                       side_effect=AssertionError('LibreOffice should not run after PowerPoint success')):
                self.assertIs(preview_document(path), rendered)
                powerpoint.assert_called_once_with(path)

    def test_office_never_launches_and_existing_cache_still_works(self):
        from corpuspick.preview import _render_word_with_com, _render_powerpoint_with_com
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}), patch('subprocess.run', side_effect=AssertionError('No Office launch')):
            for suffix, renderer, kind in [('.docx', _render_word_with_com, 'word-com-page1'), ('.pptx', _render_powerpoint_with_com, 'powerpoint-com-slide1')]:
                path = Path(directory) / ('synthetic' + suffix)
                path.write_bytes(b'synthetic')
                self.assertIsNone(renderer(path))
                _store_cached_image(path, kind, b'cached-image')
                self.assertEqual(renderer(path)['data'], b'cached-image')
