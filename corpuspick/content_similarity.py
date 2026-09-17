"""Read-only content fingerprints. No extracted text leaves the parser process."""
from collections import Counter, defaultdict
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
LIMIT = 256
MAX_CONTENT_BYTES = 128 * 1024 * 1024
TEXT_SUFFIXES = ('.docx', '.pdf', '.txt', '.md', '.csv', '.tsv')
VISUAL_VERSION = 1
LABEL_MIN_DOCUMENTS = 5
LABEL_MAX_FRACTION = .8
LABEL_STOPWORDS = {
    'без', 'для', 'документ', 'документы', 'копия', 'новый', 'новая', 'новое', 'от', 'по', 'проект',
    'редакция', 'скан', 'файл', 'файлы', 'and', 'copy', 'document', 'documents', 'file', 'final', 'new',
    'the', 'version',
}


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
    iterator = iter(parts)
    try:
        for part in iterator:
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
    finally:
        close = getattr(iterator, 'close', None)
        if close:
            close()
    return {'text': sketch.result(), 'text_sha256': sha.hexdigest(), 'words': count}


def text_parts(path, max_pages=None):
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
            for index, page in enumerate(reader.pages):
                if max_pages is not None and index >= max_pages:
                    break
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


def extracted_fingerprint(path):
    try:
        return text_fingerprint(text_parts(path))
    except UnicodeError:
        if path.suffix.lower() not in ('.txt', '.md', '.csv', '.tsv'):
            raise
        with open(path, encoding='cp1251') as stream:
            return text_fingerprint(stream)


def _perceptual_hash(image):
    """512-bit average+difference hash; decoded pixels, not compressed bytes."""
    from PIL import Image, ImageOps
    image = ImageOps.exif_transpose(image).convert('L')
    average_image = image.resize((16, 16), Image.Resampling.LANCZOS)
    flattened = getattr(average_image, 'get_flattened_data', None)
    pixels = list(flattened() if flattened else average_image.getdata())
    mean = sum(pixels) / len(pixels)
    average_bits = ''.join('1' if value > mean else '0' for value in pixels)
    difference_image = image.resize((17, 16), Image.Resampling.LANCZOS)
    flattened = getattr(difference_image, 'get_flattened_data', None)
    pixels = list(flattened() if flattened else difference_image.getdata())
    difference_bits = ''.join(
        '1' if pixels[row * 17 + column] > pixels[row * 17 + column + 1] else '0'
        for row in range(16) for column in range(16)
    )
    bits = average_bits + difference_bits
    return f'{int(bits, 2):0128x}', round(mean), round(image.width / max(image.height, 1), 3)


