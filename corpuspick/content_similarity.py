"""Read-only content fingerprints. No extracted text leaves the parser process."""
from collections import defaultdict
import base64
import hashlib
import heapq
import logging
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
import zlib
from zipfile import ZipFile

VERSION = 1
TOPIC_VERSION = 1
LIMIT = 256
TOPIC_DIMENSIONS = 8192
TOPIC_WORD_LIMIT = 3000
MAX_CONTENT_BYTES = 128 * 1024 * 1024
TOPIC_SUFFIXES = ('.docx', '.pdf', '.txt', '.md', '.csv', '.tsv')


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


def _topic_add(counts, feature):
    digest = hashlib.blake2b(feature, digest_size=4, person=b'CorpusT').digest()
    index = int.from_bytes(digest, 'little') % TOPIC_DIMENSIONS
    if counts[index] < 65535:
        counts[index] += 1


def _packed_topic(counts):
    import array
    packed = array.array('H', counts)
    if sys.byteorder != 'little':
        packed.byteswap()
    return base64.b64encode(zlib.compress(packed.tobytes(), level=6)).decode('ascii')


def _topic_result(counts, words):
    return {'topic_version': TOPIC_VERSION, 'topic_words': words, 'topic': _packed_topic(counts)}


def text_fingerprint(parts, topic_only=False):
    sketch, sha = Sketch(), hashlib.sha256()
    window, count, topic_words, previous = [], 0, 0, None
    counts = [0] * TOPIC_DIMENSIONS
    iterator = iter(parts)
    try:
        for part in iterator:
            normalized = unicodedata.normalize('NFKC', part).casefold().replace('ё', 'е')
            for word in re.findall(r'[^\W_]+', normalized):
                encoded = word.encode('utf-8')
                if topic_words < TOPIC_WORD_LIMIT:
                    _topic_add(counts, b'u\0' + encoded)
                    if previous is not None:
                        _topic_add(counts, b'b\0' + previous + b'\0' + encoded)
                    if len(word) >= 4:
                        _topic_add(counts, b'p\0' + word[:3].encode('utf-8'))
                    previous = encoded
                    topic_words += 1
                if topic_only:
                    if topic_words >= TOPIC_WORD_LIMIT:
                        return _topic_result(counts, topic_words)
                    continue
                sha.update(encoded + b'\0')
                count += 1
                window.append(encoded)
                if len(window) > 5:
                    window.pop(0)
                if len(window) == 5:
                    sketch.add(b'\0'.join(window))
    finally:
        close = getattr(iterator, 'close', None)
        if close:
            close()
    topic = _topic_result(counts, topic_words)
    if topic_only:
        return topic
    topic.update(text=sketch.result(), text_sha256=sha.hexdigest(), words=count)
    return topic


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


def extracted_fingerprint(path, topic_only=False):
    try:
        return text_fingerprint(text_parts(path), topic_only=topic_only)
    except UnicodeError:
        if path.suffix.lower() not in ('.txt', '.md', '.csv', '.tsv'):
            raise
        with open(path, encoding='cp1251') as stream:
            return text_fingerprint(stream, topic_only=topic_only)


def fingerprint(path):
    from .core import native
    path = Path(native(Path(path)))
    result = binary_fingerprint(path)
    result.update(version=VERSION, info='Байты прочитаны полностью; текст для этого формата не извлекается')
    if path.suffix.lower() in TOPIC_SUFFIXES:
        try:
            text = extracted_fingerprint(path)
            result.update(text)
            result['info'] = ('Байты и извлечённый текст; изображения и вёрстка текстом не сравниваются'
                              if text['words'] >= 10 else 'Байты прочитаны; недостаточно текста для сравнения (OCR не выполнялся)')
        except Exception:
            result['text_failed'] = True
            result['info'] = 'Байты прочитаны; текст извлечь не удалось'
    return result


