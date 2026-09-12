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
                self.assertEqual(len(app.buttons), 3)
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
                (root / 'demo.txt').unlink()  # User sorts it out using Explorer.
                app.scan()
                settle()
                self.assertFalse(app.tree.get_children())
                app.session.close()
                app.session = None
        finally:
            app.close()
