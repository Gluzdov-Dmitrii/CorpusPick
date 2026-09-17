import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from corpuspick.stats_worker import StatsWorker


def child_worker(connection):
    connection.recv()
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    connection.send({'worker': os.getpid(), 'child': child.pid})
    connection.recv()


def open_handle(pid):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    return api, api.OpenProcess(0x100000, False, pid)


@pytest.mark.skipif(os.name != 'nt', reason='Windows kernel job integration')
@pytest.mark.parametrize('crash', [False, True])
def test_worker_and_grandchild_die_with_owner(crash):
    script = (
        "import sys, json, threading; sys.path.insert(0, 'tests'); "
        "from test_process_job import child_worker; "
        "from corpuspick.stats_worker import StatsWorker; "
        "w=StatsWorker(target=child_worker); "
        "print(json.dumps(w.count('go', threading.Event())), flush=True); "
        "sys.stdin.readline(); w.close()"
    )
    parent = subprocess.Popen([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True)
    handles = []
    try:
        # A bounded reader keeps a startup failure from hanging the test suite.
        lines = []
        reader = threading.Thread(target=lambda: lines.append(parent.stdout.readline()), daemon=True)
        reader.start()
        reader.join(15)
        assert lines and lines[0], 'synthetic parent did not start'
        pids = json.loads(lines[0])
        handles = [open_handle(pid) for pid in pids.values()]
        assert all(handle for _, handle in handles)
        if crash:
            parent.kill()
        else:
            parent.stdin.write('\n')
            parent.stdin.flush()
        parent.wait(timeout=10)
        for api, handle in handles:
            assert api.WaitForSingleObject(handle, 5000) == 0
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=10)
        for api, handle in handles:
            if handle:
                api.CloseHandle(handle)
        parent.stdin.close()
        parent.stdout.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows job setup')
def test_assignment_failure_never_runs_target(monkeypatch):
    from corpuspick.process_job import ProcessJob
    def fail(*_):
        raise OSError('synthetic job assignment failure')
    monkeypatch.setattr(ProcessJob, 'assign', fail)
    with StatsWorker(target=child_worker) as worker:
        with pytest.raises(OSError):
            worker.count('go', threading.Event())
        assert worker.process is None
        assert worker.job is None


@pytest.mark.skipif(os.name != 'nt', reason='Windows external command job')
@pytest.mark.parametrize('timeout', [False, True])
def test_external_command_cleans_descendant(tmp_path, timeout):
    from corpuspick.process_job import run_owned_command
    marker = tmp_path / 'synthetic-pid.txt'
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        f"Path({str(marker)!r}).write_text(str(p.pid)); "
        f"time.sleep({120 if timeout else 0})"
    )
    if timeout:
        with pytest.raises(subprocess.TimeoutExpired):
            run_owned_command([sys.executable, '-c', script], timeout=2)
    else:
        assert run_owned_command([sys.executable, '-c', script], timeout=10).returncode == 0
    assert marker.exists()
    api, handle = open_handle(int(marker.read_text()))
    if handle:
        try:
            assert api.WaitForSingleObject(handle, 5000) == 0
        finally:
            api.CloseHandle(handle)
