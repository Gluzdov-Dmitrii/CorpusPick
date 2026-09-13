from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
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
                self.assertNotIn('error', app.tree['columns'])
                self.assertNotIn('note', app.tree['columns'])
                self.assertFalse(app.tree.bind('2'))
                for keysym, keycode in [('a', 65), ('A', 65), ('Cyrillic_ef', 65)]:
                    app.tree.selection_remove(app.tree.selection())
                    self.assertEqual(app.control_key(SimpleNamespace(widget=app.tree, keysym=keysym, keycode=keycode)), 'break')
                    self.assertEqual(len(app.tree.selection()), 2)
                app.tree.selection_remove(app.tree.selection())
                window.deiconify()
                window.update()
                app.tree.focus_force()
                window.update()
                app.tree.event_generate('<KeyPress>', keycode=65, state=4)
                window.update()
                self.assertEqual(len(app.tree.selection()), 2)
                window.withdraw()
                app.tree.selection_remove(app.tree.selection())
                app.last_tick = 0
                app.tick(10)
                self.assertEqual(app.events.get_nowait(), ('progress', 10))
                with patch('corpuspick.__main__.FilterDialog') as dialog:
                    dialog.return_value.result = {'include': 'a.txt'}
                    app.edit_filter('name')
                self.assertEqual(app.tree.get_children(), ('a.txt',))
                self.assertIn('●', app.tree.heading('name')['text'])
                app.select_all()
                self.assertEqual(app.tree.selection(), ('a.txt',))
                app.sort('size')
                self.assertEqual(app.tree.get_children(), ('a.txt',))
                app.column_filters['type'] = {'include': 'pdf'}
                app.render()
                self.assertFalse(app.tree.get_children())
                app.clear_filter()
                self.assertEqual(len(app.tree.get_children()), 2)
                app.clear_selection()
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
                # Mock clipboard writes: never read or replace the user's clipboard in tests.
                with patch.object(window, 'clipboard_clear'), patch.object(window, 'clipboard_append') as append:
                    app.copy_name()
                    append.assert_called_once_with('a.txt\nb.txt')
                self.assertIn('Стр.: 8', app.totals.get())
                app.sort('pages')
                self.assertEqual(app.tree.get_children()[0], 'b.txt')
                app.sort('pages')
                self.assertEqual(app.tree.get_children()[0], 'a.txt')
                self.assertTrue(all(app.tree.set(p, 'number') for p in app.tree.get_children()))
                # Interleaved sizes must sort within groups, never split them.
                saved_docs = app.view_docs
                app.view_docs = [dict(saved_docs[0], path=p, size=size, origin=p) for p, size in [
                    ('report 2024.pdf', 30), ('report 2025.pdf', 10), ('letter x.txt', 40), ('letter y.txt', 20)]]
                from corpuspick.similarity import similar_order
                from threading import Event
                ranks, groups = similar_order(app.view_docs, Event(), with_groups=True)
                with patch.object(app.session, 'group_similar', return_value=(ranks, groups, {})):
                    app.sort_similar()
                    settle()
                # Completion refreshes the session view; use the synthetic grouping fixture again.
                app.view_docs = [dict(saved_docs[0], path=p, size=size, origin=p) for p, size in [
                    ('report 2024.pdf', 30), ('report 2025.pdf', 10), ('letter x.txt', 40), ('letter y.txt', 20)]]
                self.assertEqual(app.similarity_groups['report 2024.pdf'], app.similarity_groups['report 2025.pdf'])
                self.assertNotEqual(app.similarity_groups['report 2024.pdf'], app.similarity_groups['letter x.txt'])
                app.sort('size')
                self.assertEqual(app.tree.get_children(), ('report 2025.pdf', 'report 2024.pdf', 'letter y.txt', 'letter x.txt'))
                report_tag = app.tree.item('report 2024.pdf', 'tags')
                self.assertTrue(report_tag and report_tag[0].startswith('group-'))
                self.assertEqual(report_tag, app.tree.item('report 2025.pdf', 'tags'))
                self.assertNotEqual(report_tag, app.tree.item('letter x.txt', 'tags'))
                app.sort('size')
                self.assertEqual(app.tree.get_children(), ('report 2024.pdf', 'report 2025.pdf', 'letter x.txt', 'letter y.txt'))
                app.sort_similar()
                self.assertFalse(app.similarity_groups)
                backup = Path(directory) / 'backup'
                (backup / 'old folder').mkdir(parents=True)
                (backup / 'old folder' / 'original.txt').write_text('one')
                source = Session(backup, Path(directory) / 'backup-state', read_only=True)
                try:
                    manifest = Path(directory) / 'structure.csv'
                    source.export_structure(manifest)
                finally:
                    source.close()
                app.session.import_structure(manifest)
                app.refreshed()
                app.tree.selection_set(['a.txt', 'b.txt'])
                with patch('corpuspick.__main__.show_files') as show:
                    app.show_in_explorer()
                    show.assert_called_once_with([root.resolve() / 'a.txt', root.resolve() / 'b.txt'])
                (root / 'b.txt').write_text('one')
                app.scan()
                settle()
                self.assertEqual(app.tree.set('b.txt', 'duplicate'), 'Не проверен')
                with patch('corpuspick.statistics.document_stats', side_effect=AssertionError('No stats during duplicate search')), patch('corpuspick.core.recycle_file', side_effect=AssertionError('Read-only search')):
                    app.find_duplicates()
                    settle()
                self.assertTrue(app.tree.set('a.txt', 'duplicate').startswith('#'))
                self.assertEqual(app.tree.set('a.txt', 'duplicate'), app.tree.set('b.txt', 'duplicate'))
                self.assertIn('Лишних копий: 1', app.status.get())
                with patch('corpuspick.core.digest', side_effect=AssertionError('Reuse hashes')):
                    app.find_duplicates()
                    settle()
                app.session.close()
                app.session = None
        finally:
            app.close()
