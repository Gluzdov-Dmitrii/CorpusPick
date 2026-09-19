from pathlib import Path
from unittest.mock import patch
import pytest
from corpuspick.core import Session
from corpuspick.path_repair import shorten_paths, units, recover


def test_shorten_nested_paths_preserves_content_and_origins(tmp_path):
    root = tmp_path / 'files'
    folder = root / ('LongDirectory' * 4)
    folder.mkdir(parents=True)
    for tail in ('alpha.txt', 'beta.txt'):
        (folder / tail).write_text(tail)
    session = Session(root, tmp_path / 'state')
    try:
        session.scan(hash_files=False)
        origins = {d['origin'] for d in session.state['documents']}
        limit = units(root) + 25
        result = shorten_paths(session, limit=limit)
        assert result['moved'] > 0
        assert not result['errors']
        assert {d['origin'] for d in session.state['documents']} == origins
        for d in session.state['documents']:
            path = root / d['path']
            assert units(path) <= limit
            assert path.read_text() in ('alpha.txt', 'beta.txt')
            assert path.suffix == '.txt'
        again = shorten_paths(session, limit=limit)
        assert again['moved'] == 0
    finally:
        session.close()


def test_collision_does_not_overwrite(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    (root / 'abcdefghijk.txt').write_text('original')
    (root / 'abcdef.txt').write_text('occupied')
    session = Session(root, tmp_path / 'state')
    try:
        result = shorten_paths(session, limit=units(root) + 11)
        assert not result['errors']
        assert (root / 'abcdef.txt').read_text() == 'occupied'
        assert sorted(p.read_text() for p in root.iterdir()) == ['occupied', 'original']
    finally:
        session.close()


def test_directory_recovery_after_restart(tmp_path):
    root = tmp_path / 'files'
    folder = root / 'old'
    folder.mkdir(parents=True)
    (folder / 'one.txt').write_text('one')
    state = tmp_path / 'state'
    session = Session(root, state)
    session.scan(hash_files=False)
    info = folder.stat()
    session.state['path_repair'] = dict(source='old', destination='new', directory=True,
        identity=[info.st_dev, info.st_ino])
    session.save()
    folder.rename(root / 'new')
    session.close()
    session = Session(root, state)
    try:
        assert session.state['documents'][0]['path'] == str(Path('new/one.txt'))
        assert session.state['documents'][0]['origin'] == str(Path('old/one.txt'))
        assert 'path_repair' not in session.state
    finally:
        session.close()


def test_unicode_trim_and_readonly(tmp_path):
    from corpuspick.path_repair import trim
    assert trim('😀ab', 3) == '😀a'
    root = tmp_path / 'files'
    root.mkdir()
    session = Session(root, tmp_path / 'state', read_only=True)
    try:
        with pytest.raises(ValueError):
            shorten_paths(session)
    finally:
        session.close()
