@echo off
rem Agent orchestrator launcher for Windows - "orch" runs TASKS.md, "orch preview" shows the plan only.
rem In PowerShell type .\orch (PowerShell does not run commands from the current folder by name). macOS/Linux: orch.sh.
rem Python: the project's .venv when there is one, else python on PATH (3.11+ either way).
setlocal
set "PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"
"%PYTHON%" "%~dp0.orch\orchestrate.py" %*
