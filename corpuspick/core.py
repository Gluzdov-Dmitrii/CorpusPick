"""Local filesystem operations. Never log document paths or content."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import uuid
from collections import Counter
from .recycle import recycle_file


def is_link(path: Path) -> bool:
    info = Path(native(path)).lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_reparse_tag", 0) & 0x20000000
    )


class Cancelled(Exception):
    pass


def digest(path: Path, cancel=None) -> str:
    hasher = hashlib.sha256()
    with open(native(path), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
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
    def __init__(self, root: Path, data_root: Path | None = None, read_only=False):
        self.read_only = read_only
        self.cancel = threading.Event()
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
        self.journal_path = data_root / f"{key}.moves.jsonl"
        self.csv_path = data_root / f"{key}.structure.csv"
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
            self.replay_journal()
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
        self.state["directories"] = []
        def error(exc):
            self.state["issues"].append(failure(exc))
        for folder, dirs, files in os.walk(native(self.root), followlinks=False, onerror=error):
            if self.cancel.is_set():
                break
            relative_folder = str(Path(folder).relative_to(Path(native(self.root))))
            if relative_folder != ".":
                self.state["directories"].append(relative_folder)
            kept = []
            for name in dirs:
                try:
                    if not is_link(Path(folder) / name):
                        kept.append(name)
                except OSError as exc:
                    error(exc)
            dirs[:] = sorted(kept)
            for name in sorted(files):
                if self.cancel.is_set():
                    break
                path = Path(folder) / name
                try:
                    if not is_link(path) and path.is_file():
                        yield path, str(path.relative_to(Path(native(self.root))))
                except OSError as exc:
                    error(exc)

    def require_write(self):
        if self.read_only:
            raise ValueError('Каталог открыт как бэкап: изменение файлов отключено')

    def replay_journal(self):
        if not self.journal_path.exists():
            return
        moves = {m.get('id'): m for m in self.state['moves'] if m.get('id')}
        docs = {d['path']: d for d in self.state['documents']}
        with self.journal_path.open(encoding='utf-8') as stream:
            for line in stream:
                if not line.endswith('\n'):
                    break  # Incomplete final append after power loss.
                record = json.loads(line)
                move = moves.get(record['id'])
                if move:
                    move['done'] = True
                    d = docs.pop(move['source'], None)
                    if d:
                        d['path'] = move['destination']
                        d['origin'] = move['origin']
                        docs[d['path']] = d
        self.save()
        self.journal_path.unlink(missing_ok=True)

    def log_move(self, move):
        with self.journal_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'id': move['id']}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def regroup(self):
        counts = Counter(d['hash'] for d in self.state['documents'] if d.get('hash'))
        groups = {h: i + 1 for i, h in enumerate(sorted(h for h, n in counts.items() if n > 1))}
        for d in self.state['documents']:
            d['duplicate'] = groups.get(d.get('hash'), 0)

    def scan(self, progress=lambda count: None, hash_files=True):
        old = {d['path']: d for d in self.state['documents']}
        moved = {m['destination']: m for m in self.state['moves'] if m['done']}
        documents = []
        for path, relative in self.walk():
            if self.cancel.is_set():
                break
            previous = old.get(relative, {})
            item = {'path': relative, 'origin': relative, 'size': None, 'hash': '',
                    'note': '', 'error': '', 'stats': {}}
            try:
                before = path.stat()
                identity = [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns]
                item.update(size=before.st_size, identity=identity)
                if previous.get('identity') == identity:
                    item = dict(previous)
                    item['error'] = ''
                elif previous:
                    item['origin'] = previous.get('origin', relative)
                    item['origins'] = previous.get('origins', [item['origin']])
                movement = moved.get(relative)
                if movement and movement.get('identity') == identity:
                    item['origin'] = movement['origin']
                if hash_files and not item['hash']:
                    item['hash'] = digest(path, self.cancel)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        item['hash'] = ''
                        item['error'] = 'Файл изменился во время чтения'
                if movement and item.get('hash') and movement.get('hash') == item['hash']:
                    item['origin'] = movement['origin']
                if item.get('hash') and previous.get('hash') == item['hash']:
                    item['note'] = previous.get('note', 'Скорее да' if previous.get('score') == 2 else '')
                    item['stats'] = previous.get('stats', {})
            except Cancelled:
                documents.append(item)
                break
            except OSError as exc:
                item['error'] = failure(exc)
            documents.append(item)
            progress(len(documents))
        if self.cancel.is_set():
            visited = {d['path'] for d in documents}
            documents.extend(d for d in old.values() if d['path'] not in visited)
        self.state['scan_complete'] = not self.cancel.is_set()
        self.state['documents'] = documents
        self.regroup()
        self.save()
        return documents

    def analyze(self, progress=lambda count: None, selected=None, hashes=True):
        from .stats_worker import StatsWorker
        last_save = time.monotonic()
        documents = [d for d in self.state['documents'] if selected is None or d['path'] in selected]
        with StatsWorker() as worker:
            for index, d in enumerate(documents):
                if self.cancel.is_set():
                    break
                try:
                    path = self.safe_path(d['path'])
                    if hashes and not d.get('hash'):
                        d['hash'] = digest(path, self.cancel)
                    stats = d.get('stats', {})
                    if not stats or stats.get('schema') != 2 or stats.get('failed'):
                        if path.suffix.lower() in ('.doc', '.docx', '.pdf') and not path.name.startswith('~$'):
                            d['stats'] = worker.count(path, self.cancel)
                        else:
                            d['stats'] = {'info': 'Подсчёт для этого формата не поддерживается'}
                        d['stats']['schema'] = 2
                    info = Path(native(path)).stat()
                    if d.get('identity') != [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]:
                        d.update(hash='', stats={}, error='Файл изменился: обновите список')
                except Cancelled:
                    break
                except OSError as exc:
                    d['error'] = failure(exc)
                except Exception:
                    d['stats'] = {'schema': 2, 'failed': True, 'info': 'Не удалось запустить или выполнить подсчёт'}
                progress(index + 1)
                if time.monotonic() - last_save > 5:
                    self.save()
                    last_save = time.monotonic()
        self.regroup()
        self.save()

    def open_catalog(self, progress=lambda count: None):
        if not self.read_only and os.name == 'nt':
            self.state['unlock_result'] = self.unlock(progress)
        self.scan(progress, hash_files=False)
        progress(0)

    def export_structure(self, destination=None, progress=lambda count: None, prepared=False):
        from .manifest import write_manifest
        path = Path(destination) if destination else self.csv_path
        if path.resolve().is_relative_to(self.root):
            raise ValueError('Сохраните CSV вне каталога документов, чтобы не менять бэкап и не включать CSV в корпус')
        if not prepared:
            self.scan(progress)
        if self.cancel.is_set() and not prepared:
            return None
        directories = set(self.state.get('original_directories', [])) | set(self.state.get('directories', []))
        for d in self.state['documents']:
            for origin in d.get('origins', [d['origin']]):
                directories.update(str(p) for p in Path(origin).parents if p != Path('.'))
        write_manifest(path, self.state['documents'], directories)
        return str(path)

    def import_structure(self, path, progress=lambda count: None):
        from .manifest import read_manifest
        rows, directories = read_manifest(path)
        self.scan(progress)
        if self.cancel.is_set():
            return {'matched': 0, 'ambiguous': 0, 'unmatched': 0, 'stopped': True}
        groups = {}
        for row in rows:
            if row['hash']:
                groups.setdefault((row['hash'], row['size']), set()).update(row['origins'])
        result = {'matched': 0, 'ambiguous': 0, 'unmatched': 0}
        for d in self.state['documents']:
            origins = sorted(groups.get((d.get('hash'), d.get('size')), []))
            if origins:
                d['origins'] = origins
                d['origin'] = origins[0] if len(origins) == 1 else d['path']
                d['origin_match'] = 'SHA-256' if len(origins) == 1 else 'Несколько исходных путей с одинаковым SHA-256'
                result['matched'] += 1
                result['ambiguous'] += len(origins) > 1
            else:
                result['unmatched'] += 1
        self.state['original_directories'] = directories
        self.state['imported_manifest'] = str(Path(path).resolve())
        self.save()
        return result

    def mark(self, relative):
        item = next(d for d in self.state["documents"] if d["path"] == relative)
        item["note"] = "" if item.get("note") else "Скорее да"
        self.save()

    def clear_similarity_cache(self):
        for document in self.state['documents']:
            document.pop('content', None)
        self.save()

    def group_similar(self, progress=lambda count: None):
        from .content_similarity import VERSION, content_worker, content_order, size_only
        from .stats_worker import StatsWorker
        self.scan(progress, hash_files=False)
        last_save = time.monotonic()
        with StatsWorker(timeout=90, target=content_worker, retry_label='По похожести') as worker:
            for index, document in enumerate(self.state['documents']):
                if self.cancel.is_set():
                    break
                signature = document.get('content', {})
                try:
                    path = self.checked_document(document)
                    if not size_only(document) and (signature.get('version') != VERSION or signature.get('failed') or signature.get('text_failed')):
                        signature = worker.count(path, self.cancel)
                        self.checked_document(document)
                        document['content'] = signature
                        if signature.get('sha256'):
                            document['hash'] = signature['sha256']
                except Cancelled:
                    break
                except (OSError, ValueError):
                    document.update(content={'failed': True, 'info': 'Файл недоступен или изменился во время анализа'}, hash='')
                progress(index + 1)
                if time.monotonic() - last_save > 5:
                    self.save()
                    last_save = time.monotonic()
        self.regroup()
        self.save()
        if self.cancel.is_set():
            return None
        return content_order(self.state['documents'], self.cancel, progress)

    def duplicate_plan(self, progress=lambda count: None):
        self.scan(progress)
        groups = {}
        for document in self.state['documents']:
            if document.get('hash') and not document.get('error'):
                groups.setdefault(document['hash'], []).append(document)
        plan = []
        for documents in groups.values():
            ordered = sorted(documents, key=lambda d: (len(Path(d['path']).parts), d['path'].casefold(), d['path']))
            plan.extend({'keep': dict(ordered[0]), 'remove': dict(d)} for d in ordered[1:])
        return plan

    def checked_document(self, document, verify_hash=False):
        path = Path(native(self.safe_path(document['path'])))
        info = path.stat()
        identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
        if not path.is_file() or not document.get('identity') or identity != document['identity']:
            raise ValueError('Файл изменился после обновления списка; нажмите F5 и повторите')
        if verify_hash and (not document.get('hash') or digest(path, self.cancel) != document['hash']):
            raise ValueError('Содержимое изменилось: удаление дубля отменено')
        return self.safe_path(document['path'])

    def trash_documents(self, documents=None, duplicate_plan=None, progress=lambda count: None):
        self.require_write()
        if any(not m['done'] for m in self.state['moves']):
            raise ValueError('Сначала завершите перенос или нажмите «Сбросить план»')
        result = {'trashed': 0, 'errors': []}
        removed_paths = set()
        entries = duplicate_plan if duplicate_plan is not None else [{'remove': d} for d in documents or []]
        for index, entry in enumerate(entries):
            if self.cancel.is_set():
                break
            document = entry['remove']
            try:
                if 'keep' in entry:
                    if entry['keep']['path'] == document['path'] or entry['keep'].get('hash') != document.get('hash'):
                        raise ValueError('Некорректный план дублей: файл сохранён')
                    self.checked_document(entry['keep'], verify_hash=True)
                path = self.checked_document(document, verify_hash='keep' in entry)
                recycle_file(path)
                result['trashed'] += 1
                removed_paths.add(document['path'])
            except Cancelled:
                break
            except (OSError, ValueError) as exc:
                result['errors'].append({'path': document['path'],
                                        'error': str(exc) if isinstance(exc, ValueError) else failure(exc)})
            progress(index + 1)
        self.state['documents'] = [d for d in self.state['documents'] if d['path'] not in removed_paths]
        self.scan(progress)
        return result

    def trim_prefix(self, documents, count, progress=lambda count: None):
        self.require_write()
        if any(not m['done'] for m in self.state['moves']):
            raise ValueError('Сначала завершите перенос или нажмите «Сбросить план»')
        if type(count) is not int or count < 1:
            raise ValueError('Введите целое число символов больше нуля')
        pending, errors = [], []
        reserved = set()
        for document in documents:
            try:
                source = self.checked_document(document)
                if len(source.stem) <= count:
                    raise ValueError('После удаления префикса имя станет пустым')
                name = source.stem[count:] + source.suffix
                stem = Path(name).stem
                if name.startswith('.') or name.endswith((' ', '.')) or stem.upper().split('.')[0] in {
                    'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)),
                    *('LPT' + str(i) for i in range(1, 10))}:
                    raise ValueError('Недопустимое новое имя Windows')
                destination = source.with_name(name)
                if Path(native(destination)).exists() or str(destination).casefold() in reserved:
                    raise ValueError('Такое имя уже существует; файл пропущен')
                reserved.add(str(destination).casefold())
                pending.append({'source': document['path'], 'destination': str(destination.relative_to(self.root)),
                                'origin': document['origin'], 'hash': document.get('hash', ''),
                                'identity': document['identity'], 'method': 'rename', 'done': False})
            except (OSError, ValueError) as exc:
                errors.append({'path': document['path'], 'error': str(exc) if isinstance(exc, ValueError) else failure(exc)})
        if not pending:
            return {'moved': 0, 'errors': errors}
        self.state['moves'].extend(pending)
        self.save()
        result = self.flatten(progress)
        result['errors'] = errors + result['errors']
        return result

    def flatten(self, progress=lambda count: None):
        self.require_write()
        pending = [m for m in self.state["moves"] if not m["done"]]
        if not pending:
            # Explorer may have changed the folder since the last scan.
            self.scan(progress)
            if self.cancel.is_set():
                return {"moved": 0, "errors": [], "stopped": True}
            self.state["original_directories"] = sorted(set(self.state.get("original_directories", [])) | set(self.state.get("directories", [])))
            occupied = {p.name.casefold() for p in Path(native(self.root)).iterdir()}
            suffixes = {}
            for document in self.state["documents"]:
                source = Path(document["path"])
                if source.parent == Path("."):
                    continue
                name, number = source.name, suffixes.get(source.name.casefold(), 1)
                while name.casefold() in occupied:
                    tail = f"__{number}{source.suffix}"
                    name = source.stem[:max(1, 240 - len(tail))] + tail
                    number += 1
                suffixes[source.name.casefold()] = number
                occupied.add(name.casefold())
                pending.append({"source": str(source), "destination": name, "origin": document["origin"],
                                "hash": document["hash"], "identity": document.get("identity"),
                                "method": "rename", "done": False})
            self.state["moves"].extend(pending)
            self.save()
        for move in pending:
            move.setdefault('id', uuid.uuid4().hex)
        self.save()
        self.export_structure(prepared=True)
        documents_by_path = {d['path']: d for d in self.state['documents']}
        result = {"moved": 0, "errors": []}
        for index, move in enumerate(pending):
            if self.cancel.is_set():
                break
            try:
                source = self.safe_path(move["source"])
                destination = self.safe_path(move["destination"])
                src, dst = Path(native(source)), Path(native(destination))
                if src.exists():
                    if move.get("method") != "rename":
                        # Recover journals made by the first hard-link version.
                        if digest(src) != move["hash"]:
                            raise ValueError("Файл изменён; нажмите «Сбросить план»")
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
                            raise ValueError("Файл изменён; нажмите «Сбросить план»")
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
                document = documents_by_path.pop(move['source'], None)
                if document:
                    document['path'] = move['destination']
                    document['origin'] = move['origin']
                    documents_by_path[document['path']] = document
                result["moved"] += 1
            except (OSError, ValueError) as exc:
                move["error"] = str(exc) if isinstance(exc, ValueError) else failure(exc)
                result["errors"].append({"path": move["source"], "error": move["error"]})
            if move["done"]:
                self.log_move(move)
            progress(index + 1)
        self.save()
        self.journal_path.unlink(missing_ok=True)
        self.export_structure(prepared=True)
        result['csv'] = str(self.csv_path)
        result['stopped'] = self.cancel.is_set()
        return result

    def remove_empty_directories(self):
        self.require_write()
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
            if self.cancel.is_set():
                break
            try:
                folder.rmdir()
                removed += 1
            except OSError as exc:
                if getattr(exc, "winerror", None) != 145 and exc.errno not in (39, 17):
                    errors.append(failure(exc))
        return {"removed": removed, "errors": errors}

    def cancel_pending(self):
        self.require_write()
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
        self.require_write()
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

    def collect_stats(self, progress=lambda count: None, use_word=False, selected=None):
        from .statistics import word_stats
        if use_word and self.read_only:
            raise ValueError('В режиме бэкапа доступен только быстрый подсчёт без запуска Word')
        self.scan(progress, hash_files=False)
        if not use_word:
            return self.analyze(progress, selected=selected, hashes=False)
        for index, d in enumerate(self.state['documents']):
            if self.cancel.is_set():
                break
            if selected is not None and d['path'] not in selected:
                continue
            if Path(d['path']).suffix.lower() not in ('.doc', '.docx'):
                continue
            d['stats'] = word_stats(self.safe_path(d['path']))
            d['stats']['schema'] = 2
            progress(index + 1)
        self.save()
