"""Bounded, metadata-only name grouping; never evidence of duplicate content."""
from pathlib import Path
import re
import unicodedata


def name_key(path):
    name = unicodedata.normalize('NFKC', Path(path).stem).casefold().replace('ё', 'е')
    return ' '.join(sorted(re.findall(r'\w+', name)))


def similar_order(documents, cancel, progress=lambda count: None, with_groups=False):
    names = [name_key(d['path']) for d in documents]
    grams = [{s[j:j + 3] for j in range(max(1, len(s) - 2))} for s in names]
    parents = list(range(len(documents)))
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    # Two sorted neighbourhoods bound comparisons, avoiding an all-pairs scan.
    for reverse in (False, True):
        indices = sorted(range(len(names)), key=lambda i: names[i][::-1] if reverse else names[i])
        for position, i in enumerate(indices):
            if cancel.is_set():
                return None
            for j in indices[position + 1:position + 25]:
                score = 2 * len(grams[i] & grams[j]) / max(1, len(grams[i]) + len(grams[j]))
                if names[i] == names[j] or score >= .65:
                    parents[find(j)] = find(i)
            progress(position + 1)
    group_keys = {}
    for i, name in enumerate(names):
        group = find(i)
        group_keys[group] = min(group_keys.get(group, name), name)
    ordered = sorted(range(len(names)), key=lambda i: (
        group_keys[find(i)], names[i], documents[i].get('size') or 0,
        Path(documents[i]['path']).suffix.casefold(), documents[i]['path']))
    ranks = {documents[i]['path']: rank for rank, i in enumerate(ordered)}
    if with_groups:
        group_numbers = {}
        groups = {}
        for i in ordered:
            group = find(i)
            groups[documents[i]['path']] = group_numbers.setdefault(group, len(group_numbers))
        return ranks, groups
    return ranks
