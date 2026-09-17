from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from corpuspick.core import Session
from corpuspick.similarity import similar_order


class NameTests(unittest.TestCase):
    def test_hundredth_file_widens_entire_series_and_retains_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'files'
            root.mkdir()
            for number in range(1, 100):
                (root / f'Тема #{number:02d}.pdf').write_text(f'Synthetic {number}')
            (root / 'new.docx').write_text('Synthetic 100')
            session = Session(root, Path(directory) / 'state')
            try:
                session.scan(hash_files=False)
                for doc in session.state['documents']:
                    doc['similarity'] = {'embedding': 'retained'}
                selected = next(d for d in session.state['documents'] if d['path'] == 'new.docx')
                result = session.rename_documents([selected], 'Тема')
                self.assertEqual(result['errors'], [])
                self.assertEqual(result['moved'], 100)
                self.assertEqual(sorted(p.name for p in root.iterdir()),
                                 [f'Тема #{n:03d}.pdf' for n in range(1, 100)] + ['Тема #100.docx'])
                for number in range(1, 100):
                    self.assertEqual((root / f'Тема #{number:03d}.pdf').read_text(), f'Synthetic {number}')
                self.assertTrue(all(d['similarity'] == {'embedding': 'retained'} for d in session.state['documents']))
                session.close()
                session = Session(root, Path(directory) / 'state')
                self.assertEqual(len(session.state['documents']), 100)
                self.assertTrue(all(Path(d['path']).name.startswith('Тема #') for d in session.state['documents']))
            finally:
                session.close()

    def test_selected_old_numbering_restarts_and_repeat_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'files'
            root.mkdir()
            for number in (7, 8):
                (root / f'Тема №{number}.pdf').write_text(f'Synthetic {number}')
            session = Session(root, Path(directory) / 'state')
            try:
                session.scan(hash_files=False)
                result = session.rename_documents(session.state['documents'], 'Тема')
                self.assertEqual(result['moved'], 2)
                self.assertEqual({p.name for p in root.iterdir()}, {'Тема #01.pdf', 'Тема #02.pdf'})
                result = session.rename_documents(list(reversed(session.state['documents'])), 'Тема')
                self.assertEqual(result['errors'], [])
                self.assertEqual({p.name for p in root.iterdir()}, {'Тема #01.pdf', 'Тема #02.pdf'})
                self.assertEqual((root / 'Тема #01.pdf').read_text(), 'Synthetic 7')
            finally:
                session.close()

    def test_natural_name_sort(self):
        from corpuspick.__main__ import natural_name_key
        names = ['Тема #10.pdf', 'Тема #2.pdf', 'Тема #30.pdf', 'Тема #1.pdf']
        self.assertEqual(sorted(names, key=natural_name_key),
                         ['Тема #1.pdf', 'Тема #2.pdf', 'Тема #10.pdf', 'Тема #30.pdf'])

    def test_rename_single_and_numbered_batch_keep_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'files'
            root.mkdir()
            for name in ('a.pdf', 'b.docx', 'c.PDF'):
                (root / name).write_text('Synthetic ' + name)
            session = Session(root, Path(directory) / 'state')
            try:
                session.scan(hash_files=False)
                docs = session.state['documents']
                single = docs[0]
                single['similarity'] = {'embedding': 'cached'}
                result = session.rename_documents([single], 'Первый')
                self.assertEqual(result['moved'], 1)
                self.assertTrue((root / 'Первый.pdf').exists())
                result = session.rename_documents(list(reversed(docs)), 'НОУ-ХАУ')
                self.assertEqual(result['moved'], 3)
                self.assertEqual({p.name for p in root.iterdir()},
                                 {'НОУ-ХАУ #01.PDF', 'НОУ-ХАУ #02.docx', 'НОУ-ХАУ #03.pdf'})
                self.assertEqual(single['similarity'], {'embedding': 'cached'})
                self.assertEqual(single['origin'], 'a.pdf')
                (root / 'Занято.pdf').write_text('Do not replace')
                result = session.rename_documents([single], 'Занято')
                self.assertEqual(result['moved'], 1)
                self.assertEqual(len(result['errors']), 0)
                self.assertTrue((root / 'Занято #01.pdf').exists())
                self.assertEqual((root / 'Занято.pdf').read_text(), 'Do not replace')
                for invalid in ('', '  ', '../name', 'bad:', 'trailing.'):
                    with self.assertRaises(ValueError):
                        session.rename_documents([single], invalid)
                session.read_only = True
                with self.assertRaises(ValueError):
                    session.rename_documents([single], 'Forbidden')
            finally:
                session.close()

    def test_numbering_continues_across_batches_and_single_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'files'
            root.mkdir()
            for name in ('a.pdf', 'b.docx', 'c.pdf', 'd.txt'):
                (root / name).write_text('Synthetic')
            session = Session(root, Path(directory) / 'state')
            try:
                session.scan(hash_files=False)
                docs = {d['path']: d for d in session.state['documents']}
                self.assertEqual(session.rename_documents([docs['a.pdf'], docs['b.docx']], 'НОУ-ХАУ')['moved'], 2)
                # A new file after the scan, in another folder and format, still counts.
                (root / 'nested').mkdir()
                (root / 'nested' / 'ноу-хау №100.txt').write_text('Existing')
                self.assertEqual(session.rename_documents([docs['c.pdf']], 'НОУ-ХАУ')['moved'], 4)
                self.assertTrue((root / 'НОУ-ХАУ #003.pdf').exists())
                session.close()
                session = Session(root, Path(directory) / 'state')
                session.scan(hash_files=False)
                remaining = next(d for d in session.state['documents'] if d['path'] == 'd.txt')
                self.assertEqual(session.rename_documents([remaining], 'НОУ-ХАУ')['moved'], 1)
                self.assertTrue((root / 'НОУ-ХАУ #004.txt').exists())
                self.assertEqual((root / 'nested' / 'НОУ-ХАУ #100.txt').read_text(), 'Existing')
            finally:
                session.close()

    def test_prefix_and_external_rename_preserve_cache_and_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'files'
            root.mkdir()
            (root / 'report.txt').write_text('Synthetic content')
            (root / 'Акты_report.txt').write_text('Collision')
            session = Session(root, Path(directory) / 'state')
            try:
                session.scan(hash_files=False)
                doc = next(d for d in session.state['documents'] if d['path'] == 'report.txt')
                doc['similarity'] = {'embedding': 'cached-content'}
                collision = session.add_prefix([doc], 'Акты_')
                self.assertEqual(collision['moved'], 0)
                self.assertEqual(len(collision['errors']), 1)
                for invalid in ('', '../', 'bad:', 'bad\n'):
                    with self.assertRaises(ValueError):
                        session.add_prefix([doc], invalid)
                result = session.add_prefix([doc], 'Отчёты_')
                self.assertEqual(result['moved'], 1)
                renamed = root / 'Отчёты_report.txt'
                self.assertEqual(renamed.read_text(), 'Synthetic content')
                renamed.rename(root / 'external.txt')
                with patch('corpuspick.core.digest', side_effect=AssertionError('No hashing for rename')):
                    session.scan(hash_files=False)
                updated = next(d for d in session.state['documents'] if d['path'] == 'external.txt')
                self.assertEqual(updated['origin'], 'report.txt')
                self.assertEqual(updated['similarity']['embedding'], 'cached-content')
                (root / 'external.txt').write_text('Actually changed content')
                session.scan(hash_files=False)
                changed = next(d for d in session.state['documents'] if d['path'] == 'external.txt')
                self.assertNotIn('similarity', changed)
                session.read_only = True
                with self.assertRaises(ValueError):
                    session.add_prefix([changed], 'No_')
            finally:
                session.close()

    def test_weighted_names_and_no_transitive_chain(self):
        names = ['alpha beta.txt', 'alpha beta gamma.txt', 'beta gamma.txt',
                 'Техническое задание насосы.docx', 'Техническое заданеи насосы.pdf',
                 'Отчет по закупке насосов.docx', 'Отчет по обучению сотрудников.docx',
                 '003_Годовой отчет насосы.docx', 'насосы отчет годовой.pdf']
        docs = [{'path': name, 'size': 100} for name in names]
        _, groups = similar_order(docs, threading.Event(), with_groups=True)
        self.assertEqual(groups[names[0]], groups[names[1]])
        self.assertNotEqual(groups[names[0]], groups[names[2]])
        self.assertEqual(groups[names[3]], groups[names[4]])
        self.assertNotEqual(groups[names[5]], groups[names[6]])
        self.assertEqual(groups[names[7]], groups[names[8]])
        _, reversed_groups = similar_order(list(reversed(docs)), threading.Event(), with_groups=True)
        self.assertEqual(groups, reversed_groups)

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
