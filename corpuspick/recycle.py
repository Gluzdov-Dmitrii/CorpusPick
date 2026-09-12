"""Recycle-only Windows shell operation. Never fall back to permanent deletion."""
import os


def _recycle_sink():
    from win32com.shell import shellcon
    from win32com.server.exception import COMException
    from send2trash.win.IFileOperationProgressSink import FileOperationProgressSink

    class RecycleOnlySink(FileOperationProgressSink):
        def PreDeleteItem(self, flags, item):
            # A Python HRESULT return is not enough to abort the COM callback.
            if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE:
                raise COMException('Recycle bin unavailable', scode=-2147467260)  # E_ABORT
            return 0

    return RecycleOnlySink()


def recycle_file(path):
    if os.name != 'nt':
        raise ValueError('Корзина в этой версии доступна только в Windows')
    try:
        import pythoncom
        import pywintypes
        from win32com.shell import shell, shellcon
        sink = _recycle_sink()
    except ImportError:
        raise ValueError('Для корзины установите зависимости: python -m pip install -r requirements.txt') from None

    pythoncom.CoInitialize()
    operation = item = wrapped = None
    try:
        operation = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None,
                                               pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IFileOperation)
        flags = (shellcon.FOF_NOCONFIRMATION | shellcon.FOF_NOERRORUI | shellcon.FOF_SILENT |
                 shellcon.FOFX_EARLYFAILURE | 0x20000000 | 0x00080000)
        operation.SetOperationFlags(flags)  # ADDUNDORECORD + RECYCLEONDELETE
        item = shell.SHCreateItemFromParsingName(str(path), None, shell.IID_IShellItem)
        wrapped = pythoncom.WrapObject(sink, shell.IID_IFileOperationProgressSink)
        operation.DeleteItem(item, wrapped)
        result = operation.PerformOperations()
        if result or operation.GetAnyOperationsAborted() or not sink.newItem:
            raise ValueError('Не удалось отправить файл в корзину. Безвозвратное удаление отключено.')
        return sink.newItem
    except pywintypes.com_error:
        raise ValueError('Корзина недоступна или файл занят. Файл не удаляется безвозвратно.') from None
    finally:
        operation = item = wrapped = None
        pythoncom.CoUninitialize()
