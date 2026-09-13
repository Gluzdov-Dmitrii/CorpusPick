"""Read-only content fingerprints. No extracted text leaves the parser process."""
from collections import defaultdict
import hashlib
import heapq
import logging
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from zipfile import ZipFile

VERSION = 1
LIMIT = 256
MAX_CONTENT_BYTES = 128 * 1024 * 1024


def size_only(document):
    return document.get('size', 0) > MAX_CONTENT_BYTES


GEAR = [int.from_bytes(hashlib.blake2b(bytes([i]), digest_size=8).digest(), 'little') for i in range(256)]

GEAR_LOW = [v & 1023 for v in GEAR]


class Sketch:
    def __init__(self):
        self.values, self.heap = set(), []

    def add(self, data):
        value = int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), 'big')
        if value in self.values:
            return
        if len(self.heap) < LIMIT:
            self.values.add(value)
            heapq.heappush(self.heap, -value)
        elif value < -self.heap[0]:
            self.values.remove(-heapq.heapreplace(self.heap, -value))
            self.values.add(value)

    def result(self):
        return [f'{v:016x}' for v in sorted(self.values)]


def _chunk_end(data, start, end):
    if end - start < 512:
        return end
    # Only the low ten bits determine a boundary. Earlier bytes vanish after
    # ten left shifts, so bytes 0..501 need not enter the rolling calculation.
    gear = GEAR_LOW
    rolling = 0
    for byte in data[start + 502:start + 512]:
        rolling = ((rolling << 1) + gear[byte]) & 1023
    if rolling == 0:
        return start + 512
    for position in range(start + 512, end):
        rolling = ((rolling << 1) + gear[data[position]]) & 1023
        if rolling == 0:
            return position + 1
    return end


def binary_fingerprint(path):
    sha, sketch = hashlib.sha256(), Sketch()
    tail, small, size = b'', bytearray(), 0
    with open(path, 'rb') as stream:
        while block := stream.read(1024 * 1024):
            sha.update(block)
            size += len(block)
            if size <= 32768:
                small.extend(block)
            else:
                small.clear()
            data, start = tail + block, 0
            while len(data) - start >= 8192:
                end = _chunk_end(data, start, start + 8192)
                sketch.add(data[start:end])
                start = end
            tail = data[start:]
    start = 0
    while start < len(tail):
        end = _chunk_end(tail, start, min(start + 8192, len(tail)))
        sketch.add(tail[start:end])
        start = end
    chunks = sketch.result()
    method = 'chunks'
    if 16 <= size <= 32768:
        method, sketch = 'shingles16', Sketch()
        for i in range(len(small) - 15):
            sketch.add(small[i:i+16])
    return {'sha256': sha.hexdigest(), 'bytes': size, 'binary': sketch.result(), 'binary_method': method, 'chunks': chunks}


def text_fingerprint(parts):
    sketch, sha = Sketch(), hashlib.sha256()
    window, count = [], 0
    for part in parts:
        normalized = unicodedata.normalize('NFKC', part).casefold().replace('ё', 'е')
        for word in re.findall(r'[^\W_]+', normalized):
            encoded = word.encode('utf-8')
            sha.update(encoded + b'\0')
            count += 1
            window.append(encoded)
            if len(window) > 5:
                window.pop(0)
            if len(window) == 5:
                sketch.add(b'\0'.join(window))
    return {'text': sketch.result(), 'text_sha256': sha.hexdigest(), 'words': count}


