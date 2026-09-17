"""Cleanup helpers for Office processes created by CorpusPick."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


def cleanup_owned_process(owner_path: Path, process_name: str) -> None:
    """Stop only the Office process whose PID/start-time was recorded by our wrapper."""
    if os.name != 'nt':
        return
    try:
        owner = json.loads(owner_path.read_text(encoding='utf-8-sig'))
        pid = int(owner['ownedPid'])
        started = str(int(owner['started']))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return
    env = os.environ.copy()
    env['CORPUSPICK_OWNER_PID'] = str(pid)
    env['CORPUSPICK_OWNER_STARTED'] = started
    env['CORPUSPICK_OWNER_PROCESS'] = process_name
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$p=Get-Process -Id $env:CORPUSPICK_OWNER_PID;"
        "if($p -and $p.ProcessName -ieq $env:CORPUSPICK_OWNER_PROCESS "
        "-and $p.StartTime.ToUniversalTime().Ticks -eq [int64]$env:CORPUSPICK_OWNER_STARTED)"
        "{Stop-Process -InputObject $p -Force}"
    )
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-Command', script],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired):
        pass
