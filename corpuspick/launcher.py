"""Detached GUI supervisor; diagnostics contain no document data."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


def record_exit(path, pid, code):
    record = {'time': datetime.now(timezone.utc).isoformat(), 'pid': pid,
              'exit_code': code, 'exit_hex': f'0x{code & 0xffffffff:08X}'}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--detach', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.detach:
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
        subprocess.Popen([sys.executable, '-m', 'corpuspick.launcher'], cwd=root,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        return
    child = subprocess.Popen([sys.executable, '-m', 'corpuspick'], cwd=root,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
    code = child.wait()
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share'))
    record_exit(base / 'CorpusPick' / 'logs' / 'process-exits.jsonl', child.pid, code)


if __name__ == '__main__':
    main()
