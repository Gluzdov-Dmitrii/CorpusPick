import queue
from pathlib import Path
from threading import Event
from unittest.mock import patch
from corpuspick import preview
from corpuspick.__main__ import App


def test_cached_office_fast_path_does_not_hash_or_trim(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    path = tmp_path / 'synthetic.docx'
    path.write_bytes(b'synthetic')
    preview._store_cached_image(path, 'libreoffice-page1', b'png')
    with patch.object(preview, '_file_content_fingerprint', side_effect=AssertionError('hash')):
        assert preview.cached_preview_result(path)['data'] == b'png'
        assert preview.cached_preview_available(path)
        assert preview.preview_document(path)['data'] == b'png'


def test_prerender_cache_survives_previous_file_limit(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    first = tmp_path / '0.pdf'
    for index in range(390):
        path = tmp_path / f'{index}.pdf'
        path.write_bytes(str(index).encode())
        preview._store_cached_image(path, 'pdf-120dpi', b'png')
    assert preview.cached_preview_available(first)
    assert len(list(preview._cache_root().glob('*.png'))) == 780


def test_text_read_is_bounded(tmp_path):
    path = tmp_path / 'synthetic.txt'
    path.write_text('example')
    with patch.object(Path, 'read_bytes', side_effect=AssertionError('unbounded read')):
        assert preview._read_text(path)['kind'] == 'text'


def test_idle_worker_released_without_warmup():
    app = App.__new__(App)
    app.preview_closed = False
    app.events = queue.Queue()
    calls = []
    class Requests:
        def get(self, timeout):
            if not calls:
                raise queue.Empty
            return None
    class Worker:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def close(self): calls.append('released')
        def count(self, *args, **kwargs): raise AssertionError('eager start')
    app.preview_requests = Requests()
    with patch('corpuspick.stats_worker.StatsWorker', Worker):
        app.preview_loop()
    assert calls == ['released']


def test_ram_cache_has_byte_budget():
    app = App.__new__(App)
    app.preview_cache = {}
    app.preview_cache_order = []
    for key in range(5):
        app.cache_preview(key, {'data': b'x' * (10 * 1024 * 1024)})
    assert len(app.preview_cache) == 3


def test_ram_cache_has_no_image_count_limit():
    app = App.__new__(App)
    app.preview_cache = {}
    app.preview_cache_order = []
    for key in range(100):
        app.cache_preview(key, {'data': b'png'})
    assert len(app.preview_cache) == 100


def test_clear_cache_only_removes_owned_files(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    root = preview._cache_root()
    owned = root / ('a' * 64 + '.png')
    owned.write_bytes(b'12345')
    other = root / 'keep.txt'
    other.write_text('keep')
    nested = root / 'nested'
    nested.mkdir()
    (nested / ('b' * 64 + '.png')).write_bytes(b'keep')
    assert preview.image_cache_size() == 5
    assert preview.clear_image_cache() == 0
    assert not owned.exists()
    assert other.exists()
    assert list(nested.iterdir())
    assert preview.image_cache_size() == 0


def test_prerender_budget_continue_and_enough(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(preview, 'image_cache_size', lambda: 512 * 1024 * 1024)
    docs = []
    for suffix in ('.docx', '.pdf', '.pptx'):
        path = tmp_path / ('synthetic' + suffix)
        path.write_bytes(suffix.encode())
        docs.append({'path': str(path)})
    monkeypatch.setattr(preview, 'cached_preview_available', lambda path: path.suffix == '.pptx')
    for proceed in (False, True):
        app = App.__new__(App)
        app.session = SimpleNamespace(cancel=Event(), checked_document=lambda d: Path(d['path']))
        app.tick = lambda *a, **kw: None
        questions = []
        rendered = []
        def ask(counts, size, cancel):
            questions.append(counts)
            return proceed
        app.ask_prerender_budget = ask
        class Worker:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def count(self, path, cancel):
                rendered.append(path.suffix)
                return {'kind': 'image', 'data': b'png'}
        with patch('corpuspick.stats_worker.StatsWorker', Worker):
            result = app.prerender_documents(docs)
        assert questions == [{'.docx': 1, '.pdf': 1}]
        assert result['stopped'] is not proceed
        assert rendered == (['.docx', '.pdf'] if proceed else [])
