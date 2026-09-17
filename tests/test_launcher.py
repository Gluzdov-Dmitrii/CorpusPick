import json
import subprocess
from unittest.mock import patch
from corpuspick.launcher import record_exit, main


def test_exit_record_has_only_numeric_diagnostics(tmp_path):
    path = tmp_path / 'exit.jsonl'
    record_exit(path, 123, -1073741819)
    value = json.loads(path.read_text())
    assert value['exit_hex'] == '0xC0000005'
    assert set(value) == {'time', 'pid', 'exit_code', 'exit_hex'}


def test_detach_does_not_inherit_console():
    with patch('sys.argv', ['launcher', '--detach']), patch('subprocess.Popen') as popen:
        main()
    options = popen.call_args.kwargs
    assert options['stdin'] == subprocess.DEVNULL
    assert options['stdout'] == subprocess.DEVNULL
    assert options['stderr'] == subprocess.DEVNULL
    if hasattr(subprocess, 'DETACHED_PROCESS'):
        assert options['creationflags'] & subprocess.DETACHED_PROCESS
