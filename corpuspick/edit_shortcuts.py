"""Text editing shortcuts that also work with the Russian keyboard layout."""
import os


def edit_shortcut(event):
    # Do not consume AltGr/Ctrl+Alt combinations used for character input.
    # Windows Mod1 (0x8) is NumLock, not Alt as on X11.
    alt_mask = 0x20000 if os.name == 'nt' else 0x8
    if getattr(event, 'state', 0) & alt_mask:
        return None
    aliases = {'c': 'Copy', 'Cyrillic_es': 'Copy',
               'v': 'Paste', 'Cyrillic_em': 'Paste',
               'x': 'Cut', 'Cyrillic_che': 'Cut',
               'a': 'SelectAll', 'Cyrillic_ef': 'SelectAll'}
    action = aliases.get(getattr(event, 'keysym', ''))
    if os.name == 'nt':
        action = {65: 'SelectAll', 67: 'Copy', 86: 'Paste', 88: 'Cut'}.get(
            getattr(event, 'keycode', None), action)
    if action:
        widget = event.widget
        if widget.winfo_class() in ('Entry', 'TEntry', 'TCombobox', 'Spinbox', 'TSpinbox'):
            import tkinter as tk
            try:
                if action == 'SelectAll':
                    widget.selection_range(0, 'end')
                    widget.icursor('end')
                elif action in ('Copy', 'Cut'):
                    if widget.selection_present():
                        text = widget.get()[int(widget.index('sel.first')):int(widget.index('sel.last'))]
                        widget.clipboard_clear()
                        widget.clipboard_append(text)
                        if action == 'Cut':
                            widget.delete('sel.first', 'sel.last')
                elif action == 'Paste':
                    text = widget.clipboard_get()
                    if widget.selection_present():
                        widget.delete('sel.first', 'sel.last')
                    widget.insert('insert', text)
            except tk.TclError:
                pass
        else:
            widget.event_generate(f'<<{action}>>')
        return 'break'
    return None


EDIT_CLASSES = {'Entry', 'TEntry', 'Text', 'TCombobox', 'Spinbox', 'TSpinbox'}
EDIT_TAG = 'CorpusPickTextEditing'


def install_edit_shortcuts(window):
    # A separate tag before native class bindings is essential: Tk's specific
    # physical/virtual class bindings can outrank a generic Control-KeyPress.
    root = window._root()
    if getattr(root, '_corpuspick_edit_installed', False):
        return
    root._corpuspick_edit_installed = True
    root.bind_class(EDIT_TAG, '<Control-KeyPress>', edit_shortcut)

    def attach(event):
        widget = event.widget
        if widget.winfo_class() in EDIT_CLASSES:
            tags = widget.bindtags()
            if EDIT_TAG not in tags:
                widget.bindtags((EDIT_TAG,) + tags)

    root.bind_all('<Map>', attach, add='+')
    root.bind_all('<FocusIn>', attach, add='+')
    def existing(widget):
        from types import SimpleNamespace
        attach(SimpleNamespace(widget=widget))
        for child in widget.winfo_children():
            existing(child)
    existing(root)


def ask_editable_string(title, prompt, parent, initialvalue=''):
    """Explicit bindings on the actual modal rename entry, before it is shown."""
    import tkinter as tk
    from tkinter import simpledialog, ttk
    class StringDialog(simpledialog.Dialog):
        def body(self, master):
            ttk.Label(master, text=prompt, wraplength=540).pack(anchor='w', pady=(0, 8))
            self.entry = ttk.Entry(master, width=65)
            self.entry.pack(fill='x')
            self.entry.insert(0, initialvalue)
            self.entry.selection_range(0, 'end')
            self.entry.bind('<Control-KeyPress>', edit_shortcut)
            menu = tk.Menu(self.entry, tearoff=False)
            from types import SimpleNamespace
            for label, key, code in [('Копировать', 'c', 67), ('Вставить', 'v', 86),
                                      ('Вырезать', 'x', 88), ('Выделить всё', 'a', 65)]:
                menu.add_command(label=label, command=lambda k=key, c=code: edit_shortcut(
                    SimpleNamespace(widget=self.entry, keysym=k, keycode=c, state=4)))
            def popup(event):
                try:
                    menu.tk_popup(event.x_root, event.y_root)
                finally:
                    menu.grab_release()
            self.entry.bind('<Button-3>', popup)
            return self.entry
        def apply(self):
            self.result = self.entry.get()
    return StringDialog(parent, title).result
