from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from corpuspick.core import Session
from corpuspick.__main__ import App


class GuiSmokeTest(unittest.TestCase):
    def test_selection_totals_sorting_and_csv(self):
        try:
            window = tk.Tk()
        except tk.TclError:
            self.skipTest('Tcl/Tk unavailable')
        window.withdraw()
        app = App(window)
        def settle():
            until = time.monotonic() + 15
            while app.busy and time.monotonic() < until:
                window.update()
                time.sleep(.01)
            self.assertFalse(app.busy)
            window.update()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'input'
                root.mkdir()
                (root / 'a.txt').write_text('one')
                (root / 'b.txt').write_text('two')
                app.session = Session(root, Path(directory) / 'state', read_only=True)
                app.scan()
                settle()
                self.assertEqual(str(app.tree.cget('selectmode')), 'extended')
                self.assertFalse(hasattr(app, 'tools_menu'))
                self.assertIn('pages', app.tree['columns'])
                self.assertNotIn('appendices', app.tree['columns'])
                app.session.state['documents'][0]['stats'] = {'pages': 5, 'tables': 2}
                app.session.state['documents'][1]['stats'] = {'pages': 3, 'tables': 1}
                app.refreshed()
                self.assertIn('Стр.: 8', app.totals.get())
                app.tree.selection_set('a.txt')
                window.update()
                self.assertIn('Стр.: 5', app.totals.get())
                app.tree.selection_set(['a.txt', 'b.txt'])
                window.update()
                self.assertEqual(len(app.selected_documents()), 2)
                self.assertIn('Стр.: 8', app.totals.get())
                app.sort('pages')
                self.assertEqual(app.tree.get_children()[0], 'b.txt')
                app.sort('pages')
                self.assertEqual(app.tree.get_children()[0], 'a.txt')
                self.assertTrue(all(app.tree.set(p, 'number') for p in app.tree.get_children()))
                app.session.close()
                app.session = None
        finally:
            app.close()
