"""Local diagnostic logging without document content."""
from __future__ import annotations

import logging
import faulthandler
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
import traceback


LOGGER_NAME = 'corpuspick'
MAX_LOG_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 4
_LOG_PATH: Path | None = None
_HOOKS_INSTALLED = False
_NATIVE_LOG = None
_WINDOWS_PATH = re.compile(r'(?i)(?:[a-z]:\\|\\\\)[^\r\n"\']+')


def _default_data_root() -> Path:
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share'))
    return base / 'CorpusPick'


def log_path(data_root: Path | None = None) -> Path:
    return (data_root or _default_data_root()) / 'logs' / 'corpuspick.log'


def sanitize(value) -> str:
    text = str(value)
    text = _WINDOWS_PATH.sub('<path>', text)
    return ''.join(ch if ch.isprintable() or ch in '\r\n\t' else '?' for ch in text)


def setup_error_logging(data_root: Path | None = None) -> Path:
    global _LOG_PATH, _HOOKS_INSTALLED
    path = log_path(data_root)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if _LOG_PATH != path:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path, maxBytes=MAX_LOG_BYTES, backupCount=BACKUP_COUNT, encoding='utf-8')
            handler.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S'))
            logger.addHandler(handler)
            _LOG_PATH = path
        except Exception:
            _LOG_PATH = None
    if not _HOOKS_INSTALLED:
        _install_global_hooks()
        _HOOKS_INSTALLED = True
    return path


def close_error_logging() -> None:
    global _LOG_PATH
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    _LOG_PATH = None


def enable_native_crash_logging(data_root: Path | None = None) -> None:
    """Keep an open descriptor for fatal Python/native stack traces, without locals."""
    global _NATIVE_LOG
    if _NATIVE_LOG is not None:
        return
    path = log_path(data_root).with_name('native-crash.log')
    stream = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open('ab')
        faulthandler.enable(file=stream, all_threads=True)
        _NATIVE_LOG = stream
    except Exception:
        if stream is not None:
            stream.close()


def _install_global_hooks() -> None:
    previous_excepthook = sys.excepthook
    previous_threading_excepthook = getattr(threading, 'excepthook', None)

    def excepthook(exc_type, exc, tb):
        log_exception('Uncaught main-thread exception', exc, tb)
        previous_excepthook(exc_type, exc, tb)

    def threading_excepthook(args):
        log_exception(f'Uncaught thread exception: {args.thread.name}', args.exc_value, args.exc_traceback)
        if previous_threading_excepthook:
            previous_threading_excepthook(args)

    sys.excepthook = excepthook
    if previous_threading_excepthook:
        threading.excepthook = threading_excepthook


def log_message(message: str, **context) -> None:
    setup_error_logging()
    extra = _format_context(context)
    logging.getLogger(LOGGER_NAME).info('%s%s', sanitize(message), extra)


def log_exception(message: str, exc: BaseException, tb=None, **context) -> None:
    setup_error_logging()
    if tb is None:
        tb = exc.__traceback__
    lines = traceback.format_exception(type(exc), exc, tb)
    details = sanitize(''.join(lines))
    extra = _format_context(context)
    logging.getLogger(LOGGER_NAME).error('%s%s\n%s', sanitize(message), extra, details)


def _format_context(context: dict) -> str:
    if not context:
        return ''
    parts = []
    for key, value in sorted(context.items()):
        if value is None:
            continue
        parts.append(f'{key}={sanitize(value)}')
    return ' [' + ' '.join(parts) + ']' if parts else ''
