"""Local TF-IDF name matching with pairwise-verified groups; no document reads."""
from collections import Counter, defaultdict
from functools import lru_cache
import heapq
from math import log, sqrt
from pathlib import Path
import re
import unicodedata


def normalize(value):
    return unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')


def name_key(path):
    return ' '.join(sorted(re.findall(r'[^\W_]+', normalize(Path(path).stem))))


def _features(name, characters=False):
    words = name.split()
    if characters:
        return Counter(g for word in words if not word.isdigit()
                       for n in (3, 4) for g in [word[i:i+n] for i in range(max(0, len(word)-n+1))])
    return Counter(w for w in words if len(w) > 1 and w not in {'копия', 'copy', 'final', 'версия'})


def _vectors(names, characters=False):
    counts = [_features(name, characters) for name in names]
    frequency = Counter(f for row in counts for f in row)
    vectors = []
    for row in counts:
        weights = {f: (1 + log(n)) * (1 + log((1 + len(names)) / (1 + frequency[f]))) *
                   (.25 if f.isdigit() else 1) for f, n in row.items()}
        norm = sqrt(sum(v*v for v in weights.values())) or 1
        vectors.append({f: v/norm for f, v in weights.items()})
    return vectors


def similar_order(documents, cancel, progress=lambda count: None, with_groups=False,
                  exhaustive=False, threshold=.62):
    # Collapse identical normalized names before matching (size/type do not change a name).
    buckets = defaultdict(list)
    for i, d in enumerate(documents):
        if cancel.is_set():
            return None
        buckets[name_key(d['path'])].append(i)
    names = sorted(buckets)
    words, chars = _vectors(names), _vectors(names, True)
    def cosine(a, b):
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(f, 0) for f, v in a.items())
    @lru_cache(maxsize=8192)
    def score(i, j):
        return .65 * cosine(words[i], words[j]) + .35 * cosine(chars[i], chars[j])
    if exhaustive:
        # Strongest-pair-first complete-link avoids a weak early filename match
        # splitting a later, clearly stronger set.
        if len(names) < 2:
            clusters = [list(range(len(names)))] if names else []
        else:
            try:
                import numpy as np
                from scipy.sparse import csr_matrix, hstack
                from sklearn.cluster import AgglomerativeClustering

                def matrix(vectors):
                    columns = {feature: column for column, feature in enumerate(
                        sorted({feature for vector in vectors for feature in vector}))}
                    rows, cols, values = [], [], []
                    for row, vector in enumerate(vectors):
                        for feature, value in vector.items():
                            rows.append(row)
                            cols.append(columns[feature])
                            values.append(value)
                    return csr_matrix((values, (rows, cols)), shape=(len(vectors), len(columns)))

                combined = hstack((matrix(words) * sqrt(.65), matrix(chars) * sqrt(.35)), format='csr')
                distances = 1 - (combined @ combined.T).toarray().astype('float32')
                np.fill_diagonal(distances, 0)
                labels = AgglomerativeClustering(
                    n_clusters=None, metric='precomputed', linkage='complete',
                    distance_threshold=1 - threshold + 1e-7,
                ).fit_predict(distances)
                grouped = defaultdict(list)
                for index, label in enumerate(labels):
                    grouped[int(label)].append(index)
                clusters = sorted(grouped.values(), key=lambda members: min(members))
                progress(len(names))
            except Exception:
                # Dependency failure must not make file operations unavailable.
                adjacency = {i: {} for i in range(len(names))}
                heap = []
                for i in range(len(names)):
                    for j in range(i):
                        value = score(i, j)
                        if value >= threshold:
                            adjacency[i][j] = adjacency[j][i] = value
                            heapq.heappush(heap, (-value, j, i, 0, 0))
                    progress(i + 1)
                members, generations = {i: [i] for i in range(len(names))}, [0] * len(names)
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
                    for c, value in updated.items():
                        adjacency[a][c] = adjacency[c][a] = value
                        left, right = sorted((a, c))
                        heapq.heappush(heap, (-value, left, right,
                                             generations[left], generations[right]))
                clusters = sorted(members.values(), key=lambda members: min(members))
    else:
        postings = defaultdict(list)
        for i in range(len(names)):
            for f in [*(('w', f) for f in words[i]), *(('c', f) for f in chars[i])]:
                postings[f].append(i)
        clusters, assigned = [], {}
        for i, name in enumerate(names):
            if cancel.is_set():
                return None
            features = [*(('w', f) for f in words[i]), *(('c', f) for f in chars[i])]
            candidates = Counter()
            # Search by rare shared features across the catalog, not lexicographic neighbours.
            for f in sorted(features, key=lambda f: (len(postings[f]), f))[:24]:
                if len(postings[f]) <= 512:
                    candidates.update(j for j in postings[f] if j < i)
            options = sorted(candidates, key=lambda j: (-candidates[j], j))[:64]
            groups = sorted({assigned[j] for j in options},
                            key=lambda g: (-score(i, clusters[g][0]), g))
            chosen = None
            for g in groups:
                # Complete compatibility prevents transitive A-B-C chains.
                compatible = True
                for j in clusters[g]:
                    if cancel.is_set():
                        return None
                    if score(i, j) < threshold:
                        compatible = False
                        break
                if compatible:
                    chosen = g
                    break
            if chosen is None:
                chosen = len(clusters)
                clusters.append([])
            clusters[chosen].append(i)
            assigned[i] = chosen
            progress(i + 1)
    ranks, groups = {}, {}
    for group, members in enumerate(clusters):
        entries = [i for member in members for i in buckets[names[member]]]
        entries.sort(key=lambda i: (name_key(documents[i]['path']), documents[i].get('size') or 0,
                                   Path(documents[i]['path']).suffix.casefold(), documents[i]['path']))
        for i in entries:
            path = documents[i]['path']
            ranks[path] = len(ranks)
            groups[path] = group
    return (ranks, groups) if with_groups else ranks
