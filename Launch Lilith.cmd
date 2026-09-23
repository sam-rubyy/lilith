@echo off
setlocal
if exist "%~dp0..\.venv-windows\Scripts\lilith.exe" (
    "%~dp0..\.venv-windows\Scripts\lilith.exe"
) else (
    python -m lilith.home
)
if errorlevel 1 pause
