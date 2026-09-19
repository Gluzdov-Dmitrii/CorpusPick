import copy
from pathlib import Path
from unittest.mock import patch
import pytest
from corpuspick.core import Session


def test_new_rename_recovers_interrupted_move_after_restart(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    (root / 'one.txt').write_text('one')
    (root / 'two.txt').write_text('two')
    state = tmp_path / 'state'
    session = Session(root, state)
    session.scan(hash_files=False)
    one = next(d for d in session.state['documents'] if d['path'] == 'one.txt')
    session.state['moves'] = [dict(source='one.txt', destination='moved.txt',
        origin=one['origin'], identity=one['identity'], hash='', method='rename', done=False)]
    session.save()
    (root / 'one.txt').rename(root / 'moved.txt')
    session.close()
    session = Session(root, state)
    try:
        two = next(d for d in session.state['documents'] if d['path'] == 'two.txt')
        result = session.rename_documents([two], 'new')
        assert result['moved'] == 1
        assert not result['errors']
        assert (root / 'moved.txt').read_text() == 'one'
        assert (root / 'new.txt').read_text() == 'two'
        assert all(m['done'] for m in session.state['moves'])
    finally:
        session.close()


def test_failed_rename_does_not_block_next_rename(tmp_path):
    root = tmp_path / 'files'
    root.mkdir()
    (root / 'one.txt').write_text('one')
    session = Session(root, tmp_path / 'state')
    try:
        session.scan(hash_files=False)
        docs = copy.deepcopy(session.state['documents'])
        with patch('corpuspick.core.move_exclusive', side_effect=PermissionError()):
            assert session.rename_documents(docs, 'failed')['errors']
        assert session.rename_documents(docs, 'success')['moved'] == 1
        assert (root / 'success.txt').read_text() == 'one'
        assert not (root / 'failed.txt').exists()
    finally:
        session.close()
