"""Shared filename composition and a local-only preview dialog."""
from pathlib import Path
import re
import tkinter as tk
from tkinter import simpledialog, ttk, messagebox
from .edit_shortcuts import edit_shortcut


def composed_name(root, source, text='', directory_count=0, at_end=False):
    if type(directory_count) is not int or not 0 <= directory_count <= 3:
        raise ValueError('Выберите от 1 до 3 каталогов')
    root, source = Path(root), Path(source)
    relative = source.relative_to(root)
    if '..' in relative.parts:
        raise ValueError('Путь выходит за открытый каталог')
    normalize = lambda value: re.sub(r'[\s/\\]+', '_', value)
    addition = normalize(text)
    if directory_count:
        # Include the open root, never any directory above it.
        parents = ([root.name] if root.name else []) + list(relative.parts[:-1])
        directories = '_'.join(normalize(part) for part in parents[-directory_count:])
        addition = '_'.join(part.strip('_') for part in (addition, directories) if part.strip('_'))
        if addition:
            addition = ('_' + addition) if at_end else (addition + '_')
    if not addition.strip('_') or any(c in '<>:"|?*' or ord(c) < 32 for c in addition):
        raise ValueError('Введите текст или включите названия каталогов; запрещённые символы: < > : " | ? *')
    name = source.stem + addition + source.suffix if at_end else addition + source.name
    if len(name.encode('utf-16-le')) > 510 or name.startswith('.') or name.endswith((' ', '.')):
        raise ValueError('Получилось недопустимое или слишком длинное имя файла')
    return name


class NameAdditionDialog(simpledialog.Dialog):
    def __init__(self, parent, root, paths):
        self.root_path = Path(root)
        self.examples = [self.root_path / path for path in paths[:3]]
        super().__init__(parent, 'Добавить к названию')

    def body(self, master):
        self.text = tk.StringVar(master=master)
        self.use_directories = tk.BooleanVar(master=master, value=False)
        self.count = tk.StringVar(master=master, value='1')
        self.at_end = tk.BooleanVar(master=master, value=False)
        ttk.Label(master, text='Текст (необязательно, если добавляются каталоги):').grid(row=0, column=0, columnspan=3, sticky='w')
        entry = ttk.Entry(master, textvariable=self.text, width=65)
        entry.bind('<Control-KeyPress>', edit_shortcut)
        entry.grid(row=1, column=0, columnspan=3, sticky='ew', pady=(4, 10))
        ttk.Checkbutton(master, text='Добавить названия верхних каталогов',
                        variable=self.use_directories).grid(row=2, column=0, sticky='w')
        self.count_box = ttk.Combobox(master, textvariable=self.count, values=['1', '2', '3'],
                                      state='disabled', width=3)
        self.count_box.grid(row=2, column=1, sticky='w')
        ttk.Label(master, text='Не выше открытого каталога; порядок от внешнего к внутреннему.').grid(
            row=3, column=0, columnspan=3, sticky='w', pady=(4, 8))
        ttk.Radiobutton(master, text='В начало', variable=self.at_end, value=False).grid(row=4, column=0, sticky='w')
        ttk.Radiobutton(master, text='В конец (перед расширением)', variable=self.at_end, value=True).grid(row=4, column=1, columnspan=2, sticky='w')
        ttk.Label(master, text='Пробелы и слэши в добавляемой части заменяются на _.').grid(
            row=5, column=0, columnspan=3, sticky='w', pady=8)
        self.preview = tk.StringVar(master=master)
        ttk.Label(master, textvariable=self.preview, wraplength=620, justify='left').grid(
            row=6, column=0, columnspan=3, sticky='w', pady=6)
        for variable in (self.text, self.use_directories, self.count, self.at_end):
            variable.trace_add('write', self.update_preview)
        self.update_preview()
        return entry

    def options(self):
        return dict(text=self.text.get(), directory_count=int(self.count.get()) if self.use_directories.get() else 0,
                    at_end=self.at_end.get())

    def update_preview(self, *_):
        self.count_box.configure(state='readonly' if self.use_directories.get() else 'disabled')
        lines = ['Предпросмотр (первые три выбранных файла):']
        for source in self.examples:
            try:
                name = composed_name(self.root_path, source, **self.options())
            except ValueError as exc:
                name = str(exc)
            lines.append(f'{source.name}\n→ {name}')
        self.preview.set('\n\n'.join(lines))

    def validate(self):
        try:
            for source in self.examples:
                composed_name(self.root_path, source, **self.options())
        except ValueError as exc:
            messagebox.showerror('Название', str(exc), parent=self)
            return False
        return True

    def apply(self):
        self.result = self.options()
