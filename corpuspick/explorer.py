"""Select a current filesystem item using the Windows Shell, without CLI quoting."""
import ctypes
from ctypes import wintypes
from pathlib import Path

MAX_EXPLORER_WINDOWS = 12
MAX_EXPLORER_ITEMS = 512


def show_file(path):
    show_files([path])


def show_files(paths):
    folders = {}
    for path in dict.fromkeys(Path(p).absolute() for p in paths):
        if not path.is_file():
            raise FileNotFoundError('Файл отсутствует. Обновите список: возможно, он перемещён в Проводнике.')
        folders.setdefault(path.parent, []).append(path)
    if not folders:
        return
    total = sum(len(files) for files in folders.values())
    if len(folders) > MAX_EXPLORER_WINDOWS:
        raise ValueError(
            f'Выбрано файлов из {len(folders)} папок. Чтобы не перегружать Проводник, '
            f'выберите не больше {MAX_EXPLORER_WINDOWS} папок за раз.')
    if total > MAX_EXPLORER_ITEMS:
        raise ValueError(
            f'Выбрано файлов: {total}. Чтобы не перегружать Проводник, '
            f'выберите не больше {MAX_EXPLORER_ITEMS} файлов за раз.')
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
    shell.ILFindLastID.argtypes = [ctypes.c_void_p]
    shell.ILFindLastID.restype = ctypes.c_void_p
    initialized = ole.CoInitializeEx(None, 2)
    # RPC_E_CHANGED_MODE means COM was already initialized in another apartment.
    if initialized < 0 and initialized != -2147417850:
        raise OSError('Не удалось подключиться к Проводнику')
    try:
        for folder, files in folders.items():
            allocated = []
            try:
                for path in [folder, *files]:
                    item = ctypes.c_void_p()
                    allocated.append(item)
                    if shell.SHParseDisplayName(str(path), None, ctypes.byref(item), 0, None) < 0:
                        raise OSError('Проводник не смог найти файл. Обновите список.')
                children = (ctypes.c_void_p * len(files))(*(shell.ILFindLastID(p) for p in allocated[1:]))
                if shell.SHOpenFolderAndSelectItems(allocated[0], len(files), children, 0) < 0:
                    raise OSError('Не удалось выделить файлы в Проводнике')
            finally:
                for item in allocated:
                    if item:
                        ole.CoTaskMemFree(item)
    finally:
        if initialized >= 0:
            ole.CoUninitialize()
