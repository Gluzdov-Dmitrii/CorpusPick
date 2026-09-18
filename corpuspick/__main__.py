"""Local folder workbench: portable structure, selection statistics and safe stop."""
import copy
import colorsys
import hashlib
from io import BytesIO
import os
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .core import Session, failure
from .edit_shortcuts import install_edit_shortcuts, ask_editable_string
from .error_logging import log_exception, log_message, sanitize, setup_error_logging, enable_native_crash_logging
from .explorer import show_files
from .filters import FilterDialog, NUMERIC, column_value, matches
from .office_profile import PREVIEW_TIMEOUT

PRERENDER_MAX_WORKERS = 2
PREVIEW_WORKER_RECYCLE_AFTER = 40
PRERENDER_WORKER_RECYCLE_AFTER = 40


def natural_name_key(value):
    return tuple((1, int(part)) if part.isdecimal() else (0, part.casefold())
                 for part in re.split(r'(\d+)', value))


def source_directory_key(document):
    origins = document.get('origins') or [document.get('origin') or document['path']]
    directories = {Path(str(origin)).parent.as_posix().casefold() for origin in origins}
    return '\0'.join(sorted(directories))


def source_directory_tag(key):
    return 'source-folder-' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]


def source_directory_background(key):
    digest = hashlib.sha256(key.encode('utf-8')).digest()
    hue = int.from_bytes(digest[:4], 'big') / 0x100000000
    red, green, blue = colorsys.hls_to_rgb(hue, .92, .55)
    return f'#{round(red*255):02x}{round(green*255):02x}{round(blue*255):02x}'


def totals_text(documents, selected=False):
    count = len(documents)
    parts = [f'Файлов: {count}', f"Байт: {sum(d.get('size') or 0 for d in documents):,}".replace(',', ' ')]
    only_word = bool(documents) and all(Path(d['path']).suffix.lower() in ('.doc', '.docx', '.rtf') for d in documents)
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


def error_message(exc):
    if isinstance(exc, ValueError):
        return sanitize(exc)
    if isinstance(exc, OSError):
        return failure(exc)
    text = sanitize(exc)
    detail = f': {text}' if text else ''
    return f'{type(exc).__name__}{detail}'


