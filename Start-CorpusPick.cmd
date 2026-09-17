@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0Setup-Preview.ps1"
if errorlevel 1 (
    echo Office preview setup failed. Starting CorpusPick with the available preview formats.
    echo To retry, run Setup-Preview.ps1 when online.
)
python -m corpuspick.launcher --detach
if errorlevel 1 pause
