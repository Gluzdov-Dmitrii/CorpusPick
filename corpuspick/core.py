"""Local filesystem operations. Never log document paths or content."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from collections import Counter
from zipfile import ZipFile
from xml.etree import ElementTree


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Session:
    def __init__(self, root: Path, data_root: Path | None = None):
        if is_link(root):
            raise ValueError("Выберите обычный каталог, не ссылку.")
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Нужен каталог.")
        if data_root is None:
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
            data_root = base / "CorpusPick"
        data_root = data_root.resolve()
        if data_root == self.root or data_root.is_relative_to(self.root):
            raise ValueError("Каталог состояния не должен находиться внутри выбранного каталога.")
        key = hashlib.sha256(os.path.normcase(str(self.root)).encode()).hexdigest()
        self.state_path = data_root / f"{key}.json"
        self.lock_path = data_root / f"{key}.lock"
        data_root.mkdir(parents=True, exist_ok=True)
        self._lock = self.lock_path.open("a+b")
        try:
            # OS releases the lock on exit, including crashes.
            self._lock.seek(0)
            self._lock.write(b"0")
            self._lock.flush()
            self._lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise ValueError("Этот каталог уже открыт в другом окне CorpusPick.") from None
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {
                "version": 1, "documents": [], "moves": [], "undo": []
            }
            if self.state.get("version") != 1:
                raise ValueError("Неизвестный формат сессии.")
        except Exception:
            self.close()
            raise

    def close(self):
        self._lock.close()

    def save(self):
        atomic_json(self.state_path, self.state)

    def safe_path(self, relative: str) -> Path:
        candidate = self.root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Недопустимый путь в журнале.")
        for component in [candidate, *candidate.parents]:
            if component == self.root:
                break
            if component.exists() and is_link(component):
                raise ValueError("Ссылки и junction не обрабатываются.")
        if not candidate.resolve().is_relative_to(self.root):
            raise ValueError("Путь выходит за выбранный каталог.")
        return candidate

    def scan(self, progress=lambda count: None):
        if any(not m["done"] for m in self.state["moves"]):
            raise ValueError("Есть незавершённый перенос. Продолжите его или нажмите «Сбросить план переноса» перед сканированием.")
        old = {d["path"]: d for d in self.state["documents"]}
        moved = {m["destination"]: m for m in self.state["moves"] if m["done"]}
        documents, issues = [], []
        def walk_error(error):
            issues.append("Не удалось прочитать один из каталогов.")
        for folder, dirs, files in os.walk(self.root, followlinks=False, onerror=walk_error):
            kept = []
            for name in dirs:
                try:
                    if is_link(Path(folder) / name):
                        issues.append("Пропущена ссылка или junction.")
                    else:
                        kept.append(name)
                except OSError:
                    issues.append("Недоступен подкаталог.")
            dirs[:] = sorted(kept)
            for name in sorted(files):
                path = Path(folder) / name
                relative = str(path.relative_to(self.root))
                item = {"path": relative, "origin": relative, "size": 0, "hash": "", "score": 1,
                        "category": "Не определено", "reviewed": False, "reason": "Требуется просмотр человеком."}
                try:
                    if is_link(path) or not path.is_file():
                        issues.append("Пропущен файл-ссылка или специальный файл.")
                        continue
                    before = path.stat()
                    item["hash"] = digest(path)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise OSError("changed")
                    item["size"] = after.st_size
                    previous = old.get(relative)
                    movement = moved.get(relative)
                    if movement and movement["hash"] == item["hash"]:
                        item["origin"] = movement["origin"]
                        previous = previous or old.get(movement["source"])
                    if previous and previous["hash"] == item["hash"]:
                        for field in ("origin", "score", "category", "reviewed"):
                            item[field] = previous[field]
                    if item["size"] == 0:
                        item["reason"] = "Пустой файл: нет данных для корпуса. Рекомендация: 0."
                        if not item["reviewed"]:
                            item["score"] = 0
                except OSError:
                    item["hash"] = ""
                    item["reason"] = "Ошибка чтения или файл изменился; повторите сканирование."
                documents.append(item)
                progress(len(documents))
        counts = Counter(d["hash"] for d in documents if d["hash"])
        groups = {h: i + 1 for i, h in enumerate(sorted(h for h, n in counts.items() if n > 1))}
        for item in documents:
            item["duplicate"] = groups.get(item["hash"], 0)
            if item["duplicate"]:
                item["reason"] += " Точная копия по SHA-256; выберите нужный экземпляр."
        self.state["documents"] = documents
        self.state["issues"] = issues
        self.save()
        return documents

    def decide(self, relative: str, score: int, category: str):
        if score not in (0, 1, 2):
            raise ValueError("Оценка должна быть 0, 1 или 2.")
        document = next(d for d in self.state["documents"] if d["path"] == relative)
        self.state["undo"].append({k: document[k] for k in ("path", "hash", "score", "category", "reviewed")})
        document.update(score=score, category=category, reviewed=True)
        self.save()

    def undo(self):
        while self.state["undo"]:
            previous = self.state["undo"].pop()
            document = next((d for d in self.state["documents"]
                             if d["path"] == previous["path"] and d["hash"] == previous["hash"]), None)
            if document:
                document.update(previous)
                self.save()
                return True
        self.save()
        return False

    def flatten(self):
        # Persist intent before each filesystem mutation. Hard links provide an
        # exclusive destination on the same filesystem without overwriting files.
        pending = [m for m in self.state["moves"] if not m["done"]]
        if not pending:
            occupied = {p.name.casefold() for p in self.root.iterdir()}
            for document in self.state["documents"]:
                source = Path(document["path"])
                if source.parent == Path("."):
                    continue
                if not document["hash"]:
                    raise ValueError("Сначала устраните ошибки чтения и повторите сканирование.")
                name, number = source.name, 1
                while name.casefold() in occupied:
                    name = f"{source.stem}__{number}{source.suffix}"
                    number += 1
                occupied.add(name.casefold())
                pending.append({"source": str(source), "destination": name, "origin": document["origin"],
                                "hash": document["hash"], "done": False})
            self.state["moves"].extend(pending)
            self.save()
        for move in pending:
            source = self.safe_path(move["source"])
            destination = self.safe_path(move["destination"])
            if source.exists():
                if digest(source) != move["hash"]:
                    raise ValueError("Файл изменился после сканирования. Перенос остановлен; оригинал сохранён.")
                if not destination.exists():
                    os.link(source, destination)
                elif not os.path.samefile(source, destination):
                    raise ValueError("Место назначения занято другим файлом. Перенос остановлен без перезаписи.")
                if digest(destination) != move["hash"]:
                    raise ValueError("Проверка целостности не пройдена. Оригинал сохранён.")
                source.unlink()
            elif not destination.is_file() or digest(destination) != move["hash"]:
                raise ValueError("Не удалось восстановить перенос. Проверьте файлы локально.")
            move["done"] = True
            for document in self.state["documents"]:
                if document["path"] == move["source"]:
                    document["path"] = move["destination"]
            for decision in self.state["undo"]:
                if decision["path"] == move["source"]:
                    decision["path"] = move["destination"]
            self.save()

    def remove_empty_directories(self):
        removed = 0
        # top-down traversal first to prune junctions, then deepest-first rmdir.
        folders = []
        for folder, dirs, _ in os.walk(self.root, followlinks=False):
            dirs[:] = [d for d in dirs if not is_link(Path(folder) / d)]
            folders.extend(Path(folder) / d for d in dirs)
        for folder in reversed(folders):
            try:
                self.safe_path(str(folder.relative_to(self.root))).rmdir()
                removed += 1
            except OSError:
                pass  # Nonempty or inaccessible directories are retained.
        return removed

    def cancel_pending(self):
        """Revert only unfinished hard links, never completed movements."""
        for move in self.state["moves"]:
            if move["done"]:
                continue
            source = self.safe_path(move["source"])
            destination = self.safe_path(move["destination"])
            if not source.is_file():
                raise ValueError("Сначала продолжите перенос: один из файлов уже находится только в корне.")
            if destination.exists() and os.path.samefile(source, destination):
                destination.unlink()
        self.state["moves"] = [m for m in self.state["moves"] if m["done"]]
        self.save()


def preview(path: Path) -> str:
    limit = 150_000
    if path.suffix.lower() in {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".html", ".xml"}:
        raw = path.open("rb")
        with raw:
            content = raw.read(limit + 1)
        try:
            result = content[:limit].decode("utf-8-sig")
        except UnicodeDecodeError:
            result = content[:limit].decode("cp1251", errors="replace")
        return result + ("\n[Показано начало файла]" if len(content) > limit else "")
    if path.suffix.lower() == ".docx":
        with ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 10_000_000:
                return "Документ слишком большой для встроенного просмотра."
            tree = ElementTree.fromstring(archive.read(info))
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        result = "\n".join("".join(t.text or "" for t in p.iter(ns + "t")) for p in tree.iter(ns + "p"))
        return "Текст DOCX: без изображений, колонтитулов и точной вёрстки.\n\n" + result[:limit] + (
            "\n[Показано начало текста]" if len(result) > limit else "")
    return "Встроенный просмотр: TXT и DOCX. PDF, сканы и остальные форматы откройте кнопкой «Открыть документ». Отсутствие предпросмотра не означает бесполезность."
