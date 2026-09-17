"""Local multilingual-e5-small ONNX inference; source text never leaves the worker."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import unicodedata
import urllib.request
import zlib

MODEL_NAME = 'multilingual-e5-small-int8'
MODEL_REVISION = '761b726dd34fb83930e26aab4e9ac3899aa1fa78'
EMBEDDING_VERSION = 5
EMBEDDING_DIMENSIONS = 384
MAX_TOKENS = 512
MODEL_FILES = {
    'model_int8.onnx': (
        f'https://huggingface.co/Xenova/multilingual-e5-small/resolve/{MODEL_REVISION}/onnx/model_int8.onnx',
        118_054_593, '4d24e2bc01a447951524466ef533e52944bf48509e6552810bcee1a2711cb02c'),
    'tokenizer.json': (
        f'https://huggingface.co/Xenova/multilingual-e5-small/resolve/{MODEL_REVISION}/tokenizer.json',
        17_082_730, '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39'),
}


def _normalize_text(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')).strip()


_FIRST_NAMES = {
    'александр', 'алексей', 'алена', 'алёна', 'анастасия', 'анатолий', 'андрей', 'анна', 'антон',
    'артем', 'артём', 'борис', 'вадим', 'валентина', 'валерий', 'василий', 'вера', 'виктор',
    'виктория', 'виталий', 'владимир', 'владислав', 'галина', 'геннадий', 'георгий', 'дарья',
    'денис', 'дмитрий', 'евгений', 'евгения', 'екатерина', 'елена', 'иван', 'игорь', 'илья',
    'ирина', 'кирилл', 'константин', 'лариса', 'лев', 'лидия', 'любовь', 'людмила', 'максим',
    'маргарита', 'марина', 'мария', 'михаил', 'надежда', 'наталья', 'никита', 'николай',
    'олег', 'ольга', 'павел', 'петр', 'пётр', 'полина', 'роман', 'светлана', 'семен', 'семён',
    'сергей', 'софия', 'станислав', 'степан', 'татьяна', 'тимофей', 'федор', 'фёдор', 'юлия', 'юрий', 'яна',
}
_NORMALIZED_FIRST_NAMES = {name.replace('ё', 'е') for name in _FIRST_NAMES}
_SURNAME_ENDINGS = ('ов', 'ова', 'ев', 'ева', 'ин', 'ина', 'ын', 'ына', 'ский', 'ская',
                    'цкий', 'цкая', 'енко', 'ко', 'ук', 'юк', 'дзе', 'швили')
_DATE_RE = re.compile(
    r'(?<!\d)(?:\d{1,2}[.\-/]\d{1,2}[.\-/](?:\d{2}|\d{4})|'
    r'(?:19|20)\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}|(?:19|20)\d{2})(?!\d)')
_MONTH_RE = re.compile(
    r'(?<!\w)\d{1,2}\s+(?:январ[ья]|феврал[ья]|марта|апрел[ья]|мая|июн[ья]|июл[ья]|'
    r'августа|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])\s+(?:19|20)\d{2}(?:\s*г(?:ода|\.)?)?', re.I)
_INITIALS_RE = re.compile(
    r'(?<!\w)(?:[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\s*\.\s*[А-ЯЁ]\s*\.|'
    r'[А-ЯЁ]\s*\.\s*[А-ЯЁ]\s*\.\s*[А-ЯЁ][а-яё-]+)(?!\w)')
_TITLE_WORDS_RE = re.compile(r'(?<!\w)(?:[А-ЯЁ][а-яё-]{2,}\s+){1,2}[А-ЯЁ][а-яё-]{2,}(?!\w)')


def _looks_like_person(words):
    lowered = [word.casefold().replace('ё', 'е') for word in words]
    first_name = any(word in _NORMALIZED_FIRST_NAMES for word in lowered)
    patronymic = any(word.endswith(('ович', 'евич', 'ич', 'овна', 'евна', 'ична')) for word in lowered)
    surname = any(word.endswith(_SURNAME_ENDINGS) and len(word) > 4 for word in lowered)
    return patronymic or (first_name and surname)


def _replace_people(value):
    value, count = _INITIALS_RE.subn(' персона ', value)
    def replace(match):
        nonlocal count
        words = re.findall(r'[А-ЯЁ][а-яё-]+', match.group(0))
        if _looks_like_person(words):
            count += 1
            return ' персона '
        return match.group(0)
    return _TITLE_WORDS_RE.sub(replace, value), count


def metadata_features(paths):
    """Build value-invariant local features from filenames and directory names."""
    values = sorted({unicodedata.normalize('NFKC', str(value)).strip() for value in paths if str(value).strip()})
    titles, folders, people, dates, numbers = [], [], 0, 0, 0
    for value in values:
        parts = [part for part in re.split(r'[\\/]+', value) if part and part not in ('.', '..')]
        if not parts:
            continue
        title = Path(parts[-1]).stem
        folder = ' / '.join(parts[-3:-1])
        converted = []
        for text in (title, folder):
            text, found = _replace_people(text)
            people += found
            text = text.replace('_', ' ')
            text, found = _MONTH_RE.subn(' ', text)
            dates += found
            text, found = _DATE_RE.subn(' ', text)
            dates += found
            text, found = re.subn(r'(?<!\w)(?:№|no\.?)\s*(?=\d)', ' ', text, flags=re.I)
            text, found = re.subn(r'\d+', '', text)
            numbers += found
            # Keep words and compound phrases; discard numeric separators too.
            text = ' '.join(re.findall(r'[^\W\d_]+(?:-[^\W\d_]+)*', text))
            converted.append(_normalize_text(text))
        if converted[0]:
            titles.append(converted[0])
        if converted[1]:
            folders.append(converted[1])
    entities = []
    if people:
        entities.append('имя или фамилия')
    return {'title': ' ; '.join(dict.fromkeys(titles)),
            'folders': ' ; '.join(dict.fromkeys(folders)),
            'entities': ' '.join(entities),
            'entity_counts': {'person': people, 'date': dates, 'number': numbers}}


def metadata_key(paths):
    normalized = '\0'.join(sorted(_normalize_text(str(value)) for value in paths))
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _lexical_embedding(features):
    """Stable sparse metadata embedding; complements anisotropic short-text E5."""
    import numpy as np
    vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float32)
    for field, field_weight in (('title', 1.0), ('folders', .8)):
        words = [word for word in re.findall(r'[^\W_]+', features[field])
                 if word not in {'персона', 'дата', 'номер'}]
        terms = [(word, 1.0) for word in words]
        terms.extend((left + '_' + right, 1.5) for left, right in zip(words, words[1:]))
        for term, term_weight in terms:
            digest = hashlib.blake2b(term.encode('utf-8'), digest_size=8).digest()
            index = int.from_bytes(digest[:4], 'little') % EMBEDDING_DIMENSIONS
            sign = 1 if digest[4] & 1 else -1
            vector[index] += sign * field_weight * term_weight
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def model_directory(data_root):
    return Path(data_root) / 'models' / MODEL_NAME


def model_ready(data_root):
    directory = model_directory(data_root)
    return all((directory / name).is_file() and (directory / name).stat().st_size == expected
               for name, (_, expected, _) in MODEL_FILES.items())


def _download(url, destination, expected_size, expected_sha, cancel=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.part')
    hasher, size = hashlib.sha256(), 0
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'CorpusPick/1 local-model-download'})
        with urllib.request.urlopen(request, timeout=60) as source, open(temporary, 'wb') as target:
            while block := source.read(1024 * 1024):
                if cancel is not None and cancel.is_set():
                    from .core import Cancelled
                    raise Cancelled()
                target.write(block)
                hasher.update(block)
                size += len(block)
            target.flush()
            os.fsync(target.fileno())
        if size != expected_size or hasher.hexdigest() != expected_sha:
            raise ValueError('Контрольная сумма модели не совпала; файл не установлен')
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_model(data_root, cancel=None):
    directory = model_directory(data_root)
    for name, (url, expected_size, expected_sha) in MODEL_FILES.items():
        destination = directory / name
        if destination.is_file() and destination.stat().st_size == expected_size:
            continue
        try:
            _download(url, destination, expected_size, expected_sha, cancel)
        except Exception as exc:
            from .core import Cancelled
            if isinstance(exc, Cancelled):
                raise
            raise ValueError('Не удалось скачать локальную модель embeddings. Проверьте интернет и повторите.') from None
    return directory


def pack_embedding(vector):
    import numpy as np
    value = np.asarray(vector, dtype='<f2')
    if value.shape != (EMBEDDING_DIMENSIONS,):
        raise ValueError('Invalid embedding')
    return base64.b64encode(zlib.compress(value.tobytes(), level=6)).decode('ascii')


def unpack_embedding(encoded):
    import numpy as np
    vector = np.frombuffer(zlib.decompress(base64.b64decode(encoded)), dtype='<f2').astype('f4')
    if vector.shape != (EMBEDDING_DIMENSIONS,):
        raise ValueError('Invalid embedding')
    norm = np.linalg.norm(vector)
    if not norm:
        raise ValueError('Empty embedding')
    return vector / norm


def _load_model(directory):
    import onnxruntime as ort
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(str(Path(directory) / 'tokenizer.json'))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    tokenizer.enable_padding()
    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
    model = ort.InferenceSession(str(Path(directory) / 'model_int8.onnx'),
                                 sess_options=options, providers=['CPUExecutionProvider'])
    return tokenizer, model


def _sample_text(path):
    from .content_similarity import text_parts
    head, middle, tail, snippets = '', '', '', []
    generator = iter(text_parts(path))
    try:
        for index, part in enumerate(generator):
            clean = _normalize_text(part)
            if not clean:
                continue
            if len(head) < 12_000:
                head = (head + ' ' + clean)[:12_000]
            tail = (tail + ' ' + clean)[-12_000:]
            if index % 7 == 0 and len(middle) < 12_000:
                snippets.append(clean[:1_500])
                middle = ' '.join(snippets)[:12_000]
    finally:
        close = getattr(generator, 'close', None)
        if close:
            close()
    result = []
    for value in (head, middle, tail):
        if value and value not in result:
            result.append(value)
    return result


def _encode_passages(tokenizer, model, passages, weights, return_chunks=False):
    import numpy as np
    encoded = tokenizer.encode_batch(passages)
    input_ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
    attention = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
    inputs = {'input_ids': input_ids, 'attention_mask': attention}
    available = {item.name for item in model.get_inputs()}
    if 'token_type_ids' in available:
        inputs['token_type_ids'] = np.zeros_like(input_ids)
    hidden = model.run(None, {name: value for name, value in inputs.items() if name in available})[0]
    mask = attention[..., None].astype(hidden.dtype)
    chunks = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
    chunks /= np.maximum(np.linalg.norm(chunks, axis=1, keepdims=True), 1e-12)
    weights = np.asarray(weights, dtype=np.float32)
    weights /= weights.sum()
    vector = (chunks * weights[:, None]).sum(axis=0)
    vector /= max(float(np.linalg.norm(vector)), 1e-12)
    return (vector, chunks) if return_chunks else vector


def _metadata_passages(features, has_content):
    weights = (.23, .12, .05) if has_content else (.62, .33, .05)
    entries = [(features['title'], 'название документа: ', weights[0]),
               (features['folders'], 'каталоги документа: ', weights[1]),
               (features['entities'], 'типы переменных в названии: ', weights[2])]
    return (['passage: ' + prefix + value for value, prefix, _ in entries if value],
            [weight for value, _, weight in entries if value])


def reusable_similarity(signature):
    return (signature.get('embedding_version') in (4, EMBEDDING_VERSION)
            and bool(signature.get('embedding')) and not signature.get('embedding_failed'))


def _refresh_metadata(signature, tokenizer, model, metadata_paths):
    """Recombine cached content with new metadata; never read a document or run OCR."""
    import numpy as np
    result = dict(signature)
    features = metadata_features(metadata_paths)
    if 'content_embedding' not in result:
        # Older caches contain only the mixed vector. Preserve it as a fixed
        # anchor, not a repeatedly blended vector, until content is re-extracted.
        has_content = result.get('embedding_source') != 'metadata'
        result['content_embedding'] = result['embedding'] if has_content else ''
        result['content_embedding_norm'] = 1.0
        result['content_embedding_legacy'] = has_content
    content = result.get('content_embedding')
    passages, weights = _metadata_passages(features, bool(content))
    vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float32)
    if passages:
        _, chunks = _encode_passages(tokenizer, model, passages, weights, return_chunks=True)
        vector += (chunks * np.asarray(weights, dtype=np.float32)[:, None]).sum(axis=0)
    if content:
        vector += .60 * result.get('content_embedding_norm', 1.0) * unpack_embedding(content)
    norm = float(np.linalg.norm(vector))
    result.update(embedding=pack_embedding(vector / norm) if norm else '',
                  embedding_version=EMBEDDING_VERSION,
                  metadata_key=metadata_key(metadata_paths),
                  metadata_lexical=pack_embedding(_lexical_embedding(features)),
                  metadata_entities=features['entity_counts'])
    return result


def _encode_document(path, tokenizer, model, metadata_paths, ocr_engine=None, ner=None):
    from collections import Counter
    from .ocr import anonymize_entities, sample_pdf
    features = metadata_features(metadata_paths)
    raw_texts, layout, extraction = [], None, {}
    if path is not None:
        suffix = Path(path).suffix.lower()
        if suffix == '.pdf':
            try:
                extraction = sample_pdf(path, ocr_engine)
                raw_texts, layout = extraction['texts'], extraction['layout']
            except Exception:
                extraction = {'ocr_failed': True}
        elif suffix in ('.docx', '.txt', '.md', '.csv', '.tsv'):
            try:
                raw_texts = _sample_text(path)
                extraction = {'native_pages': len(raw_texts), 'sampled_pages': len(raw_texts)}
            except Exception:
                extraction = {'text_failed': True}

    content, entity_counts = [], Counter()
    for text in raw_texts:
        clean, counts = anonymize_entities(text, ner)
        if clean:
            content.append(clean)
        entity_counts.update(counts)
    passages, weights = [], []
    if content:
        passages.extend('passage: текст документа: ' + value for value in content)
        weights.extend([.60 / len(content)] * len(content))
    metadata_passages, metadata_weights = _metadata_passages(features, bool(content))
    passages.extend(metadata_passages)
    weights.extend(metadata_weights)
    if not passages:
        return {'embedding_version': EMBEDDING_VERSION, 'embedding': '', 'embedding_chunks': 0}
    vector, chunks = _encode_passages(tokenizer, model, passages, weights, return_chunks=True)
    import numpy as np
    content_vector = chunks[:len(content)].mean(axis=0) if content else None
    content_norm = float(np.linalg.norm(content_vector)) if content else 0.0
    if extraction.get('ocr_pages'):
        source = 'ocr+metadata'
    elif content:
        source = 'text+metadata'
    else:
        source = 'metadata'
    return {'embedding_version': EMBEDDING_VERSION, 'embedding': pack_embedding(vector),
            'content_embedding': pack_embedding(content_vector) if content_norm else '',
            'content_embedding_norm': content_norm, 'content_embedding_legacy': False,
            'embedding_chunks': len(passages),
            'metadata_lexical': pack_embedding(_lexical_embedding(features)),
            'metadata_entities': features['entity_counts'], 'ner_entities': dict(entity_counts),
            'metadata_key': metadata_key(metadata_paths),
            'layout_embedding': pack_embedding(layout) if layout is not None and any(layout) else '',
            'sampled_pages': extraction.get('sampled_pages', 0),
            'ocr_pages': extraction.get('ocr_pages', 0),
            'ocr_provider': (getattr(ocr_engine, 'provider', '') if extraction.get('ocr_pages') else ''),
            'native_pages': extraction.get('native_pages', 0),
            'page_count': extraction.get('page_count', 0),
            'ocr_failed': extraction.get('ocr_failed', False),
            'text_failed': extraction.get('text_failed', False),
            'embedding_source': source}


def _encode_metadata(tokenizer, model, metadata_paths):
    return _encode_document(None, tokenizer, model, metadata_paths)


def embedding_worker(connection, directory, ocr_model=None):
    """Extract E5 embeddings in an isolated process; only numeric vectors cross IPC."""
    try:
        tokenizer, model = _load_model(directory)
    except Exception:
        tokenizer = model = None
    from .ocr import load_ner, load_ocr
    ner = ocr_engine = None
    parsers_loaded = False
    try:
        while True:
            request = connection.recv()
            if request is None:
                break
            try:
                if model is None:
                    result = {'embedding_version': EMBEDDING_VERSION, 'embedding_failed': True,
                              'info': 'Локальная модель embeddings не загрузилась'}
                else:
                    metadata_paths = request.get('metadata_paths', []) if isinstance(request, dict) else [request]
                    path = request.get('path') if isinstance(request, dict) else None
                    cached = request.get('cached_similarity') if isinstance(request, dict) else None
                    if cached and reusable_similarity(cached):
                        result = _refresh_metadata(cached, tokenizer, model, metadata_paths)
                    else:
                        if not parsers_loaded:
                            try:
                                ner = load_ner()
                            except Exception:
                                ner = None
                            try:
                                ocr_engine = load_ocr(ocr_model) if ocr_model else None
                            except Exception:
                                ocr_engine = None
                            parsers_loaded = True
                        result = _encode_document(path, tokenizer, model, metadata_paths, ocr_engine, ner)
            except Exception:
                result = {'embedding_version': EMBEDDING_VERSION, 'embedding_failed': True,
                          'info': 'Embedding документа построить не удалось'}
            connection.send(result)
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()