def topic_worker(connection):
    """Extract only a bounded, hashed topic vector; no source text crosses IPC."""
    from .core import native
    with open(os.devnull, 'w') as quiet:
        sys.stdout = sys.stderr = quiet
        try:
            while True:
                path = connection.recv()
                try:
                    source = Path(native(Path(path)))
                    if source.suffix.lower() not in TOPIC_SUFFIXES:
                        result = {'topic_version': TOPIC_VERSION, 'topic_words': 0, 'topic': ''}
                    else:
                        result = extracted_fingerprint(source, topic_only=True)
                except Exception:
                    result = {'topic_version': TOPIC_VERSION, 'topic_failed': True,
                              'info': 'Тематические признаки извлечь не удалось'}
                connection.send(result)
        except (EOFError, BrokenPipeError):
            pass
        finally:
            connection.close()


def content_worker(connection):
    with open(os.devnull, 'w') as quiet:
        sys.stdout = sys.stderr = quiet
        try:
            while True:
                path = connection.recv()
                try:
                    result = fingerprint(path)
                except Exception:
                    result = {'version': VERSION, 'failed': True,
                              'info': 'Не удалось прочитать содержимое файла; повтор — после сброса кэша'}
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


def semantic_layout(documents, cancel):
    """Cluster bounded hashed text features with TF-IDF, LSA and HDBSCAN."""
    candidates = [document for document in documents
                  if not size_only(document)
                  and document.get('content', {}).get('topic_version') == TOPIC_VERSION
                  and document.get('content', {}).get('topic_words', 0) >= 20
                  and document.get('content', {}).get('topic')]
    if len(candidates) < 5 or cancel.is_set():
        return {}, {'points': [], 'clusters': 0, 'clustered': 0, 'eligible': len(candidates)}
    try:
        import numpy as np
        from sklearn.cluster import HDBSCAN
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfTransformer
        from sklearn.metrics import silhouette_score
        from sklearn.preprocessing import normalize

        # Exact topic vectors must not distort density merely because copies exist.
        vectors = {}
        for document in candidates:
            signature = document['content']
            vectors.setdefault((signature['topic'], signature['topic_words']), []).append(document)
        keys = sorted(vectors)
        rows = []
        for encoded, _ in keys:
            raw = zlib.decompress(base64.b64decode(encoded))
            row = np.frombuffer(raw, dtype='<u2')
            if len(row) != TOPIC_DIMENSIONS:
                raise ValueError('Invalid topic vector')
            rows.append(row)
        if len(rows) < 5:
            return {}, {'points': [], 'clusters': 0, 'clustered': 0, 'eligible': len(candidates)}
        matrix = np.stack(rows)
        tfidf = TfidfTransformer(sublinear_tf=True).fit_transform(matrix)
        dimensions = min(64, len(rows) - 1, TOPIC_DIMENSIONS - 1)
        embedding = TruncatedSVD(n_components=dimensions, random_state=0).fit_transform(tfidf)
        embedding = normalize(embedding)
        model = HDBSCAN(min_cluster_size=5, min_samples=2, copy=True,
                        cluster_selection_method='eom', allow_single_cluster=False).fit(embedding)
        labels, probabilities = model.labels_, model.probabilities_
        result, points = {}, []
        for index, key in enumerate(keys):
            label, probability = int(labels[index]), float(probabilities[index])
            x = float(embedding[index, 0]) if dimensions else 0.0
            y = float(embedding[index, 1]) if dimensions > 1 else 0.0
            for document in vectors[key]:
                path = document['path']
                result[path] = (label, probability)
                points.append({'path': path, 'x': x, 'y': y, 'label': label,
                               'confidence': probability})
        cluster_labels = {label for label in labels if label >= 0}
        clustered_mask = labels >= 0
        quality = None
        if len(cluster_labels) > 1 and clustered_mask.sum() > len(cluster_labels):
            quality = float(silhouette_score(embedding[clustered_mask], labels[clustered_mask]))
        return result, {'points': points, 'clusters': len(cluster_labels),
                        'clustered': sum(item['label'] >= 0 for item in points),
                        'eligible': len(candidates), 'quality': quality}
    except Exception:
        return {}, {'points': [], 'clusters': 0, 'clustered': 0, 'eligible': len(candidates),
                    'error': 'ML-кластеризация недоступна'}


def physical_key(document):
    """Fallback display order only; it does not claim content similarity."""
    return Path(document['path']).suffix.casefold(), document.get('size', 0)


