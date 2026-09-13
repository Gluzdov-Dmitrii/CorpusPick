"""Column filtering, without evaluating expressions or reading documents."""
from pathlib import Path
import re
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from .similarity import normalize

NUMERIC = {'number', 'size', 'duplicate', 'pages', 'figures', 'tables'}


def column_value(document, column, number=0):
    path = Path(document['path'])
    return {'number': number, 'name': path.name, 'folder': str(path.parent),
            'origin': ' | '.join(document.get('origins', [document.get('origin', document['path'])])),
            'type': path.suffix, 'size': document.get('size'), 'duplicate': document.get('duplicate'),
            **{k: document.get('stats', {}).get(k) for k in ('pages', 'figures', 'tables')}}.get(column)


def matches(value, rule):
    if rule.get('numeric'):
        if value is None:
            return False
        return (rule.get('min') is None or value >= rule['min']) and (rule.get('max') is None or value <= rule['max'])
    text = normalize(str(value or ''))
    tokens = set(re.findall(r'[^\W_]+', text))
    def contains(word):
        return word in tokens if rule.get('whole') else word in text
    include = normalize(rule.get('include', '')).split()
    exclude = normalize(rule.get('exclude', '')).split()
    found = [contains(w) for w in include]
    return (not include or (any(found) if rule.get('any') else all(found))) and not any(contains(w) for w in exclude)


class FilterDialog(simpledialog.Dialog):
    def __init__(self, parent, title, numeric, current=None):
        self.numeric, self.current = numeric, current or {}
        super().__init__(parent, title='Фильтр: ' + title)

    def body(self, master):
        low = self.current.get('min' if self.numeric else 'include')
        high = self.current.get('max' if self.numeric else 'exclude')
        self.first = tk.StringVar(value=low if low is not None else '')
        self.second = tk.StringVar(value=high if high is not None else '')
        for row, (label, variable) in enumerate(zip(
                ['От (включительно)', 'До (включительно)'] if self.numeric else
                ['Содержит слова / части слов (через пробел)', 'Исключить слова / части слов'], [self.first, self.second])):
            ttk.Label(master, text=label).grid(row=row*2, column=0, sticky='w')
            entry = ttk.Entry(master, textvariable=variable, width=48)
            entry.grid(row=row*2+1, column=0, sticky='ew', pady=(0, 8))
            if row == 0:
                initial = entry
        self.whole = tk.BooleanVar(value=self.current.get('whole', False))
        self.any_word = tk.BooleanVar(value=self.current.get('any', False))
        if not self.numeric:
            ttk.Checkbutton(master, text='Слова целиком', variable=self.whole).grid(row=4, column=0, sticky='w')
            ttk.Checkbutton(master, text='Достаточно любого слова (иначе — все)', variable=self.any_word).grid(row=5, column=0, sticky='w')
        ttk.Label(master, text='Пустые поля снимают фильтр этой колонки.').grid(row=6, column=0, sticky='w')
        return initial

    def validate(self):
        if self.numeric:
            try:
                low, high = [int(v.get()) if v.get().strip() else None for v in (self.first, self.second)]
                if (low is not None and low < 0) or (high is not None and high < 0) or (low is not None and high is not None and low > high):
                    raise ValueError()
            except ValueError:
                messagebox.showerror('Фильтр', 'Введите неотрицательные целые числа; «От» не больше «До».', parent=self)
                return False
            self.rule = {'numeric': True, 'min': low, 'max': high} if low is not None or high is not None else {}
        else:
            self.rule = {'include': self.first.get().strip(), 'exclude': self.second.get().strip(),
                         'whole': self.whole.get(), 'any': self.any_word.get()} if self.first.get().strip() or self.second.get().strip() else {}
        return True

    def apply(self):
        self.result = self.rule
