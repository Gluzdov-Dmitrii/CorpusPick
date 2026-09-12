"""A small folder workbench used alongside Windows Explorer."""
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import Session, failure


class App:
    def __init__(self, window):
        self.window, self.session = window, None
        self.events = queue.Queue()
        self.busy = False
        self.operation = ''
        self.sort_column, self.sort_reverse = 'name', False
        self.search, self.filter = tk.StringVar(), tk.StringVar(value='Все файлы')
        self.status = tk.StringVar(value='Откройте рабочий каталог. Просмотр и сортировка документов — в Explorer.')
        self.details = tk.StringVar(value='F5 — обновить · Enter или двойной щелчок — показать файл в Проводнике')
        self.totals = tk.StringVar(value='Итого по каталогу: —')
        window.title('CorpusPick — каталоги и состав документов')
        window.geometry('1120x700')
        window.minsize(720, 420)
        window.rowconfigure(2, weight=1)
        window.columnconfigure(0, weight=1)
        menu = tk.Menu(window)
        self.tools_menu = tk.Menu(menu, tearoff=False)
        menu.add_cascade(label='Инструменты', menu=self.tools_menu)
        for title, command in [('Unlock — разблокировать все вложенные файлы…', self.unlock),
                               ('Подсчитать состав DOCX / страницы PDF', self.statistics),
                               ('Подсчитать через Word (DOC / DOCX)…', lambda: self.statistics(True)),
                               ('Обновить список     F5', self.scan),
                               ('Сбросить незавершённый план переноса', self.reset_plan)]:
            self.tools_menu.add_command(label=title, command=command)
        window.configure(menu=menu)
        bar = ttk.Frame(window, padding=(10, 10, 10, 5))
        bar.grid(row=0, column=0, sticky='ew')
        self.buttons = []
        for title, command in [('Открыть каталог', self.open_folder), ('Перенести в корень', self.flatten),
                               ('Удалить пустые папки', self.clean), ('Удалить дубликаты', self.delete_duplicates)]:
            button = ttk.Button(bar, text=title, command=command)
            button.pack(side='left', padx=(0, 8))
            self.buttons.append(button)
        filters = ttk.Frame(window, padding=(10, 5))
        filters.grid(row=1, column=0, sticky='ew')
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text='Найти:').grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(filters, textvariable=self.search).grid(row=0, column=1, sticky='ew')
        ttk.Combobox(filters, textvariable=self.filter, state='readonly', width=19,
                     values=['Все файлы', 'Точные дубли', 'Есть ошибки', 'Скорее да']).grid(row=0, column=2, padx=(8, 0))
        listing = ttk.Frame(window, padding=(10, 0))
        listing.grid(row=2, column=0, sticky='nsew')
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.columns = ('name', 'folder', 'type', 'size', 'duplicate', 'note', 'pages', 'figures', 'tables', 'appendices', 'error')
        self.tree = ttk.Treeview(listing, columns=self.columns, show='headings', selectmode='browse')
        for column, title, width in zip(self.columns,
            ['Файл', 'Папка', 'Тип', 'Байт', 'Дубль', 'Метка', 'Страниц', 'Рисунков', 'Таблиц', 'Приложений ≈', 'Примечание'],
            [250, 160, 60, 85, 65, 85, 75, 80, 75, 105, 180]):
            self.tree.heading(column, text=title, command=lambda c=column: self.sort(c))
            self.tree.column(column, width=width, minwidth=50, stretch=column in ('name', 'folder', 'error'),
                             anchor='e' if column in ('size', 'pages', 'figures', 'tables', 'appendices') else 'w')
        self.tree.tag_configure('error', foreground='#9c3a16')
        ybar = ttk.Scrollbar(listing, command=self.tree.yview)
        xbar = ttk.Scrollbar(listing, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        ybar.grid(row=0, column=1, sticky='ns')
        xbar.grid(row=1, column=0, sticky='ew')
        self.progress = ttk.Progressbar(window, mode='indeterminate')
        self.progress.grid(row=3, column=0, sticky='ew', padx=10, pady=(6, 0))
        self.status_label = ttk.Label(window, textvariable=self.status, padding=(10, 5), wraplength=1000)
        self.status_label.grid(row=4, column=0, sticky='ew')
        self.detail_label = ttk.Label(window, textvariable=self.details, padding=(10, 0, 10, 10), wraplength=1000)
        self.detail_label.grid(row=5, column=0, sticky='ew')
        self.totals_label = ttk.Label(window, textvariable=self.totals, padding=(10, 6, 10, 8),
                                     font=('Segoe UI', 9), foreground='#666666', wraplength=1000)
        self.totals_label.grid(row=6, column=0, sticky='ew')
        def resize(event):
            width = max(500, window.winfo_width() - 30)
            self.detail_label.configure(wraplength=width)
            self.status_label.configure(wraplength=width)
            self.totals_label.configure(wraplength=width)
        window.bind('<Configure>', resize)
        self.context = tk.Menu(window, tearoff=False)
        self.context.add_command(label='Показать в Проводнике', command=self.show_in_explorer)
        self.context.add_command(label='Копировать путь', command=self.copy_path)
        self.context.add_command(label='Удалить в корзину     Delete', command=self.delete_selected)
        self.context.add_separator()
        self.context.add_command(label='Скорее да — поставить / снять     2', command=self.mark)
        self.tree.bind('<Button-3>', self.popup)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        self.tree.bind('<Double-1>', lambda _: self.show_in_explorer())
        self.tree.bind('<Return>', lambda _: self.show_in_explorer())
        self.tree.bind('2', lambda _: self.mark())
        self.tree.bind('<Delete>', lambda _: self.delete_selected())
        window.bind('<F5>', lambda _: self.scan())
        self.search.trace_add('write', lambda *_: self.render())
        self.filter.trace_add('write', lambda *_: self.render())
        window.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh_controls()
        window.after(100, self.poll)

    def refresh_controls(self):
        for i, button in enumerate(self.buttons):
            button.configure(state='disabled' if self.busy or (i and not self.session) else 'normal')
        for i in range(self.tools_menu.index('end') + 1):
            self.tools_menu.entryconfigure(i, state='normal' if self.session and not self.busy else 'disabled')

    def run(self, label, function, done=None):
        if self.busy:
            return
        self.busy, self.operation = True, label
        self.status.set(label + '…')
        self.refresh_controls()
        self.progress.start(12)
        def worker():
            try:
                self.events.put(('done', done, function()))
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ValueError) else failure(exc)
                self.events.put(('error', reason))
        threading.Thread(target=worker, daemon=True).start()

    def tick(self, count):
        if count % 20 == 0:
            self.events.put(('progress', count))

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == 'progress':
                    self.status.set(f'{self.operation}… обработано {event[1]}')
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
        chosen = filedialog.askdirectory(title='Рабочий каталог документов')
        if not chosen:
            return
        if self.session and self.session.root == Path(chosen).resolve():
            self.scan()
            return
        try:
            session = Session(Path(chosen))
        except Exception as exc:
            messagebox.showerror('Не удалось открыть каталог', str(exc) if isinstance(exc, ValueError) else failure(exc))
            return
        if self.session:
            self.session.close()
        self.session = session
        self.window.title(f'CorpusPick — {chosen}')
        self.scan()

    def scan(self):
        if self.session and not self.busy:
            self.run('Обновление списка и проверка дублей', lambda: self.session.scan(self.tick))

    def refreshed(self):
        if not self.session:
            return
        docs = self.session.state['documents']
        hashes = [d['hash'] for d in docs if d.get('hash')]
        errors = sum(bool(d.get('error')) for d in docs)
        pending = sum(not m['done'] for m in self.session.state['moves'])
        self.status.set(f"Файлов: {len(docs)} · {sum(d.get('size') or 0 for d in docs)/1024/1024:.1f} МБ · "
                        f"Лишних точных копий: {len(hashes)-len(set(hashes))} · Ошибок: {errors} · "
                        f"Недоступных каталогов: {len(self.session.state.get('issues', []))}" +
                        (f' · Не перенесено: {pending} (повторите перенос)' if pending else ''))
        totals = [f"Файлов: {len(docs)}", f"Байт: {sum(d.get('size') or 0 for d in docs):,}".replace(',', ' ')]
        for field, label in [('pages', 'Страниц'), ('figures', 'Рисунков'), ('tables', 'Таблиц'), ('appendices', 'Приложений')]:
            known = [d['stats'] for d in docs if d.get('stats', {}).get(field) is not None]
            if known:
                prefix = '≈' if any(s.get(field + '_estimated') for s in known) else ''
                totals.append(f"{label}: {prefix}{sum(s[field] for s in known)} ({len(known)} файлов)")
            else:
                totals.append(f'{label}: —')
        self.totals.set('Итого по всему каталогу, включая копии: ' + ' · '.join(totals))
        self.render()

    def render(self):
        if not self.session or self.busy:
            return
        current = self.selected()
        selected_path = current['path'] if current else None
        self.tree.delete(*self.tree.get_children())
        docs = self.session.state['documents']
        has_stats = any(d.get('stats') for d in docs)
        self.tree.configure(displaycolumns=self.columns if has_stats else tuple(c for c in self.columns if c not in ('pages', 'figures', 'tables', 'appendices')))
        def key(d):
            c = self.sort_column
            if c in ('pages', 'figures', 'tables', 'appendices'):
                return d.get('stats', {}).get(c) if d.get('stats', {}).get(c) is not None else -1
            if c in ('size', 'duplicate'):
                return d.get(c) or 0
            return {'name': Path(d['path']).name, 'folder': str(Path(d['path']).parent),
                    'type': Path(d['path']).suffix}.get(c, d.get(c, '')).casefold()
        for d in sorted(docs, key=key, reverse=self.sort_reverse):
            choice = self.filter.get()
            if self.search.get().casefold() not in (d['path'] + ' ' + d['origin']).casefold():
                continue
            if choice == 'Точные дубли' and not d.get('duplicate') or choice == 'Есть ошибки' and not d.get('error') or choice == 'Скорее да' and not d.get('note'):
                continue
            stats = d.get('stats', {})
            def metric(name):
                value = stats.get(name)
                return '—' if value is None else ('≈' if stats.get(name + '_estimated') else '') + str(value)
            self.tree.insert('', 'end', iid=d['path'], values=(Path(d['path']).name, str(Path(d['path']).parent),
                Path(d['path']).suffix.lower() or '—', d.get('size') if d.get('size') is not None else '—',
                f"#{d['duplicate']}" if d.get('duplicate') else ('?' if not d.get('hash') else '—'), d.get('note', ''),
                metric('pages'), metric('figures'), metric('tables'), metric('appendices'), d.get('error', '') or stats.get('info', '')),
                tags=('error',) if d.get('error') else ())
        if selected_path and self.tree.exists(selected_path):
            self.tree.selection_set(selected_path)
        else:
            self.details.set('F5 — обновить после сортировки в Explorer · Двойной щелчок — показать в Проводнике · 2 — метка «Скорее да»')

    def selected(self):
        selection = self.tree.selection()
        if self.session and selection:
            return next((d for d in self.session.state['documents'] if d['path'] == selection[0]), None)

    def select(self, _=None):
        d = self.selected()
        if d:
            self.details.set(f"Путь: {self.session.root / d['path']}\nБыл: {d['origin']}" +
                             ('\n' + (d.get('error') or d.get('stats', {}).get('info', '')) if d.get('error') or d.get('stats') else ''))

    def sort(self, column):
        self.sort_reverse = not self.sort_reverse if self.sort_column == column else False
        self.sort_column = column
        self.render()

    def popup(self, event):
        row = self.tree.identify_row(event.y)
        if row and not self.busy:
            self.tree.selection_set(row)
            self.context.tk_popup(event.x_root, event.y_root)

    def mark(self):
        if self.selected() and not self.busy:
            try:
                self.session.mark(self.selected()['path'])
                self.render()
            except OSError as exc:
                messagebox.showerror('Не удалось сохранить метку', failure(exc))

    def show_in_explorer(self):
        if self.selected() and not self.busy and os.name == 'nt':
            try:
                path = self.session.safe_path(self.selected()['path'])
                subprocess.Popen(['explorer.exe', f'/select,{path}'])
            except (OSError, ValueError) as exc:
                messagebox.showerror('Проводник', failure(exc))

    def copy_path(self):
        if self.selected():
            self.window.clipboard_clear()
            self.window.clipboard_append(str(self.session.root / self.selected()['path']))

    def delete_selected(self):
        if self.busy or not self.selected():
            return
        document = dict(self.selected())
        following = self.tree.next(document['path']) or self.tree.prev(document['path'])
        if not messagebox.askyesno('Удалить в корзину', f"Отправить в корзину файл?\n\n{document['path']}"):
            return
        def finished(result):
            if result['errors']:
                self.result('Корзина', f"Отправлено в корзину: {result['trashed']}.", result['errors'])
            elif following and self.tree.exists(following):
                self.tree.selection_set(following)
                self.tree.see(following)
                self.tree.focus_set()
        self.run('Удаление в корзину', lambda: self.session.trash_documents([document], progress=self.tick), finished)

    def delete_duplicates(self):
        if not self.session or self.busy:
            return
        def confirm(plan):
            if not plan:
                messagebox.showinfo('Дубликаты', 'Точных дубликатов не найдено.')
                return
            size = sum(entry['remove'].get('size') or 0 for entry in plan)
            if messagebox.askyesno('Удалить дубликаты',
                f'Отправить в корзину {len(plan)} точных копий ({size / 1024 / 1024:.1f} МБ)?\n\n'
                'В каждой группе останется один файл: сначала с меткой «Скорее да», затем ближе к корню, '
                'затем первый по имени. Перед удалением содержимое проверяется повторно.\n\n'
                'Действие относится ко всему каталогу, независимо от фильтра списка.'):
                self.run('Удаление дублей в корзину',
                         lambda: self.session.trash_documents(duplicate_plan=plan, progress=self.tick),
                         lambda result: self.result('Дубликаты', f"Отправлено в корзину: {result['trashed']}.", result['errors']))
        self.run('Проверка точных дублей', lambda: self.session.duplicate_plan(self.tick), confirm)

    def result(self, title, summary, errors):
        if errors:
            lines = [f"{e['path']}: {e['error']}" if isinstance(e, dict) else e for e in errors[:10]]
            summary += '\n\n' + '\n'.join(lines) + (f'\nЕщё ошибок: {len(errors)-10}' if len(errors) > 10 else '')
        messagebox.showinfo(title, summary)

    def flatten(self):
        if self.session and not self.busy and messagebox.askyesno('Перенести в корень',
            'Перенести все файлы из вложенных папок в корень текущего каталога? Совпадающие имена получат суффикс. '
            'Недоступные файлы останутся на месте. Фильтр списка не ограничивает перенос.'):
            self.run('Перенос файлов', lambda: self.session.flatten(self.tick),
                     lambda r: self.result('Перенос', f"Перенесено: {r['moved']}. Не перенесено: {len(r['errors'])}.", r['errors']))

    def clean(self):
        if self.session and not self.busy:
            self.run('Удаление пустых папок', self.session.remove_empty_directories,
                     lambda r: self.result('Пустые папки', f"Удалено: {r['removed']}. Непустые папки сохранены.", r['errors']))

    def unlock(self):
        if self.session and not self.busy and messagebox.askyesno('Unlock',
            'Снять отметку «скачано из интернета» со всех файлов в каталоге и подпапках? '
            'Это разрешает их обычное открытие/предпросмотр Windows. Применяйте к файлам, которым доверяете. '
            'Права доступа, пароли и занятость файла другой программой не меняются.'):
            self.run('Unlock', lambda: self.session.unlock(self.tick),
                     lambda r: self.result('Unlock', f"Разблокировано: {r['unlocked']}. Без отметки: {r['unchanged']}.", r['errors']))

    def statistics(self, use_word=False):
        if not self.session or self.busy:
            return
        if use_word and not messagebox.askyesno('Подсчёт через Word',
            'Открыть DOC/DOCX в отдельном скрытом Word только для чтения и пересчитать страницы, рисунки и таблицы? '
            'Макросы и обновление ссылок отключаются, документы не сохраняются. Нужен установленный Word; '
            'его подключённые службы зависят от настроек Office. До 120 секунд на файл.'):
            return
        self.run('Подсчёт состава документов', lambda: self.session.collect_stats(self.tick, use_word))

    def reset_plan(self):
        if self.session and not self.busy:
            def operation():
                self.session.cancel_pending()
                self.session.scan(self.tick)
            self.run('Сброс оставшегося плана', operation)

    def close(self):
        if self.busy:
            messagebox.showinfo('Операция выполняется', 'Дождитесь завершения операции перед закрытием.')
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
