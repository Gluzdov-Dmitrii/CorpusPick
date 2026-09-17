from pathlib import Path
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from corpuspick.office_cleanup import cleanup_owned_process


class OfficeCleanupTests(unittest.TestCase):
    def test_cleanup_uses_recorded_pid_and_start_time(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Path(directory) / 'owner.json'
            owner.write_text(json.dumps({'ownedPid': 1234, 'started': 5678}), encoding='utf-8')
            with patch('corpuspick.office_cleanup.os.name', 'nt'), \
                    patch('corpuspick.office_cleanup.subprocess.run',
                          return_value=SimpleNamespace(returncode=0)) as run:
                cleanup_owned_process(owner, 'WINWORD')

            run.assert_called_once()
            args, kwargs = run.call_args
            self.assertEqual(args[0][:4], ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy'])
            self.assertEqual(kwargs['env']['CORPUSPICK_OWNER_PID'], '1234')
            self.assertEqual(kwargs['env']['CORPUSPICK_OWNER_STARTED'], '5678')
            self.assertEqual(kwargs['env']['CORPUSPICK_OWNER_PROCESS'], 'WINWORD')

    def test_cleanup_ignores_missing_owner_file(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('corpuspick.office_cleanup.os.name', 'nt'), \
                patch('corpuspick.office_cleanup.subprocess.run',
                      side_effect=AssertionError('No owner means no process cleanup')):
            cleanup_owned_process(Path(directory) / 'missing.json', 'WINWORD')
