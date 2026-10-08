@echo off
rem Reset orchestrator history: doc\*.md, .orch\runs, the TASKS.md task list and caches.
rem Lists what will be deleted and asks first; "orch-reset --yes" skips the question.
rem In PowerShell type .\orch-reset (PowerShell does not run commands from the current folder by name).
python "%~dp0.orch\reset.py" %*
