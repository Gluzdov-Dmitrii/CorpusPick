from pathlib import Path
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