def append_physical_groups(documents, ranks, groups, notes, first_group, cancel):
    """Put unconfirmed files last, grouped by extension and a 5% size band."""
    group = first_group - 1
    anchor, extension = None, None
    for document in sorted(documents, key=physical_key):
        if cancel.is_set():
            return None
        current_extension, size = physical_key(document)
        new_band = current_extension != extension
        if not new_band and anchor is not None:
            new_band = ((anchor == 0) != (size == 0)) or bool(size and anchor / size < .95)
        if new_band or anchor is None:
            group += 1
            extension, anchor = current_extension, size
        path = document['path']
        ranks[path], groups[path] = len(ranks), group
        if size_only(document):
            notes[path] = ('Только тип и размер: файл >128 МиБ, содержимое не проверялось; '
                           'группа с разбросом размера до 5%. Не подтверждённый дубль.')
        elif document.get('content', {}).get('failed') or not document.get('content', {}).get('sha256'):
            notes[path] = ('Содержимое не проанализировано; расположен в конце только по типу и размеру. '
                           'Не подтверждённый дубль.')
        else:
            notes[path] = ('Совпадений по содержимому выше порога не найдено; расположен в конце только по типу '
                           'и размеру. Не подтверждённый дубль.')
    return group + 1


def content_order(documents, cancel, progress=lambda count: None, with_map=False):
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
    semantic, visual = semantic_layout(documents, cancel)
    if cancel.is_set():
        return None

    # Merge the strict near-duplicate components with ML topic communities.
    # A semantic group can contain versions that share vocabulary but no exact shingles.
    parents = list(range(len(units)))
    def find(value):
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value
    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parents[right] = left
    content_notes = {}
    for indices in members.values():
        for i in indices[1:]:
            union(indices[0], i)
        if sum(len(units[i]) for i in indices) > 1:
            representative = fingerprints[indices[0]]
            for i in indices:
                comparison = fingerprints[indices[1]] if i == indices[0] and len(indices) > 1 else representative
                note = evidence(fingerprints[i], comparison)
                for document in units[i]:
                    if note:
                        content_notes[document['path']] = note
    semantic_units = defaultdict(list)
    for i, unit in enumerate(units):
        for document in unit:
            label = semantic.get(document['path'], (-1, 0))[0]
            if label >= 0:
                semantic_units[label].append(i)
    for indices in semantic_units.values():
        for i in indices[1:]:
            union(indices[0], i)
    combined = defaultdict(list)
    for i in range(len(units)):
        combined[find(i)].append(i)

    ranks, groups, unmatched, notes = {}, {}, list(unknown), {}
    confirmed = []
    for indices in combined.values():
        if sum(len(units[i]) for i in indices) > 1:
            confirmed.append(indices)
        else:
            unmatched.extend(units[indices[0]])
    # ML/content groups first. Larger groups are easier to process in bulk;
    # format and size only make their block order stable and readable.
    confirmed.sort(key=lambda indices: (
        -sum(len(units[i]) for i in indices),
        min(physical_key(document) for i in indices for document in units[i]),
        min(indices),
    ))
    for group, indices in enumerate(confirmed):
        indices = sorted(indices)
        representative = fingerprints[indices[0]]
        cluster_documents = sorted(
            ((i, document) for i in indices for document in units[i]),
            key=lambda pair: physical_key(pair[1]),
        )
        for i, document in cluster_documents:
            path = document['path']
            ranks[path], groups[path] = len(ranks), group
            reasons = []
            if path in content_notes:
                reasons.append(content_notes[path])
            label, probability = semantic.get(path, (-1, 0))
            if label >= 0:
                reasons.append(f'Тематическая группа ML (TF-IDF + LSA + HDBSCAN), уверенность: {probability:.0%}; не признак дубля')
            if reasons:
                notes[path] = ' · '.join(reasons)
    unmatched.extend(document for extension in large.values() for document in extension)
    if append_physical_groups(unmatched, ranks, groups, notes, len(confirmed), cancel) is None:
        return None
    result = ranks, groups, notes
    return result + (visual,) if with_map else result