def pdf_visual_fingerprint(path):
    """Hash the largest embedded image on first/middle/last PDF pages without OCR."""
    from pypdf import PdfReader
    logging.getLogger('pypdf').setLevel(logging.CRITICAL)
    result = {'visual_version': VISUAL_VERSION, 'visual_pages': 0, 'visual': []}
    with open(path, 'rb') as stream:
        reader = PdfReader(stream, strict=False)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('Encrypted')
        total = len(reader.pages)
        result['visual_pages'] = total
        positions = [('first', 0), ('middle', total // 2), ('last', total - 1)] if total else []
        seen = set()
        for slot, index in positions:
            if index in seen:
                continue
            seen.add(index)
            page = reader.pages[index]
            largest = None
            for key in list(page.images.keys())[:32]:
                try:
                    candidate = page.images[key].image
                    if candidate is not None and (largest is None or candidate.width * candidate.height > largest.width * largest.height):
                        largest = candidate.copy()
                except Exception:
                    continue
            if largest is not None:
                fingerprint, tone, aspect = _perceptual_hash(largest)
                result['visual'].append({'slot': slot, 'hash': fingerprint,
                                         'tone': tone, 'aspect': aspect})
    return result


def fingerprint(path):
    from .core import native
    path = Path(native(Path(path)))
    result = binary_fingerprint(path)
    result.update(version=VERSION, info='Байты прочитаны полностью; текст для этого формата не извлекается')
    if path.suffix.lower() in TEXT_SUFFIXES:
        try:
            text = extracted_fingerprint(path)
            result.update(text)
            if path.suffix.lower() == '.pdf' and text['words'] < 20:
                result.update(pdf_visual_fingerprint(path))
            result['info'] = ('Байты и извлечённый текст; изображения и вёрстка текстом не сравниваются'
                              if text['words'] >= 10 else 'Байты прочитаны; недостаточно текста для сравнения (OCR не выполнялся)')
        except Exception:
            result['text_failed'] = True
            result['info'] = 'Байты прочитаны; текст извлечь не удалось'
            if path.suffix.lower() == '.pdf':
                try:
                    result.update(pdf_visual_fingerprint(path))
                except Exception:
                    result['visual_failed'] = True
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


def visual_similarity(a, b):
    left = {item['slot']: item for item in a.get('visual', [])}
    right = {item['slot']: item for item in b.get('visual', [])}
    common = sorted(left.keys() & right.keys())
    if not common:
        return 0.0
    if min(a.get('visual_pages', 0), b.get('visual_pages', 0)):
        ratio = min(a['visual_pages'], b['visual_pages']) / max(a['visual_pages'], b['visual_pages'])
        if ratio < .8:
            return 0.0
    scores = []
    for slot in common:
        first, second = left[slot], right[slot]
        if abs(first['aspect'] - second['aspect']) > .12 or abs(first['tone'] - second['tone']) > 40:
            return 0.0
        distance = (int(first['hash'], 16) ^ int(second['hash'], 16)).bit_count()
        scores.append(1 - distance / 512)
    if (max(len(left), len(right)) > 1 and len(common) < 2) or min(scores) < .80:
        return 0.0
    return sum(scores) / len(scores)


def visual_index_keys(fp):
    keys = []
    for item in fp.get('visual', []):
        value = item.get('hash', '')
        if len(value) != 128:
            continue
        for band in range(32):
            keys.append(('v', item['slot'], band, value[band * 4:(band + 1) * 4]))
    return keys


def content_match(a, b):
    if not a or not b or a.get('failed') or b.get('failed'):
        return None
    if a.get('sha256') and a['sha256'] == b.get('sha256'):
        return 1.01, 'Точные байты (SHA-256)'
    matches = []
    score = visual_similarity(a, b)
    if score >= .88:
        matches.append((score, f'Визуально близкие страницы PDF без OCR: ≈{score:.0%} совпадения отпечатков'))
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
    fields = ('sha256', 'text_sha256', 'words', 'bytes', 'binary_method', 'binary', 'chunks', 'text',
              'visual_version', 'visual_pages', 'visual')
    return json.dumps({k: fp.get(k) for k in fields}, sort_keys=True, separators=(',', ':'))


def _label_tokens(value):
    """Return local weak-label tokens without exposing names outside this process."""
    normalized = unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')
    tokens = re.findall(r'[^\W_]+(?:-[^\W_]+)+|[^\W_]+', normalized)
    result, sequence = set(), []
    for token in tokens:
        token = token.replace('-', '_')
        if (token in LABEL_STOPWORDS or token.isdigit() or len(token) < 2
                or re.fullmatch(r'(?:v|ver|версия)?\d+[a-zа-я]?', token)):
            sequence.append(None)
            continue
        if any(character.isalpha() for character in token):
            result.add(token)
            sequence.append(token)
        else:
            sequence.append(None)
    for left, right in zip(sequence, sequence[1:]):
        if left and right:
            result.add(left + '_' + right)
    return result


def weak_label_candidates(documents, minimum=LABEL_MIN_DOCUMENTS):
    """Mine repeated name/path terms and return overlap-safe candidate labels.

    Near-identical postings (for example ``акт приема передачи``) are aliases,
    not competing classes.  Independent overlaps remain ambiguous and therefore
    are not used as SVM training examples.
    """
    sources, postings = {}, defaultdict(set)
    for index, document in enumerate(documents):
        paths = {document['path'], document.get('origin', document['path'])}
        paths.update(document.get('origins', []))
        name_terms, folder_terms = set(), set()
        for value in paths:
            path = Path(value)
            name_terms.update(_label_tokens(path.stem))
            for part in path.parts[:-1]:
                folder_terms.update(_label_tokens(part))
        sources[index] = (name_terms, folder_terms)
        for term in name_terms | folder_terms:
            postings[term].add(index)
    limit = max(minimum, int(len(documents) * LABEL_MAX_FRACTION) + 1)
    terms = sorted(term for term, indices in postings.items() if minimum <= len(indices) < limit)
    parents = {term: term for term in terms}

    def find(term):
        while parents[term] != term:
            parents[term] = parents[parents[term]]
            term = parents[term]
        return term

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parents[right] = left

    for offset, left in enumerate(terms):
        for right in terms[offset + 1:]:
            overlap = len(postings[left] & postings[right])
            smaller, larger = sorted((len(postings[left]), len(postings[right])))
            if overlap / smaller >= .85 and larger / smaller <= 1.25:
                union(left, right)
    aliases = defaultdict(list)
    for term in terms:
        aliases[find(term)].append(term)
    canonical = {}
    for group in aliases.values():
        # Preserve a repeated phrase as one class instead of letting its
        # individual words create competing classes.
        representative = min(group, key=lambda term: ('_' not in term, len(term), term))
        canonical.update((term, representative) for term in group)
    result = {}
    for index, document in enumerate(documents):
        name_terms, folder_terms = sources[index]
        name_labels = {canonical[term] for term in name_terms if term in canonical}
        folder_labels = {canonical[term] for term in folder_terms if term in canonical}
        # A unique filename label is stronger than unrelated broad directory labels.
        labels = name_labels if len(name_labels) == 1 else name_labels | folder_labels
        result[document['path']] = sorted(labels)
    return result, {label: len(set().union(*(postings[t] for t, value in canonical.items() if value == label)))
                    for label in set(canonical.values())}


def _semantic_embedding(documents):
    import numpy as np
    from .embeddings import unpack_embedding

    vectors = {}
    for document in documents:
        signature = document['content']
        vectors.setdefault(signature['embedding'], []).append(document)
    keys = sorted(vectors)
    return keys, vectors, np.stack([unpack_embedding(encoded) for encoded in keys])


def semantic_layout(documents, cancel):
    """Weak supervision from names/paths plus open-set Linear SVM over E5.

    Unlabelled dense content communities become ``other_N`` seed classes.  The
    SVM may extend a class, but low-margin or centroid-distant documents remain
    unassigned instead of being forced into the closest class.
    """
    from .embeddings import EMBEDDING_VERSION
    candidates = [document for document in documents
                  if not size_only(document)
                  and document.get('content', {}).get('embedding_version') == EMBEDDING_VERSION
                  and document.get('content', {}).get('embedding')]
    if len(candidates) < 5 or cancel.is_set():
        return {}, {'clusters': 0, 'clustered': 0, 'eligible': len(candidates)}
    try:
        from sklearn.cluster import HDBSCAN
        import numpy as np
        from sklearn.svm import LinearSVC

        keys, vectors, embedding = _semantic_embedding(candidates)
        weak, frequencies = weak_label_candidates(documents)
        training, training_support = {}, {}
        ambiguous = sum(len(labels) > 1 for labels in weak.values())
        for index, key in enumerate(keys):
            labels = set()
            for document in vectors[key]:
                document_labels = weak.get(document['path'], [])
                if len(document_labels) == 1:
                    labels.add(document_labels[0])
            if len(labels) == 1:
                training[index] = 'label:' + labels.pop()
                training_support[index] = sum(weak.get(document['path']) == [training[index][6:]]
                                              for document in vectors[key])

        # Discover content-only classes among documents that supplied no reliable
        # filename/path seed.  They are deliberately named other_N.
        unlabelled = [index for index, key in enumerate(keys)
                      if index not in training and not any(weak.get(document['path']) for document in vectors[key])]
        if len(unlabelled) >= 5:
            cluster = HDBSCAN(min_cluster_size=5, min_samples=2, copy=True,
                              cluster_selection_method='eom', allow_single_cluster=True).fit(embedding[unlabelled])
            other_labels = sorted(set(int(label) for label in cluster.labels_ if label >= 0))
            other_names = {label: f'other_{number + 1}' for number, label in enumerate(other_labels)}
            for position, label in zip(unlabelled, cluster.labels_):
                if int(label) >= 0:
                    training[position] = other_names[int(label)]
                    training_support[position] = len(vectors[keys[position]])

        class_counts = Counter()
        for index, label in training.items():
            class_counts[label] += training_support.get(index, 1)
        usable = {label for label, count in class_counts.items() if count >= 2}
        training = {index: label for index, label in training.items() if label in usable}
        available_classes = sorted(set(training.values()))
        if not available_classes:
            return {}, {'clusters': 0, 'clustered': 0, 'eligible': len(candidates),
                        'labels': len(frequencies), 'ambiguous': ambiguous,
                        'error': 'Недостаточно независимых слабых классов для SVM'}
        if len(available_classes) == 1:
            label = available_classes[0]
            shown = label.removeprefix('label:')
            source = ('метка из имени/пути; SVM не обучен' if label.startswith('label:')
                      else 'кластер содержания; SVM не обучен')
            result = {}
            for index, value in training.items():
                if value == label:
                    for document in vectors[keys[index]]:
                        result[document['path']] = (0, 1.0, shown, source)
            return result, {'clusters': 1, 'clustered': len(result), 'eligible': len(candidates),
                            'labels': len(frequencies), 'ambiguous': ambiguous}

        indices = sorted(training)
        # Lower C means a wider geometric margin and less sensitivity to noisy
        # weak labels from filenames.
        model = LinearSVC(C=.25, class_weight='balanced', dual='auto', random_state=0)
        model.fit(embedding[indices], [training[index] for index in indices],
                  sample_weight=[training_support.get(index, 1) for index in indices])
        scores = model.decision_function(embedding)
        if scores.ndim == 1:
            scores = np.column_stack((-scores, scores))
        classes = list(model.classes_)
        centroids, thresholds = {}, {}
        for label in classes:
            member_indices = [index for index, value in training.items() if value == label]
            centroid = np.asarray(embedding[member_indices].mean(axis=0)).ravel()
            centroid /= np.linalg.norm(centroid) or 1
            similarities = embedding[member_indices] @ centroid
            centroids[label] = centroid
            thresholds[label] = max(.12, float(np.quantile(similarities, .1)) - .12)

        # E5 may reveal that two filename labels or two HDBSCAN fragments are
        # really the same semantic class. Merge only very close centroids; a
        # shared phrase component permits a slightly lower threshold.
        class_parents = {label: label for label in classes}
        def class_find(label):
            while class_parents[label] != label:
                class_parents[label] = class_parents[class_parents[label]]
                label = class_parents[label]
            return label
        def class_union(left, right):
            left, right = class_find(left), class_find(right)
            if left != right:
                class_parents[right] = left
        for offset, left in enumerate(classes):
            for right in classes[offset + 1:]:
                left_words = set(left.removeprefix('label:').split('_'))
                right_words = set(right.removeprefix('label:').split('_'))
                threshold = .90 if left.startswith('label:') and right.startswith('label:') and left_words & right_words else .97
                if float(centroids[left] @ centroids[right]) >= threshold:
                    class_union(left, right)
        merged_classes = sorted({class_find(label) for label in classes})
        representatives = {}
        for root in merged_classes:
            members = [label for label in classes if class_find(label) == root]
            representatives[root] = max(members, key=lambda label: (class_counts[label], -len(label), label))

        result, numeric = {}, {label: index for index, label in enumerate(merged_classes)}
        for index, key in enumerate(keys):
            if index in training:
                label, confidence = training[index], 1.0
            else:
                order = np.argsort(scores[index])
                best, second = int(order[-1]), int(order[-2])
                label = classes[best]
                margin = float(scores[index, best] - scores[index, second])
                similarity = float(embedding[index] @ centroids[label])
                if margin < .15 or similarity < thresholds[label]:
                    continue
                confidence = min(.99, max(.51, .55 + .2 * margin + .2 * similarity))
            root = class_find(label)
            representative = representatives[root]
            source = 'метка из имени/пути' if representative.startswith('label:') else 'кластер содержания'
            if root != label or representative != label:
                source += '; объединён близкий E5-класс'
            shown = representative.removeprefix('label:')
            for document in vectors[key]:
                result[document['path']] = (numeric[root], confidence, shown, source)
        return result, {'clusters': len({value[0] for value in result.values()}),
                        'clustered': len(result), 'eligible': len(candidates),
                        'labels': len(frequencies), 'ambiguous': ambiguous}
    except Exception:
        return {}, {'clusters': 0, 'clustered': 0, 'eligible': len(candidates),
                    'error': 'ML-кластеризация недоступна'}


def metadata_order(documents, cancel, progress=lambda count: None, distance_threshold=.35):
    """Average-link clustering over semantic, lexical and optional layout vectors."""
    from .embeddings import EMBEDDING_VERSION, unpack_embedding
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    candidates, unmatched = [], []
    for document in documents:
        signature = document.get('similarity', {})
        if signature.get('embedding_version') == EMBEDDING_VERSION and signature.get('embedding'):
            try:
                semantic = unpack_embedding(signature['embedding'])
                try:
                    lexical = unpack_embedding(signature.get('metadata_lexical', ''))
                except (ValueError, TypeError, zlib.error, base64.binascii.Error):
                    lexical = np.zeros_like(semantic)
                try:
                    layout = unpack_embedding(signature.get('layout_embedding', ''))
                except (ValueError, TypeError, zlib.error, base64.binascii.Error):
                    layout = None
                candidates.append((document, semantic, lexical, layout))
            except (ValueError, TypeError):
                unmatched.append(document)
        else:
            unmatched.append(document)
    if cancel.is_set():
        return None

    buckets = []
    if len(candidates) >= 2:
        # sklearn/scipy require several dense copies in addition to our matrix.
        # Fail visibly before a large allocation can exhaust the desktop process.
        if len(candidates) ** 2 * 32 > 1024 ** 3:
            raise ValueError('Для попарной группировки слишком много документов: оценка памяти превышает 1 ГиБ. Откройте меньший каталог.')
        distances = np.zeros((len(candidates), len(candidates)), dtype=np.float32)
        for left in range(len(candidates)):
            if cancel.is_set():
                return None
            _, semantic_left, lexical_left, layout_left = candidates[left]
            for right in range(left):
                _, semantic_right, lexical_right, layout_right = candidates[right]
                semantic_similarity = float(semantic_left @ semantic_right)
                if np.linalg.norm(lexical_left) and np.linalg.norm(lexical_right):
                    text_similarity = .60 * semantic_similarity + .40 * float(lexical_left @ lexical_right)
                else:
                    text_similarity = semantic_similarity
                similarity = text_similarity
                if layout_left is not None and layout_right is not None:
                    layout_similarity = float(layout_left @ layout_right)
                    # A highly similar page template can rescue reports whose
                    # actual period text differs, but cannot act alone.
                    similarity = max(similarity, .55 * text_similarity + .45 * layout_similarity)
                distances[left, right] = distances[right, left] = 1 - np.clip(similarity, -1, 1)
        labels = AgglomerativeClustering(
            n_clusters=None, metric='precomputed', linkage='average',
            distance_threshold=distance_threshold,
        ).fit_predict(distances)
        grouped = defaultdict(list)
        for index, label in enumerate(labels):
            grouped[int(label)].append(index)
        buckets = [indices for indices in grouped.values() if len(indices) > 1]
        unmatched.extend(candidates[indices[0]][0] for indices in grouped.values() if len(indices) == 1)
    elif candidates:
        unmatched.append(candidates[0][0])

    buckets.sort(key=lambda indices: (
        -len(indices), min(candidates[index][0]['path'].casefold() for index in indices)))
    ranks, groups, notes = {}, {}, {}
    for group, indices in enumerate(buckets):
        for index in sorted(indices, key=lambda value: candidates[value][0]['path'].casefold()):
            document = candidates[index][0]
            path = document['path']
            peers = [1 - float(distances[index, other]) for other in indices if other != index]
            confidence = sum(peers) / len(peers)
            signature = document.get('similarity', {})
            entities = Counter(signature.get('metadata_entities', {}))
            entities.update(signature.get('ner_entities', {}))
            recognized = [label for key, label in (
                ('person', 'ФИО'), ('per', 'ФИО'), ('org', 'организации'), ('loc', 'места'),
                ('date', 'даты'), ('number', 'номера')) if entities.get(key)]
            recognized = list(dict.fromkeys(recognized))
            suffix = f"; распознаны: {', '.join(recognized)}" if recognized else ''
            if signature.get('metadata_refresh_failed'):
                suffix += '; обновить название не удалось, использованы прежние признаки'
            elif signature.get('content_embedding_legacy'):
                suffix += '; основа содержания из прежнего смешанного кэша'
            pages = signature.get('sampled_pages', 0)
            ocr_pages = signature.get('ocr_pages', 0)
            provider = {'DmlExecutionProvider': 'GPU DirectML',
                        'CUDAExecutionProvider': 'GPU CUDA',
                        'CPUExecutionProvider': 'CPU'}.get(signature.get('ocr_provider'), '')
            provider_text = f' ({provider})' if provider and ocr_pages else ''
            page_signal = (f'; страницы: {pages}, OCR: {ocr_pages}, макет: '
                           f"{'да' if signature.get('layout_embedding') else 'нет'}{provider_text}") if pages else ''
            ranks[path], groups[path] = len(ranks), group
            notes[path] = (f'Группа по embeddings текста/названия/пути и доступного макета; '
                           f'порог близости: {1 - distance_threshold:.0%}; '
                           f'средняя близость к группе: {confidence:.0%}{page_signal}{suffix}. '
                           'OCR-текст не сохраняется.')
            progress(len(ranks))

    next_group = len(buckets)
    for document in sorted(unmatched, key=lambda item: item['path'].casefold()):
        if cancel.is_set():
            return None
        path = document['path']
        ranks[path], groups[path] = len(ranks), next_group
        next_group += 1
        signature = document.get('similarity', {})
        if signature.get('embedding_failed'):
            notes[path] = 'Embedding построить не удалось; файл оставлен отдельно.'
        else:
            notes[path] = ('Сходства текста, названия, пути или макета выше порога не найдено; '
                           'файл оставлен отдельно. OCR-текст не сохраняется.')
        progress(len(ranks))
    return ranks, groups, notes


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


def append_residual_name_groups(documents, ranks, groups, notes, first_group, cancel):
    """Use names only for files left ungrouped by content.

    Partitions never mix formats and span at most 2x in size.  This last-resort
    stage cannot change a content or ML group because it only receives the
    unmatched remainder.
    """
    from .similarity import similar_order

    partitions, current, extension, anchor = [], [], None, None
    for document in sorted(documents, key=physical_key):
        if cancel.is_set():
            return None
        current_extension, size = physical_key(document)
        new_partition = current_extension != extension
        if not new_partition and anchor is not None:
            new_partition = ((anchor == 0) != (size == 0)) or bool(size and anchor / size < .5)
        if new_partition:
            if current:
                partitions.append(current)
            current, extension, anchor = [], current_extension, size
        elif anchor is None:
            extension, anchor = current_extension, size
        current.append(document)
    if current:
        partitions.append(current)

    accepted, remaining = [], []
    for partition in partitions:
        if cancel.is_set():
            return None
        result = similar_order(partition, cancel, with_groups=True,
                               exhaustive=len(partition) <= 4000, threshold=.4)
        if result is None:
            return None
        _, local_groups = result
        buckets = defaultdict(list)
        for document in partition:
            buckets[local_groups[document['path']]].append(document)
        for bucket in buckets.values():
            (accepted if len(bucket) > 1 else remaining).append(bucket)

    accepted.sort(key=lambda bucket: (-len(bucket), min(physical_key(document) for document in bucket),
                                      min(document['path'].casefold() for document in bucket)))
    group = first_group
    for bucket in accepted:
        for document in sorted(bucket, key=lambda item: (item['path'].casefold(), physical_key(item))):
            path = document['path']
            ranks[path], groups[path] = len(ranks), group
            notes[path] = ('Резервная группа: похожее название, одинаковый формат и размер в пределах 2×; '
                           'содержимое не подтвердило общую тему или дубль.')
        group += 1
    return [document for bucket in remaining for document in bucket], group


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
        keys += visual_index_keys(fp)
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
    semantic, _ = semantic_layout(documents, cancel)
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
            label, probability, class_name, source = semantic.get(path, (-1, 0, '', ''))
            if label >= 0:
                embedding_source = document.get('content', {}).get('embedding_source', 'content')
                source_names = {'content': 'содержимое', 'content+metadata': 'содержимое + имя/путь',
                                'metadata': 'только имя/путь'}
                reasons.append(f'SVM-класс «{class_name}» ({source}), уверенность: {probability:.0%}; '
                               f'E5: {source_names.get(embedding_source, embedding_source)}; '
                               'слабая локальная метка, не признак дубля')
            if reasons:
                notes[path] = ' · '.join(reasons)
    unmatched.extend(document for extension in large.values() for document in extension)
    residual = append_residual_name_groups(unmatched, ranks, groups, notes, len(confirmed), cancel)
    if residual is None:
        return None
    unmatched, next_group = residual
    if append_physical_groups(unmatched, ranks, groups, notes, next_group, cancel) is None:
        return None
    return ranks, groups, notes
