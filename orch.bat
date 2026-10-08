@echo off
rem Agent orchestrator launcher - "orch" runs TASKS.md, "orch preview" shows the plan only.
rem In PowerShell type .\orch (PowerShell does not run commands from the current folder by name).
python "%~dp0.orch\orchestrate.py" %*
