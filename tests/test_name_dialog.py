from pathlib import Path
import tempfile
import unittest

from corpuspick.name_dialog import composed_name
from corpuspick.core import Session


class NameCompositionTests(unittest.TestCase):
    def test_parent_limit_order_normalization_and_extension(self):
        root = Path('C:/Synthetic/Открытый каталог')
        source = root / 'Первый уровень' / 'Второй уровень' / 'Отчёт.pdf'
        self.assertEqual(composed_name(root, source, directory_count=1), 'Второй_уровень_Отчёт.pdf')
        self.assertEqual(composed_name(root, source, directory_count=2), 'Первый_уровень_Второй_уровень_Отчёт.pdf')
        self.assertEqual(composed_name(root, source, directory_count=3, at_end=True),
                         'Отчёт_Открытый_каталог_Первый_уровень_Второй_уровень.pdf')
        self.assertEqual(composed_name(root, root / 'Файл.txt', directory_count=3), 'Открытый_каталог_Файл.txt')
        self.assertEqual(composed_name(root, source, text='Общий / класс\\тип ', directory_count=1),
                         'Общий_класс_тип_Второй_уровень_Отчёт.pdf')

    def test_actual_rename_matches_preview_and_keeps_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'Corpus'
            folder = root / 'Folder name'
            folder.mkdir(parents=True)
            path = folder / 'Synthetic.txt'
            path.write_text('synthetic content')
            session = Session(root, Path(directory) / 'state')
            try:
                session.scan(hash_files=False)
                doc = session.state['documents'][0]
                doc['similarity'] = {'embedding': 'synthetic-cache'}
                expected = composed_name(root, path, directory_count=3, at_end=True)
                result = session.add_name_parts([doc], directory_count=3, at_end=True)
                self.assertEqual(result['moved'], 1)
                self.assertEqual((folder / expected).read_text(), 'synthetic content')
                self.assertEqual(session.state['documents'][0]['similarity'], {'embedding': 'synthetic-cache'})
            finally:
                session.close()

    def test_invalid_options_and_outside_root(self):
        root = Path('C:/Synthetic/Corpus')
        for count in (-1, 4, True):
            with self.assertRaises(ValueError):
                composed_name(root, root / 'file.txt', directory_count=count)
        with self.assertRaises(ValueError):
            composed_name(root, root.parent / 'file.txt', directory_count=1)
        with self.assertRaises(ValueError):
            composed_name(root, root / 'file.txt', text='bad:name')
