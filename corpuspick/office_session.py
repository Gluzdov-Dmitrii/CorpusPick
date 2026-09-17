"""Reusable, bounded LibreOffice renderer. Owned by one preview worker."""
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import uuid

from .office_profile import create_profile, OFFICE_MEMORY_LIMIT, OFFICE_CONVERSION_TIMEOUT
from .process_job import ProcessJob


class OfficeSession:
    def __init__(self, soffice):
        self.soffice = Path(soffice)
        self.process = self.job = self.directory = self.reader = None
        self.replies = queue.Queue()
        self.office_pid = None

    def start(self):
        if self.process is not None:
            return
        program = self.soffice.parent
        interpreters = sorted(program.glob('python-core-*/bin/python.exe'))
        if os.name != 'nt' or not interpreters:
            raise OSError('Bundled LibreOffice Python required')
        python = interpreters[-1]
        try:
            self.directory = tempfile.TemporaryDirectory(prefix='corpuspick-office-session-')
            profile = Path(self.directory.name) / 'profile'
            create_profile(profile)
            self.job = ProcessJob(memory_limit=OFFICE_MEMORY_LIMIT)
            env = os.environ.copy()
            env['PYTHONHOME'] = str(python.parent.parent)
            env['PYTHONPATH'] = str(program)
            env['PATH'] = str(program) + os.pathsep + env.get('PATH', '')
            env['URE_BOOTSTRAP'] = 'vnd.sun.star.pathname:' + str(program / 'fundamental.ini')
            self.process = subprocess.Popen(
                [str(python), '-u', str(Path(__file__).with_name('office_bridge.py')),
                 str(program), str(profile), 'corpuspick_' + uuid.uuid4().hex],
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
            self.job.assign(self.process.pid)
            # Capture local objects so a closing/restarting session cannot confuse readers.
            stream, replies = self.process.stdout, self.replies
            def read():
                try:
                    for line in stream:
                        try:
                            replies.put(json.loads(line))
                        except ValueError:
                            pass
                finally:
                    replies.put(None)
            self.reader = threading.Thread(target=read, daemon=True)
            self.reader.start()
            self.process.stdin.write('ready\n')
            self.process.stdin.flush()
            response = self._receive()
            if not response.get('ready'):
                raise OSError('Office startup failed')
            self.office_pid = response['office_pid']
        except Exception:
            self.close(graceful=False)
            raise

    def _receive(self):
        try:
            response = self.replies.get(timeout=OFFICE_CONVERSION_TIMEOUT)
        except queue.Empty:
            raise subprocess.TimeoutExpired('Office renderer', OFFICE_CONVERSION_TIMEOUT) from None
        if response is None:
            raise OSError('Office renderer stopped')
        return response

    def render(self, source, output, filter_name):
        self.start()
        try:
            self.process.stdin.write(json.dumps({'source': str(source), 'output': str(output),
                                                'filter': filter_name}) + '\n')
            self.process.stdin.flush()
            response = self._receive()
            if response.get('reset') or not response.get('ok'):
                self.close()
            return response.get('ok', False)
        except Exception:
            self.close(graceful=False)
            raise

    def stats(self, source):
        self.start()
        try:
            self.process.stdin.write(json.dumps({'action': 'stats', 'source': str(source)}) + '\n')
            self.process.stdin.flush()
            response = self._receive()
            if response.get('reset'):
                self.close(graceful=False)
                return {'failed': True, 'info': 'Сессия LibreOffice перезапущена после ошибки документа.'}
            if not response.get('ok'):
                return {'failed': True, 'info': 'LibreOffice не смог открыть документ; файл пропущен.'}
            values = response.get('stats')
            if not isinstance(values, dict):
                self.close(graceful=False)
                return {'failed': True, 'info': 'LibreOffice вернул некорректные данные статистики.'}
            result = {}
            for key in ('pages', 'figures', 'tables'):
                value = values.get(key)
                result[key] = value if type(value) is int and value >= 0 else None
            result['info'] = 'Точный подсчёт LibreOffice по текущей разметке документа.'
            return result
        except Exception:
            self.close(graceful=False)
            raise

    def close(self, graceful=True):
        if graceful and self.process is not None and self.process.poll() is None:
            try:
                self.process.stdin.write('{"stop":true}\n')
                self.process.stdin.flush()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.job is not None:
            self.job.close()
            self.job = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)
            if self.reader is not None:
                self.reader.join(timeout=2)
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None
        self.reader = self.office_pid = None
        self.replies = queue.Queue()
