"""Agent orchestrator: every TASKS.md task runs on its own - work -> fresh review -> revision -> result.
"mode: full" tasks use the multi-agent pipeline (plan -> parallel parts -> ring review -> merge -> integration review).
Tasks with "after:" wait for those tasks; [sys] tasks change the orchestrator itself and are applied at the end.
When the run ends, every code and doc task that ran gets a Korean progress report in report/ (reporting/progress.py).

Lives in <project>/.orch/ and always works on <project>, the folder that contains .orch/.
Usage (via the launcher at the project root):
    orch                 run the pending tasks in TASKS.md      (PowerShell: .\\orch)
    orch preview         show tasks, reviewer pairs and the agent count without running anything
Options: [--max-parallel N] [--max-rounds N] [--budget-per-agent USD] [--max-turns N]
         [--provider claude|gemini] [--model NAME] [--cli-path PATH]
Their defaults, and the provider and model of each agent role, come from orch.toml at the project root
(core/config.py); options given here win over the file. --model and --cli-path apply to the --provider one.

Packages under .orch/, each importing only the ones listed before it:
    core/       settings, provider registry, task model, TASKS.md, refs, run state (SDK-free)
    workspace/  git worktrees for code tasks; sandboxes and automated checks for sys tasks
    agents/     one agent call on its role's provider: runner.py (provider-neutral), claude.py, gemini.py
    reporting/  review state and gating, summary.md, upstream history, progress reports
    pipeline/   one task's plan -> work -> review -> merge
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

from core.capacity import AutoParallel
from core.config import AgentSummary, LoadConfig, OrchConfig, Problems, ProvidersInUse
from core.context import RefLabel, RefTasks, ResolveRefs, ResultFiles, StaleNotices
from core.paths import PROJECT_ROOT, RUNS_DIR, SKILL_DIR, TASKS_FILE, WORKTREES_DIR
from core.providers import PROVIDER_SPECS, PROVIDERS
from core.runcontext import RunContext
from core.taskfile import LinkDependencies, ParseTasks, RecordResult, UnreadTaskLines, UpdateTaskStatus
from core.tasks import MAX_PARTS, STATUS_DONE, STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING, Task, TaskFileError
from pipeline.merge import ApplySystemTasks
from pipeline.taskflow import RunTask
from reporting.progress import WriteProgressReports
from reporting.report import WriteRunSummary
from workspace import gitops
from workspace.sandbox import CreateSnapshot

ROLE_SKILL_DIR = SKILL_DIR / "develop"
MAX_LISTED_REFS = 5  # preview lists this many ref entries per task


def ParseArgs(argv: list[str] | None, config: OrchConfig) -> argparse.Namespace:
    # Defaults come from orch.toml (or the built-in values), so --help shows what a run would use;
    # ranges are checked once on the final values (config.Problems)
    parser = argparse.ArgumentParser(prog="orch", description="Run TASKS.md through parallel work, review and merge.")
    parser.add_argument("command", nargs="?", choices=["run", "preview"], default="run", help="default: run")
    parser.add_argument(
        "--max-parallel", type=int, default=config.maxParallel, help="concurrent agents, 0 = automatic (default: %(default)s)"
    )
    parser.add_argument(
        "--max-rounds", type=int, default=config.maxRounds, help="review/fix rounds, 0 = no review (default: %(default)s)"
    )
    parser.add_argument(
        "--budget-per-agent", type=float, default=config.budgetPerAgent, help="USD cap per agent call (default: %(default)s)"
    )
    parser.add_argument("--max-turns", type=int, default=config.maxTurns, help="turn cap per agent call (default: %(default)s)")
    parser.add_argument(
        "--provider", choices=PROVIDERS, default=config.provider,
        help="provider of every role without its own in orch.toml [roles.*] (default: %(default)s)",
    )
    parser.add_argument("--model", default=None, help="model alias or ID for that provider (default: orch.toml, else CLI default)")
    parser.add_argument("--cli-path", default=None, help="CLI executable for that provider (default: orch.toml, else auto-detected)")
    return parser.parse_args(argv)


def ApplyArgs(config: OrchConfig, args: argparse.Namespace) -> OrchConfig:
    """Command-line options over orch.toml; the file's [roles.*] overrides still apply."""
    config.maxParallel, config.maxRounds = args.max_parallel, args.max_rounds
    config.budgetPerAgent, config.maxTurns = args.budget_per_agent, args.max_turns
    config.provider = args.provider
    if args.model is not None:
        config.providers[config.provider].model = args.model.strip() or None
    if args.cli_path is not None:  # like --model: the default provider's, not every provider's
        config.providers[config.provider].cliPath = args.cli_path.strip() or None
    return config


def ConfigureOutput() -> None:
    # Pipes and redirects (Claude Code, log files) get UTF-8; real consoles already render Unicode
    for stream in (sys.stdout, sys.stderr):
        if stream.isatty():
            stream.reconfigure(errors="replace")
        else:
            stream.reconfigure(encoding="utf-8", errors="replace")


