"""Select a current filesystem item using the Windows Shell, without CLI quoting."""
import ctypes
from ctypes import wintypes
from pathlib import Path


def show_file(path):
    path = Path(path).absolute()
    if not path.is_file():
        raise FileNotFoundError('Файл отсутствует. Обновите список: возможно, он перемещён в Проводнике.')
    shell = ctypes.WinDLL('shell32')
    ole = ctypes.WinDLL('ole32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    shell.SHParseDisplayName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD, ctypes.c_void_p]
    shell.SHParseDisplayName.restype = ctypes.c_long
    shell.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
    shell.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)
    # RPC_E_CHANGED_MODE means COM was already initialized in another apartment.
    if initialized < 0 and initialized != -2147417850:
        raise OSError('Не удалось подключиться к Проводнику')
    item = ctypes.c_void_p()
    try:
        if shell.SHParseDisplayName(str(path), None, ctypes.byref(item), 0, None) < 0:
            raise OSError('Проводник не смог найти файл. Обновите список.')
        # With zero children the absolute PIDL denotes the file; Shell opens its parent.
        if shell.SHOpenFolderAndSelectItems(item, 0, None, 0) < 0:
            raise OSError('Не удалось выделить файл в Проводнике')
    finally:
        if item:
            ole.CoTaskMemFree(item)
        if initialized >= 0:
            ole.CoUninitialize()
