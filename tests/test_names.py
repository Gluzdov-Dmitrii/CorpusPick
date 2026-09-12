from pathlib import Path
import tempfile
import threading
import unittest

from corpuspick.core import Session
from corpuspick.similarity import similar_order


class NameTests(unittest.TestCase):
    def test_trim_three_and_reject_empty_or_invalid_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'files'
            root.mkdir()
            for name in ('123Отчёт.docx', '123Письмо.pdf', 'abc.txt'):
                (root / name).write_text('Synthetic')
            session = Session(root, Path(directory) / 'state')
            try:
                docs = session.scan()
                for count in (0, -1, 1.5, True):
                    with self.assertRaises(ValueError):
                        session.trim_prefix(docs, count)
                result = session.trim_prefix(docs, 3)
                self.assertEqual(result['moved'], 2)
                self.assertEqual(len(result['errors']), 1)
                self.assertEqual({p.name for p in root.iterdir()}, {'Отчёт.docx', 'Письмо.pdf', 'abc.txt'})
            finally:
                session.close()

    def test_trim_preserves_origin_cache_and_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'files'
            root.mkdir()
            for name in ('_report.txt', 'xreport.txt', 'a.txt', '_CON.txt'):
                (root / name).write_text('Synthetic ' + name)
            session = Session(root, Path(directory) / 'state')
            try:
                session.scan()
                docs = session.state['documents']
                report = next(d for d in docs if d['path'] == '_report.txt')
                report['origins'] = ['old/report.txt', 'other/report.txt']
                report['stats'] = {'pages': 3}
                result = session.trim_prefix(sorted(docs, key=lambda d: d['path']), 1)
                self.assertEqual(result['moved'], 1)
                self.assertEqual(len(result['errors']), 3)
                self.assertEqual((root / 'report.txt').read_text(), 'Synthetic _report.txt')
                self.assertTrue((root / 'xreport.txt').exists())
                session.close()
                session = Session(root, Path(directory) / 'state')
                d = next(d for d in session.scan() if d['path'] == 'report.txt')
                self.assertEqual(d['origin'], '_report.txt')
                self.assertEqual(d['origins'], ['old/report.txt', 'other/report.txt'])
                self.assertEqual(d['stats']['pages'], 3)
                session.read_only = True
                with self.assertRaises(ValueError):
                    session.trim_prefix([d], 1)
            finally:
                session.close()

    def test_similarity_and_secondary_keys(self):
        docs = [{'path': p, 'size': s} for p, s in [
            ('Отчёт годовой 2025.pdf', 20), ('Годовой отчёт 2025.docx', 10),
            ('Годовой отчёт 2025.pdf', 10), ('Совсем другое письмо.txt', 10),
            ('Годовой отчёт 2024.docx', 10)]]
        ranks = similar_order(docs, threading.Event())
        self.assertEqual(len(ranks), 5)
        self.assertLess(ranks[docs[1]['path']], ranks[docs[2]['path']])
        self.assertLess(ranks[docs[2]['path']], ranks[docs[0]['path']])
        self.assertEqual(abs(ranks[docs[4]['path']] - ranks[docs[1]['path']]), 1)
        cancel = threading.Event()
        cancel.set()
        self.assertIsNone(similar_order(docs, cancel))
