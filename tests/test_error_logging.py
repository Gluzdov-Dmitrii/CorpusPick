from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from corpuspick.error_logging import close_error_logging, log_exception, setup_error_logging


class ErrorLoggingTests(unittest.TestCase):
    def test_native_crash_log_keeps_descriptor_open(self):
        import corpuspick.error_logging as module
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(module, '_NATIVE_LOG', None), \
                patch.object(module.faulthandler, 'enable') as enable:
            try:
                module.enable_native_crash_logging(Path(directory))
                stream = module._NATIVE_LOG
                self.assertFalse(stream.closed)
                enable.assert_called_once_with(file=stream, all_threads=True)
                module.enable_native_crash_logging(Path(directory))
                self.assertEqual(enable.call_count, 1)
            finally:
                if module._NATIVE_LOG is not None:
                    module._NATIVE_LOG.close()

    def test_exception_log_sanitizes_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'LOCALAPPDATA': directory}):
            try:
                path = setup_error_logging()
                try:
                    raise RuntimeError(r'failed C:\Sensitive Docs\Private Folder\secret.docx')
                except RuntimeError as exc:
                    log_exception('Synthetic failure', exc, operation='test')

                text = Path(path).read_text(encoding='utf-8')
                self.assertIn('Synthetic failure', text)
                self.assertIn('RuntimeError', text)
                self.assertIn('<path>', text)
                self.assertNotIn('Sensitive', text)
                self.assertNotIn('Private Folder', text)
                self.assertNotIn('secret.docx', text)
            finally:
                close_error_logging()
