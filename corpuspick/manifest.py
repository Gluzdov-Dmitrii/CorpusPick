"""Portable origin manifest. Imported paths are labels, never action targets."""
import csv
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile

FIELDS = ['schema', 'kind', 'current_path', 'original_path', 'size', 'sha256', 'origin_candidates']


def safe_label(value):
    value = str(value)
    p = PureWindowsPath(value)
    if not value or p.is_absolute() or p.drive or '..' in p.parts or ':' in value or '\x00' in value:
        raise ValueError('CSV содержит недопустимый относительный путь')
    return str(Path(*p.parts))


def encode_cell(value):
    value = str(value)
    return "'" + value if value.startswith("'") or value.lstrip().startswith(('=', '+', '-', '@')) else value


def decode_cell(value):
    return value[1:] if value.startswith("'") else value


def write_manifest(path, documents, directories):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, FIELDS, delimiter=';')
            writer.writeheader()
            for directory in sorted(set(directories)):
                writer.writerow({'schema': 'corpuspick-1', 'kind': 'directory', 'original_path': encode_cell(directory)})
            for d in documents:
                writer.writerow({'schema': 'corpuspick-1', 'kind': 'file', 'current_path': encode_cell(d['path']),
                    'original_path': encode_cell(d.get('origin', d['path'])), 'size': d.get('size'), 'sha256': d.get('hash', ''),
                    'origin_candidates': json.dumps(d.get('origins', [d.get('origin', d['path'])]), ensure_ascii=False)})
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_manifest(path):
    rows, directories = [], []
    csv.field_size_limit(16 * 1024 * 1024)
    with open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter=';')
        if not set(FIELDS).issubset(reader.fieldnames or []):
            raise ValueError('Нужен CSV структуры, сохранённый CorpusPick')
        for row in reader:
            if row['schema'] != 'corpuspick-1':
                raise ValueError('Неизвестная версия CSV')
            origin = safe_label(decode_cell(row['original_path']))
            if row['kind'] == 'directory':
                directories.append(origin)
                continue
            if row['kind'] != 'file':
                raise ValueError('Неизвестная строка CSV')
            hash_value = row['sha256']
            if hash_value and not re.fullmatch('[0-9a-f]{64}', hash_value):
                raise ValueError('Некорректный SHA-256 в CSV')
            try:
                size = int(row['size']) if row['size'] else None
                origins = json.loads(row['origin_candidates'] or '[]')
                if not isinstance(origins, list) or any(not isinstance(v, str) for v in origins):
                    raise ValueError()
                origins = [safe_label(v) for v in origins] or [origin]
                if size is not None and size < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('Некорректные данные структуры CSV') from None
            rows.append({'hash': hash_value, 'size': size, 'origins': origins})
    return rows, directories
