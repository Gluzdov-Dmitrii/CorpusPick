"""Isolate document parsers so a slow/corrupt document cannot monopolize the UI."""
import multiprocessing
import os
from pathlib import Path
import time


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


class StatsWorker:
    def __init__(self, timeout=30, target=_worker_main, retry_label='Статистика'):
        self.timeout = timeout
        self.target = target
        self.retry_label = retry_label
        self.process = self.connection = None

    def _start(self):
        if self.process is not None:
            return
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.process = context.Process(target=self.target, args=(child,), daemon=True)
        try:
            self.process.start()
        except Exception:
            self.connection.close()
            self.process = self.connection = None
            raise
        finally:
            child.close()

    def count(self, path, cancel):
        from .core import Cancelled
        if cancel.is_set():
            raise Cancelled()
        self._start()
        deadline = time.monotonic() + self.timeout
        try:
            self.connection.send(str(path))
            while True:
                if cancel.is_set():
                    self.close()
                    raise Cancelled()
                if self.connection.poll(0.05):
                    return self.connection.recv()
                if time.monotonic() >= deadline:
                    self.close()
                    return {'failed': True, 'info': f'Обработка превысила {self.timeout:g} с; файл пропущен. Нажмите «{self.retry_label}» для повтора.'}
                if not self.process.is_alive():
                    raise EOFError()
        except (EOFError, BrokenPipeError, OSError):
            self.close()
            return {'failed': True, 'info': 'Процесс подсчёта завершился; файл пропущен'}

    def close(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
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
