"""Read-only content fingerprints. No extracted text leaves the parser process."""
from collections import defaultdict
from functools import lru_cache
import hashlib
import heapq
import logging
import os
from pathlib import Path
import re
import sys
import unicodedata
from zipfile import ZipFile

VERSION = 1
LIMIT = 256
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


def evidence(a, b):
    if not a or not b or a.get('failed') or b.get('failed'):
        return None
    if a.get('sha256') and a['sha256'] == b.get('sha256'):
        return 'Точные байты (SHA-256)'
    if min(a.get('words', 0), b.get('words', 0)) >= 10:
        ratio = min(a['words'], b['words']) / max(a['words'], b['words'])
        score = 1.0 if a['text_sha256'] == b['text_sha256'] else overlap(a.get('text', []), b.get('text', []))
        if score >= .82 and ratio >= .8:
            return f'Близкий текст: ≈{score:.0%} общих фрагментов; изображения не проверены'
    if max(a.get('bytes', 0), b.get('bytes', 0)):
        score = overlap(a.get('chunks', []), b.get('chunks', []))
        if a.get('binary_method') == b.get('binary_method'):
            score = max(score, overlap(a.get('binary', []), b.get('binary', [])))
        ratio = min(a['bytes'], b['bytes']) / max(a['bytes'], b['bytes'])
        if score >= .85 and ratio >= .8:
            return f'Близкие байты: ≈{score:.0%} общих фрагментов'
    return None


def content_order(documents, cancel, progress=lambda count: None):
    from .similarity import similar_order, name_key
    baseline = similar_order(documents, cancel, with_groups=True)
    if baseline is None:
        return None
    _, name_groups = baseline
    ordered = sorted(documents, key=lambda d: (name_key(d['path']), d.get('size') or 0, d['path']))
    postings, by_name, clusters, assigned = defaultdict(list), defaultdict(list), [], {}
    notes = {}
    @lru_cache(maxsize=8192)
    def pair(i, j):
        a, b = ordered[i].get('content', {}), ordered[j].get('content', {})
        found = evidence(a, b)
        if found:
            return found
        # Readable conflicting texts override the weak filename fallback.
        if min(a.get('words', 0), b.get('words', 0)) >= 10:
            return None
        if name_groups[ordered[i]['path']] == name_groups[ordered[j]['path']]:
            return 'Похожее название; содержимое не подтвердило совпадение'
        return None
    for i, document in enumerate(ordered):
        if cancel.is_set():
            return None
        fp = document.get('content', {})
        keys = [('b', h) for h in fp.get('binary', [])] + [('t', h) for h in fp.get('text', [])]
        keys += [('c', h) for h in fp.get('chunks', [])]
        keys += [(k, fp[k]) for k in ('sha256', 'text_sha256') if fp.get(k) and (k != 'text_sha256' or fp.get('words', 0) >= 10)]
        candidates = set(by_name[name_groups[document['path']]][:128])
        for key in keys:
            candidates.update(postings[key][:256])
        options = sorted({assigned[j] for j in candidates}, key=lambda g: (not bool(evidence(fp, ordered[clusters[g][0]].get('content', {}))), g))
        chosen = None
        for g in options:
            compatible = True
            for j in clusters[g]:
                if cancel.is_set():
                    return None
                if not pair(i, j):
                    compatible = False
                    break
            if compatible:
                chosen = g
                notes[document['path']] = pair(i, clusters[g][0])
                notes.setdefault(ordered[clusters[g][0]]['path'], notes[document['path']])
                break
        if chosen is None:
            chosen = len(clusters)
            clusters.append([])
        clusters[chosen].append(i)
        assigned[i] = chosen
        by_name[name_groups[document['path']]].append(i)
        for key in keys:
            if len(postings[key]) < 256:
                postings[key].append(i)
        progress(i + 1)
    ranks, groups = {}, {}
    for group, members in enumerate(clusters):
        for i in members:
            path = ordered[i]['path']
            ranks[path], groups[path] = len(ranks), group
    return ranks, groups, notes
