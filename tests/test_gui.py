from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from corpuspick.core import Session
from corpuspick.__main__ import App


class GuiSmokeTest(unittest.TestCase):
    def test_simplified_workflow(self):
        try:
            window = tk.Tk()
        except tk.TclError:
            self.skipTest('Tcl/Tk or desktop unavailable')
        window.withdraw()
        app = App(window)
        def settle():
            until = time.monotonic() + 10
            while app.busy and time.monotonic() < until:
                window.update()
                time.sleep(.01)
            self.assertFalse(app.busy)
            window.update()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'input'
                (root / 'nested').mkdir(parents=True)
                (root / 'nested/demo.txt').write_text('Synthetic')
                app.session = Session(root, Path(directory) / 'state')
                app.scan()
                settle()
                self.assertEqual(len(app.buttons), 4)
                self.assertEqual(len(app.tree.get_children()), 1)
                self.assertFalse(hasattr(app, 'text'))
                app.tree.selection_set(str(Path('nested/demo.txt')))
                app.mark()
                self.assertEqual(app.selected()['note'], 'Скорее да')
                with patch('corpuspick.__main__.messagebox.askyesno', return_value=True), patch('corpuspick.__main__.messagebox.showinfo'):
                    app.flatten()
                    settle()
                self.assertTrue((root / 'demo.txt').exists())
                self.assertEqual(len(app.tree.get_children()), 1)
                app.session.state['documents'][0]['stats'] = {'pages': 5, 'tables': 2}
                app.refreshed()
                self.assertIn('Страниц: 5', app.totals.get())
                app.search.set('no-match')
                self.assertIn('Страниц: 5', app.totals.get())
                app.search.set('')
                app.tree.selection_set('demo.txt')
                with patch('corpuspick.__main__.messagebox.askyesno', return_value=True), patch('corpuspick.core.recycle_file', side_effect=lambda path: path.rename(Path(directory) / 'fake-trash.txt')):
                    app.delete_selected()
                    settle()
                self.assertFalse(app.tree.get_children())
                self.assertIn('Файлов: 0', app.totals.get())
                app.session.close()
                app.session = None
        finally:
            app.close()
