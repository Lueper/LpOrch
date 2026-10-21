@echo off
rem Reset orchestrator history: doc\*.md, report\*.md, .orch\runs, .orch\worktrees, the TASKS.md task list and caches.
rem Lists what will be deleted and asks first; "orch-reset --yes" skips the question.
rem In PowerShell type .\orch-reset (PowerShell does not run commands from the current folder by name). macOS/Linux: orch-reset.sh.
rem Python: the project's .venv when there is one, else python on PATH (3.11+ either way).
setlocal
set "PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"
"%PYTHON%" "%~dp0.orch\reset.py" %*