class App:
    def __init__(self, window):
        install_edit_shortcuts(window)
        self.window, self.session = window, None
        self.error_log_path = setup_error_logging()
        window.report_callback_exception = self.report_callback_exception
        self.events = queue.Queue()
        self.busy, self.operation = False, ''
        self.view_docs = []
        self.last_tick = 0
        self.sort_column, self.sort_reverse = 'number', False
        self.sort_orders = []
        self.similarity_ranks = {}
        self.similarity_groups = {}
        self.column_filters = {}
        self.similarity_notes = {}
        self.similarity_identities = {}
        self.origin_colors_enabled = tk.BooleanVar(value=False)
        self.origin_color_tags = set()
        self.similarity_threshold = tk.DoubleVar(value=.65)
        self.similarity_threshold_text = tk.StringVar(value='Совпадение: 0.65')
        self.group_tags = set()
        self.preview_visible = tk.BooleanVar(value=True)
        self.preview_after = None
        self.preview_cancel = None
        self.preview_token = 0
        self.preview_key = None
        self.preview_image_original = None
        self.preview_photo = None
        self.preview_fit_after = None
        self.preview_cache = {}
        self.preview_cache_order = []
        self.preview_requests = queue.Queue()
        self.preview_thread = None
        self.preview_shutdown = threading.Event()
        self.preview_closed = False
        self.explorer_pending = False
        self.ui_error_signatures = set()
        self.search = tk.StringVar()
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
                          ('Статистика', self.statistics_menu), ('Пререндер', self.prerender),
                          ('Обновить', self.scan)]:
            b = ttk.Button(actions, text=label, command=fn)
            b.pack(side='left', padx=(0, 6))
            self.buttons.append(b)
            if label == 'Статистика':
                self.statistics_button = b
        self.stop_button = ttk.Button(actions, text='Стоп', command=self.stop)
        self.stop_button.pack(side='left', padx=(0, 6))
        self.preview_button = ttk.Button(actions, text='Скрыть preview', command=self.toggle_preview)
        self.preview_button.pack(side='left', padx=(8, 0))
        filters = ttk.Frame(window, padding=(10, 5))
        filters.grid(row=2, column=0, sticky='ew')
        filters.columnconfigure(1, weight=1)
        self.reset_filters_button = ttk.Button(filters, text='Сбросить фильтры',
                                               command=self.reset_filters_and_sorting)
        self.reset_filters_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(filters, text='Файл / каталог:').grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(filters, textvariable=self.search).grid(row=0, column=1, sticky='ew')
        ttk.Style(window).configure('Toolbutton', anchor='center')
        self.origin_color_button = ttk.Checkbutton(
            filters, text='🎨', width=3, variable=self.origin_colors_enabled,
            command=self.render, style='Toolbutton')
        self.origin_color_button.grid(row=0, column=3, padx=(8, 0))
        self.buttons.append(self.origin_color_button)
        for column, label, fn in [(4, 'По похожести', self.sort_similar),
                                  (6, 'Убрать префикс', self.trim_names),
                                  (7, 'Добавить префикс', self.add_names_prefix)]:
            button = ttk.Button(filters, text=label, command=fn)
            button.grid(row=0, column=column, padx=(8, 0))
            self.buttons.append(button)
            if column == 4:
                self.similarity_button = button
                button.bind('<Button-3>', self.similarity_menu)
            if column in (6, 7):
                self.mutation_buttons.append(button)
        granularity = ttk.Frame(filters)
        granularity.grid(row=0, column=5, padx=(10, 0))
        ttk.Label(granularity, textvariable=self.similarity_threshold_text, width=17).pack(side='left')
        ttk.Label(granularity, text='0.60').pack(side='left')
        self.similarity_scale = ttk.Scale(
            granularity, from_=.60, to=.95, length=120, variable=self.similarity_threshold,
            command=self.similarity_threshold_changed)
        self.similarity_scale.pack(side='left')
        ttk.Label(granularity, text='0.95').pack(side='left', padx=(3, 0))
        self.similarity_scale.bind('<ButtonRelease-1>', self.commit_similarity_threshold)
        self.similarity_scale.bind('<KeyRelease>', self.commit_similarity_threshold)
        self.content_area = ttk.Frame(window)
        self.content_area.grid(row=3, column=0, sticky='nsew', padx=10)
        self.content_area.columnconfigure(0, weight=1, minsize=320)
        self.content_area.columnconfigure(1, weight=0, minsize=4)
        self.content_area.columnconfigure(2, weight=0, minsize=320)
        self.content_area.rowconfigure(0, weight=1)
        self.preview_width = 320
        listing = ttk.Frame(self.content_area)
        listing.grid(row=0, column=0, sticky='nsew')
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.columns = ('number', 'name', 'folder', 'origin', 'type', 'size', 'duplicate', 'pages', 'figures', 'tables')
        self.tree = ttk.Treeview(listing, columns=self.columns, show='headings', selectmode='extended')
        titles = ['№', 'Файл', 'Текущая папка', 'Исходный путь / варианты', 'Тип', 'Байт', 'Дубль', 'Стр.', 'Рис.', 'Табл.']
        self.titles = dict(zip(self.columns, titles))
        for column, title, width in zip(self.columns, titles, [50, 220, 110, 240, 55, 90, 110, 65, 60, 60]):
            self.tree.heading(column, text=title + ' ▾', command=lambda c=column: self.header_menu(c))
            self.tree.column(column, width=width, minwidth=40, stretch=False,
                             anchor='e' if column in ('number', 'size', 'pages', 'figures', 'tables') else 'w')
        self.tree.tag_configure('error', foreground='#9c3a16')
        ybar = ttk.Scrollbar(listing, command=self.tree.yview)
        xbar = ttk.Scrollbar(listing, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        ybar.grid(row=0, column=1, sticky='ns')
        xbar.grid(row=1, column=0, sticky='ew')
        self.build_preview_panel()
        self.progress = ttk.Progressbar(window, mode='indeterminate')
        self.progress.grid(row=4, column=0, sticky='ew', padx=10, pady=(5, 0))
        self.labels = []
        for row, variable in [(5, self.status), (6, self.details), (7, self.totals)]:
            label = ttk.Label(window, textvariable=variable, padding=(10, 4), wraplength=1180)
            if row == 7:
                label.configure(font=('Segoe UI', 9), foreground='#555555')
            label.grid(row=row, column=0, sticky='ew')
            self.labels.append(label)
        cache_bar = ttk.Frame(window)
        cache_bar.grid(row=8, column=0, sticky='e', padx=10, pady=(0, 5))
        self.cache_size_label = tk.StringVar(value='Кэш preview: …')
        ttk.Label(cache_bar, textvariable=self.cache_size_label).pack(side='left', padx=5)
        self.cache_clear_button = ttk.Button(cache_bar, text='🗑', width=3, command=self.clear_preview_cache)
        self.cache_clear_button.pack(side='left')
        self.cache_scan_running = False
        self.cache_clearing = False
        window.after(500, self.refresh_cache_size)
        def resize(event):
            if event.widget == window:
                for label in self.labels:
                    label.configure(wraplength=max(600, window.winfo_width()-30))
        window.bind('<Configure>', resize)
        self.context = tk.Menu(window, tearoff=False)
        self.context.add_command(label='Показать в Проводнике', command=self.show_in_explorer)
        self.context.add_command(label='Скопировать название', command=self.copy_name)
        self.context.add_command(label='Копировать пути', command=self.copy_path)
        self.context.add_command(label='Переименовать…', command=self.rename_selected)
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
        window.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh_controls()
        window.after(100, self.poll)

    def refresh_cache_size(self):
        if self.preview_closed:
            return
        if not self.cache_scan_running and not self.cache_clearing:
            self.cache_scan_running = True
            def measure():
                from .preview import image_cache_size
                try:
                    size = image_cache_size()
                except OSError:
                    size = None
                self.events.put(('cache_size', size))
            threading.Thread(target=measure, daemon=True).start()
        self.window.after(10000, self.refresh_cache_size)

    def clear_preview_cache(self):
        if self.busy or self.cache_clearing:
            return
        if not messagebox.askyesno('Очистить кэш preview?',
                'Удалить все сохранённые preview? Документы останутся на месте. '
                'При следующем просмотре картинки будут созданы заново.'):
            return
        self.cache_clearing = True
        self.cache_clear_button.configure(state='disabled')
        self.cancel_preview()
        self.preview_requests.put(None)
        thread = self.preview_thread
        self.preview_cache.clear()
        self.preview_cache_order.clear()
        self.preview_key = None
        self.preview_placeholder('Кэш очищается…')
        def clear():
            from .preview import clear_image_cache, image_cache_size
            if thread and thread.is_alive():
                thread.join(timeout=10)
                if thread.is_alive():
                    raise RuntimeError('Обработчик preview ещё завершается. Повторите очистку.')
            return clear_image_cache(), image_cache_size()
        def done(result):
            failed, size = result
            self.cache_size_label.set(f'Кэш preview: {size / (1024 * 1024):.1f} МиБ')
            self.status.set('Кэш preview очищен.' if not failed else
                            f'Не удалось удалить файлов кэша: {failed}. Повторите очистку.')
            self.preview_placeholder('Выберите файл для preview.')
        self.run('Очистка кэша preview', clear, done)

    def report_callback_exception(self, exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            raise exc
        self.recover_ui_error(exc, context='tk-callback', tb=tb)

    def recover_ui_error(self, exc, context='ui', tb=None):
        log_exception('UI callback failed', exc, tb, context=context, operation=getattr(self, 'operation', ''))
        message = error_message(exc)
        try:
            self.busy = False
            self.progress.stop()
        except Exception:
            pass
        try:
            self.refresh_controls()
        except Exception:
            pass
        try:
            self.status.set(f'Ошибка интерфейса: {message}')
        except Exception:
            pass
        if isinstance(exc, tk.TclError):
            return
        signature = (context, type(exc).__name__, message)
        signatures = getattr(self, 'ui_error_signatures', None)
        if signatures is None:
            signatures = self.ui_error_signatures = set()
        if signature not in signatures:
            signatures.add(signature)
            try:
                messagebox.showerror('Ошибка интерфейса', self.with_log_path(message))
            except Exception:
                pass

    def with_log_path(self, message):
        path = getattr(self, 'error_log_path', None)
        if path is None:
            path = setup_error_logging()
            self.error_log_path = path
        return f'{message}\n\nЖурнал ошибок: {path}'

    def build_preview_panel(self):
        self.preview_separator = tk.Frame(self.content_area, width=4, cursor='sb_h_double_arrow')
        self.preview_separator.grid(row=0, column=1, sticky='ns')
        self.preview_separator.grid_propagate(False)
        self.preview_separator_line = tk.Frame(self.preview_separator, width=2, background='#111111')
        self.preview_separator_line.pack(side='left', fill='y', padx=(1, 1))
        self.preview_separator.bind('<ButtonPress-1>', self.start_preview_resize)
        self.preview_separator.bind('<B1-Motion>', self.drag_preview_resize)
        self.preview_separator.bind('<ButtonRelease-1>', self.end_preview_resize)
        self.preview_separator_line.bind('<ButtonPress-1>', self.start_preview_resize)
        self.preview_separator_line.bind('<B1-Motion>', self.drag_preview_resize)
        self.preview_separator_line.bind('<ButtonRelease-1>', self.end_preview_resize)

        self.preview_shell = ttk.Frame(self.content_area, padding=(8, 0, 0, 0), width=self.preview_width)
        self.preview_shell.grid(row=0, column=2, sticky='nsew')
        self.preview_shell.grid_propagate(False)
        self.preview_shell.columnconfigure(0, weight=1)
        self.preview_shell.rowconfigure(0, weight=1)
        self.preview_title = tk.StringVar(value='Preview')
        self.preview_image_frame = ttk.Frame(self.preview_shell)
        self.preview_image_frame.columnconfigure(0, weight=1)
        self.preview_image_frame.rowconfigure(0, weight=1)
        self.preview_canvas = tk.Canvas(
            self.preview_image_frame, background='#f7f7f7', highlightthickness=1,
            highlightbackground='#d0d0d0')
        self.preview_canvas.grid(row=0, column=0, sticky='nsew')
        self.preview_canvas.bind('<Configure>', lambda _: self.schedule_fit_preview_image())
        self.preview_text_frame = ttk.Frame(self.preview_shell)
        self.preview_text_frame.columnconfigure(0, weight=1)
        self.preview_text_frame.rowconfigure(0, weight=1)
        self.preview_text = tk.Text(self.preview_text_frame, wrap='word', height=8, padx=8, pady=8,
                                    relief='solid', borderwidth=1, font=('Segoe UI', 9))
        text_bar = ttk.Scrollbar(self.preview_text_frame, command=self.preview_text.yview)
        self.preview_text.configure(yscrollcommand=text_bar.set, state='disabled')
        self.preview_text.grid(row=0, column=0, sticky='nsew')
        text_bar.grid(row=0, column=1, sticky='ns')
        self.preview_image_frame.grid(row=0, column=0, sticky='nsew')
        self.preview_text_frame.grid(row=0, column=0, sticky='nsew')
        ttk.Label(self.preview_shell, textvariable=self.preview_title, padding=(0, 5, 0, 0)).grid(
            row=1, column=0, sticky='ew')
        self.preview_placeholder('Выберите один файл в списке.')
        self.set_preview_width(self.preview_width)

    def set_preview_width(self, width):
        self.preview_width = max(220, int(width))
        self.content_area.columnconfigure(2, minsize=self.preview_width)
        self.preview_shell.configure(width=self.preview_width)

    def start_preview_resize(self, event):
        self.resize_start_x = event.x_root
        self.resize_start_width = self.preview_width
        return 'break'

    def drag_preview_resize(self, event):
        total = max(0, self.content_area.winfo_width())
        max_width = max(220, total - 320 - 4)
        width = self.resize_start_width - (event.x_root - self.resize_start_x)
        self.set_preview_width(min(max_width, max(220, width)))
        return 'break'

    def end_preview_resize(self, _event):
        self.resize_start_x = None
        self.resize_start_width = self.preview_width
        return 'break'

    def toggle_preview(self):
        if self.preview_visible.get():
            self.preview_visible.set(False)
            self.preview_button.configure(text='Показать preview')
            self.preview_separator.grid_remove()
            self.preview_shell.grid_remove()
            self.content_area.columnconfigure(1, minsize=0)
            self.content_area.columnconfigure(2, minsize=0)
            self.cancel_preview()
            return
        self.preview_visible.set(True)
        self.preview_button.configure(text='Скрыть preview')
        self.content_area.columnconfigure(1, minsize=4)
        self.preview_separator.grid()
        self.preview_shell.grid()
        self.set_preview_width(self.preview_width)
        self.select()

    def cancel_preview(self):
        if self.preview_after is not None:
            self.window.after_cancel(self.preview_after)
            self.preview_after = None
        if self.preview_fit_after is not None:
            self.window.after_cancel(self.preview_fit_after)
            self.preview_fit_after = None
        if self.preview_cancel is not None:
            self.preview_cancel.set()
            self.preview_cancel = None
        self.preview_key = None

    def preview_placeholder(self, text, title='Preview'):
        self.preview_title.set(title)
        self.preview_image_original = None
        self.preview_photo = None
        self.preview_canvas.delete('all')
        self.preview_image_frame.grid_remove()
        self.preview_text_frame.grid()
        self.preview_text.configure(state='normal')
        self.preview_text.delete('1.0', 'end')
        self.preview_text.insert('1.0', text)
        self.preview_text.configure(state='disabled')

    def show_preview_text(self, title, text):
        self.preview_title.set(title or 'Preview')
        self.preview_image_original = None
        self.preview_photo = None
        self.preview_canvas.delete('all')
        self.preview_image_frame.grid_remove()
        self.preview_text_frame.grid()
        self.preview_text.configure(state='normal')
        self.preview_text.delete('1.0', 'end')
        self.preview_text.insert('1.0', text)
        self.preview_text.configure(state='disabled')

    def show_preview_image(self, title, data):
        try:
            from PIL import Image
            self.preview_image_original = Image.open(BytesIO(data)).copy()
        except Exception:
            self.show_preview_text('Preview', 'Preview-картинка недоступна.')
            return
        self.preview_title.set(title or 'Preview')
        self.preview_text_frame.grid_remove()
        self.preview_image_frame.grid()
        self.schedule_fit_preview_image(immediate=True)

    def schedule_fit_preview_image(self, immediate=False):
        if self.preview_image_original is None or not self.preview_visible.get():
            return
        if self.preview_fit_after is not None:
            self.window.after_cancel(self.preview_fit_after)
            self.preview_fit_after = None
        if immediate:
            self.fit_preview_image()
            return
        self.preview_fit_after = self.window.after(40, self.fit_preview_image)

    def fit_preview_image(self):
        self.preview_fit_after = None
        if self.preview_image_original is None or not self.preview_visible.get():
            return
        try:
            from PIL import Image, ImageTk
            if not self.preview_canvas.winfo_exists():
                return
            width = max(20, self.preview_canvas.winfo_width() - 12)
            height = max(20, self.preview_canvas.winfo_height() - 12)
            image = self.preview_image_original.copy()
            image.thumbnail((width, height), Image.Resampling.LANCZOS)
            self.preview_photo = ImageTk.PhotoImage(image, master=self.window)
            self.preview_canvas.delete('all')
            self.preview_canvas.create_image(
                max(6, self.preview_canvas.winfo_width() // 2),
                max(6, self.preview_canvas.winfo_height() // 2),
                image=self.preview_photo, anchor='center')
        except Exception:
            self.show_preview_text('Preview', 'Preview-картинка недоступна.')

    def document_preview_key(self, document):
        identity = document.get('identity') or []
        root = str(self.session.root) if self.session else ''
        return root, document['path'], tuple(identity)

    def cache_preview(self, key, result):
        if key in self.preview_cache_order:
            self.preview_cache_order.remove(key)
        self.preview_cache_order.append(key)
        self.preview_cache[key] = result
        while sum(
                len(item.get('data', b'')) + len(item.get('text', '')) * 4
                for item in self.preview_cache.values()) > 32 * 1024 * 1024:
            self.preview_cache.pop(self.preview_cache_order.pop(0), None)

    def schedule_preview(self, document):
        if getattr(self, 'cache_clearing', False):
            return
        if not self.preview_visible.get() or not self.session:
            return
        key = self.document_preview_key(document)
        if key == self.preview_key:
            return
        if key in self.preview_cache:
            self.cancel_preview()
            self.release_preview_renderer()
            self.preview_key = key
            self.cache_preview(key, self.preview_cache[key])
            self.apply_preview_result(key, self.preview_cache[key])
            return
        self.cancel_preview()
        self.preview_key = key
        self.preview_placeholder('Готовим preview…', 'Preview')
        self.preview_after = self.window.after(40, lambda d=dict(document), k=key: self.start_preview(d, k))

    def start_preview(self, document, key):
        self.preview_after = None
        if not self.preview_visible.get() or key != self.preview_key or not self.session:
            return
        self.preview_token += 1
        token = self.preview_token
        cancel = threading.Event()
        self.preview_cancel = cancel
        try:
            path = self.session.checked_document(document)
        except (OSError, ValueError) as exc:
            self.events.put(('preview', token, key, {'kind': 'message', 'title': 'Preview', 'text': str(exc)}))
            return

        try:
            from .preview import cached_preview_result
            cached = cached_preview_result(path)
        except Exception:
            cached = None
        if cached:
            self.release_preview_renderer()
            self.events.put(('preview', token, key, cached))
            return

        self.ensure_preview_thread()
        self.preview_requests.put((token, key, path, cancel))

    def release_preview_renderer(self):
        if self.preview_thread and self.preview_thread.is_alive():
            self.preview_requests.put('release')

    def ensure_preview_thread(self):
        if self.preview_closed:
            return
        if self.preview_thread and self.preview_thread.is_alive():
            return
        self.preview_thread = threading.Thread(target=self.preview_loop, daemon=True)
        self.preview_thread.start()

    def preview_loop(self):
        from .core import Cancelled
        from .preview import preview_worker
        from .stats_worker import StatsWorker
        while not self.preview_closed:
            with StatsWorker(timeout=PREVIEW_TIMEOUT, target=preview_worker, retry_label='Preview') as preview:
                handled = 0
                while handled < PREVIEW_WORKER_RECYCLE_AFTER:
                    try:
                        request = self.preview_requests.get(timeout=15)
                    except queue.Empty:
                        preview.close()
                        continue
                    if request is None:
                        return
                    while True:
                        try:
                            newer = self.preview_requests.get_nowait()
                        except queue.Empty:
                            break
                        if newer is None:
                            request = None
                            break
                        request = newer
                    if request is None:
                        return
                    if request == 'release':
                        preview.close()
                        continue
                    token, key, path, cancel = request
                    if cancel.is_set() or self.preview_closed:
                        continue
                    try:
                        result = preview.count(path, cancel)
                        handled += 1
                    except Cancelled:
                        continue
                    except Exception:
                        if cancel.is_set():
                            continue
                        result = {'kind': 'message', 'title': 'Preview',
                                  'text': 'Preview недоступен: файл повреждён, защищён или занят.'}
                        handled += 1
                    if not cancel.is_set() and not self.preview_closed:
                        self.events.put(('preview', token, key, result))

    def apply_preview_result(self, key, result):
        if not self.preview_visible.get() or key != self.preview_key:
            return
        if result.get('kind') == 'image':
            self.show_preview_image(result.get('title', 'Preview'), result.get('data', b''))
        else:
            self.show_preview_text(result.get('title', 'Preview'), result.get('text', 'Preview недоступен.'))

    def finish_preview(self, token, key, result):
        if token != self.preview_token or key != self.preview_key:
            return
        self.preview_cancel = None
        if result.get('failed'):
            result = {'kind': 'message', 'title': 'Preview',
                      'text': result.get('info', 'Preview недоступен.')}
        self.cache_preview(key, result)
        self.apply_preview_result(key, result)

    def refresh_controls(self):
        for index, b in enumerate(self.buttons):
            disabled = self.busy or (index > 0 and not self.session)
            if b in self.mutation_buttons and self.session and self.session.read_only:
                disabled = True
            b.configure(state='disabled' if disabled else 'normal')
        self.backup_check.configure(state='disabled' if self.busy else 'normal')
        self.similarity_scale.configure(state='disabled' if self.busy or not self.session else 'normal')
        self.stop_button.configure(state='normal' if self.busy else 'disabled')

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
                log_exception('Background operation failed', exc, operation=label)
                self.events.put(('error', error_message(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def progress_detail(self, current):
        if not current:
            return ''
        path = Path(str(current))
        name = path.name or str(current)
        if len(name) > 86:
            name = name[:41].rstrip() + '…' + name[-41:].lstrip()
        file_type = path.suffix.upper().lstrip('.') or 'без расширения'
        return f' · {name} · {file_type}'

    def progress_status(self, count, current=None):
        return f'{self.operation}… обработано {count}{self.progress_detail(current)}'

    def tick(self, count, current=None, force=False):
        now = time.monotonic()
        if force or (count == 0 and current is None) or now - self.last_tick > 1:
            self.last_tick = now
            if count == 0 and current is None:
                session = self.session
                self.events.put(('snapshot', count, copy.deepcopy(session.state['documents'] if session else [])))
            elif current is None:
                self.events.put(('progress', count))
            else:
                self.events.put(('progress', count, str(current)))

    def stop(self):
        if self.busy and self.session:
            self.session.cancel.set()
            self.status.set('Остановка: завершаем текущий безопасный шаг и сохраняем прогресс…')

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                try:
                    if event[0] == 'cache_budget':
                        self.show_cache_budget(event)
                        continue
                    if event[0] == 'cache_size':
                        self.cache_scan_running = False
                        if not self.cache_clearing:
                            self.cache_size_label.set('Кэш preview: недоступен' if event[1] is None else
                                                      f'Кэш preview: {event[1] / (1024 * 1024):.1f} МиБ')
                        continue
                    if event[0] == 'progress':
                        session = self.session
                        if session and not session.cancel.is_set():
                            self.status.set(self.progress_status(event[1], event[2] if len(event) > 2 else None))
                        continue
                    if event[0] == 'snapshot':
                        self.view_docs = event[2]
                        session = self.session
                        if session and not session.cancel.is_set():
                            self.status.set(self.progress_status(event[1]))
                        self.render()
                        continue
                    if event[0] == 'preview':
                        self.finish_preview(event[1], event[2], event[3])
                        continue
                    if event[0] == 'explorer_done':
                        self.explorer_pending = False
                        if event[1]:
                            messagebox.showerror('Показать в Проводнике', event[1])
                        continue
                    if getattr(self, 'cache_clearing', False):
                        self.cache_clearing = False
                        self.cache_clear_button.configure(state='normal')
                        self.preview_requests = queue.Queue()
                    self.busy = False
                    self.progress.stop()
                    self.refresh_controls()
                    self.refreshed()
                    if event[0] == 'error':
                        messagebox.showerror('Операция остановлена', self.with_log_path(event[1]))
                    elif event[1]:
                        event[1](event[2])
                except Exception as exc:
                    log_exception(
                        'UI event handling failed', exc,
                        event=event[0], operation=getattr(self, 'operation', ''))
                    try:
                        self.status.set(f'Ошибка интерфейса: {error_message(exc)}')
                    except Exception:
                        pass
                    if event[0] not in ('preview', 'progress'):
                        self.busy = False
                        try:
                            self.progress.stop()
                            self.refresh_controls()
                        except Exception:
                            pass
        except queue.Empty:
            pass
        if not self.preview_closed:
            try:
                self.window.after(100, self.poll)
            except tk.TclError:
                self.preview_closed = True

    def open_folder(self):
        try:
            self._open_folder()
        except Exception as exc:
            self.recover_ui_error(exc, context='open-folder')

    def _open_folder(self):
        if self.busy:
            return
        chosen = self.choose_directory()
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
            if not isinstance(exc, ValueError):
                log_exception('Opening catalog failed', exc, operation='Открыть каталог')
            messagebox.showerror('Каталог', error_message(exc))
            return
        if self.session:
            self.session.close()
        self.session = session
        self.cancel_preview()
        self.preview_cache.clear()
        self.preview_cache_order.clear()
        if self.preview_visible.get():
            self.preview_placeholder('Выберите один файл в списке.')
        self.similarity_notes = {}
        self.column_filters = {}
        self.similarity_groups = {}
        self.sort_orders = []
        self.similarity_ranks = {}
        self.similarity_button.configure(text='По похожести')
        self.sort_column, self.sort_reverse = 'number', False
        self.view_docs = []
        self.window.title(f"CorpusPick — {'БЭКАП, БЕЗ ИЗМЕНЕНИЙ — ' if session.read_only else ''}{chosen}")
        self.scan()

    def choose_directory(self):
        title = 'Выберите каталог (режим бэкапа задаётся галочкой в главном окне)'
        try:
            return filedialog.askdirectory(parent=self.window, title=title)
        except tk.TclError as exc:
            log_exception('Native folder dialog failed', exc, operation='Открыть каталог')
            try:
                self.status.set(
                    'Системный диалог выбора папки недоступен: '
                    f'{error_message(exc)}. Введите путь вручную.')
            except Exception:
                pass
            try:
                return simpledialog.askstring(
                    'Открыть каталог',
                    'Системный диалог выбора папки не ответил.\nВведите полный путь к каталогу:',
                    parent=self.window)
            except tk.TclError as fallback_exc:
                log_exception('Manual folder path dialog failed', fallback_exc, operation='Открыть каталог')
                try:
                    self.status.set(f'Открыть каталог не удалось: {error_message(fallback_exc)}')
                except Exception:
                    pass
                return None

    def scan(self):
        if self.session and not self.busy:
            grouped = bool(self.similarity_groups)
            threshold = self.similarity_distance()
            def update():
                self.session.open_catalog(self.tick)
                if grouped and not self.session.cancel.is_set():
                    return self.session.group_similar(self.tick, threshold, rescan=False)
            self.run('Обновление списка и групп' if grouped else 'Обновление списка', update,
                     self.apply_similarity_result if grouped else None)

    def refreshed(self):
        if not self.session:
            return
        self.view_docs = copy.deepcopy(self.session.state['documents'])
        current = {d['path']: d.get('identity') for d in self.view_docs}
        if self.similarity_groups and current != self.similarity_identities:
            old_groups, old_ranks, old_notes = self.similarity_groups, self.similarity_ranks, self.similarity_notes
            by_identity = {}
            for path, identity in self.similarity_identities.items():
                if path not in current and identity and len(identity) > 1 and identity[1]:
                    by_identity.setdefault(tuple(identity), []).append(path)
            groups, ranks, notes = {}, {}, {}
            next_group = max(old_groups.values(), default=-1) + 1
            affected = {g for p, g in old_groups.items() if p not in current}
            for path, identity in current.items():
                previous = path if path in old_groups else None
                if previous is None and identity:
                    candidates = by_identity.get(tuple(identity), [])
                    if len(candidates) == 1:
                        previous = candidates[0]
                if previous is not None:
                    groups[path] = old_groups[previous]
                    ranks[path] = old_ranks.get(previous, len(old_ranks))
                    stale = (previous != path or self.similarity_identities.get(previous) != identity or
                             groups[path] in affected)
                    notes[path] = ('Прежняя группа сохранена; для учёта изменений обновите группы (F5).'
                                   if stale else old_notes.get(previous, ''))
                else:
                    groups[path], ranks[path] = next_group, len(old_ranks) + next_group
                    notes[path] = 'Новый файл: похожесть ещё не обновлена (F5).'
                    next_group += 1
            self.similarity_groups, self.similarity_ranks, self.similarity_notes = groups, ranks, notes
            self.similarity_identities = current
            if not current:
                self.similarity_button.configure(text='По похожести')
                if self.sort_column == 'similarity':
                    self.sort_column, self.sort_reverse = 'number', False
        docs = self.view_docs
        pending = sum(not m['done'] for m in self.session.state['moves'])
        self.status.set(f"Файлов: {len(docs)} · SHA-256: {sum(bool(d.get('hash')) for d in docs)}/{len(docs)} · "
                        f"Ошибок: {sum(bool(d.get('error') or d.get('stats', {}).get('failed') or d.get('stats', {}).get('libreoffice_failed') or d.get('similarity', {}).get('embedding_failed') or d.get('content', {}).get('failed') or d.get('content', {}).get('text_failed') or d.get('content', {}).get('embedding_failed') or d.get('content', {}).get('visual_failed')) for d in docs)}" +
                        (' · БЭКАП: файлы не изменяются' if self.session.read_only else ' · Unlock автоматически') +
                        (f' · Не перенесено: {pending}' if pending else '') +
                        (' · Остановлено, прогресс сохранён' if self.session.cancel.is_set() else ''))
        self.render()

    def render(self):
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        indexed = [(i + 1, d) for i, d in enumerate(self.view_docs)]
        def key(pair, c):
            number, d = pair
            if c == 'similarity':
                return self.similarity_ranks.get(d['path'], len(self.similarity_ranks) + number)
            if c == 'number':
                return number
            if c in ('pages', 'figures', 'tables'):
                return d.get('stats', {}).get(c) if d.get('stats', {}).get(c) is not None else -1
            if c in ('size', 'duplicate'):
                return d.get(c) or 0
            if c == 'name':
                return natural_name_key(Path(d['path']).name)
            return {'name': Path(d['path']).name, 'folder': str(Path(d['path']).parent),
                    'origin': ' | '.join(d.get('origins', [d['origin']])),
                    'type': Path(d['path']).suffix}.get(c, d.get(c, '')).casefold()
        orders = self.sort_orders or [(self.sort_column, self.sort_reverse)]
        ordered = indexed
        for column, reverse in reversed(orders):
            ordered = sorted(ordered, key=lambda pair, c=column: key(pair, c), reverse=reverse)
        if self.similarity_groups:
            ordered.sort(key=lambda pair: self.similarity_groups.get(pair[1]['path'], len(self.similarity_groups) + pair[0]))
        for number, d in ordered:
            if not all(matches(column_value(d, c, number), rule) for c, rule in self.column_filters.items()):
                continue
            origins = d.get('origins', [d['origin']])
            if self.search.get().casefold() not in (d['path'] + ' ' + ' '.join(origins)).casefold():
                continue
            stats = d.get('stats', {})
            def metric(name):
                value = stats.get(name)
                return '—' if value is None else ('≈' if stats.get(name + '_estimated') else '') + str(value)
            row_tags = []
            if self.origin_colors_enabled.get():
                source_key = source_directory_key(d)
                source_tag = source_directory_tag(source_key)
                if source_tag not in self.origin_color_tags:
                    self.tree.tag_configure(
                        source_tag, background=source_directory_background(source_key))
                    self.origin_color_tags.add(source_tag)
                row_tags.append(source_tag)
            if self.similarity_groups and d['path'] in self.similarity_groups:
                group = self.similarity_groups[d['path']]
                tag = f'group-{group}'
                if tag not in self.group_tags:
                    red, green, blue = colorsys.hsv_to_rgb((group * .61803398875) % 1, .72, .58)
                    self.tree.tag_configure(
                        tag, foreground=f'#{int(red*255):02x}{int(green*255):02x}{int(blue*255):02x}')
                    self.group_tags.add(tag)
                row_tags.append(tag)
            elif d.get('error') or stats.get('failed') or stats.get('libreoffice_failed'):
                row_tags.append('error')
            self.tree.insert('', 'end', iid=d['path'], values=(number, Path(d['path']).name, str(Path(d['path']).parent),
                ' | '.join(origins), Path(d['path']).suffix.lower() or '—', d.get('size') if d.get('size') is not None else '—',
                f"#{d['duplicate']}" if d.get('duplicate') else ('Не проверен' if not d.get('hash') else '—'), metric('pages'),
                metric('figures'), metric('tables')),
                tags=tuple(row_tags))
        existing = [p for p in selected if self.tree.exists(p)]
        if existing:
            self.tree.selection_set(existing)
        for column, title in self.titles.items():
            direction = ''
            for priority, (active, reverse) in enumerate(orders, 1):
                if column == active:
                    direction = (' ↓' if reverse else ' ↑') + (str(priority) if len(orders) > 1 else '')
                    break
            self.tree.heading(column, text=title + direction + (' ●▾' if column in self.column_filters else ' ▾'))
        self.select()

    def selected_documents(self):
        paths = set(self.tree.selection())
        return [d for d in self.view_docs if d['path'] in paths]

    def visible_documents(self):
        paths = list(self.tree.get_children())
        documents = {d['path']: d for d in self.view_docs}
        return [documents[path] for path in paths if path in documents]

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
            self.schedule_preview(d)
        else:
            self.details.set(f'Выделено: {len(docs)} · Ctrl/Shift — выбор · Ctrl+A — все видимые · Esc — снять выбор / стоп · Delete — корзина')
            if self.preview_visible.get():
                self.cancel_preview()
                self.preview_placeholder('Выберите один файл в списке.')

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
        if not messagebox.askyesno('Кэш похожести', 'Удалить OCR/NER/layout embeddings для текущего каталога?\nИсходные пути, CSV, SHA точных дублей, прежние отпечатки содержимого и статистика сохранятся.\nСледующая группировка заново обработает до трёх страниц документа.'):
            return
        def done(_):
            self.similarity_notes, self.similarity_groups, self.similarity_ranks = {}, {}, {}
            self.similarity_button.configure(text='По похожести')
            self.sort_column, self.sort_reverse = 'number', False
            self.refreshed()
            self.status.set('Кэш похожести текущего каталога сброшен. Исходная структура сохранена.')
        self.run('Сброс кэша похожести', self.session.clear_similarity_cache, done)

    def similarity_threshold_changed(self, value):
        """Dragging only updates the label; release commits the chosen threshold."""
        self.similarity_threshold_text.set(f'Совпадение: {float(value):.2f}')

    def similarity_distance(self):
        return 1 - round(self.similarity_threshold.get(), 2)

    def commit_similarity_threshold(self, event=None):
        if event is not None and getattr(event, 'keysym', '') not in (
                '', '??', 'Left', 'Right', 'Up', 'Down', 'Home', 'End', 'Prior', 'Next'):
            return
        self.regroup_similar()

    def apply_similarity_result(self, ranks):
        if ranks is None:
            return
        new_ranks, new_groups, new_notes = ranks
        if self.similarity_groups:
            from collections import Counter
            overlaps = Counter((group, self.similarity_groups[path]) for path, group in new_groups.items()
                               if path in self.similarity_groups)
            remap, used = {}, set()
            for (new, old), _ in sorted(overlaps.items(), key=lambda item: (-item[1], item[0])):
                if new not in remap and old not in used:
                    remap[new] = old
                    used.add(old)
            next_group = max(self.similarity_groups.values(), default=-1) + 1
            for group in sorted(set(new_groups.values())):
                if group not in remap:
                    remap[group] = next_group
                    next_group += 1
            new_groups = {path: remap[group] for path, group in new_groups.items()}
        self.similarity_ranks, self.similarity_groups, self.similarity_notes = new_ranks, new_groups, new_notes
        self.similarity_identities = {d['path']: d.get('identity') for d in self.view_docs}
        self.similarity_button.configure(text='Снять группировку')
        self.sort_column, self.sort_reverse = 'similarity', False
        self.render()
        unavailable = sum(bool(d.get('similarity', {}).get('embedding_failed') or
                               d.get('similarity', {}).get('metadata_refresh_failed') or
                               d.get('similarity', {}).get('ocr_failed') or
                               d.get('similarity', {}).get('text_failed')) for d in self.view_docs)
        grouped_paths = [path for path, note in self.similarity_notes.items()
                         if note.startswith('Группа по embeddings')]
        grouped_text = (f"Групп: {len({self.similarity_groups[path] for path in grouped_paths})} · "
                        f"В группах: {len(grouped_paths)} · " if grouped_paths else '')
        gpu_ocr = sum(d.get('similarity', {}).get('ocr_provider') in
                      ('DmlExecutionProvider', 'CUDAExecutionProvider') and
                      bool(d.get('similarity', {}).get('ocr_pages')) for d in self.view_docs)
        gpu_text = f'GPU OCR: {gpu_ocr} · ' if gpu_ocr else ''
        self.status.set(f'{grouped_text}E5 + локальный NER; PDF: первая/средняя/последняя страницы и макет · '
                        f'{gpu_text}Порог совпадения: {self.similarity_threshold.get():.2f} · '
                        f'Ошибок OCR/embeddings: {unavailable}. '
                        'Цвет текста показывает группу. Основание — под выбранным файлом.')

    def regroup_similar(self):
        if self.busy or not self.session or not self.similarity_groups:
            return
        threshold = self.similarity_distance()
        self.run('Перестройка групп',
                 lambda: self.session.regroup_similar(threshold, self.tick),
                 self.apply_similarity_result)

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
        from .embeddings import model_ready
        from .ocr import ocr_model_ready
        needs_ocr = any(Path(document['path']).suffix.lower() == '.pdf' for document in self.view_docs)
        missing = not model_ready(self.session.data_root) or (needs_ocr and not ocr_model_ready(self.session.data_root))
        if missing and not messagebox.askyesno(
                'Локальные модели OCR и embeddings',
                'Для группировки один раз скачать недостающие локальные модели '
                'multilingual-e5-small и Cyrillic PP-OCRv5 (до 143 МБ)?\n\nДокументы никуда '
                'не отправляются. После загрузки модели работают локально и офлайн.', parent=self.window):
            return
        threshold = self.similarity_distance()
        self.run('OCR, NER и embeddings',
                 lambda: self.session.group_similar(self.tick, threshold),
                 self.apply_similarity_result)

    def trim_names(self):
        docs = copy.deepcopy(self.selected_documents())
        if self.busy or not docs or self.session.read_only:
            return
        count = simpledialog.askinteger('Убрать префикс', 'Сколько символов убрать?',
                                        parent=self.window, initialvalue=1, minvalue=1)
        if count is not None:
            self.run('Переименование', lambda: self.session.trim_prefix(docs, count, self.tick),
                     self.names_changed)

    def add_names_prefix(self):
        docs = copy.deepcopy(self.selected_documents())
        if self.busy or not docs or not self.session or self.session.read_only:
            return
        from .name_dialog import NameAdditionDialog
        order = {path: index for index, path in enumerate(self.tree.get_children())}
        docs.sort(key=lambda document: order.get(document['path'], len(order)))
        dialog = NameAdditionDialog(self.window, self.session.root, [d['path'] for d in docs])
        if dialog.result is not None:
            options = dialog.result
            self.run('Добавление к названию', lambda: self.session.add_name_parts(docs, **options, progress=self.tick),
                     self.names_changed)

    def names_changed(self, result):
        summary = f"Переименовано: {result['moved']}. Исходные пути сохранены."
        self.status.set(summary)
        if result['errors']:
            self.result('Ошибки переименования', summary, result['errors'])
        if result['moved'] and self.similarity_groups and not self.session.cancel.is_set():
            threshold = self.similarity_distance()
            self.run('Обновление названий и групп',
                     lambda: self.session.group_similar(self.tick, threshold, rescan=False),
                     self.apply_similarity_result)

    def rename_selected(self):
        if self.busy or not self.session or self.session.read_only:
            return
        docs = copy.deepcopy(self.selected_documents())
        if not docs:
            return
        order = {path: index for index, path in enumerate(self.tree.get_children())}
        docs.sort(key=lambda document: order.get(document['path'], len(order)))
        multiple = len(docs) > 1
        prompt = (f'Общее название для {len(docs)} файлов, без расширения.\n'
                  'Свободные номера: #01, #02, …; расширения сохранятся.\n'
                  'При росте серии ведущие нули обновятся и у прежних файлов.'
                  if multiple else 'Новое название без расширения (расширение сохранится):')
        name = ask_editable_string('Переименовать', prompt, parent=self.window,
                                      initialvalue='' if multiple else Path(docs[0]['path']).stem)
        if name is not None:
            self.run('Переименование', lambda: self.session.rename_documents(docs, name, self.tick),
                     self.names_changed)

    def clear_selection(self):
        self.tree.selection_remove(self.tree.selection())

    def sort(self, column, reverse=None):
        previous = dict(self.sort_orders).get(column)
        if reverse is None:
            reverse = not previous if previous is not None else False
        if previous is None:
            self.sort_orders.append((column, reverse))
        else:
            self.sort_orders = [(c, reverse if c == column else r) for c, r in self.sort_orders]
        self.sort_column, self.sort_reverse = self.sort_orders[0]
        self.render()

    def reset_filters_and_sorting(self):
        self.sort_orders = []
        self.sort_column, self.sort_reverse = ('similarity' if self.similarity_groups else 'number'), False
        self.clear_filter()

    def header_menu(self, column):
        menu = tk.Menu(self.window, tearoff=False)
        def order(reverse):
            self.sort(column, reverse)
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
            self.context.entryconfigure('Переименовать…',
                                        state='disabled' if self.session.read_only else 'normal')
            self.context.tk_popup(event.x_root, event.y_root)

    def show_in_explorer(self):
        docs = self.selected_documents()
        if docs and not self.busy and os.name == 'nt' and not self.explorer_pending:
            try:
                paths = [self.session.safe_path(d['path']) for d in docs]
            except (OSError, ValueError) as exc:
                messagebox.showerror('Показать в Проводнике', str(exc))
                return
            self.explorer_pending = True
            self.status.set('Открываем Проводник…')

            def worker():
                try:
                    show_files(paths)
                    self.events.put(('explorer_done', None))
                except (OSError, ValueError) as exc:
                    log_exception('Explorer operation failed', exc, operation='Показать в Проводнике')
                    self.events.put(('explorer_done', str(exc)))
                except Exception as exc:
                    log_exception('Explorer operation crashed', exc, operation='Показать в Проводнике')
                    self.events.put(('explorer_done', 'Проводник не ответил. Попробуйте ещё раз после обновления списка.'))

            threading.Thread(target=worker, daemon=True).start()

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

    def select_next_after_trash(self, previous_order, removed_paths):
        removed_indexes = [index for index, path in enumerate(previous_order) if path in removed_paths]
        if not removed_indexes:
            return
        current_paths = set(self.tree.get_children())
        candidate = next((path for path in previous_order[max(removed_indexes) + 1:]
                          if path in current_paths), None)
        if candidate is None:
            candidate = next((path for path in reversed(previous_order[:min(removed_indexes)])
                              if path in current_paths), None)
        if candidate is not None:
            self.tree.selection_set(candidate)
            self.tree.focus(candidate)
            self.tree.see(candidate)
            self.select()

    def trash_completed(self, title, result, previous_order, requested_paths):
        errors = result.get('errors', [])
        current_paths = set(self.tree.get_children())
        removed_paths = set(requested_paths) - current_paths
        if removed_paths:
            self.select_next_after_trash(previous_order, removed_paths)
        if errors:
            self.result(title, f"Отправлено: {result['trashed']}", errors)
        elif result['trashed']:
            self.status.set(f"Отправлено в корзину: {result['trashed']}")

    def delete_selected(self):
        docs = copy.deepcopy(self.selected_documents())
        if self.busy or not docs or self.session.read_only:
            return
        previous_order = list(self.tree.get_children())
        selected_paths = {document['path'] for document in docs}
        requested_paths = [path for path in previous_order if path in selected_paths]
        docs_by_path = {document['path']: document for document in docs}
        docs = [docs_by_path[path] for path in requested_paths]
        if messagebox.askyesno('Корзина', f'Отправить выделенные файлы в корзину: {len(docs)}?'):
            self.run('Корзина', lambda: self.session.trash_documents(docs, progress=self.tick),
                     lambda r: self.trash_completed('Корзина', r, previous_order, requested_paths))

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
                previous_order = list(self.tree.get_children())
                requested_paths = [entry['remove']['path'] for entry in plan]
                self.run('Удаление дублей', lambda: self.session.trash_documents(duplicate_plan=plan, progress=self.tick),
                         lambda r: self.trash_completed('Дубликаты', r, previous_order, requested_paths))
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

    def statistics_menu(self):
        if not self.session or self.busy:
            return
        menu = tk.Menu(self.window, tearoff=False)
        menu.add_command(label='Быстрая статистика', command=lambda: self.statistics(False))
        menu.add_command(label='Точный подсчёт DOC/DOCX/RTF через LibreOffice',
                         command=lambda: self.statistics(True))
        try:
            menu.tk_popup(self.statistics_button.winfo_rootx(),
                          self.statistics_button.winfo_rooty() + self.statistics_button.winfo_height())
        finally:
            menu.grab_release()

    def statistics(self, use_libreoffice=False):
        if not self.session or self.busy:
            return
        selected = {d['path'] for d in self.selected_documents()} or None
        label = 'Точный подсчёт LibreOffice' if use_libreoffice else 'Быстрая статистика'
        def completed(result):
            if use_libreoffice and result:
                self.status.set(f"LibreOffice: обновлено {result['updated']}, пропущено {result['skipped']}, "
                                f"ошибок {result['errors']}" + (' · остановлено' if result['stopped'] else ''))
        self.run(('Статистика выбранных' if selected else 'Статистика всего каталога') + f' · {label}',
                 lambda: self.session.collect_stats(self.tick, use_libreoffice, selected), completed)

    def prerender(self):
        if not self.session or self.busy:
            return
        docs = copy.deepcopy(self.selected_documents() or self.visible_documents())
        if not docs:
            return
        selected = bool(self.selected_documents())
        if not selected and len(docs) > 200 and not messagebox.askyesno(
                'Пререндер preview',
                f'Пререндерить preview для всех видимых файлов: {len(docs)}?\n'
                'Картинки первой страницы будут сохранены в локальный кэш CorpusPick.'):
            return
        self.cancel_preview()
        self.release_preview_renderer()
        self.preview_cache.clear()
        self.preview_cache_order.clear()
        self.run('Пререндер preview',
                 lambda: self.prerender_documents(docs),
                 self.finish_prerender)

    def finish_prerender(self, r):
        self.cancel_preview()
        self.release_preview_renderer()
        self.preview_cache.clear()
        self.preview_cache_order.clear()
        self.preview_key = None
        self.status.set(
                     f"Пререндер: обработано {r['processed']}/{r['total']} · "
                     f"картинок: {r['images']}" +
                     (f" · уже в кэше: {r['cached']}" if r.get('cached') else '') +
                     (f" · пропущено: {r['skipped']}" if r.get('skipped') else '') +
                     f" · ошибок: {r['errors']}" +
                     (' · Остановлено' if r.get('stopped') else ''))

    def ask_prerender_budget(self, counts, size, cancel):
        answer = {'continue': False}
        ready = threading.Event()
        self.events.put(('cache_budget', counts, size, answer, ready, cancel))
        while not ready.wait(.1):
            if cancel.is_set():
                return False
        return answer['continue'] and not cancel.is_set()

    def show_cache_budget(self, event):
        _, counts, size, answer, ready, cancel = event
        if cancel.is_set():
            ready.set()
            return
        dialog = tk.Toplevel(self.window)
        dialog.withdraw()
        dialog.title('Размер кэша preview')
        dialog.transient(self.window)
        text = ('Размер кэша достиг 2 ГиБ.\n'
                f'Общий кэш всех каталогов: {size / (1024 * 1024):.1f} МиБ.\n\n'
                f'В текущем пререндере осталось файлов: {sum(counts.values())}.\n'
                'Без готового preview по форматам:\n' +
                '\n'.join(f'{suffix.upper().lstrip(".")}: {count}' for suffix, count in sorted(counts.items())) +
                '\n\nПродолжить выполнение? Сохранённый кэш не удаляется.')
        ttk.Label(dialog, text=text, padding=16, wraplength=460).pack()
        buttons = ttk.Frame(dialog, padding=10)
        buttons.pack(fill='x')
        def finish(proceed):
            answer['continue'] = proceed
            ready.set()
            dialog.destroy()
        ttk.Button(buttons, text='Продолжить', command=lambda: finish(True)).pack(side='left', padx=5)
        enough = ttk.Button(buttons, text='Достаточно', command=lambda: finish(False))
        enough.pack(side='right', padx=5)
        dialog.protocol('WM_DELETE_WINDOW', lambda: finish(False))
        dialog.bind('<Escape>', lambda _: finish(False))
        dialog.update_idletasks()
        width, height = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
        x = max(0, (dialog.winfo_screenwidth() - width) // 2)
        y = max(0, (dialog.winfo_screenheight() - height) // 2)
        dialog.geometry(f'{width}x{height}+{x}+{y}')
        dialog.deiconify()
        dialog.grab_set()
        enough.focus_set()
        def check_cancel():
            if not dialog.winfo_exists():
                return
            if cancel.is_set():
                finish(False)
            else:
                dialog.after(100, check_cancel)
        dialog.after(100, check_cancel)

    def prerender_documents(self, docs):
        import concurrent.futures
        from .core import Cancelled
        from .preview import (
            cached_preview_available, office_preview_candidate, parallel_preview_candidate,
            preview_cache_candidate, preview_worker)
        from .stats_worker import StatsWorker

        operation_cancel = self.session.cancel
        result = {
            'total': len(docs), 'processed': 0, 'images': 0, 'cached': 0,
            'skipped': 0, 'errors': 0, 'consecutive_errors': 0,
            'stopped': False,
        }
        from .preview import image_cache_size
        budget_lock = threading.Lock()
        budget = {'size': image_cache_size(), 'accepted': False}
        def allow_render():
            with budget_lock:
                if operation_cancel.is_set() or result['stopped']:
                    return False
                if budget['accepted'] or budget['size'] < 2 * 1024 * 1024 * 1024:
                    return True
                # Recheck actual disk usage only near the threshold, not on every hit.
                size = image_cache_size()
                budget['size'] = size
                if size < 2 * 1024 * 1024 * 1024:
                    return True
                counts = {}
                for document in docs:
                    if operation_cancel.is_set():
                        return False
                    try:
                        candidate = self.session.checked_document(document)
                        if preview_cache_candidate(candidate) and not cached_preview_available(candidate):
                            suffix = candidate.suffix.lower()
                            counts[suffix] = counts.get(suffix, 0) + 1
                    except (OSError, ValueError):
                        continue
                if not counts:
                    return True
                if self.ask_prerender_budget(counts, size, operation_cancel):
                    budget['accepted'] = True
                    return True
                result['stopped'] = True
                operation_cancel.set()
                return False

        lock = threading.Lock()
        def mark_stopped():
            with lock:
                result['stopped'] = True

        def processed_count():
            with lock:
                return result['processed']

        def finish(current=None, *, image=False, cached=False, skipped=False, error=False):
            with lock:
                result['processed'] += 1
                if image:
                    result['images'] += 1
                if cached:
                    result['cached'] += 1
                if skipped:
                    result['skipped'] += 1
                if error:
                    result['errors'] += 1
                    result['consecutive_errors'] += 1
                else:
                    result['consecutive_errors'] = 0
                processed = result['processed']
            self.tick(processed, current)

        def prerender_priority(document):
            hint = Path(str(document.get('path') or ''))
            if office_preview_candidate(hint):
                return 0
            if parallel_preview_candidate(hint):
                return 2
            return 1

        def collect_paths(documents):
            office_paths = []
            parallel_paths = []
            for document in documents:
                if operation_cancel.is_set():
                    mark_stopped()
                    break
                try:
                    path = self.session.checked_document(document)
                except (OSError, ValueError):
                    finish(document.get('path'), error=True)
                    continue
                if not preview_cache_candidate(path):
                    finish(path, skipped=True)
                    continue
                if cached_preview_available(path):
                    finish(path, image=True, cached=True)
                    continue
                if office_preview_candidate(path):
                    office_paths.append(path)
                elif parallel_preview_candidate(path):
                    parallel_paths.append(path)
                else:
                    finish(path, skipped=True)
            return office_paths, parallel_paths

        def render_paths(paths):
            remaining = list(paths)
            if not remaining:
                return
            if operation_cancel.is_set():
                mark_stopped()
                return
            try:
                while remaining:
                    if operation_cancel.is_set():
                        mark_stopped()
                        break
                    with StatsWorker(timeout=PREVIEW_TIMEOUT, target=preview_worker, retry_label='Пререндер') as preview:
                        handled = 0
                        while remaining and handled < PRERENDER_WORKER_RECYCLE_AFTER:
                            path = remaining.pop(0)
                            if operation_cancel.is_set():
                                mark_stopped()
                                break
                            if cached_preview_available(path):
                                finish(path, image=True, cached=True)
                                continue
                            if not allow_render():
                                mark_stopped()
                                break
                            try:
                                self.tick(processed_count(), path, force=True)
                                item = preview.count(path, operation_cancel)
                            except Cancelled:
                                mark_stopped()
                                break
                            except Exception:
                                finish(path, error=True)
                                continue
                            finally:
                                handled += 1
                            with budget_lock:
                                budget['size'] += 2 * len(item.get('data', b''))
                            finish(
                                path,
                                image=item.get('kind') == 'image',
                                error=bool(item.get('failed') or item.get('kind') == 'message'))
                        if operation_cancel.is_set() or result['stopped']:
                            break
                    if result['stopped']:
                        break
            except Exception:
                for path in remaining:
                    if operation_cancel.is_set():
                        mark_stopped()
                        break
                    finish(path, error=True)

        def render_parallel_paths(paths):
            if not paths:
                return
            if result['stopped'] or operation_cancel.is_set():
                mark_stopped()
                return
            workers = min(len(paths), max(1, min(PRERENDER_MAX_WORKERS, os.cpu_count() or 1)))
            chunks = [[] for _ in range(workers)]
            for index, path in enumerate(paths):
                chunks[index % workers].append(path)
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(render_paths, chunk) for chunk in chunks if chunk]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception:
                        mark_stopped()

        document_batches = [[], [], []]
        for document in docs:
            document_batches[prerender_priority(document)].append(document)
        late_parallel_paths = []
        for documents in document_batches:
            if result['stopped'] or operation_cancel.is_set():
                if operation_cancel.is_set():
                    mark_stopped()
                break
            office_paths, parallel_paths = collect_paths(documents)
            late_parallel_paths.extend(parallel_paths)
            if office_paths and not result['stopped']:
                render_paths(office_paths)
        if late_parallel_paths and not result['stopped']:
            render_parallel_paths(late_parallel_paths)
        return result

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
        log_message('Close requested', pid=os.getpid(), busy=self.busy)
        if self.busy:
            self.stop()
            messagebox.showinfo('Остановка', 'Сохраняется прогресс. Закройте окно после завершения остановки.')
            return
        self.preview_closed = True
        self.preview_shutdown.set()
        if self.session:
            self.session.close()
        self.cancel_preview()
        self.preview_requests.put(None)
        if self.preview_thread and self.preview_thread.is_alive():
            self.preview_thread.join(timeout=2)
        self.window.destroy()


def main():
    setup_error_logging()
    enable_native_crash_logging()
    log_message('Application started', pid=os.getpid())
    window = tk.Tk()
    App(window)
    window.mainloop()
    log_message('Application closed normally', pid=os.getpid())


if __name__ == '__main__':
    main()
