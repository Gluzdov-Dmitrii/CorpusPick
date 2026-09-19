"""Explicit path-length repair, with a durable intent for each rename."""
import os
from pathlib import Path


def units(text):
    return len(str(text).encode('utf-16-le')) // 2


def trim(text, limit):
    return text.encode('utf-16-le')[:max(0, limit) * 2].decode('utf-16-le', errors='ignore')


def recover(session):
    from .core import native
    change = session.state.get('path_repair')
    if not change:
        return
    source = session.safe_path(change['source'])
    target = session.safe_path(change['destination'])
    src, dst = Path(native(source)), Path(native(target))
    if src.exists():
        session.state.pop('path_repair', None)
        session.save()
        return
    if not dst.exists():
        raise ValueError('Не найден объект прерванного сокращения пути; изменения остановлены.')
    info = dst.stat()
    if [info.st_dev, info.st_ino] != change['identity']:
        raise ValueError('Не удалось подтвердить объект после сокращения пути; изменения остановлены.')
    for doc in session.state['documents']:
        old = Path(doc['path'])
        if old == Path(change['source']) or (change['directory'] and old.is_relative_to(change['source'])):
            tail = old.relative_to(change['source'])
            doc['path'] = str(Path(change['destination']) / tail)
    for field in ('directories',):
        session.state[field] = [str(Path(change['destination']) / Path(p).relative_to(change['source']))
            if Path(p).is_relative_to(change['source']) else p for p in session.state.get(field, [])]
    session.state.pop('path_repair', None)
    session.save()


def shorten_paths(session, progress=lambda count: None, limit=259):
    from .core import native, is_link, failure, progress_update
    session.require_write()
    session.cancel_pending()
    recover(session)
    session.scan(progress, hash_files=False)
    if session.cancel.is_set():
        return {'moved': 0, 'errors': [], 'stopped': True}
    session.export_structure(prepared=True)
    entries = []
    result = {'moved': 0, 'errors': [], 'stopped': False}
    def walk_error(exc):
        result['errors'].append({'path': '', 'error': failure(exc)})
    for base, dirs, files in os.walk(native(session.root), followlinks=False, onerror=walk_error):
        if session.cancel.is_set():
            result['stopped'] = True
            return result
        dirs[:] = [name for name in dirs if not is_link(Path(base) / name)]
        for name in dirs + files:
            path = Path(base) / name
            if not is_link(path):
                entries.append((str(path.relative_to(Path(native(session.root)))), name in dirs))
    # Work on the longest paths first; directory shortening benefits descendants.
    while not session.cancel.is_set():
        bad = [(p, d) for p, d in entries if units(session.root / p) > limit or
               any(units(part) > 255 for part in Path(p).parts)]
        if not bad:
            break
        relative, is_dir = max(bad, key=lambda item: units(session.root / item[0]))
        parts = Path(relative).parts
        excess = max(0, units(session.root / relative) - limit)
        choices = []
        for index, name in enumerate(parts):
            directory = index < len(parts) - 1 or is_dir
            suffix = '' if directory else Path(name).suffix
            capacity = units(name) - units(suffix) - 1
            if capacity > 0:
                choices.append((capacity, index, directory, suffix))
        if not choices:
            result['errors'].append({'path': relative, 'error': 'Путь нельзя сократить достаточно: выберите более короткий путь к корневому каталогу.'})
            entries.remove((relative, is_dir))
            continue
        _, index, directory, suffix = max(choices)
        source_rel = Path(*parts[:index + 1])
        source = session.safe_path(str(source_rel))
        name = source.name
        stem = name[:-len(suffix)] if suffix else name
        budget = min(255, units(name) - max(1, excess))
        budget = max(units(suffix) + 1, budget)
        target = None
        occupied = {p.name.casefold() for p in Path(native(source.parent)).iterdir()}
        for number in range(10000):
            tail = '' if number == 0 else '~' + str(number)
            room = budget - units(suffix) - units(tail)
            if room < 1:
                break
            candidate = trim(stem, room).rstrip(' .') + tail + suffix
            if not candidate or candidate.startswith('.'):
                continue
            if Path(candidate).stem.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}:
                continue
            if candidate.casefold() not in occupied:
                target = source.with_name(candidate)
                break
        if target is None:
            result['errors'].append({'path': relative, 'error': 'Не удалось подобрать короткое свободное имя.'})
            entries.remove((relative, is_dir))
            continue
        target_rel = target.relative_to(session.root)
        try:
            info = Path(native(source)).stat()
            session.state['path_repair'] = {'source': str(source_rel), 'destination': str(target_rel),
                'directory': directory, 'identity': [info.st_dev, info.st_ino]}
            session.save()
            # Windows rename refuses an existing target, including directories.
            if Path(native(target)).exists():
                raise FileExistsError()
            os.rename(native(source), native(target))
            recover(session)
            entries = [(str(target_rel / Path(p).relative_to(source_rel)), d)
                       if Path(p).is_relative_to(source_rel) else (p, d) for p, d in entries]
            result['moved'] += 1
            progress_update(progress, result['moved'], str(target_rel))
        except (OSError, ValueError) as exc:
            result['errors'].append({'path': relative, 'error': failure(exc) if isinstance(exc, OSError) else str(exc)})
            # Do not continue if a rename succeeded but saving its result failed.
            break
    result['stopped'] = session.cancel.is_set()
    session.export_structure(prepared=True)
    return result
