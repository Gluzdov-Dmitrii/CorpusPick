"""Local folder workbench: portable structure, selection statistics and safe stop."""
import copy
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .core import Session, failure
from .explorer import show_files
from .filters import FilterDialog, NUMERIC, column_value, matches


def totals_text(documents, selected=False):
    count = len(documents)
    parts = [f'Файлов: {count}', f"Байт: {sum(d.get('size') or 0 for d in documents):,}".replace(',', ' ')]
    only_word = bool(documents) and all(Path(d['path']).suffix.lower() in ('.doc', '.docx') for d in documents)
    for field, label in [('pages', 'Стр.'), ('figures', 'Рис.'), ('tables', 'Табл.')]:
        known = [d.get('stats', {}) for d in documents if d.get('stats', {}).get(field) is not None]
        if not known:
            parts.append(f'{label}: —')
            continue
        incomplete = len(known) < count or (field != 'pages' and not only_word)
        estimated = any(s.get(field + '_estimated') for s in known)
        prefix = ('>' if incomplete else '') + ('≈' if estimated else '')
        parts.append(f"{label}: {prefix}{sum(s[field] for s in known)} ({len(known)}/{count})")
    return ('Выбрано: ' if selected else 'Весь каталог, включая копии: ') + ' · '.join(parts)


