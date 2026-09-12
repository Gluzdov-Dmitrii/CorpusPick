import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import unquote

from corpuspick.explorer import show_file


class ExplorerTests(unittest.TestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                show_file(Path(directory) / 'missing.txt')

    @unittest.skipUnless(os.environ.get('CORPUSPICK_TEST_EXPLORER') == '1', 'Optional synthetic Explorer integration')
    def test_select_file_with_spaces_unicode_and_comma(self):
        import pythoncom
        from win32com.client import Dispatch
        pythoncom.CoInitialize()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'Тест папка, 1'
                root.mkdir()
                target = root / 'Отчёт, тест 1.txt'
                target.write_text('Synthetic fixture', encoding='utf-8')
                (root / 'other.txt').write_text('Synthetic neighbour')
                show_file(target)
                shell = Dispatch('Shell.Application')
                found = False
                deadline = time.monotonic() + 15
                owned_window = None
                try:
                    while time.monotonic() < deadline and not found:
                        for window in shell.Windows():
                            # Inspect selection only inside the exact synthetic folder.
                            try:
                                if unquote(window.LocationURL).casefold() != unquote(root.as_uri()).casefold():
                                    continue
                                owned_window = window
                                items = window.Document.SelectedItems()
                                found = items.Count == 1 and Path(items.Item(0).Path) == target
                            except pythoncom.com_error:
                                continue
                        if not found:
                            time.sleep(.1)
                    self.assertTrue(found, 'Synthetic file was not selected')
                finally:
                    if owned_window is not None:
                        owned_window.Quit()
        finally:
            pythoncom.CoUninitialize()