def ResolveParallel(config: OrchConfig, tasks: list[Task]) -> tuple[int, str]:
    """Global cap on concurrent agents across all tasks (one per standard task, up to MAX_PARTS per full task)."""
    if config.maxParallel:
        return config.maxParallel, "set by max_parallel"
    return AutoParallel(sum(MAX_PARTS if task.full else 1 for task in tasks))


def WarnMissingRoles(tasks: list[Task]) -> None:
    for task in tasks:
        if task.role and not (ROLE_SKILL_DIR / f"{task.role}.md").is_file():
            print(f"warning: {task.id} role skill '{task.role}' not found in {ROLE_SKILL_DIR}; running without it")


def PrintPlan(tasks: list[Task], config: OrchConfig, parallel: int, reason: str) -> None:
    print(f"{len(tasks)} pending task(s) in {TASKS_FILE} - each runs independently and yields its own result")
    print("standard tasks: 1 agent -> fresh review -> revision (verified only after CRITICAL findings)")
    if any(task.full for task in tasks):
        print(f"mode: full tasks: planner splits into 1-{MAX_PARTS} parts, ring review, merge + integration review")
    print(f"concurrent agents (all tasks): {parallel} ({reason}); review/fix rounds: {config.maxRounds}")
    # Which settings file applied and which provider/model each role runs on
    print(f"settings: {config.source}; per agent call: ${config.budgetPerAgent:g} cap, {config.maxTurns} turns")
    for line in AgentSummary(config):
        print(f"agents: {line}")
    for name in ProvidersInUse(config):  # what differs between providers (core/providers.py)
        for note in PROVIDER_SPECS[name].notes:
            print(f"{name}: {note}")
    if any(task.kind == "code" for task in tasks):
        repo = gitops.ResolveRepo(PROJECT_ROOT)
        print(f"git: {repo[0]} @ {repo[1][:8]}" if repo else "git: NOT a repo with commits - code tasks will fail")
    if any(task.kind == "sys" for task in tasks):
        print("sys: edited in sandbox copies; applied to the project when the run ends if checks and reviews pass")
    for task in tasks:
        role = f":{task.role}" if task.role else ""
        print(f"  {task.id} [{task.kind}{role}] {task.title}")
        for label, value in TaskSettings(task):
            print(f"      {label}: {value}")
        for detail in task.details:
            print(f"      {detail}")


def TaskSettings(task: Task) -> list[tuple[str, str]]:
    settings = []
    if task.upstream:
        waits = [f"{up.id} ({'this run' if up.status == STATUS_PENDING else StatusName(up.status)})" for up in task.upstream]
        settings.append(("after", ", ".join(waits)))
    refs = [TaskRefLabel(up) for up in RefTasks(task)]
    refs += [RefLabel(path, PROJECT_ROOT) for path in task.refFiles] + task.refUrls
    if refs:
        more = f" (+{len(refs) - MAX_LISTED_REFS} more)" if len(refs) > MAX_LISTED_REFS else ""
        settings.append(("ref", ", ".join(refs[:MAX_LISTED_REFS]) + more))
    if task.outputLang:
        settings.append(("lang", task.outputLang))
    if task.verify:
        settings.append(("verify", task.verify))
    if task.full:
        settings.append(("mode", task.mode))
    return settings


def TaskRefLabel(up: Task) -> str:
    """"ref: Txx" as preview shows it: the result file, or that it comes from this run."""
    if up.status == STATUS_PENDING:
        return f"{up.id} result (this run)"
    files = ", ".join(RefLabel(path, PROJECT_ROOT) for path in ResultFiles(up, PROJECT_ROOT))
    history = f", reviews from run {up.resultRun}" if up.resultRun else ""
    return f"{up.id} -> {files}{history}" if files else f"{up.id} (no result file)"


def StatusName(status: str) -> str:
    blocked = " - this task will be blocked"
    names = {STATUS_DONE: "done", STATUS_FAILED: f"failed{blocked}", STATUS_RUNNING: f"stuck running{blocked}"}
    return names.get(status, status)


def CreateLogger(runDir: Path) -> logging.Logger:
    logger = logging.getLogger("orch")
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)  # CLI stderr (DEBUG) stays in run.log only
    for handler in (console, logging.FileHandler(runDir / "run.log", encoding="utf-8")):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def CreateContext(config: OrchConfig, parallel: int) -> RunContext:
    runId = datetime.now().strftime("%Y%m%d-%H%M%S")
    runDir = RUNS_DIR / runId
    runDir.mkdir(parents=True)
    repo = gitops.ResolveRepo(PROJECT_ROOT)
    repoRoot, baseCommit = repo if repo else (None, None)
    return RunContext(
        runId=runId,
        runDir=runDir,
        projectDir=PROJECT_ROOT,
        repoRoot=repoRoot,
        worktreeDir=WORKTREES_DIR / runId,
        baseCommit=baseCommit,
        config=config,
        log=CreateLogger(runDir),
        semaphore=asyncio.Semaphore(parallel),
    )


