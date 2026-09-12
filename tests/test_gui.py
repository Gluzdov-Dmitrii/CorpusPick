from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from corpuspick.core import Session
from corpuspick.__main__ import App


class GuiSmokeTest(unittest.TestCase):
    def test_synthetic_workflow(self):
        try:
            window = tk.Tk()
        except tk.TclError:
            self.skipTest("Tcl/Tk or desktop unavailable")
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
                base = Path(directory)
                root = base / "input"
                (root / "nested").mkdir(parents=True)
                (root / "nested" / "demo.txt").write_text("Синтетическое письмо", encoding="utf-8")
                app.session = Session(root, base / "state")
                app.scan()
                settle()
                self.assertEqual(len(app.tree.get_children()), 1)
                app.tree.selection_set("0")
                window.update()
                app.category.set("Письмо")
                app.decide(2)
                self.assertEqual(app.session.state["documents"][0]["score"], 2)
                app.undo()
                self.assertEqual(app.session.state["documents"][0]["score"], 1)
                with patch("corpuspick.__main__.messagebox.askyesno", return_value=True):
                    app.flatten()
                    settle()
                self.assertTrue((root / "demo.txt").exists())
                self.assertIn("nested", app.tree.item("0", "values")[1])
                app.preview_executor.shutdown(wait=True, cancel_futures=True)
                app.session.close()
                app.session = None
        finally:
            app.close()
