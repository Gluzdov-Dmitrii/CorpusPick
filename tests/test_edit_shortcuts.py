from types import SimpleNamespace
import os
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from corpuspick.edit_shortcuts import edit_shortcut, install_edit_shortcuts


class EditShortcutTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('CORPUSPICK_TEST_TK') == '1', 'Optional isolated Tk keyboard test')
    def test_prefix_entry_pastes_and_updates_preview(self):
        import tkinter as tk
        from tkinter import ttk
        from corpuspick.name_dialog import NameAdditionDialog
        root = tk.Tk()
        root.geometry('700x300+-2000+-2000')
        try:
            # Replace Tcl clipboard access in this test interpreter: never touch
            # the user's real clipboard, even when native virtual events run.
            root.tk.eval('rename clipboard original_clipboard; set ::testclip "Synthetic prefix_"; proc clipboard {args} {set op [lindex $args 0]; if {$op eq "clear"} {set ::testclip ""}; if {$op eq "append"} {append ::testclip [lindex $args end]}; if {$op eq "get"} {return $::testclip}; return ""}')
            root.tk.eval('rename ::tk::GetSelection ::tk::OriginalGetSelection; '
                         'proc ::tk::GetSelection {args} {return "Synthetic prefix_"}')
            frame = ttk.Frame(root)
            frame.pack()
            dialog = NameAdditionDialog.__new__(NameAdditionDialog)
            dialog.root_path = Path('C:/Synthetic')
            dialog.examples = [dialog.root_path / 'document.pdf']
            entry = dialog.body(frame)
            root.update()
            entry.focus_force()
            root.update()
            entry.event_generate('<KeyPress>', keycode=86, state=12)
            root.update()
            self.assertEqual(entry.get(), 'Synthetic prefix_')
            self.assertIn('Synthetic_prefix_document.pdf', dialog.preview.get())
            entry.event_generate('<KeyPress>', keycode=65, state=4)
            root.update()
            self.assertTrue(entry.selection_present())
            entry.event_generate('<KeyPress>', keycode=88, state=4)
            root.update()
            self.assertEqual(entry.get(), '')
        finally:
            root.destroy()

    def test_russian_and_english_editing_without_accessing_clipboard(self):
        with patch('corpuspick.edit_shortcuts.os.name', 'nt'):
            for symbol, code, action in [('Cyrillic_es', 67, 'Copy'), ('Cyrillic_em', 86, 'Paste'),
                                         ('Cyrillic_che', 88, 'Cut'), ('Cyrillic_ef', 65, 'SelectAll'),
                                         ('c', 67, 'Copy'), ('v', 86, 'Paste')]:
                widget = Mock()
                event = SimpleNamespace(widget=widget, keysym=symbol, keycode=code, state=4)
                self.assertEqual(edit_shortcut(event), 'break')
                widget.event_generate.assert_called_once_with(f'<<{action}>>')

    def test_unrelated_keys_and_altgr_are_untouched(self):
        widget = Mock()
        for symbol, code, state in [('z', 90, 4), ('v', 86, 0x20004 if os.name == 'nt' else 12)]:
            self.assertIsNone(edit_shortcut(SimpleNamespace(widget=widget, keysym=symbol, keycode=code, state=state)))
        widget.event_generate.assert_not_called()

    def test_dialog_widget_classes_are_covered(self):
        from corpuspick.edit_shortcuts import EDIT_CLASSES
        self.assertTrue({'Entry', 'TEntry', 'Text', 'TCombobox'}.issubset(EDIT_CLASSES))

    @unittest.skipUnless(os.environ.get('CORPUSPICK_TEST_TK') == '1', 'Isolated Tk test')
    def test_shortcuts_in_future_dialog_entries(self):
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.withdraw()
        try:
            install_edit_shortcuts(root)
            install_edit_shortcuts(root)
            root.tk.eval('rename clipboard original_clipboard; set ::testclip "Synthetic prefix_"; proc clipboard {args} {set op [lindex $args 0]; if {$op eq "clear"} {set ::testclip ""}; if {$op eq "append"} {append ::testclip [lindex $args end]}; if {$op eq "get"} {return $::testclip}; return ""}')
            root.tk.eval('rename ::tk::GetSelection ::tk::OriginalGetSelection; '
                         'proc ::tk::GetSelection {args} {return "Synthetic"}')
            dialog = tk.Toplevel(root)
            dialog.geometry('300x100+-2000+-2000')
            for cls in (tk.Entry, ttk.Entry):
                entry = cls(dialog)
                entry.pack()
                root.update()
                entry.focus_force()
                root.update()
                root.tk.eval('set ::testclip Synthetic')
                entry.event_generate('<KeyPress>', keycode=86, state=12)
                root.update()
                self.assertEqual(entry.get(), 'Synthetic')
                entry.event_generate('<KeyPress>', keycode=65, state=4)
                root.update()
                self.assertTrue(entry.selection_present())
                copied = []
                entry.bind('<<Copy>>', lambda e: copied.append(True), add='+')
                entry.event_generate('<KeyPress>', keycode=67, state=12)
                root.update()
                self.assertEqual(root.tk.eval('set ::testclip'), 'Synthetic')
                entry.destroy()
        finally:
            root.destroy()


    @unittest.skipUnless(os.environ.get('CORPUSPICK_TEST_TK') == '1', 'Isolated actual modal dialog')
    def test_actual_rename_modal_copy_paste(self):
        import tkinter as tk
        from corpuspick.edit_shortcuts import ask_editable_string
        root = tk.Tk()
        root.withdraw()
        failures = []
        root.tk.eval('rename clipboard original_clipboard; set ::testclip ""; '
                     'proc clipboard {args} {set op [lindex $args 0]; '
                     'if {$op eq "clear"} {set ::testclip ""}; '
                     'if {$op eq "append"} {append ::testclip [lindex $args end]}; '
                     'if {$op eq "get"} {return $::testclip}; return ""}')
        def exercise():
            try:
                dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
                entry = dialog.entry
                entry.focus_force()
                root.update()
                entry.event_generate('<KeyPress>', keycode=67, state=12)
                root.update()
                self.assertEqual(root.tk.eval('set ::testclip'), 'Original')
                root.tk.eval('set ::testclip Replacement')
                entry.event_generate('<KeyPress>', keycode=86, state=12)
                root.update()
                self.assertEqual(entry.get(), 'Replacement')
                dialog.ok()
            except Exception as exc:
                failures.append(exc)
                for widget in root.winfo_children():
                    widget.destroy()
        root.after(150, exercise)
        try:
            result = ask_editable_string('Synthetic rename', 'New name', root, 'Original')
            if failures:
                raise failures[0]
            self.assertEqual(result, 'Replacement')
        finally:
            root.destroy()


    def test_windows_numlock_is_not_alt(self):
        with patch('corpuspick.edit_shortcuts.os.name', 'nt'):
            for state in (4, 12, 14, 44):
                for code in (65, 67, 86, 88):
                    widget = Mock()
                    self.assertEqual(edit_shortcut(SimpleNamespace(
                        widget=widget, keycode=code, keysym='unknown', state=state)), 'break')
                    widget.event_generate.assert_called_once()
            widget = Mock()
            self.assertIsNone(edit_shortcut(SimpleNamespace(
                widget=widget, keycode=86, keysym='v', state=0x20004)))
            widget.event_generate.assert_not_called()

    def test_windows_shortcuts_independent_of_layout_and_locks(self):
        mappings = [(67, ('c', 'C', 'Cyrillic_es', 'Cyrillic_ES', 'с', 'С'), 'Copy'),
                    (86, ('v', 'V', 'Cyrillic_em', 'Cyrillic_EM', 'м', 'М'), 'Paste'),
                    (88, ('x', 'X', 'Cyrillic_che', 'Cyrillic_CHE', 'ч', 'Ч'), 'Cut'),
                    (65, ('a', 'A', 'Cyrillic_ef', 'Cyrillic_EF', 'ф', 'Ф'), 'SelectAll')]
        with patch('corpuspick.edit_shortcuts.os.name', 'nt'):
            for code, symbols, action in mappings:
                for symbol in (*symbols, 'unknown'):
                    for locks in (0, 2, 8, 10, 40, 42):
                        with self.subTest(code=code, symbol=symbol, locks=locks):
                            widget = Mock()
                            result = edit_shortcut(SimpleNamespace(
                                widget=widget, keysym=symbol, keycode=code, state=4 | locks))
                            self.assertEqual(result, 'break')
                            widget.event_generate.assert_called_once_with(f'<<{action}>>')
