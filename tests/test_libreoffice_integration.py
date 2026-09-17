"""Real renderer regression tests. All inputs are generated, never user documents."""
import hashlib
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from corpuspick.office_profile import create_profile, pdf_filter
from corpuspick.process_job import run_owned_command
from corpuspick.preview import _find_soffice, preview_document


def make_docx(path, external_url=None):
    with ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        picture = ''
        if external_url:
            picture = '<w:p><w:r><w:pict><v:shape style="width:100pt;height:100pt"><v:imagedata r:id="external"/></v:shape></w:pict></w:r></w:p>'
            z.writestr('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="external" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" TargetMode="External" Target="' + external_url + '"/></Relationships>')
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:t>Синтетический отчёт — первая страница</w:t></w:r></w:p><w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="4000"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>Тестовая таблица 123</w:t></w:r></w:p></w:tc></w:tr></w:tbl>' + picture + '<w:p><w:r><w:br w:type="page"/><w:t>SECOND PAGE MUST NOT BE EXPORTED</w:t></w:r></w:p><w:sectPr/></w:body></w:document>')


def convert(source, output, target):
    office = _find_soffice()
    with tempfile.TemporaryDirectory() as directory:
        profile = Path(directory) / 'profile'
        create_profile(profile)
        input_filter = {'.fods': 'OpenDocument Spreadsheet Flat XML', '.fodp': 'OpenDocument Presentation Flat XML'}.get(source.suffix)
        result = run_owned_command([office, '-env:UserInstallation=' + profile.as_uri(),
                                    '--headless', '--norestore', '--nodefault', '--nofirststartwizard',
                                    *(['--infilter=' + input_filter] if input_filter else []),
                                    '--convert-to', target, '--outdir', str(output), str(source)], timeout=25)
        assert result.returncode == 0


integration = pytest.mark.skipif(os.environ.get('CORPUSPICK_TEST_LIBREOFFICE') != '1', reason='Opt-in synthetic LibreOffice integration')


@pytest.fixture
def warm_runtime():
    from corpuspick.preview import close_office_session
    with patch('corpuspick.preview._REUSE_OFFICE', True):
        try:
            yield
        finally:
            close_office_session()


@integration
def test_real_office_formats_and_first_page(tmp_path, warm_runtime):
    import fitz
    assert _find_soffice(), 'Run Setup-Preview.ps1 first'
    docx = tmp_path / 'report.docx'
    make_docx(docx)
    sheet = tmp_path / 'sheet.fods'
    sheet.write_text('''<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:mimetype="application/vnd.oasis.opendocument.spreadsheet" office:version="1.2"><office:body><office:spreadsheet><table:table table:name="Synthetic"><table:table-row><table:table-cell office:value-type="string"><text:p>Тестовая таблица</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet></office:body></office:document>''', encoding='utf-8')
    slides = tmp_path / 'slides.fodp'
    slides.write_text('''<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:mimetype="application/vnd.oasis.opendocument.presentation" office:version="1.2"><office:body><office:presentation><draw:page draw:name="Synthetic"><draw:frame svg:x="2cm" svg:y="2cm" svg:width="20cm" svg:height="5cm"><draw:text-box><text:p>Синтетический слайд</text:p></draw:text-box></draw:frame></draw:page></office:presentation></office:body></office:document>''', encoding='utf-8')
    for source, target in [(docx, 'doc:MS Word 97'), (sheet, 'xlsx:Calc MS Excel 2007 XML'),
                            (sheet, 'xls:MS Excel 97'), (slides, 'pptx:Impress MS PowerPoint 2007 XML'),
                            (slides, 'ppt:MS PowerPoint 97')]:
        convert(source, tmp_path, target)
    rtf = tmp_path / 'report.rtf'
    rtf.write_text(r'{\rtf1\ansi Synthetic RTF preview\par Second line}', encoding='ascii')
    paths = [docx, docx.with_suffix('.doc'), sheet.with_suffix('.xlsx'), sheet.with_suffix('.xls'),
             slides.with_suffix('.pptx'), slides.with_suffix('.ppt'), rtf]
    with patch.dict(os.environ, {'LOCALAPPDATA': str(tmp_path / 'state')}):
        for source in paths:
            assert source.is_file(), source.suffix
            before = hashlib.sha256(source.read_bytes()).digest()
            started = time.monotonic()
            result = preview_document(source)
            assert result['kind'] == 'image', (source.suffix, result.get('title'))
            assert result['data'].startswith(b'\x89PNG')
            assert hashlib.sha256(source.read_bytes()).digest() == before
            print(source.suffix, round(time.monotonic() - started, 2), 'image')
        # A fresh interpreter has no RAM cache or warm renderer. Every Office
        # format must load the persisted PNG without even discovering LibreOffice.
        from corpuspick.preview import close_office_session
        import subprocess
        import sys
        close_office_session()
        check = """
import sys, time
from pathlib import Path
from unittest.mock import patch
from corpuspick.preview import preview_document, cached_preview_result
with patch('corpuspick.preview._find_soffice', side_effect=AssertionError('renderer started')):
    for name in sys.argv[1:]:
        source = Path(name)
        started = time.monotonic()
        cached = cached_preview_result(source)
        assert cached and cached['kind'] == 'image', source.suffix
        assert preview_document(source)['data'] == cached['data']
        print(source.suffix, round(time.monotonic() - started, 4), 'persisted cache')
"""
        completed = subprocess.run([sys.executable, '-c', check, *map(str, paths)],
                                   capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
        print(completed.stdout)
        convert(docx, tmp_path, pdf_filter('.docx'))
        with fitz.open(docx.with_suffix('.pdf')) as pdf:
            assert len(pdf) == 1
            assert 'SECOND PAGE' not in pdf[0].get_text()


@integration
def test_external_image_is_not_requested(tmp_path, warm_runtime):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(1)
            self.send_response(404)
            self.end_headers()
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = tmp_path / 'external.docx'
        make_docx(source, f'http://127.0.0.1:{server.server_port}/synthetic.png')
        with patch.dict(os.environ, {'LOCALAPPDATA': str(tmp_path / 'state')}):
            assert preview_document(source)['kind'] == 'image'
        assert requests == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
