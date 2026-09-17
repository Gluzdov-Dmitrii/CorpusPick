from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from corpuspick.core import Session
from corpuspick.error_logging import close_error_logging
from corpuspick.__main__ import (
    App, source_directory_background, source_directory_key, source_directory_tag)


class DummyStatus:
    def __init__(self):
        self.value = ''

    def set(self, value):
        self.value = value


class DummyWidget:
    def __init__(self):
        self.options = {}
        self.stopped = False

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def stop(self):
        self.stopped = True


class DummyTree:
    def __init__(self, children):
        self.children = tuple(children)
        self.selected = ()
        self.focused = None
        self.visible = None

    def get_children(self):
        return self.children

    def selection_set(self, item):
        self.selected = (item,)

    def focus(self, item):
        self.focused = item

    def see(self, item):
        self.visible = item


class DummyWindow:
    def __init__(self):
        self.title_text = ''

    def title(self, value):
        self.title_text = value


class GuiSmokeTest(unittest.TestCase):
    def test_similarity_slider_only_commits_on_release(self):
        app = App.__new__(App)
        app.similarity_threshold_text = DummyStatus()
        app.similarity_threshold = SimpleNamespace(get=lambda: .85)
        with patch.object(app, 'regroup_similar') as regroup:
            app.similarity_threshold_changed('.85')
            self.assertEqual(app.similarity_threshold_text.value, 'Совпадение: 0.85')
            regroup.assert_not_called()
            app.commit_similarity_threshold()
            regroup.assert_called_once()
        self.assertAlmostEqual(app.similarity_distance(), .15)

    def test_column_sorting_and_global_reset_preserve_groups(self):
        app = App.__new__(App)
        app.sort_orders = []
        app.column_filters = {'name': {'include': 'акт'}, 'type': {'include': 'pdf'}}
        app.similarity_groups = {'synthetic.pdf': 7}
        app.search = DummyStatus()
        app.render = lambda: None
        app.sort('type', False)
        app.sort('size', True)
        self.assertEqual(app.sort_orders, [('type', False), ('size', True)])
        app.sort('type', True)
        self.assertEqual(app.sort_orders, [('type', True), ('size', True)])
        self.assertEqual(len(app.column_filters), 2)
        app.clear_filter('name')
        self.assertEqual(app.column_filters, {'type': {'include': 'pdf'}})
        self.assertEqual(len(app.sort_orders), 2)
        app.reset_filters_and_sorting()
        self.assertEqual(app.sort_orders, [])
        self.assertEqual(app.column_filters, {})
        self.assertEqual(app.search.value, '')
        self.assertEqual(app.similarity_groups, {'synthetic.pdf': 7})

    def test_deletion_preserves_group_ids_and_sorting(self):
        from threading import Event
        app = App.__new__(App)
        app.session = SimpleNamespace(state={'documents': [
            {'path': 'b.txt', 'identity': [2]}, {'path': 'c.txt', 'identity': [3]}],
            'moves': []}, read_only=True, cancel=Event())
        app.similarity_identities = {'a.txt': [1], 'b.txt': [2], 'c.txt': [3]}
        app.similarity_groups = {'a.txt': 7, 'b.txt': 7, 'c.txt': 12}
        app.similarity_ranks = {'a.txt': 0, 'b.txt': 1, 'c.txt': 2}
        app.similarity_notes = {'a.txt': 'old', 'b.txt': 'old', 'c.txt': 'unaffected'}
        app.similarity_button = DummyWidget()
        app.status = DummyStatus()
        app.sort_column, app.sort_reverse = 'similarity', False
        app.render = lambda: None
        app.refreshed()
        self.assertEqual(app.similarity_groups, {'b.txt': 7, 'c.txt': 12})
        self.assertEqual(app.similarity_ranks, {'b.txt': 1, 'c.txt': 2})
        self.assertEqual(app.sort_column, 'similarity')
        self.assertNotIn('a.txt', app.similarity_notes)
        self.assertEqual(app.similarity_notes['c.txt'], 'unaffected')
        app.session.state['documents'][0]['identity'] = [99]
        app.refreshed()
        self.assertEqual(app.similarity_groups, {'b.txt': 7, 'c.txt': 12})
        self.assertEqual(app.sort_column, 'similarity')
        self.assertIn('Прежняя группа', app.similarity_notes['b.txt'])

    def test_renamed_document_and_regroup_keep_existing_colour_ids(self):
        from threading import Event
        app = App.__new__(App)
        app.session = SimpleNamespace(state={'documents': [
            {'path': 'prefix_a.pdf', 'identity': [1, 101, 10, 20]},
            {'path': 'b.pdf', 'identity': [1, 102, 10, 20]}], 'moves': []},
            read_only=True, cancel=Event())
        app.similarity_groups = {'a.pdf': 7, 'b.pdf': 12}
        app.similarity_ranks = {'a.pdf': 0, 'b.pdf': 1}
        app.similarity_notes = {'a.pdf': 'old', 'b.pdf': 'old'}
        app.similarity_identities = {'a.pdf': [1, 101, 10, 20], 'b.pdf': [1, 102, 10, 20]}
        app.similarity_button, app.status = DummyWidget(), DummyStatus()
        app.similarity_threshold = SimpleNamespace(get=lambda: .65)
        app.sort_column, app.sort_reverse = 'similarity', False
        app.render = lambda: None
        app.refreshed()
        self.assertEqual(app.similarity_groups, {'prefix_a.pdf': 7, 'b.pdf': 12})
        app.apply_similarity_result(({'prefix_a.pdf': 0, 'b.pdf': 1},
                                     {'prefix_a.pdf': 0, 'b.pdf': 1}, {}))
        self.assertEqual(app.similarity_groups, {'prefix_a.pdf': 7, 'b.pdf': 12})

    def test_selection_totals_sorting_and_csv(self):
        try:
            window = tk.Tk()
        except tk.TclError:
            self.skipTest('Tcl/Tk unavailable')
        window.withdraw()
        app = App(window)
        self.assertTrue(app.preview_visible.get())
        self.assertEqual(app.preview_button['text'], 'Скрыть preview')
        self.assertEqual(int(app.preview_separator.cget('width')), 4)
        self.assertEqual(int(app.preview_separator_line.cget('width')), 2)
        self.assertEqual(app.preview_separator.grid_info()['column'], 1)
        self.assertEqual(app.preview_shell.grid_info()['column'], 2)
        app.toggle_preview()
        self.assertFalse(app.preview_visible.get())
        self.assertFalse(app.preview_separator.winfo_ismapped())
        self.assertFalse(app.preview_shell.winfo_ismapped())
        app.toggle_preview()
        self.assertTrue(app.preview_visible.get())
        self.assertEqual(app.preview_separator.grid_info()['column'], 1)
        self.assertEqual(app.preview_shell.grid_info()['column'], 2)
        self.assertEqual(app.similarity_threshold.get(), .65)
        self.assertEqual(float(app.similarity_scale.cget('from')), .60)
        self.assertEqual(float(app.similarity_scale.cget('to')), .95)
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
                app.reset_filters_and_sorting()
                app.sort('pages')
                self.assertEqual(app.tree.get_children()[0], 'b.txt')
                app.sort('pages')
                self.assertEqual(app.tree.get_children()[0], 'a.txt')
                self.assertTrue(all(app.tree.set(p, 'number') for p in app.tree.get_children()))
                # Interleaved sizes must sort within groups, never split them.
                app.reset_filters_and_sorting()
                saved_docs = app.view_docs
                app.view_docs = [dict(saved_docs[0], path=p, size=size, origin=p) for p, size in [
                    ('report 2024.pdf', 30), ('report 2025.pdf', 10), ('letter x.txt', 40), ('letter y.txt', 20)]]
                from corpuspick.similarity import similar_order
                from threading import Event
                ranks, groups = similar_order(app.view_docs, Event(), with_groups=True)
                with patch.object(app.session, 'group_similar', return_value=(ranks, groups, {})) as group_similar, \
                     patch('corpuspick.embeddings.model_ready', return_value=True):
                    app.sort_similar()
                    settle()
                    self.assertAlmostEqual(group_similar.call_args.args[1], .35)
                with patch.object(app.session, 'regroup_similar',
                                  return_value=(ranks, groups, {})) as regroup_similar:
                    app.similarity_threshold.set(.72)
                    app.similarity_threshold_changed('.72')
                    self.assertEqual(app.similarity_threshold_text.get(), 'Совпадение: 0.72')
                    regroup_similar.assert_not_called()
                    app.commit_similarity_threshold()
                    settle()
                    self.assertAlmostEqual(regroup_similar.call_args.args[0], .28)
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
                    until = time.monotonic() + 2
                    while not show.called and time.monotonic() < until:
                        window.update()
                        time.sleep(.01)
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

    def test_manual_column_widths_survive_window_resize(self):
        try:
            window = tk.Tk()
        except tk.TclError:
            self.skipTest('Tcl/Tk unavailable')
        window.withdraw()
        try:
            app = App(window)
            for column, width in [('name', 1800), ('origin', 2200)]:
                self.assertFalse(int(app.tree.column(column, 'stretch')))
                app.tree.column(column, width=width)
            window.geometry('850x600')
            window.deiconify()
            window.update()
            self.assertEqual(int(app.tree.column('name', 'width')), 1800)
            self.assertEqual(int(app.tree.column('origin', 'width')), 2200)
            app.tree.xview_moveto(1.0)
            window.update_idletasks()
            self.assertGreater(app.tree.xview()[0], 0)
        finally:
            window.destroy()

    def test_origin_palette_button_colors_rows_by_source_folder(self):
        try:
            window = tk.Tk()
        except tk.TclError:
            self.skipTest('Tcl/Tk unavailable')
        window.withdraw()
        app = None
        try:
            app = App(window)
            app.select = lambda *_: None
            self.assertEqual(ttk.Style(window).lookup('Toolbutton', 'anchor'), 'center')
            app.view_docs = [
                {'path': 'flat/a.pdf', 'origin': 'source/2024/a.pdf', 'origins': ['source/2024/a.pdf'],
                 'size': 1, 'stats': {}},
                {'path': 'flat/b.pdf', 'origin': 'source/2024/b.pdf', 'origins': ['source/2024/b.pdf'],
                 'size': 1, 'stats': {}},
                {'path': 'flat/c.pdf', 'origin': 'source/2025/c.pdf', 'origins': ['source/2025/c.pdf'],
                 'size': 1, 'stats': {}},
            ]
            self.assertEqual(app.origin_color_button.grid_info()['column'], 3)
            self.assertEqual(app.similarity_button.grid_info()['column'], 4)
            app.origin_color_button.invoke()
            window.update_idletasks()
            a_tags = app.tree.item('flat/a.pdf', 'tags')
            b_tags = app.tree.item('flat/b.pdf', 'tags')
            c_tags = app.tree.item('flat/c.pdf', 'tags')
            self.assertEqual(a_tags, b_tags)
            self.assertNotEqual(a_tags, c_tags)
            self.assertTrue(a_tags[0].startswith('source-folder-'))
            self.assertEqual(app.tree.tag_configure(a_tags[0], 'background'),
                             source_directory_background(source_directory_key(app.view_docs[0])))
            app.origin_color_button.invoke()
            self.assertEqual(app.tree.item('flat/a.pdf', 'tags'), ())
        finally:
            if app is not None:
                app.close()
            else:
                window.destroy()


