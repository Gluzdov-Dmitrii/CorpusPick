from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from corpuspick.core import Cancelled, Session
from corpuspick.stats_worker import StatsWorker


def slow_worker(connection):
    while True:
        path = connection.recv()
        if path == 'slow':
            time.sleep(60)
        connection.send({'pages': 7})


class WorkerTests(unittest.TestCase):
    def test_libreoffice_stats_refines_writer_formats_and_skips_transients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'input'
            root.mkdir()
            for name in ('report.doc', 'report.docx', 'report.rtf', 'notes.pdf', '~$draft.docx'):
                (root / name).write_bytes(b'synthetic')
            session = Session(root, Path(directory) / 'state', read_only=True)
            try:
                session.scan(hash_files=False)
                for document in session.state['documents']:
                    document['stats'] = {'schema': 2, 'pages': 8, 'pages_estimated': True}

                class FakeWorker:
                    def __init__(self, **kwargs):
                        self.paths = []

                    def __enter__(self):
                        return self

                    def __exit__(self, *_):
                        pass

                    def count(self, path, cancel, request=None):
                        self.paths.append(Path(request['path']).suffix.lower())
                        return {'pages': 3, 'figures': 1, 'tables': 2, 'info': 'synthetic'}

                with patch('corpuspick.stats_worker.StatsWorker', FakeWorker):
                    result = session.collect_stats(use_libreoffice=True)
                documents = {document['path']: document for document in session.state['documents']}
                self.assertEqual(result, {'updated': 3, 'errors': 0, 'skipped': 2, 'stopped': False})
                self.assertEqual([documents[name]['stats']['pages'] for name in ('report.doc', 'report.docx', 'report.rtf')], [3, 3, 3])
                self.assertTrue(all('pages_estimated' not in documents[name]['stats'] for name in ('report.doc', 'report.docx', 'report.rtf')))
                self.assertEqual(documents['notes.pdf']['stats']['pages'], 8)
                self.assertEqual(documents['~$draft.docx']['stats']['pages'], 8)
                self.assertTrue(session.read_only)
            finally:
                session.close()

    def test_libreoffice_stats_failure_preserves_quick_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'input'
            root.mkdir()
            (root / 'report.docx').write_bytes(b'synthetic')
            session = Session(root, Path(directory) / 'state', read_only=True)
            try:
                session.scan(hash_files=False)
                session.state['documents'][0]['stats'] = {
                    'schema': 2, 'pages': 5, 'pages_estimated': True, 'tables': 1,
                }

                class FailedWorker:
                    def __init__(self, **kwargs):
                        pass

                    def __enter__(self):
                        return self

                    def __exit__(self, *_):
                        pass

                    def count(self, path, cancel, request=None):
                        return {'failed': True, 'info': 'synthetic failure'}

                with patch('corpuspick.stats_worker.StatsWorker', FailedWorker):
                    result = session.collect_stats(use_libreoffice=True)
                stats = session.state['documents'][0]['stats']
                self.assertEqual(result['errors'], 1)
                self.assertEqual((stats['pages'], stats['tables']), (5, 1))
                self.assertTrue(stats['pages_estimated'])
                self.assertTrue(stats['libreoffice_failed'])
            finally:
                session.close()

    def test_timeout_then_restart(self):
        with StatsWorker(timeout=1, target=slow_worker) as worker:
            self.assertTrue(worker.count('slow', threading.Event())['failed'])
            self.assertIsNone(worker.process)
            worker.timeout = 10
            self.assertEqual(worker.count('fast', threading.Event())['pages'], 7)

    def test_stop_terminates_current_parser(self):
        cancel = threading.Event()
        with StatsWorker(timeout=30, target=slow_worker) as worker:
            timer = threading.Timer(.5, cancel.set)
            timer.start()
            start = time.monotonic()
            try:
                with self.assertRaises(Cancelled):
                    worker.count('slow', cancel)
                self.assertLess(time.monotonic() - start, 5)
                self.assertIsNone(worker.process)
            finally:
                timer.cancel()

    def test_windows_close_terminates_process_tree(self):
        class FakeProcess:
            pid = 12345

            def __init__(self):
                self.alive = True
                self.terminated = False
                self.killed = False
                self.closed = False

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                pass

            def terminate(self):
                self.terminated = True
                self.alive = False

            def kill(self):
                self.killed = True
                self.alive = False

            def close(self):
                self.closed = True

        worker = StatsWorker()
        process = FakeProcess()
        worker.process = process
        worker.connection = SimpleNamespace(close=lambda: None)

        def fake_run(args, **_kwargs):
            self.assertEqual(args[:2], ['taskkill', '/PID'])
            self.assertIn('/T', args)
            self.assertIn('/F', args)
            process.alive = False
            return SimpleNamespace(returncode=0)

        with patch('corpuspick.stats_worker.os.name', 'nt'), \
                patch('corpuspick.stats_worker.subprocess.run', side_effect=fake_run) as run:
            worker.close()

        run.assert_called_once()
        self.assertFalse(process.terminated)
        self.assertFalse(process.killed)
        self.assertTrue(process.closed)
        self.assertIsNone(worker.process)

    def test_windows_tree_kill_falls_back_to_root_terminate(self):
        class FakeProcess:
            pid = 12345
            closed = False

            def is_alive(self):
                return True

            def join(self, timeout=None):
                pass

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def close(self):
                self.closed = True

        worker = StatsWorker()
        process = FakeProcess()
        process.terminated = False
        process.killed = False
        worker.process = process
        worker.connection = SimpleNamespace(close=lambda: None)
        with patch('corpuspick.stats_worker.os.name', 'nt'), \
                patch('corpuspick.stats_worker.subprocess.run',
                      return_value=SimpleNamespace(returncode=1)):
            worker.close()
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertTrue(process.closed)

    def test_open_is_light_and_explicit_selection_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'input'
            root.mkdir()
            for name in ('a.docx', 'b.docx'):
                with ZipFile(root / name, 'w') as archive:
                    archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:tbl/><w:drawing/></w:document>')
            session = Session(root, Path(directory) / 'state', read_only=True)
            try:
                with patch('corpuspick.core.digest', side_effect=AssertionError('No hashes on open')), patch.object(StatsWorker, 'count', side_effect=AssertionError('No parsing on open')):
                    session.open_catalog()
                self.assertTrue(all(not d.get('stats') for d in session.state['documents']))
                session.collect_stats(selected={'a.docx'})
                docs = {d['path']: d for d in session.state['documents']}
                self.assertEqual(docs['a.docx']['stats']['tables'], 1)
                self.assertEqual(docs['a.docx']['stats']['figures'], 1)
                self.assertFalse(docs['b.docx'].get('stats'))
                with patch.object(StatsWorker, 'count', side_effect=AssertionError('Use cache')):
                    session.open_catalog()
                    session.collect_stats(selected={'a.docx'})
                docs = {d['path']: d for d in session.state['documents']}
                docs['a.docx']['stats'] = {'schema': 2, 'failed': True}
                with patch.object(StatsWorker, 'count', return_value={'pages': 3}) as count:
                    session.collect_stats(selected={'a.docx'})
                    count.assert_called_once()
            finally:
                session.close()
