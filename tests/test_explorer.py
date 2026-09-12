import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import unquote

from corpuspick.explorer import show_file, show_files


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
                (root / 'unselected.txt').write_text('Not selected')
                nested = root / 'nested'
                nested.mkdir()
                (nested / 'third.txt').write_text('Synthetic nested selection')
                targets = {root: {target, root / 'other.txt'}, nested: {nested / 'third.txt'}}
                show_files([p for paths in targets.values() for p in paths])
                shell = Dispatch('Shell.Application')
                found = False
                deadline = time.monotonic() + 15
                owned_windows = {}
                try:
                    while time.monotonic() < deadline and not found:
                        matched = set()
                        for window in shell.Windows():
                            # Inspect selection only inside the exact synthetic folder.
                            try:
                                folder = next((p for p in targets if unquote(window.LocationURL).casefold() == unquote(p.as_uri()).casefold()), None)
                                if folder is None:
                                    continue
                                owned_windows[folder] = window
                                items = window.Document.SelectedItems()
                                if {Path(items.Item(i).Path) for i in range(items.Count)} == targets[folder]:
                                    matched.add(folder)
                            except pythoncom.com_error:
                                continue
                        found = matched == set(targets)
                        if not found:
                            time.sleep(.1)
                    self.assertTrue(found, 'Synthetic file was not selected')
                finally:
                    for window in owned_windows.values():
                        window.Quit()
        finally:
            pythoncom.CoUninitialize()
