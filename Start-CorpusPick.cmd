@echo off
cd /d "%~dp0"
python -m corpuspick
if errorlevel 1 pause
