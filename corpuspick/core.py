"""Local filesystem operations. Never log document paths or content."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from collections import Counter
from .recycle import recycle_file


def is_link(path: Path) -> bool:
    info = Path(native(path)).lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_reparse_tag", 0) & 0x20000000
    )


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(native(path), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def native(path):
    """Extended Windows paths work even when LongPathsEnabled is disabled."""
    value = os.path.abspath(path)
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    return "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value


def failure(error):
    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    if isinstance(error, FileNotFoundError):
        return "Файл уже перемещён или удалён в Explorer"
    if code in (32, 33):
        return "Файл занят: закройте его в редакторе или Preview и повторите"
    if isinstance(error, PermissionError):
        return "Нет доступа: проверьте права или закройте файл в другой программе"
    if isinstance(error, FileExistsError):
        return "Имя уже занято; существующий файл сохранён"
    return f"Ошибка файловой системы (код {code or 'неизвестен'})"


def move_exclusive(source, destination):
    if os.name == "nt":
        # Windows rename fails on an existing target and preserves ADS/metadata.
        os.rename(native(source), native(destination))
    else:
        os.link(source, destination)
        source.unlink()


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

    def walk(self):
        self.state["issues"] = []
        def error(exc):
            self.state["issues"].append(failure(exc))
        for folder, dirs, files in os.walk(native(self.root), followlinks=False, onerror=error):
            kept = []
            for name in dirs:
                try:
                    if not is_link(Path(folder) / name):
                        kept.append(name)
                except OSError as exc:
                    error(exc)
            dirs[:] = sorted(kept)
            for name in sorted(files):
                path = Path(folder) / name
                try:
                    if not is_link(path) and path.is_file():
                        yield path, str(path.relative_to(Path(native(self.root))))
                except OSError as exc:
                    error(exc)

    def scan(self, progress=lambda count: None):
        old = {d["path"]: d for d in self.state["documents"]}
        moved = {m["destination"]: m for m in self.state["moves"] if m["done"]}
        documents = []
        for path, relative in self.walk():
            item = {"path": relative, "origin": relative, "size": None, "hash": "",
                    "note": "", "error": "", "stats": {}}
            previous = old.get(relative, {})
            try:
                before = path.stat()
                item["size"] = before.st_size
                item["identity"] = [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns]
                item["hash"] = digest(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    item["hash"] = ""
                    item["error"] = "Файл изменился во время проверки дублей; F5 для обновления"
            except OSError as exc:
                item["error"] = failure(exc)
            movement = moved.get(relative)
            if movement and (movement.get("hash") == item["hash"] and item["hash"] or
                             movement.get("identity") == item.get("identity") and item.get("identity")):
                item["origin"] = movement["origin"]
            unchanged = item["hash"] and previous.get("hash") == item["hash"]
            if unchanged:
                item["origin"] = previous.get("origin", item["origin"])
                item["note"] = previous.get("note", "Скорее да" if previous.get("score") == 2 else "")
                item["stats"] = previous.get("stats", {})
            documents.append(item)
            progress(len(documents))
        counts = Counter(d["hash"] for d in documents if d["hash"])
        groups = {h: i + 1 for i, h in enumerate(sorted(h for h, n in counts.items() if n > 1))}
        for item in documents:
            item["duplicate"] = groups.get(item["hash"], 0)
        self.state["documents"] = documents
        self.save()
        return documents

    def mark(self, relative):
        item = next(d for d in self.state["documents"] if d["path"] == relative)
        item["note"] = "" if item.get("note") else "Скорее да"
        self.save()

    def duplicate_plan(self, progress=lambda count: None):
        self.scan(progress)
        groups = {}
        for document in self.state['documents']:
            if document.get('hash') and not document.get('error'):
                groups.setdefault(document['hash'], []).append(document)
        plan = []
        for documents in groups.values():
            ordered = sorted(documents, key=lambda d: (not bool(d.get('note')),
                             len(Path(d['path']).parts), d['path'].casefold(), d['path']))
            plan.extend({'keep': dict(ordered[0]), 'remove': dict(d)} for d in ordered[1:])
        return plan

    def checked_document(self, document, verify_hash=False):
        path = Path(native(self.safe_path(document['path'])))
        info = path.stat()
        identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
        if not path.is_file() or not document.get('identity') or identity != document['identity']:
            raise ValueError('Файл изменился после обновления списка; нажмите F5 и повторите')
        if verify_hash and (not document.get('hash') or digest(path) != document['hash']):
            raise ValueError('Содержимое изменилось: удаление дубля отменено')
        return self.safe_path(document['path'])

    def trash_documents(self, documents=None, duplicate_plan=None, progress=lambda count: None):
        if any(not m['done'] for m in self.state['moves']):
            raise ValueError('Сначала завершите перенос или сбросьте его план в меню «Инструменты»')
        result = {'trashed': 0, 'errors': []}
        entries = duplicate_plan if duplicate_plan is not None else [{'remove': d} for d in documents or []]
        for index, entry in enumerate(entries):
            document = entry['remove']
            try:
                if 'keep' in entry:
                    if entry['keep']['path'] == document['path'] or entry['keep'].get('hash') != document.get('hash'):
                        raise ValueError('Некорректный план дублей: файл сохранён')
                    self.checked_document(entry['keep'], verify_hash=True)
                path = self.checked_document(document, verify_hash='keep' in entry)
                recycle_file(path)
                result['trashed'] += 1
            except (OSError, ValueError) as exc:
                result['errors'].append({'path': document['path'],
                                        'error': str(exc) if isinstance(exc, ValueError) else failure(exc)})
            progress(index + 1)
        self.scan(progress)
        return result

    def flatten(self, progress=lambda count: None):
        pending = [m for m in self.state["moves"] if not m["done"]]
        if not pending:
            # Explorer may have changed the folder since the last scan.
            self.scan(progress)
            occupied = {p.name.casefold() for p in Path(native(self.root)).iterdir()}
            for document in self.state["documents"]:
                source = Path(document["path"])
                if source.parent == Path("."):
                    continue
                name, number = source.name, 1
                while name.casefold() in occupied:
                    name = f"{source.stem}__{number}{source.suffix}"
                    number += 1
                occupied.add(name.casefold())
                pending.append({"source": str(source), "destination": name, "origin": document["origin"],
                                "hash": document["hash"], "identity": document.get("identity"),
                                "method": "rename", "done": False})
            self.state["moves"].extend(pending)
            self.save()
        result = {"moved": 0, "errors": []}
        for index, move in enumerate(pending):
            try:
                source = self.safe_path(move["source"])
                destination = self.safe_path(move["destination"])
                src, dst = Path(native(source)), Path(native(destination))
                if src.exists():
                    if move.get("method") != "rename":
                        # Recover journals made by the first hard-link version.
                        if digest(src) != move["hash"]:
                            raise ValueError("Файл изменён; сбросьте незавершённый план в меню «Инструменты»")
                        if dst.exists():
                            if not os.path.samefile(src, dst):
                                raise FileExistsError()
                            src.unlink()
                        else:
                            move_exclusive(src, dst)
                    else:
                        before = src.stat()
                        identity = [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns]
                        if move.get("identity") and identity != move["identity"]:
                            raise ValueError("Файл изменён; сбросьте незавершённый план в меню «Инструменты»")
                        move_exclusive(src, dst)
                elif dst.is_file():
                    info = dst.stat()
                    identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
                    if not (move.get("identity") == identity or move.get("hash") and digest(dst) == move["hash"]):
                        raise ValueError("Не удалось подтвердить файл после прерывания переноса")
                else:
                    raise FileNotFoundError()
                move["done"] = True
                move.pop("error", None)
                for document in self.state["documents"]:
                    if document["path"] == move["source"]:
                        document["path"] = move["destination"]
                        document["origin"] = move["origin"]
                result["moved"] += 1
            except (OSError, ValueError) as exc:
                move["error"] = str(exc) if isinstance(exc, ValueError) else failure(exc)
                result["errors"].append({"path": move["source"], "error": move["error"]})
            # Storage errors must stop the operation; do not continue without journal.
            self.save()
            progress(index + 1)
        self.scan(progress)
        return result

    def remove_empty_directories(self):
        folders = []
        errors = []
        for folder, dirs, _ in os.walk(native(self.root), followlinks=False,
                                       onerror=lambda exc: errors.append(failure(exc))):
            kept = []
            for name in dirs:
                path = Path(folder) / name
                try:
                    if not is_link(path):
                        kept.append(name)
                        folders.append(path)
                except OSError as exc:
                    errors.append(failure(exc))
            dirs[:] = kept
        removed = 0
        for folder in reversed(folders):
            try:
                folder.rmdir()
                removed += 1
            except OSError as exc:
                if getattr(exc, "winerror", None) != 145 and exc.errno not in (39, 17):
                    errors.append(failure(exc))
        return {"removed": removed, "errors": errors}

    def cancel_pending(self):
        for move in self.state["moves"]:
            if move["done"]:
                continue
            source = Path(native(self.safe_path(move["source"])))
            destination = Path(native(self.safe_path(move["destination"])))
            if not source.exists() and destination.exists():
                raise ValueError("Сначала продолжите перенос: один из файлов уже находится только в корне")
            if source.exists() and destination.exists() and os.path.samefile(source, destination):
                destination.unlink()
        self.state["moves"] = [m for m in self.state["moves"] if m["done"]]
        self.save()

    def unlock(self, progress=lambda count: None):
        if os.name != "nt":
            raise ValueError("Unlock доступен только в Windows")
        result = {"unlocked": 0, "unchanged": 0, "errors": []}
        for index, (path, relative) in enumerate(self.walk()):
            try:
                os.unlink(str(path) + ":Zone.Identifier")
                result["unlocked"] += 1
            except FileNotFoundError:
                result["unchanged"] += 1
            except OSError as exc:
                result["errors"].append({"path": relative, "error": failure(exc)})
            progress(index + 1)
        result["errors"].extend({"path": "Каталог", "error": e} for e in self.state["issues"])
        return result

    def collect_stats(self, progress=lambda count: None, use_word=False):
        from .statistics import document_stats, word_stats
        self.scan(progress)
        for index, document in enumerate(self.state["documents"]):
            path = self.safe_path(document["path"])
            try:
                before = Path(native(path)).stat()
                result = word_stats(path) if use_word and path.suffix.lower() in (".doc", ".docx") else document_stats(path)
                after = Path(native(path)).stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    result = {"info": "Файл изменился во время подсчёта; повторите"}
                document["stats"] = result
            except Exception:
                document["stats"] = {"info": "Не удалось посчитать: файл недоступен, повреждён или защищён"}
            progress(index + 1)
        self.save()