def text_parts(path):
    suffix = path.suffix.lower()
    if suffix == '.docx':
        from .statistics import xml_part, W
        with ZipFile(path) as archive:
            names = ['word/document.xml'] + sorted(n for n in archive.namelist() if
                re.fullmatch(r'word/(header\d+|footer\d+|footnotes|endnotes)\.xml', n))
            for name in names:
                root = xml_part(archive, name)
                for paragraph in root.iter(W + 'p'):
                    yield ''.join(t.text or '' for t in paragraph.iter(W + 't'))
    elif suffix == '.pdf':
        from pypdf import PdfReader
        logging.getLogger('pypdf').setLevel(logging.CRITICAL)
        with open(path, 'rb') as stream:
            reader = PdfReader(stream, strict=False)
            if reader.is_encrypted and not reader.decrypt(''):
                raise ValueError('Encrypted')
            for page in reader.pages:
                yield page.extract_text() or ''
    elif suffix in ('.txt', '.md', '.csv', '.tsv'):
        # Explicit Unicode/BOM detection, then legacy Russian text. No lossy decoding.
        with open(path, 'rb') as stream:
            prefix = stream.read(4)
        encoding = 'utf-16' if prefix.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
        try:
            with open(path, encoding=encoding) as stream:
                for line in stream:
                    yield line
        except UnicodeError:
            # Restart must occur outside the generator to avoid mixing partial decodings.
            raise


def fingerprint(path):
    from .core import native
    path = Path(native(Path(path)))
    result = binary_fingerprint(path)
    result.update(version=VERSION, info='Байты прочитаны полностью; текст для этого формата не извлекается')
    if path.suffix.lower() in ('.docx', '.pdf', '.txt', '.md', '.csv', '.tsv'):
        try:
            try:
                text = text_fingerprint(text_parts(path))
            except UnicodeError:
                if path.suffix.lower() not in ('.txt', '.md', '.csv', '.tsv'):
                    raise
                with open(path, encoding='cp1251') as stream:
                    text = text_fingerprint(stream)
            result.update(text)
            result['info'] = ('Байты и извлечённый текст; изображения и вёрстка текстом не сравниваются'
                              if text['words'] >= 10 else 'Байты прочитаны; недостаточно текста для сравнения (OCR не выполнялся)')
        except Exception:
            result['text_failed'] = True
            result['info'] = 'Байты прочитаны; текст извлечь не удалось'
    return result


def content_worker(connection):
    with open(os.devnull, 'w') as quiet:
        sys.stdout = sys.stderr = quiet
        try:
            while True:
                path = connection.recv()
                try:
                    result = fingerprint(path)
                except Exception:
                    result = {'failed': True, 'info': 'Не удалось прочитать содержимое файла'}
                connection.send(result)
        except (EOFError, BrokenPipeError):
            pass
        finally:
            connection.close()


def overlap(a, b):
    a, b = set(a), set(b)
    sampled = sorted(a | b)[:LIMIT]
    if len(sampled) < 8:
        return 0.0
    return sum(v in a and v in b for v in sampled) / len(sampled)


def content_match(a, b):
    if not a or not b or a.get('failed') or b.get('failed'):
        return None
    if a.get('sha256') and a['sha256'] == b.get('sha256'):
        return 1.01, 'Точные байты (SHA-256)'
    matches = []
    if min(a.get('words', 0), b.get('words', 0)) >= 10:
        ratio = min(a['words'], b['words']) / max(a['words'], b['words'])
        score = 1.0 if a['text_sha256'] == b['text_sha256'] else overlap(a.get('text', []), b.get('text', []))
        if score >= .82 and ratio >= .8:
            matches.append((score, f'Близкий текст: ≈{score:.0%} общих фрагментов; изображения не проверены'))
    if max(a.get('bytes', 0), b.get('bytes', 0)):
        score = overlap(a.get('chunks', []), b.get('chunks', []))
        if a.get('binary_method') == b.get('binary_method'):
            score = max(score, overlap(a.get('binary', []), b.get('binary', [])))
        ratio = min(a['bytes'], b['bytes']) / max(a['bytes'], b['bytes'])
        if score >= .85 and ratio >= .8:
            matches.append((score, f'Близкие байты: ≈{score:.0%} общих фрагментов'))
    return max(matches, default=None)


def evidence(a, b):
    match = content_match(a, b)
    return match[1] if match else None


def fingerprint_key(fp):
    # Only content-derived fields determine processing order / identical units.
    fields = ('sha256', 'text_sha256', 'words', 'bytes', 'binary_method', 'binary', 'chunks', 'text')
    return json.dumps({k: fp.get(k) for k in fields}, sort_keys=True, separators=(',', ':'))


