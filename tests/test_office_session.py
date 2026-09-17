import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from corpuspick.office_session import OfficeSession
from corpuspick.preview import _find_soffice, preview_worker
from corpuspick.stats_worker import StatsWorker, libreoffice_stats_worker
from test_libreoffice_integration import make_docx
from test_process_job import open_handle


integration = pytest.mark.skipif(os.environ.get('CORPUSPICK_TEST_LIBREOFFICE') != '1', reason='Opt-in warm LibreOffice integration')


@integration
def test_reuse_timeout_restart_and_source_preservation(tmp_path):
    import fitz
    session = OfficeSession(_find_soffice())
    source = tmp_path / 'input.docx'
    make_docx(source)
    original = source.read_bytes()
    try:
        session.start()
        pid = session.office_pid
        directory = Path(session.directory.name)
        for i in range(3):
            output = tmp_path / f'{i}.pdf'
            started = time.monotonic()
            assert session.render(source, output, 'writer_pdf_Export')
            print({'seconds': round(time.monotonic() - started, 3), 'same_process': session.office_pid == pid}, flush=True)
            with fitz.open(output) as doc:
                assert len(doc) == 1
                assert 'Синтетический' in doc[0].get_text()
            assert session.office_pid == pid
        api, handle = open_handle(pid)
        try:
            # Simulate a transport that never produces a reply after dispatch.
            with patch.object(session, '_receive', side_effect=subprocess.TimeoutExpired('synthetic', .01)):
                with pytest.raises(subprocess.TimeoutExpired):
                    session.render(source, tmp_path / 'timeout.pdf', 'writer_pdf_Export')
            assert api.WaitForSingleObject(handle, 5000) == 0
        finally:
            api.CloseHandle(handle)
        assert not directory.exists()
        assert session.render(source, tmp_path / 'restart.pdf', 'writer_pdf_Export')
        assert source.read_bytes() == original
    finally:
        session.close()


@integration
def test_warm_worker_cleanup_and_cache(tmp_path):
    with patch.dict(os.environ, {'LOCALAPPDATA': str(tmp_path / 'state')}):
        with StatsWorker(timeout=25, target=preview_worker) as worker:
            assert worker.count('', threading.Event(), request={'warmup': True})['ready']
            worker_pid = worker.process.pid
            for i in range(3):
                source = tmp_path / f'{i}.docx'
                make_docx(source)
                # Change bytes to prevent the preview cache masking renderer reuse.
                with open(source, 'ab') as stream:
                    stream.write(str(i).encode())
                assert worker.count(source, threading.Event())['kind'] == 'image'
                assert worker.process.pid == worker_pid
        assert worker.process is None


@integration
def test_libreoffice_stats_uses_disposable_copy(tmp_path):
    source = tmp_path / 'synthetic.docx'
    make_docx(source)
    original = source.read_bytes()
    with StatsWorker(timeout=45, target=libreoffice_stats_worker) as worker:
        result = worker.count(source, threading.Event(), request={'path': str(source)})
    assert not result.get('failed'), result
    assert result['pages'] == 2
    assert result['tables'] == 1
    assert result['figures'] == 0
    assert source.read_bytes() == original


@integration
def test_bad_writer_document_does_not_block_next_statistics_request(tmp_path):
    broken = tmp_path / 'broken.docx'
    with ZipFile(broken, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<broken>')
    valid = tmp_path / 'valid.docx'
    make_docx(valid)
    with StatsWorker(timeout=45, target=libreoffice_stats_worker) as worker:
        failed = worker.count(broken, threading.Event(), request={'path': str(broken)})
        result = worker.count(valid, threading.Event(), request={'path': str(valid)})
    assert failed.get('failed')
    assert result['pages'] == 2
    assert result['tables'] == 1


@integration
def test_warm_office_dies_when_owner_is_killed():
    script = (
        "import sys,json; from corpuspick.office_session import OfficeSession; "
        "from corpuspick.preview import _find_soffice; "
        "s=OfficeSession(_find_soffice()); s.start(); "
        "print(json.dumps([s.process.pid,s.office_pid]),flush=True); sys.stdin.readline()"
    )
    parent = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    handles = []
    try:
        lines = []
        thread = threading.Thread(target=lambda: lines.append(parent.stdout.readline()), daemon=True)
        thread.start()
        thread.join(25)
        assert lines and lines[0]
        handles = [open_handle(pid) for pid in json.loads(lines[0])]
        assert all(handle for _, handle in handles)
        parent.kill()
        parent.wait(timeout=5)
        for api, handle in handles:
            assert api.WaitForSingleObject(handle, 5000) == 0
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        for api, handle in handles:
            api.CloseHandle(handle)
        parent.stdin.close()
        parent.stdout.close()