class OriginDirectoryPaletteTest(unittest.TestCase):
    def test_source_folder_key_and_color_are_stable_for_origin_variants(self):
        first = {'path': 'flat/a.pdf', 'origins': ['source/2024/a.pdf', 'source/2025/a.pdf']}
        reordered = {'path': 'flat/b.pdf', 'origins': ['source/2025/b.pdf', 'source/2024/b.pdf']}
        other = {'path': 'flat/c.pdf', 'origin': 'source/2026/c.pdf'}
        first_key = source_directory_key(first)
        self.assertEqual(first_key, source_directory_key(reordered))
        self.assertEqual(source_directory_tag(first_key), source_directory_tag(source_directory_key(reordered)))
        self.assertNotEqual(source_directory_tag(first_key), source_directory_tag(source_directory_key(other)))
        self.assertRegex(source_directory_background(first_key), r'^#[0-9a-f]{6}$')


class HeadlessGuiSafetyTest(unittest.TestCase):
    def make_app(self):
        app = App.__new__(App)
        app.window = DummyWindow()
        app.session = None
        app.events = __import__('queue').Queue()
        app.busy = False
        app.operation = ''
        app.last_tick = 0
        app.buttons = []
        app.mutation_buttons = []
        app.backup_check = DummyWidget()
        app.similarity_scale = DummyWidget()
        app.stop_button = DummyWidget()
        app.progress = DummyWidget()
        app.status = DummyStatus()
        app.backup_mode = SimpleNamespace(get=lambda: True)
        app.preview_visible = SimpleNamespace(get=lambda: False)
        app.preview_cache = {}
        app.preview_cache_order = []
        app.similarity_notes = {}
        app.column_filters = {}
        app.similarity_groups = {}
        app.similarity_ranks = {}
        app.similarity_button = DummyWidget()
        app.sort_column = 'number'
        app.sort_reverse = False
        app.view_docs = []
        app.ui_error_signatures = set()
        app.cancel_preview = lambda: None
        app.scan = lambda: None
        return app

    def test_open_folder_callback_error_is_contained(self):
        app = self.make_app()
        app.cancel_preview = lambda: (_ for _ in ()).throw(RuntimeError('synthetic callback failure'))
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'LOCALAPPDATA': str(Path(directory) / 'state')}), \
                patch('corpuspick.__main__.filedialog.askdirectory',
                      return_value=str(Path(directory) / 'input')), \
                patch('corpuspick.__main__.messagebox.showerror') as showerror:
            root = Path(directory) / 'input'
            root.mkdir()
            try:
                app.open_folder()
                showerror.assert_called_once()
                self.assertFalse(app.busy)
                self.assertIn('RuntimeError', app.status.value)
                self.assertIn('synthetic callback failure', app.status.value)
            finally:
                if app.session:
                    app.session.close()
                close_error_logging()

    def test_repeated_ui_error_shows_dialog_once(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'LOCALAPPDATA': str(Path(directory) / 'state')}), \
                patch('corpuspick.__main__.messagebox.showerror') as showerror:
            try:
                error = RuntimeError('repeat failure')
                app.recover_ui_error(error, context='same-callback')
                app.recover_ui_error(error, context='same-callback')
                showerror.assert_called_once()
                self.assertIn('RuntimeError', app.status.value)
            finally:
                close_error_logging()

    def test_tcl_error_does_not_open_error_dialog(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'LOCALAPPDATA': str(Path(directory) / 'state')}), \
                patch('corpuspick.__main__.messagebox.showerror') as showerror:
            try:
                app.recover_ui_error(tk.TclError('unspecified'), context='tk-callback')
                showerror.assert_not_called()
                self.assertIn('TclError', app.status.value)
                self.assertIn('unspecified', app.status.value)
                log_file = Path(directory) / 'state' / 'CorpusPick' / 'logs' / 'corpuspick.log'
                self.assertIn('tk-callback', log_file.read_text(encoding='utf-8'))
            finally:
                close_error_logging()

    def test_open_folder_falls_back_to_manual_path_after_tcl_dialog_error(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'LOCALAPPDATA': str(Path(directory) / 'state')}), \
                patch('corpuspick.__main__.filedialog.askdirectory',
                      side_effect=tk.TclError('unspecified')), \
                patch('corpuspick.__main__.simpledialog.askstring',
                      return_value=str(Path(directory) / 'input')):
            root = Path(directory) / 'input'
            root.mkdir()
            try:
                app.open_folder()
                self.assertIsNotNone(app.session)
                self.assertEqual(app.session.root, root.resolve())
                self.assertIn('Системный диалог выбора папки недоступен', app.status.value)
            finally:
                if app.session:
                    app.session.close()
                close_error_logging()

    def test_tick_snapshot_is_safe_without_session(self):
        app = self.make_app()
        app.tick(0)
        self.assertEqual(app.events.get_nowait(), ('snapshot', 0, []))

    def test_successful_trash_selects_next_row_without_modal_message(self):
        app = self.make_app()
        app.tree = DummyTree(['a.pdf', 'd.pdf', 'e.pdf'])
        app.select = lambda: setattr(app, 'selection_refreshed', True)
        with patch('corpuspick.__main__.messagebox.showinfo') as showinfo:
            app.trash_completed('Корзина', {'trashed': 2, 'errors': []},
                                ['a.pdf', 'b.pdf', 'c.pdf', 'd.pdf', 'e.pdf'],
                                ['b.pdf', 'c.pdf'])
        showinfo.assert_not_called()
        self.assertEqual(app.tree.selected, ('d.pdf',))
        self.assertEqual(app.tree.focused, 'd.pdf')
        self.assertEqual(app.tree.visible, 'd.pdf')
        self.assertTrue(app.selection_refreshed)
        self.assertEqual(app.status.value, 'Отправлено в корзину: 2')

    def test_successful_trash_at_end_selects_previous_row(self):
        app = self.make_app()
        app.tree = DummyTree(['a.pdf'])
        app.select = lambda: None
        app.trash_completed('Корзина', {'trashed': 1, 'errors': []},
                            ['a.pdf', 'b.pdf'], ['b.pdf'])
        self.assertEqual(app.tree.selected, ('a.pdf',))