class App:
    def __init__(self, window):
        self.window, self.session = window, None
        self.events = queue.Queue()
        self.busy, self.operation = False, ''
        self.view_docs = []
        self.last_tick = 0
        self.sort_column, self.sort_reverse = 'number', False
        self.similarity_ranks = {}
        self.similarity_groups = {}
        self.column_filters = {}
        self.similarity_notes = {}
        self.similarity_identities = {}
        self.search, self.filter = tk.StringVar(), tk.StringVar(value='Все файлы')
        self.backup_mode = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='Откройте каталог. Для нетронутого бэкапа сначала включите режим «Бэкап».')
        self.details = tk.StringVar(value='Ctrl/Shift — выделение · Delete — корзина · F5 — обновить · Esc — стоп')
        self.totals = tk.StringVar(value='Весь каталог: —')
        window.title('CorpusPick — структура и документы')
        window.geometry('1250x740')
        window.minsize(850, 440)
        window.rowconfigure(3, weight=1)
        window.columnconfigure(0, weight=1)
        self.buttons = []
        self.mutation_buttons = []
        main = ttk.Frame(window, padding=(10, 10, 10, 4))
        main.grid(row=0, column=0, sticky='ew')
        for label, fn in [('Открыть каталог', self.open_folder), ('Перенести в корень', self.flatten),
                          ('Удалить пустые папки', self.clean), ('Найти дубли', self.find_duplicates),
                          ('Удалить дубликаты', self.delete_duplicates)]:
            b = ttk.Button(main, text=label, command=fn)
            b.pack(side='left', padx=(0, 6))
            self.buttons.append(b)
            if fn not in (self.open_folder, self.find_duplicates):
                self.mutation_buttons.append(b)
        self.backup_check = ttk.Checkbutton(main, text='Бэкап: открыть без изменений', variable=self.backup_mode)
        self.backup_check.pack(side='left')
        actions = ttk.Frame(window, padding=(10, 3))
        actions.grid(row=1, column=0, sticky='ew')
        for label, fn in [('Сохранить CSV', self.export_csv), ('Загрузить CSV', self.import_csv),
                          ('Статистика', self.statistics), ('Уточнить Word', lambda: self.statistics(True)),
                          ('Обновить', self.scan)]:
            b = ttk.Button(actions, text=label, command=fn)
            b.pack(side='left', padx=(0, 6))
            self.buttons.append(b)
            if label == 'Уточнить Word':
                self.mutation_buttons.append(b)
        self.stop_button = ttk.Button(actions, text='Стоп', command=self.stop)
        self.stop_button.pack(side='left', padx=(0, 6))
        self.reset_button = ttk.Button(actions, text='Сбросить план', command=self.reset_plan)
        self.reset_button.pack(side='left')
        filters = ttk.Frame(window, padding=(10, 5))
        filters.grid(row=2, column=0, sticky='ew')
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text='Файл / исходная папка:').grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(filters, textvariable=self.search).grid(row=0, column=1, sticky='ew')
        ttk.Combobox(filters, textvariable=self.filter, state='readonly', width=19,
                     values=['Все файлы', 'Точные дубли', 'Есть ошибки']).grid(row=0, column=2, padx=(8, 0))
        for column, label, fn in [(3, 'По похожести', self.sort_similar), (4, 'Убрать префикс', self.trim_names)]:
            button = ttk.Button(filters, text=label, command=fn)
            button.grid(row=0, column=column, padx=(8, 0))
            self.buttons.append(button)
            if column == 3:
                self.similarity_button = button
                button.bind('<Button-3>', self.similarity_menu)
            if column == 4:
                self.mutation_buttons.append(button)
        listing = ttk.Frame(window, padding=(10, 0))
        listing.grid(row=3, column=0, sticky='nsew')
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.columns = ('number', 'name', 'folder', 'origin', 'type', 'size', 'duplicate', 'pages', 'figures', 'tables')
        self.tree = ttk.Treeview(listing, columns=self.columns, show='headings', selectmode='extended')
        titles = ['№', 'Файл', 'Текущая папка', 'Исходный путь / варианты', 'Тип', 'Байт', 'Дубль', 'Стр.', 'Рис.', 'Табл.']
        self.titles = dict(zip(self.columns, titles))
        for column, title, width in zip(self.columns, titles, [50, 220, 110, 240, 55, 90, 110, 65, 60, 60]):
            self.tree.heading(column, text=title + ' ▾', command=lambda c=column: self.header_menu(c))
            self.tree.column(column, width=width, minwidth=40, stretch=column in ('name', 'origin'),
                             anchor='e' if column in ('number', 'size', 'pages', 'figures', 'tables') else 'w')
        self.tree.tag_configure('error', foreground='#9c3a16')
        ybar = ttk.Scrollbar(listing, command=self.tree.yview)
        xbar = ttk.Scrollbar(listing, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        ybar.grid(row=0, column=1, sticky='ns')
        xbar.grid(row=1, column=0, sticky='ew')
        self.progress = ttk.Progressbar(window, mode='indeterminate')
        self.progress.grid(row=4, column=0, sticky='ew', padx=10, pady=(5, 0))
        self.labels = []
        for row, variable in [(5, self.status), (6, self.details), (7, self.totals)]:
            label = ttk.Label(window, textvariable=variable, padding=(10, 4), wraplength=1180)
            if row == 7:
                label.configure(font=('Segoe UI', 9), foreground='#555555')
            label.grid(row=row, column=0, sticky='ew')
            self.labels.append(label)
        def resize(event):
            if event.widget == window:
                for label in self.labels:
                    label.configure(wraplength=max(600, window.winfo_width()-30))
        window.bind('<Configure>', resize)
        self.context = tk.Menu(window, tearoff=False)
        self.context.add_command(label='Показать в Проводнике', command=self.show_in_explorer)
        self.context.add_command(label='Скопировать название', command=self.copy_name)
        self.context.add_command(label='Копировать пути', command=self.copy_path)
        self.context.add_command(label='Удалить выделенные в корзину     Delete', command=self.delete_selected)
        self.context.add_command(label='Снять выделение', command=self.clear_selection)
        self.tree.bind('<Button-3>', self.popup)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        self.tree.bind('<Double-1>', lambda _: self.show_in_explorer())
        self.tree.bind('<Return>', lambda _: self.show_in_explorer())
        self.tree.bind('<Delete>', lambda _: self.delete_selected())
        self.tree.bind('<Control-KeyPress>', self.control_key)
        window.bind('<Control-KeyPress>', self.control_key)
        window.bind('<F5>', lambda _: self.scan())
        window.bind('<Escape>', lambda _: self.stop() if self.busy else self.clear_selection())
        self.search.trace_add('write', lambda *_: self.render())
        self.filter.trace_add('write', lambda *_: self.render())
        window.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh_controls()
        window.after(100, self.poll)

    def refresh_controls(self):
        for index, b in enumerate(self.buttons):
            disabled = self.busy or (index > 0 and not self.session)
            if b in self.mutation_buttons and self.session and self.session.read_only:
                disabled = True
            b.configure(state='disabled' if disabled else 'normal')
        self.backup_check.configure(state='disabled' if self.busy else 'normal')
        self.stop_button.configure(state='normal' if self.busy else 'disabled')
        pending = self.session and any(not m['done'] for m in self.session.state['moves'])
        self.reset_button.configure(state='normal' if pending and not self.busy and not self.session.read_only else 'disabled')

    def run(self, label, function, done=None):
        if self.busy:
            return
        if self.session:
            self.session.cancel.clear()
        self.busy, self.operation = True, label
        self.last_tick = 0
        self.status.set(label + '…')
        self.refresh_controls()
        self.progress.start(12)
        def worker():
            try:
                self.events.put(('done', done, function()))
            except Exception as exc:
                self.events.put(('error', str(exc) if isinstance(exc, ValueError) else failure(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def tick(self, count):
        now = time.monotonic()
        if count == 0 or now - self.last_tick > 1:
            self.last_tick = now
            if count == 0:
                self.events.put(('snapshot', count, copy.deepcopy(self.session.state['documents'])))
            else:
                self.events.put(('progress', count))

    def stop(self):
        if self.busy and self.session:
            self.session.cancel.set()
            self.status.set('Остановка: завершаем текущий безопасный шаг и сохраняем прогресс…')

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == 'progress':
                    if not self.session.cancel.is_set():
                        self.status.set(f'{self.operation}… обработано {event[1]}')
                    continue
                if event[0] == 'snapshot':
                    self.view_docs = event[2]
                    if not self.session.cancel.is_set():
                        self.status.set(f'{self.operation}… обработано {event[1]}')
                    self.render()
                    continue
                self.busy = False
                self.progress.stop()
                self.refresh_controls()
                self.refreshed()
                if event[0] == 'error':
                    messagebox.showerror('Операция остановлена', event[1])
                elif event[1]:
                    event[1](event[2])
        except queue.Empty:
            pass
        self.window.after(100, self.poll)

    def open_folder(self):
        if self.busy:
            return
        chosen = filedialog.askdirectory(title='Выберите каталог (режим бэкапа задаётся галочкой в главном окне)')
        if not chosen:
            return
        if self.session and self.session.root == Path(chosen).resolve():
            self.session.read_only = self.backup_mode.get()
            self.window.title(f"CorpusPick — {'БЭКАП, БЕЗ ИЗМЕНЕНИЙ — ' if self.session.read_only else ''}{chosen}")
            self.scan()
            return
        try:
            session = Session(Path(chosen), read_only=self.backup_mode.get())
        except Exception as exc:
            messagebox.showerror('Каталог', str(exc) if isinstance(exc, ValueError) else failure(exc))
            return
        if self.session:
            self.session.close()
        self.session = session
        self.similarity_notes = {}
        self.column_filters = {}
        self.similarity_groups = {}
        self.similarity_ranks = {}
        self.similarity_button.configure(text='По похожести')
        self.sort_column, self.sort_reverse = 'number', False
        self.view_docs = []
        self.window.title(f"CorpusPick — {'БЭКАП, БЕЗ ИЗМЕНЕНИЙ — ' if session.read_only else ''}{chosen}")
        self.scan()

    def scan(self):
        if self.session and not self.busy:
            self.run('Обновление списка', lambda: self.session.open_catalog(self.tick))

    def refreshed(self):
        if not self.session:
            return
        self.view_docs = copy.deepcopy(self.session.state['documents'])
        current = {d['path']: d.get('identity') for d in self.view_docs}
        if self.similarity_groups and current != self.similarity_identities:
            self.similarity_groups, self.similarity_ranks, self.similarity_notes = {}, {}, {}
            self.similarity_button.configure(text='По похожести')
            if self.sort_column == 'similarity':
                self.sort_column, self.sort_reverse = 'number', False
        docs = self.view_docs
        pending = sum(not m['done'] for m in self.session.state['moves'])
        self.status.set(f"Файлов: {len(docs)} · SHA-256: {sum(bool(d.get('hash')) for d in docs)}/{len(docs)} · "
                        f"Ошибок: {sum(bool(d.get('error') or d.get('stats', {}).get('failed') or d.get('content', {}).get('failed') or d.get('content', {}).get('text_failed')) for d in docs)}" +
                        (' · БЭКАП: файлы не изменяются' if self.session.read_only else ' · Unlock автоматически') +
                        (f' · Не перенесено: {pending}' if pending else '') +
                        (' · Остановлено, прогресс сохранён' if self.session.cancel.is_set() else ''))
        self.render()

    def render(self):
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        indexed = [(i + 1, d) for i, d in enumerate(self.view_docs)]
        def key(pair):
            number, d = pair
            c = self.sort_column
            if c == 'similarity':
                return self.similarity_ranks.get(d['path'], len(self.similarity_ranks) + number)
            if c == 'number':
                return number
            if c in ('pages', 'figures', 'tables'):
                return d.get('stats', {}).get(c) if d.get('stats', {}).get(c) is not None else -1
            if c in ('size', 'duplicate'):
                return d.get(c) or 0
            return {'name': Path(d['path']).name, 'folder': str(Path(d['path']).parent),
                    'origin': ' | '.join(d.get('origins', [d['origin']])),
                    'type': Path(d['path']).suffix}.get(c, d.get(c, '')).casefold()
        ordered = sorted(indexed, key=key, reverse=self.sort_reverse)
        if self.similarity_groups:
            ordered.sort(key=lambda pair: self.similarity_groups.get(pair[1]['path'], len(self.similarity_groups) + pair[0]))
        for number, d in ordered:
            if not all(matches(column_value(d, c, number), rule) for c, rule in self.column_filters.items()):
                continue
            origins = d.get('origins', [d['origin']])
            if self.search.get().casefold() not in (d['path'] + ' ' + ' '.join(origins)).casefold():
                continue
            f = self.filter.get()
            if f == 'Точные дубли' and not d.get('duplicate') or f == 'Есть ошибки' and not (d.get('error') or d.get('stats', {}).get('failed') or d.get('content', {}).get('failed') or d.get('content', {}).get('text_failed')):
                continue
            stats = d.get('stats', {})
            def metric(name):
                value = stats.get(name)
                return '—' if value is None else ('≈' if stats.get(name + '_estimated') else '') + str(value)
            self.tree.insert('', 'end', iid=d['path'], values=(number, Path(d['path']).name, str(Path(d['path']).parent),
                ' | '.join(origins), Path(d['path']).suffix.lower() or '—', d.get('size') if d.get('size') is not None else '—',
                f"#{d['duplicate']}" if d.get('duplicate') else ('Не проверен' if not d.get('hash') else '—'), metric('pages'),
                metric('figures'), metric('tables')),
                tags=('error',) if d.get('error') or stats.get('failed') else ())
        existing = [p for p in selected if self.tree.exists(p)]
        if existing:
            self.tree.selection_set(existing)
        for column, title in self.titles.items():
            direction = (' ↓' if self.sort_reverse else ' ↑') if column == self.sort_column else ''
            self.tree.heading(column, text=title + direction + (' ●▾' if column in self.column_filters else ' ▾'))
        self.select()

    def selected_documents(self):
        paths = set(self.tree.selection())
        return [d for d in self.view_docs if d['path'] in paths]

    def selected(self):
        return next(iter(self.selected_documents()), None)

    def select(self, _=None):
        docs = self.selected_documents()
        self.totals.set(totals_text(docs or self.view_docs, selected=bool(docs)) + ' · > неполный подсчёт; ≈ сохранённые страницы')
        if len(docs) == 1:
            d = docs[0]
            self.details.set(f"Путь: {self.session.root / d['path']}\nИсходные пути: {' | '.join(d.get('origins', [d['origin']]))}" +
                             (' · ' + d['origin_match'] if d.get('origin_match') else '') +
                             ('\n' + (d.get('error') or d['stats']['info']) if d.get('error') or d.get('stats', {}).get('failed') else '') +
                             ('\nСравнение: ' + self.similarity_notes[d['path']] if d['path'] in self.similarity_notes else '') +
                             ('\n' + d['content']['info'] if d.get('content', {}).get('info') else ''))
        else:
            self.details.set(f'Выделено: {len(docs)} · Ctrl/Shift — выбор · Ctrl+A — все видимые · Esc — снять выбор / стоп · Delete — корзина')

    def select_all(self, _=None):
        self.tree.selection_set(self.tree.get_children())
        return 'break'

    def control_key(self, event):
        if event.widget.winfo_class() in ('Entry', 'TEntry', 'Text', 'TCombobox'):
            return
        if event.keysym.casefold() in ('a', 'cyrillic_ef') or (os.name == 'nt' and event.keycode == 65):
            return self.select_all()

    def similarity_menu(self, event):
        if self.busy or not self.session:
            return
        if hasattr(self, 'similarity_context'):
            self.similarity_context.destroy()
        menu = self.similarity_context = tk.Menu(self.window, tearoff=False)
        menu.add_command(label='Сбросить кэш похожести', command=self.clear_similarity_cache)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def clear_similarity_cache(self):
        if self.busy or not self.session:
            return
        if not messagebox.askyesno('Кэш похожести', 'Удалить отпечатки содержимого для текущего каталога?\nИсходные пути, CSV, SHA точных дублей и статистика сохранятся.\nСледующее сравнение заново прочитает файлы до 128 МиБ.'):
            return
        def done(_):
            self.similarity_notes, self.similarity_groups, self.similarity_ranks = {}, {}, {}
            self.similarity_button.configure(text='По похожести')
            self.sort_column, self.sort_reverse = 'number', False
            self.refreshed()
            self.status.set('Кэш похожести текущего каталога сброшен. Исходная структура сохранена.')
        self.run('Сброс кэша похожести', self.session.clear_similarity_cache, done)

    def sort_similar(self):
        if self.busy or not self.session:
            return
        if self.similarity_groups:
            self.similarity_notes = {}
            self.similarity_groups = {}
            self.similarity_ranks = {}
            self.similarity_button.configure(text='По похожести')
            self.sort_column, self.sort_reverse = 'number', False
            self.refreshed()
            return
        def done(ranks):
            if ranks is not None:
                self.similarity_ranks, self.similarity_groups, self.similarity_notes = ranks
                self.similarity_identities = {d['path']: d.get('identity') for d in self.view_docs}
                self.similarity_button.configure(text='Снять группировку')
                self.sort_column, self.sort_reverse = 'similarity', False
                self.render()
                unavailable = sum(bool(d.get('content', {}).get('failed') or d.get('content', {}).get('text_failed')) for d in self.view_docs)
                from .content_similarity import size_only
                large = sum(size_only(d) for d in self.view_docs)
                self.status.set(f'Группировка по содержимому (complete-link) · Только размер (>128 МиБ): {large} · Ошибок чтения/извлечения: {unavailable}. '
                                'Заголовки сортируют внутри групп. Основание сравнения — под выбранным файлом.')
        self.run('Сравнение содержимого файлов', lambda: self.session.group_similar(self.tick), done)

    def trim_names(self):
        docs = copy.deepcopy(self.selected_documents())
        if self.busy or not docs or self.session.read_only:
            return
        count = simpledialog.askinteger('Убрать префикс', 'Сколько символов убрать?',
                                        parent=self.window, initialvalue=1, minvalue=1)
        if count is not None:
            self.run('Переименование', lambda: self.session.trim_prefix(docs, count, self.tick),
                     lambda r: self.result('Переименование', f"Переименовано: {r['moved']}. Исходные пути сохранены.", r['errors']))

    def clear_selection(self):
        self.tree.selection_remove(self.tree.selection())

    def sort(self, column):
        self.sort_reverse = not self.sort_reverse if self.sort_column == column else False
        self.sort_column = column
        self.render()

    def header_menu(self, column):
        menu = tk.Menu(self.window, tearoff=False)
        def order(reverse):
            self.sort_column, self.sort_reverse = column, reverse
            self.render()
        menu.add_command(label='По возрастанию', command=lambda: order(False))
        menu.add_command(label='По убыванию', command=lambda: order(True))
        menu.add_separator()
        menu.add_command(label='Фильтр…', command=lambda: self.edit_filter(column))
        menu.add_command(label='Снять фильтр колонки', command=lambda: self.clear_filter(column))
        menu.add_command(label='Снять все фильтры', command=lambda: self.clear_filter())
        try:
            menu.tk_popup(self.window.winfo_pointerx(), self.window.winfo_pointery())
        finally:
            menu.grab_release()

    def edit_filter(self, column):
        dialog = FilterDialog(self.window, self.titles[column], column in NUMERIC, self.column_filters.get(column))
        if dialog.result is not None:
            if dialog.result:
                self.column_filters[column] = dialog.result
            else:
                self.column_filters.pop(column, None)
            self.render()

    def clear_filter(self, column=None):
        if column is None:
            self.column_filters.clear()
            self.search.set('')
            self.filter.set('Все файлы')
        else:
            self.column_filters.pop(column, None)
        self.render()

    def popup(self, event):
        if self.tree.identify_region(event.x, event.y) == 'heading':
            column = self.tree.identify_column(event.x)
            if column:
                self.header_menu(self.columns[int(column[1:]) - 1])
            return
        row = self.tree.identify_row(event.y)
        if row and not self.busy:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.context.tk_popup(event.x_root, event.y_root)

    def show_in_explorer(self):
        docs = self.selected_documents()
        if docs and not self.busy and os.name == 'nt':
            try:
                show_files([self.session.safe_path(d['path']) for d in docs])
            except (OSError, ValueError) as exc:
                messagebox.showerror('Показать в Проводнике', str(exc))

    def copy_name(self):
        docs = self.selected_documents()
        if docs:
            self.window.clipboard_clear()
            self.window.clipboard_append('\n'.join(Path(d['path']).name for d in docs))

    def copy_path(self):
        if self.selected_documents():
            self.window.clipboard_clear()
            self.window.clipboard_append('\n'.join(str(self.session.root / d['path']) for d in self.selected_documents()))

    def result(self, title, summary, errors=()):
        if errors:
            summary += '\n\n' + '\n'.join(f"{e['path']}: {e['error']}" if isinstance(e, dict) else e for e in errors[:10])
        messagebox.showinfo(title, summary)

    def delete_selected(self):
        docs = copy.deepcopy(self.selected_documents())
        if self.busy or not docs or self.session.read_only:
            return
        if messagebox.askyesno('Корзина', f'Отправить выделенные файлы в корзину: {len(docs)}?'):
            self.run('Корзина', lambda: self.session.trash_documents(docs, progress=self.tick),
                     lambda r: self.result('Корзина', f"Отправлено: {r['trashed']}", r['errors']))

    def delete_duplicates(self):
        if not self.session or self.busy or self.session.read_only:
            return
        selected = {d['path'] for d in self.selected_documents()}
        def confirm(plan):
            if selected:
                plan = [e for e in plan if e['remove']['path'] in selected]
            if self.session.cancel.is_set():
                return
            if not plan:
                self.result('Дубликаты', 'Лишних точных копий в области выбора нет.')
            elif messagebox.askyesno('Дубликаты', f'Отправить в корзину {len(plan)} копий?\nСохраняется один экземпляр: ближе к корню, затем по имени.\n' +
                                    ('Удаляются только выделенные лишние копии.' if selected else 'Проверяется весь каталог.')):
                self.run('Удаление дублей', lambda: self.session.trash_documents(duplicate_plan=plan, progress=self.tick),
                         lambda r: self.result('Дубликаты', f"Отправлено: {r['trashed']}", r['errors']))
        self.run('Проверка дублей', lambda: self.session.duplicate_plan(self.tick), confirm)

    def find_duplicates(self):
        if not self.session or self.busy:
            return
        def done(plan):
            documents = self.session.state['documents']
            unknown = sum(not d.get('hash') for d in documents)
            groups = len({d['duplicate'] for d in documents if d.get('duplicate')})
            self.status.set(f"Проверка дублей: {len(documents) - unknown}/{len(documents)} · "
                            f"Групп: {groups} · Лишних копий: {len(plan)}" +
                            (f" · Не проверено: {unknown}. Нажмите «Найти дубли» для продолжения." if unknown else '') +
                            (' · Остановлено' if self.session.cancel.is_set() else ''))
        self.run('Поиск точных дублей по SHA-256', lambda: self.session.duplicate_plan(self.tick), done)

    def flatten(self):
        if self.session and not self.busy and not self.session.read_only and messagebox.askyesno('Перенос',
            'Перенести все вложенные файлы в корень? CSV исходной структуры автоматически сохраняется до переноса и после него. '
            'Стоп приостановит работу; эта же кнопка продолжит её.'):
            self.run('Перенос в корень', lambda: self.session.flatten(self.tick),
                lambda r: self.result('Перенос', f"Перенесено: {r['moved']}. " + ('Остановлено.' if r.get('stopped') else 'Готово.') +
                                      (f"\nCSV структуры: {r['csv']}" if r.get('csv') else ''), r['errors']))

    def clean(self):
        if self.session and not self.busy and not self.session.read_only:
            self.run('Пустые папки', self.session.remove_empty_directories,
                     lambda r: self.result('Папки', f"Удалено: {r['removed']}", r['errors']))

    def statistics(self, use_word=False):
        if not self.session or self.busy:
            return
        selected = {d['path'] for d in self.selected_documents()} or None
        if use_word and not messagebox.askyesno('Уточнить Word', 'Пересчитать DOC/DOCX через установленный Word? Это медленнее; Стоп сработает после текущего документа (до 120 секунд). Файлы открываются только для чтения.'):
            return
        self.run('Статистика выбранных' if selected else 'Статистика всего каталога',
                 lambda: self.session.collect_stats(self.tick, use_word, selected))

    def export_csv(self):
        if not self.session or self.busy:
            return
        filename = filedialog.asksaveasfilename(title='Сохранить исходную структуру вне каталога документов',
                                               defaultextension='.csv', filetypes=[('Структура CorpusPick', '*.csv')])
        if filename:
            self.run('Сохранение структуры и SHA-256', lambda: self.session.export_structure(filename, self.tick),
                     lambda path: self.result('CSV', 'Структура сохранена: ' + path) if path else None)

    def import_csv(self):
        if not self.session or self.busy:
            return
        filename = filedialog.askopenfilename(title='Загрузить структуру из бэкапа', filetypes=[('Структура CorpusPick', '*.csv')])
        if filename:
            self.run('Сопоставление CSV по SHA-256', lambda: self.session.import_structure(filename, self.tick),
                     lambda r: self.result('Структура CSV', f"Совпало файлов: {r['matched']}. Несколько возможных исходных путей: {r['ambiguous']}. Без совпадений: {r['unmatched']}."))

    def reset_plan(self):
        if self.session and not self.busy:
            self.run('Сброс оставшегося плана', self.session.cancel_pending)

    def close(self):
        if self.busy:
            self.stop()
            messagebox.showinfo('Остановка', 'Сохраняется прогресс. Закройте окно после завершения остановки.')
            return
        if self.session:
            self.session.close()
        self.window.destroy()


def main():
    window = tk.Tk()
    App(window)
    window.mainloop()


if __name__ == '__main__':
    main()
