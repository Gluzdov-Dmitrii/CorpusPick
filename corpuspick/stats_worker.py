"""Isolate document parsers so a slow/corrupt document cannot monopolize the UI."""
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def _guarded_worker(connection, target, init_args):
    # No parser/subprocess runs until the parent has established kernel ownership.
    try:
        if connection.recv() != 'ready':
            return
        target(connection, *init_args)
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


def _worker_main(connection):
    # All output is local numeric metadata over IPC, never parser logs/content.
    import sys
    with open(os.devnull, 'w') as quiet:
        sys.stdout = sys.stderr = quiet
        from .statistics import document_stats
        try:
            while True:
                path = connection.recv()
                if path is None:
                    break
                try:
                    result = document_stats(Path(path))
                except Exception:
                    result = {'failed': True, 'info': 'Статистика недоступна: файл повреждён или защищён'}
                connection.send(result)
        except (EOFError, BrokenPipeError):
            pass
        finally:
            connection.close()


def libreoffice_stats_worker(connection):
    """Use one bounded LibreOffice session for accurate Writer statistics."""
    import sys
    with open(os.devnull, 'w') as quiet:
        sys.stdout = sys.stderr = quiet
        office = None
        try:
            from .office_session import OfficeSession
            from .preview import _find_soffice
            soffice = _find_soffice()
            if soffice:
                office = OfficeSession(soffice)
            with tempfile.TemporaryDirectory(prefix='corpuspick-stats-') as directory:
                staged_dir = Path(directory)
                while True:
                    request = connection.recv()
                    if request is None:
                        break
                    try:
                        if office is None:
                            result = {'failed': True, 'info': 'LibreOffice не найден; установите или подготовьте preview runtime.'}
                        else:
                            source = Path(request['path'])
                            staged = staged_dir / ('document' + source.suffix.lower())
                            staged.unlink(missing_ok=True)
                            shutil.copyfile(source, staged)
                            result = office.stats(staged)
                    except Exception:
                        result = {'failed': True, 'info': 'LibreOffice не смог прочитать документ; файл пропущен.'}
                    connection.send(result)
        except (EOFError, BrokenPipeError):
            pass
        finally:
            if office is not None:
                office.close()
            connection.close()


class StatsWorker:
    def __init__(self, timeout=30, target=_worker_main, retry_label='Статистика', init_args=()):
        self.timeout = timeout
        self.target = target
        self.init_args = init_args
        self.retry_label = retry_label
        self.process = self.connection = None
        self.job = None

    def _start(self):
        if self.process is not None:
            return
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_guarded_worker, args=(child, self.target, self.init_args), daemon=True)
        try:
            if os.name == 'nt':
                from .process_job import ProcessJob
                self.job = ProcessJob()
            self.process.start()
            if self.job is not None:
                self.job.assign(self.process.pid)
            self.connection.send('ready')
        except Exception:
            if self.job is not None:
                self.job.close()
                self.job = None
            if self.process.pid is not None:
                if self.process.is_alive():
                    self.process.terminate()
                self.process.join(timeout=2)
            self.process.close()
            self.connection.close()
            self.process = self.connection = None
            raise
        finally:
            child.close()

    def count(self, path, cancel, request=None):
        from .core import Cancelled
        if cancel.is_set():
            raise Cancelled()
        self._start()
        deadline = time.monotonic() + self.timeout
        try:
            self.connection.send(str(path) if request is None else request)
            while True:
                if cancel.is_set():
                    self.close(graceful=False)
                    raise Cancelled()
                if self.connection.poll(0.05):
                    return self.connection.recv()
                if time.monotonic() >= deadline:
                    from .error_logging import log_message
                    log_message('Worker timeout', worker=self.target.__name__, timeout=self.timeout)
                    self.close(graceful=False)
                    return {'failed': True, 'info': f'Обработка превысила {self.timeout:g} с; файл пропущен. Нажмите «{self.retry_label}» для повтора.'}
                if not self.process.is_alive():
                    raise EOFError()
        except (EOFError, BrokenPipeError, OSError):
            from .error_logging import log_message
            self.process.join(timeout=.1)
            log_message('Worker disconnected', worker=self.target.__name__,
                        exitcode=self.process.exitcode)
            self.close(graceful=False)
            return {'failed': True, 'info': 'Процесс подсчёта завершился; файл пропущен'}

    def close(self, graceful=True):
        # An idle worker can release its temporary profile normally. Busy/hung
        # workers still hit the kernel cleanup after this short bounded grace.
        if graceful and self.job is not None and self.process is not None and self.process.is_alive():
            try:
                self.connection.send(None)
                self.process.join(timeout=4)
            except (OSError, EOFError, BrokenPipeError):
                pass
        if self.job is not None:
            self.job.close()
            self.job = None
        if self.process is not None:
            if self.process.is_alive():
                self._terminate_process_tree()
            self.process.join(timeout=1)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=1)
            self.process.close()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _terminate_process_tree(self):
        if os.name == 'nt' and getattr(self.process, 'pid', None):
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            try:
                completed = subprocess.run(
                    ['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, check=False, creationflags=flags)
                if completed.returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.process.terminate()