def MarkTasks(tasks: list[Task], statusOf, log: logging.Logger) -> None:
    for task in tasks:
        try:
            UpdateTaskStatus(TASKS_FILE, task, statusOf(task))
        except TaskFileError as error:  # never let bookkeeping hide the run's results
            log.warning("%s", error)


def FinalStatus(task: Task) -> str:
    if task.blocked:  # an upstream task did not succeed; retry once it is fixed
        return STATUS_PENDING
    return STATUS_DONE if task.ok else STATUS_FAILED


def SettleEarlierTasks(allTasks: list[Task]) -> None:
    """Tasks outside this run count as finished for "after:" waits: done ones succeeded, the others did not."""
    for task in allTasks:
        if task.status == STATUS_PENDING:
            continue
        task.finished = True
        if task.status != STATUS_DONE:
            task.error = f"status [{task.status}] in TASKS.md, not done"
        task.doneEvent.set()


def RemoveWorkspaces(tasks: list[Task], ctx: RunContext) -> None:
    for part in (part for task in tasks for part in task.parts):  # branches and stashes survive
        if part.kind == "code" and part.worktree:
            gitops.RemoveWorktree(ctx.repoRoot, part.worktree)
    shutil.rmtree(ctx.worktreeDir, ignore_errors=True)  # sys sandboxes and the system snapshot


def RecordResults(tasks: list[Task], log: logging.Logger) -> None:
    for task in tasks:
        if task.status != STATUS_DONE or not task.resultRef:
            continue
        try:
            RecordResult(TASKS_FILE, task)
        except TaskFileError as error:
            log.warning("%s", error)


async def RunPipeline(tasks: list[Task], allTasks: list[Task], config: OrchConfig, parallel: int, reason: str) -> int:
    ctx = CreateContext(config, parallel)
    ctx.log.info("%d task(s), up to %d concurrent agent(s) (%s)", len(tasks), parallel, reason)
    for line in AgentSummary(config):  # run.log records which provider/model each role ran on
        ctx.log.info("agents: %s", line)
    SettleEarlierTasks(allTasks)
    MarkTasks(tasks, lambda task: STATUS_RUNNING, ctx.log)
    if any(task.kind == "sys" for task in tasks):
        ctx.systemBase = CreateSnapshot(PROJECT_ROOT, ctx.worktreeDir / "_base")
    completed = False
    try:
        # Successful sys tasks are marked only once applied, after every other task is done
        finish = lambda task: None if task.kind == "sys" and task.ok else MarkTasks([task], FinalStatus, ctx.log)
        await asyncio.gather(*(RunTask(task, ctx, config.maxRounds, finish) for task in tasks))
        ApplySystemTasks(tasks, ctx, lambda task: MarkTasks([task], FinalStatus, ctx.log))
        completed = True
        await WriteProgressReports(tasks, ctx)  # every task is final now, [sys] applies included
    finally:
        RemoveWorkspaces(tasks, ctx)
        # Aborted runs (and sys tasks never applied) go back to pending so the next run picks them up again
        MarkTasks([task for task in tasks if task.status == STATUS_RUNNING], lambda task: STATUS_PENDING, ctx.log)
        RecordResults(tasks, ctx.log)
        print(WriteRunSummary(tasks, ctx, completed))
    return 0 if all(task.ok for task in tasks) else 1


def Main(argv: list[str] | None = None) -> int:
    ConfigureOutput()
    config, errors = LoadConfig(PROJECT_ROOT)
    if not errors:  # the file supplies the option defaults, so it is read before the command line
        args = ParseArgs(argv, config)
        errors = Problems(ApplyArgs(config, args))
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 2
    try:
        allTasks = ParseTasks(TASKS_FILE)
    except TaskFileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for warning in UnreadTaskLines(TASKS_FILE):  # a typo in a task line would otherwise drop the task silently
        print(f"warning: {warning}")
    pending = [task for task in allTasks if task.status == STATUS_PENDING]
    # A missing ref stops the run: the agent would otherwise guess at another file
    errors = LinkDependencies(allTasks) + [error for task in pending for error in ResolveRefs(task, PROJECT_ROOT)]
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 2
    for notice in StaleNotices(allTasks, PROJECT_ROOT):  # informational: rebuilding is the user's call
        print(f"notice: {notice}")
    if not pending:
        print(f"No pending tasks in {TASKS_FILE}")
        return 0
    WarnMissingRoles(pending)
    parallel, reason = ResolveParallel(config, pending)
    if args.command == "preview":
        PrintPlan(pending, config, parallel, reason)
        return 0
    return asyncio.run(RunPipeline(pending, allTasks, config, parallel, reason))


if __name__ == "__main__":
    sys.exit(Main())
