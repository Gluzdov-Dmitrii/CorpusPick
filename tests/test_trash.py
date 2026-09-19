import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from corpuspick.core import Session
from corpuspick.recycle import recycle_file


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'input'
        self.root.mkdir()
        self.bin = self.base / 'fake-bin'
        self.bin.mkdir()
        self.session = Session(self.root, self.base / 'state')

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def put(self, name, content='synthetic'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def fake_recycle(self, path):
        destination = self.bin / str(len(list(self.bin.iterdir())))
        path.rename(destination)

    def test_duplicates_ignore_legacy_mark_keep_root(self):
        self.put('root.txt')
        self.put('a/nested.txt')
        self.put('b/copy.txt')
        self.put('unique.txt', 'different')
        self.session.scan()
        self.session.mark(str(Path('a/nested.txt')))
        plan = self.session.duplicate_plan()
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]['keep']['path'], 'root.txt')
        with patch('corpuspick.core.recycle_file', self.fake_recycle):
            result = self.session.trash_documents(duplicate_plan=plan)
        self.assertEqual(result['trashed'], 2)
        self.assertTrue((self.root / 'root.txt').exists())
        self.assertTrue((self.root / 'unique.txt').exists())
        self.assertEqual(len(self.session.state['documents']), 2)

    def test_root_preferred_without_mark(self):
        self.put('z.txt')
        self.put('nested/a.txt')
        self.assertEqual(self.session.duplicate_plan()[0]['keep']['path'], 'z.txt')

    def test_changed_keeper_prevents_recycling(self):
        keeper = self.put('a.txt')
        self.put('b.txt')
        plan = self.session.duplicate_plan()
        keeper.write_text('changed')
        with patch('corpuspick.core.recycle_file') as recycle:
            result = self.session.trash_documents(duplicate_plan=plan)
            recycle.assert_not_called()
        self.assertEqual(result['trashed'], 0)
        self.assertEqual(len(result['errors']), 1)

    def test_missing_keeper_prevents_recycling(self):
        keeper = self.put('a.txt')
        self.put('b.txt')
        plan = self.session.duplicate_plan()
        keeper.unlink()
        with patch('corpuspick.core.recycle_file') as recycle:
            self.session.trash_documents(duplicate_plan=plan)
            recycle.assert_not_called()

    def test_changed_duplicate_prevents_recycling(self):
        self.put('a.txt')
        duplicate = self.put('b.txt')
        plan = self.session.duplicate_plan()
        duplicate.write_text('different')
        with patch('corpuspick.core.recycle_file') as recycle:
            self.session.trash_documents(duplicate_plan=plan)
            recycle.assert_not_called()

    def test_single_file_and_unavailable_bin(self):
        path = self.put('a.txt')
        document = self.session.scan()[0]
        with patch('corpuspick.core.recycle_file', side_effect=ValueError('Bin unavailable')):
            result = self.session.trash_documents([document])
        self.assertTrue(path.exists())
        self.assertEqual(result['trashed'], 0)
        with patch('corpuspick.core.recycle_file', self.fake_recycle):
            result = self.session.trash_documents([document])
        self.assertFalse(path.exists())
        self.assertEqual(result['trashed'], 1)

    def test_pending_move_is_cancelled_before_trash(self):
        self.put('a.txt')
        document = self.session.scan()[0]
        self.session.state['moves'] = [dict(
            source='a.txt', destination='renamed.txt', done=False)]
        with patch('corpuspick.core.recycle_file', self.fake_recycle):
            result = self.session.trash_documents([document])
        self.assertEqual(result['trashed'], 1)
        self.assertFalse((self.root / 'a.txt').exists())
        self.assertFalse((self.root / 'renamed.txt').exists())
        self.assertFalse(any(not move['done'] for move in self.session.state['moves']))

    @unittest.skipUnless(os.name == 'nt', 'Windows shell callback')
    def test_shell_callback_refuses_permanent_delete(self):
        from corpuspick.recycle import _recycle_sink
        from win32com.server.exception import COMException
        from win32com.shell import shellcon
        sink = _recycle_sink()
        with self.assertRaises(COMException):
            sink.PreDeleteItem(0, None)
        self.assertEqual(sink.PreDeleteItem(shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE, None), 0)


@unittest.skipUnless(os.environ.get('CORPUSPICK_TEST_RECYCLE') == '1', 'Optional Windows recycle integration')
class RecycleIntegration(unittest.TestCase):
    def test_synthetic_file_recycled_and_restored(self):
        import pythoncom
        from win32com.shell import shell, shellcon
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'corpuspick-synthetic-recycle-test.txt'
            path.write_text('Only synthetic content')
            recycled = recycle_file(path)
            self.assertFalse(path.exists())
            # Inspect only the precise synthetic item returned by our operation.
            self.assertEqual(Path(recycled).read_text(), 'Only synthetic content')
            pythoncom.CoInitialize()
            operation = item = folder = None
            try:
                operation = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None,
                    pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IFileOperation)
                operation.SetOperationFlags(shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI)
                item = shell.SHCreateItemFromParsingName(recycled, None, shell.IID_IShellItem)
                folder = shell.SHCreateItemFromParsingName(directory, None, shell.IID_IShellItem)
                operation.MoveItem(item, folder, path.name, None)
                operation.PerformOperations()
                self.assertFalse(operation.GetAnyOperationsAborted())
                self.assertEqual(path.read_text(), 'Only synthetic content')
            finally:
                operation = item = folder = None
                pythoncom.CoUninitialize()
