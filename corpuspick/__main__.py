"""Run with python -m corpuspick."""
import csv
import os
from pathlib import Path
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import Session, preview


class App:
    def __init__(self, window):
        self.window, self.session = window, None
        self.events = queue.Queue()
        self.busy = False
        self.preview_token = 0
        self.preview_executor = ThreadPoolExecutor(max_workers=1)
        self.preview_future = None
        window.title("CorpusPick — подготовка корпуса")
        window.geometry("1250x800")
        window.minsize(900, 600)
        self.status = tk.StringVar(value="Откройте отдельную рабочую копию каталога с документами.")
        self.search = tk.StringVar()
        self.filter = tk.StringVar(value="Все")
        self.category = tk.StringVar(value="Не определено")
        bar = ttk.Frame(window, padding=8)
        bar.pack(fill="x")
        self.buttons = []
        for label, command in [("Открыть каталог", self.open_folder), ("Обновить", self.scan),
                               ("Перенести в корень", self.flatten), ("Удалить пустые папки", self.clean),
                               ("Сбросить план переноса", self.cancel_pending), ("Экспорт CSV", self.export)]:
            button = ttk.Button(bar, text=label, command=command)
            button.pack(side="left", padx=3)
            self.buttons.append(button)
        ttk.Label(window, textvariable=self.status, padding=8, wraplength=1150).pack(fill="x")
        filters = ttk.Frame(window, padding=8)
        filters.pack(fill="x")
        ttk.Label(filters, text="Поиск по пути:").pack(side="left")
        ttk.Entry(filters, textvariable=self.search, width=40).pack(side="left", padx=8)
        ttk.Combobox(filters, textvariable=self.filter, state="readonly", width=22,
                     values=["Все", "Точные дубли", "Непроверенные", "Оценка 0", "Оценка 1", "Оценка 2"]).pack(side="left")
        self.search.trace_add("write", lambda *_: self.render())
        self.filter.trace_add("write", lambda *_: self.render())
        panes = ttk.Panedwindow(window, orient="vertical")
        panes.pack(fill="both", expand=True, padx=8)
        listing = ttk.Frame(panes)
        columns = ("name", "origin", "type", "size", "duplicate", "score", "category", "reviewed")
        self.tree = ttk.Treeview(listing, columns=columns, show="headings", selectmode="browse")
        for column, title, width in zip(columns,
                ["Файл", "Исходный путь", "Тип", "Байт", "Дубль: группа", "0/1/2", "Категория", "Проверен"],
                [220, 300, 65, 90, 100, 60, 120, 80]):
            self.tree.heading(column, text=title, command=lambda c=column: self.sort(c))
            self.tree.column(column, width=width)
        yscroll = ttk.Scrollbar(listing, command=self.tree.yview)
        xscroll = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        panes.add(listing, weight=3)
        details = ttk.Frame(panes)
        self.info = tk.StringVar(value="Оценка: 0 — исключить; 1 — ручная проверка; 2 — полезен для выбранной задачи.")
        ttk.Label(details, textvariable=self.info, wraplength=1150, padding=5).pack(fill="x")
        actions = ttk.Frame(details)
        actions.pack(fill="x", pady=5)
        ttk.Label(actions, text="Категория:").pack(side="left")
        ttk.Combobox(actions, textvariable=self.category, width=20, state="readonly",
                     values=["Не определено", "Отчёт", "Письмо", "Таблица", "Взять под OCR", "Другое"]).pack(side="left", padx=5)
        for score, label in [(0, "0 — исключить"), (1, "1 — проверить"), (2, "2 — полезен")]:
            ttk.Button(actions, text=label, command=lambda s=score: self.decide(s)).pack(side="left", padx=3)
        ttk.Button(actions, text="Отменить оценку", command=self.undo).pack(side="left", padx=3)
        ttk.Button(actions, text="Открыть документ", command=self.open_document).pack(side="left", padx=3)
        self.text = tk.Text(details, wrap="word", state="disabled", font=("Segoe UI", 11))
        scroll = ttk.Scrollbar(details, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        panes.add(details, weight=2)
        ttk.Label(window, text="Оценки сохраняются автоматически. 0/1/2 в списке — оценить и перейти дальше; Ctrl+Z — отмена. "
                  "SHA-256 находит только побайтовые копии, не версии DOCX/PDF.", padding=8, wraplength=1150).pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self.select)
        for score in (0, 1, 2):
            self.tree.bind(str(score), lambda event, s=score: self.decide(s))
        self.tree.bind("<Control-z>", lambda event: self.undo())
        self.tree.bind("<Double-1>", lambda event: self.open_document())
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.after(100, self.poll)

    def run(self, function, done):
        if self.busy:
            return
        self.busy = True
        self.status.set("Выполняется локальная операция…")
        for button in self.buttons:
            button.configure(state="disabled")
        def worker():
            try:
                self.events.put(("done", done, function()))
            except Exception as error:
                # OSError may contain private paths: display only a generic message.
                reason = str(error) if isinstance(error, ValueError) else "Операция не завершена. Проверьте доступ, свободное место и формат файлов."
                self.events.put(("error", reason))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "preview":
                    if event[1] == self.preview_token:
                        self.set_text(event[2])
                    continue
                self.busy = False
                for button in self.buttons:
                    button.configure(state="normal")
                if event[0] == "error":
                    self.status.set("Операция остановлена. Журнал сохранён; перенос можно продолжить той же кнопкой.")
                    messagebox.showerror("CorpusPick", event[1])
                    self.render()
                else:
                    event[1](event[2])
        except queue.Empty:
            pass
        self.window.after(100, self.poll)

    def open_folder(self):
        if self.busy:
            return
        chosen = filedialog.askdirectory(title="Выберите рабочую копию каталога")
        if not chosen:
            return
        if self.session and self.session.root == Path(chosen).resolve():
            self.scan()
            return
        try:
            session = Session(Path(chosen))
        except Exception:
            messagebox.showerror("Не удалось открыть", "Проверьте права, состояние сессии и не открыт ли каталог в другом окне.")
            return
        if self.session:
            self.session.close()
        self.session = session
        self.preview_token += 1
        self.set_text("")
        self.window.title(f"CorpusPick — {chosen}")
        self.scan()

    def scan(self):
        if self.session:
            if any(not m["done"] for m in self.session.state["moves"]):
                self.refreshed()
                messagebox.showinfo("Незавершённый перенос", "Нажмите «Перенести в корень» для продолжения или «Сбросить план переноса» для отмены оставшихся шагов.")
                return
            self.run(self.session.scan, lambda _: self.refreshed())

    def refreshed(self):
        docs = self.session.state["documents"]
        hashes = [d["hash"] for d in docs if d["hash"]]
        groups = len({d["duplicate"] for d in docs if d["duplicate"]})
        scores = " / ".join(f"{s}: {sum(d['score'] == s for d in docs)}" for s in (0, 1, 2))
        pending = sum(not m["done"] for m in self.session.state["moves"])
        self.status.set(f"Файлов: {len(docs)} · {sum(d['size'] for d in docs)/1024/1024:.1f} МБ · "
                        f"Групп дублей: {groups}, лишних копий: {len(hashes)-len(set(hashes))} · "
                        f"Оценки {scores} · Ошибок чтения: {sum(not d['hash'] for d in docs)} · "
                        f"Пропусков/предупреждений: {len(self.session.state.get('issues', []))} · Переносов в ожидании: {pending}")
        self.render()

    def render(self):
        if not self.session or self.busy:
            return
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i, d in enumerate(self.session.state["documents"]):
            if self.search.get().casefold() not in (d["path"] + " " + d["origin"]).casefold():
                continue
            choice = self.filter.get()
            if choice == "Точные дубли" and not d.get("duplicate"):
                continue
            if choice == "Непроверенные" and d["reviewed"]:
                continue
            if choice.startswith("Оценка") and d["score"] != int(choice[-1]):
                continue
            self.tree.insert("", "end", iid=str(i), values=(d["path"], d["origin"], Path(d["path"]).suffix,
                             d["size"], d.get("duplicate") or "—", d["score"], d["category"], "Да" if d["reviewed"] else "Нет"))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        else:
            self.preview_token += 1
            self.set_text("")
            self.info.set("Выберите документ для просмотра и оценки.")

    def sort(self, column):
        rows = list(self.tree.get_children())
        def key(row):
            value = self.tree.set(row, column)
            return int(value) if column in ("size", "score") else value.casefold()
        for index, row in enumerate(sorted(rows, key=key)):
            self.tree.move(row, "", index)

    def selected(self):
        selection = self.tree.selection()
        if self.session and selection:
            return self.session.state["documents"][int(selection[0])]

    def set_text(self, content):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")

    def select(self, _=None):
        if self.busy:
            return
        d = self.selected()
        if not d:
            return
        self.category.set(d["category"])
        self.info.set(f"Был: {d['origin']}\n{d['reason']} "
                      "Оценка 2 — ваше решение о пользе; документ ещё не является готовым обучающим примером.")
        self.preview_token += 1
        token = self.preview_token
        session, relative = self.session, d["path"]
        self.set_text("Загрузка…")
        def worker():
            try:
                content = preview(session.safe_path(relative))
            except Exception:
                content = "Не удалось прочитать предпросмотр. Проверьте документ локально."
            self.events.put(("preview", token, content))
        if self.preview_future:
            self.preview_future.cancel()
        self.preview_future = self.preview_executor.submit(worker)

    def decide(self, score):
        if self.busy or not self.selected():
            return
        current = self.tree.selection()[0]
        following = self.tree.next(current)
        try:
            self.session.decide(self.selected()["path"], score, self.category.get())
        except OSError:
            messagebox.showerror("Ошибка сохранения", "Не удалось сохранить оценку. Проверьте каталог состояния.")
            return
        self.refreshed()
        if following and self.tree.exists(following):
            self.tree.selection_set(following)
            self.tree.see(following)
        self.tree.focus_set()

    def undo(self):
        if self.session and not self.busy:
            try:
                self.session.undo()
                self.refreshed()
            except OSError:
                messagebox.showerror("Ошибка сохранения", "Не удалось сохранить отмену.")

    def flatten(self):
        if not self.session or self.busy:
            return
        if messagebox.askyesno("Перенос файлов", "Все просканированные файлы из подпапок будут перенесены в корень выбранного каталога. "
                               "Совпадающие имена получат суффикс. Автоматической отмены переноса нет. "
                               "Используйте отдельную копию архива и закройте документы в редакторах. Продолжить?"):
            def operation():
                self.session.flatten()
                self.session.scan()
            self.run(operation, lambda _: self.refreshed())

    def clean(self):
        if self.session and not self.busy and messagebox.askyesno("Пустые папки", "Удалить только пустые подкаталоги выбранного корня?"):
            self.run(self.session.remove_empty_directories,
                     lambda count: messagebox.showinfo("Готово", f"Удалено пустых папок: {count}") or self.refreshed())

    def cancel_pending(self):
        if self.session and not self.busy:
            def operation():
                self.session.cancel_pending()
                self.session.scan()
            self.run(operation, lambda _: self.refreshed())

    def open_document(self):
        if self.busy or not self.selected():
            return
        path = self.session.safe_path(self.selected()["path"])
        if path.suffix.lower() not in {".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".xlsx", ".xls", ".csv", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
            messagebox.showinfo("Формат", "Для этого типа файлов запуск из приложения отключён.")
            return
        if messagebox.askyesno("Внешний просмотрщик", "Открыть файл в системной программе? Её сетевые подключения и макросы управляются её собственными настройками."):
            try:
                os.startfile(path)
            except (OSError, AttributeError):
                messagebox.showerror("Не удалось открыть", "Нет доступной программы для этого формата.")

    def export(self):
        if not self.session or self.busy:
            return
        filename = filedialog.asksaveasfilename(title="Локальный отчёт (содержит имена и пути)",
                                               defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not filename:
            return
        def operation():
            # Exclusive creation protects documents and previously exported reports.
            with open(filename, "x", encoding="utf-8-sig", newline="") as stream:
                fields = ["path", "origin", "size", "hash", "duplicate", "score", "category", "reviewed", "reason"]
                writer = csv.DictWriter(stream, fields, delimiter=";")
                writer.writeheader()
                for doc in self.session.state["documents"]:
                    row = {key: doc.get(key, "") for key in fields}
                    for key, value in row.items():
                        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                            row[key] = "'" + value
                    writer.writerow(row)
        self.run(operation, lambda _: self.refreshed())

    def close(self):
        if self.busy:
            messagebox.showinfo("Операция выполняется", "Дождитесь завершения операции перед закрытием.")
            return
        if self.session:
            self.session.close()
        self.preview_executor.shutdown(wait=False, cancel_futures=True)
        self.window.destroy()


def main():
    window = tk.Tk()
    App(window)
    window.mainloop()


if __name__ == "__main__":
    main()