def content_order(documents, cancel, progress=lambda count: None):
    # Collapse identical fingerprints, but never collapse unknown/failed files.
    buckets, unknown, large = {}, [], {}
    for document in documents:
        if cancel.is_set():
            return None
        if size_only(document):
            large.setdefault(Path(document['path']).suffix.casefold(), []).append(document)
            continue
        fp = document.get('content', {})
        if fp.get('failed') or not fp.get('sha256'):
            unknown.append(document)
        else:
            buckets.setdefault(fingerprint_key(fp), []).append(document)
    units = [buckets[key] for key in sorted(buckets)]
    fingerprints = [unit[0]['content'] for unit in units]
    postings, adjacency, heap = defaultdict(list), {i: {} for i in range(len(units))}, []
    notes = {}
    for i, fp in enumerate(fingerprints):
        if cancel.is_set():
            return None
        keys = [('b', h) for h in fp.get('binary', [])] + [('t', h) for h in fp.get('text', [])]
        keys += [('c', h) for h in fp.get('chunks', [])]
        keys += [(k, fp[k]) for k in ('sha256', 'text_sha256') if fp.get(k) and (k != 'text_sha256' or fp.get('words', 0) >= 10)]
        candidates = set()
        for key in keys:
            candidates.update(postings[key])
        for j in sorted(candidates):
            if cancel.is_set():
                return None
            match = content_match(fp, fingerprints[j])
            if match:
                score = match[0]
                adjacency[i][j] = adjacency[j][i] = score
                heapq.heappush(heap, (-score, j, i, 0, 0))
        for key in set(keys):
            postings[key].append(i)
        progress(i + 1)
    # Agglomerative complete-link: after merging, only common neighbours remain;
    # their similarity is the minimum across the two former clusters.
    members = {i: [i] for i in range(len(units))}
    generations = [0] * len(units)
    merged = 0
    while heap:
        if cancel.is_set():
            return None
        negative, a, b, va, vb = heapq.heappop(heap)
        if a not in members or b not in members or generations[a] != va or generations[b] != vb:
            continue
        common = adjacency[a].keys() & adjacency[b].keys()
        updated = {c: min(adjacency[a][c], adjacency[b][c]) for c in common}
        for c in adjacency[a].keys() | adjacency[b].keys():
            adjacency[c].pop(a, None)
            adjacency[c].pop(b, None)
        adjacency[a] = {}
        adjacency.pop(b)
        members[a].extend(members.pop(b))
        generations[a] += 1
        for c, score in updated.items():
            adjacency[a][c] = adjacency[c][a] = score
            left, right = sorted((a, c))
            heapq.heappush(heap, (-score, left, right, generations[left], generations[right]))
        merged += 1
        progress(len(units) + merged)
    ranks, groups = {}, {}
    for group, indices in enumerate(sorted(members.values(), key=min)):
        indices = sorted(indices)
        representative = fingerprints[indices[0]]
        for i in sorted(indices):
            note = evidence(fingerprints[i], representative) if len(indices) > 1 or len(units[i]) > 1 else None
            if i == indices[0] and len(indices) > 1:
                note = 'Представитель группы: ' + evidence(representative, fingerprints[indices[1]])
            for document in units[i]:
                path = document['path']
                ranks[path], groups[path] = len(ranks), group
                if note:
                    notes[path] = note if i == indices[0] else note + ' (с представителем группы)'
    for offset, document in enumerate(unknown):
        path = document['path']
        ranks[path], groups[path] = len(ranks), len(members) + offset
        notes[path] = 'Содержимое не проанализировано; оставлен отдельно'
    group = len(members) + len(unknown) - 1
    for extension in sorted(large):
        anchor = None
        for document in sorted(large[extension], key=lambda d: d['size']):
            if cancel.is_set():
                return None
            size = document['size']
            if anchor is None or anchor / size < .95:
                group += 1
                anchor = size
            path = document['path']
            ranks[path], groups[path] = len(ranks), group
            notes[path] = 'Только размер и расширение: разброс до 5%; содержимое не проверялось в этом проходе (файл >128 МиБ). Не подтверждённые дубли.'
    return ranks, groups, notes
