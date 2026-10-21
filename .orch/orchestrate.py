"""Agent orchestrator: every TASKS.md task runs on its own - work -> fresh review -> revision -> result.
"mode: full" tasks use the multi-agent pipeline (plan -> parallel parts -> ring review -> merge -> integration review).
Tasks with "after:" wait for those tasks; [sys] tasks change the orchestrator itself and are applied at the end.
A task whose agents asked for follow-up work gets those tasks appended to the task list as new pending ones.
[test] tasks run in a disposable copy of the project and leave only a report; [task] tasks write new tasks.
When the run ends, every task that ran (sys tasks too) gets a Korean progress report in report/ (reporting/progress.py);
for every kind but doc, whose result is its document in doc/, the task list's result line then points at that report.

Lives in <project>/.orch/ and always works on <project>, the folder that contains .orch/.
Usage (via the launcher at the project root: orch.bat on Windows, orch.sh on macOS/Linux):
    orch                 run the pending tasks in TASKS.md      (PowerShell: .\\orch, macOS/Linux: ./orch.sh)
    orch preview         show tasks, reviewer pairs and the agent count without running anything
Options: [--max-parallel N] [--max-rounds N] [--budget-per-agent USD] [--max-turns N]
         [--provider claude|gemini] [--model NAME] [--cli-path PATH] [--source file|notion]
Their defaults, and the provider and model of each agent role, come from orch.toml at the project root
(core/config.py); options given here win over the file. --model and --cli-path apply to the --provider one.
--source picks the task list: TASKS.md, or the Notion database of orch.toml's [notion] section.

Packages under .orch/, each importing only the ones listed before it:
    core/       settings, provider registry, task model, the task list (TASKS.md or Notion), refs, run state (SDK-free)
    workspace/  git worktrees for code tasks; sandboxes and automated checks for sys tasks
    agents/     one agent call on its role's provider: runner.py (provider-neutral), claude.py, gemini.py
    reporting/  review state and gating, summary.md, upstream history, progress reports
    pipeline/   one task's plan -> work -> review -> merge
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Before the imports below, which need tomllib (3.11+): macOS's own /usr/bin/python3 is older
if sys.version_info < (3, 11):
    sys.exit(f"error: LpOrch needs Python 3.11 or newer; {sys.executable} is {sys.version.split()[0]}")

from agents.runner import CONSOLE_TEXT
from core.capacity import AutoParallel
from core.config import TASK_SOURCES, AgentSummary, LoadConfig, OrchConfig, Problems, ProvidersInUse
from core.context import RefLabel, RefTasks, ResolveRefs, ResultFiles, StaleNotices
from core.paths import PROJECT_ROOT, RUNS_DIR, SKILL_DIR, WORKTREES_DIR
from core.providers import PROVIDER_SPECS, PROVIDERS
from core.runcontext import RunContext
from core.taskfile import DIRECTIVE_PATTERN, TASK_REF_PATTERN, AddDetail, LinkDependencies
from core.tasks import (
    MAX_FOLLOWUPS, MAX_PARTS, MODE_FULL, MODE_STANDARD, STATUS_DONE, STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING,
    FollowUp, FollowUpsOf, Task, TaskFileError, TaskId, TitleKey,
)
from core.tasksource import CreateTaskSource, TaskSource
from pipeline.merge import ApplySystemTasks
from pipeline.taskflow import RunTask
from reporting.progress import WriteProgressReports
from reporting.report import WriteRunSummary
from workspace import gitops
from workspace.sandbox import CreateSnapshot

ROLE_SKILL_DIR = SKILL_DIR / "develop"
MAX_LISTED_REFS = 5  # preview lists this many ref entries per task
OWN_ID_PATTERN = re.compile(r"\bT(\d+)\b", re.IGNORECASE)  # a task ID in an "after:" or "ref:" value


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
    parser.add_argument(
        "--source", choices=TASK_SOURCES, default=config.taskSource,
        help="task list: TASKS.md, or the Notion database of orch.toml [notion] (default: %(default)s)",
    )
    return parser.parse_args(argv)


def ApplyArgs(config: OrchConfig, args: argparse.Namespace) -> OrchConfig:
    """Command-line options over orch.toml; the file's [roles.*] overrides still apply."""
    config.maxParallel, config.maxRounds = args.max_parallel, args.max_rounds
    config.budgetPerAgent, config.maxTurns = args.budget_per_agent, args.max_turns
    config.provider, config.taskSource = args.provider, args.source
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


def PrintPlan(tasks: list[Task], source: TaskSource, config: OrchConfig, parallel: int, reason: str) -> None:
    print(f"{len(tasks)} pending task(s) in {source.label} - each runs independently and yields its own result")
    for note in source.Notes():  # Notion: which property fills which role, so a wrong name shows up before a run
        print(f"tasks: {note}")
    if config.followUps:  # the run may add tasks to the list, so preview says so before it does
        print(f"follow-ups: a succeeded task may add up to {MAX_FOLLOWUPS} follow-up task(s) to {source.label}")
    if any(task.kind == "task" for task in tasks):
        print(f"task tasks: the tasks they write are added to {source.label} when the run ends,"
              f" numbered from {TaskId(source.NextNumber())}")
    if any(task.kind == "test" for task in tasks):
        print("test tasks: run in a disposable copy of the project and leave only their progress report")
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


class ConsoleFormatter(logging.Formatter):
    """Prints a record's shorter console text (agents.runner.CONSOLE_TEXT) in place of its message when it has one,
    so tool progress lines stay one line on the console while run.log keeps them whole."""

    def format(self, record: logging.LogRecord) -> str:
        short = getattr(record, CONSOLE_TEXT, None)
        if short is None:
            return super().format(record)
        # A copy: the record itself goes on to the run.log handler with its full message
        return super().format(logging.makeLogRecord({**record.__dict__, "msg": short, "args": None}))


def CreateLogger(runDir: Path) -> logging.Logger:
    logger = logging.getLogger("orch")
    logger.setLevel(logging.DEBUG)
    layout, timeFormat = "%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)  # CLI stderr (DEBUG) stays in run.log only
    console.setFormatter(ConsoleFormatter(layout, timeFormat))
    logFile = logging.FileHandler(runDir / "run.log", encoding="utf-8")
    logFile.setFormatter(logging.Formatter(layout, timeFormat))  # full messages, tool details included
    for handler in (console, logFile):
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


def MarkTasks(tasks: list[Task], statusOf, log: logging.Logger, source: TaskSource) -> None:
    for task in tasks:
        try:
            source.SetStatus(task, statusOf(task))
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
            task.error = f"status [{task.status}] in the task list, not done"
        task.doneEvent.set()


def RemoveWorkspaces(tasks: list[Task], ctx: RunContext) -> None:
    for part in (part for task in tasks for part in task.parts):  # branches and stashes survive
        if part.kind == "code" and part.worktree:
            gitops.RemoveWorktree(ctx.repoRoot, part.worktree)
    shutil.rmtree(ctx.worktreeDir, ignore_errors=True)  # sys sandboxes and the system snapshot


def RecordResults(tasks: list[Task], log: logging.Logger, source: TaskSource) -> None:
    for task in tasks:
        if task.status != STATUS_DONE or not task.resultRef:
            continue
        try:
            source.RecordResult(task)
        except TaskFileError as error:
            log.warning("%s", error)


def UnusableDirective(line: str, knownIds: set[str], projectDir: Path) -> str | None:
    """Why a follow-up's detail line cannot be used as a task setting, or None when it can.

    An agent writes these lines freehand, and a pending task with an unknown "after:" ID, a "ref:" that matches
    nothing or an unknown "mode:" stops the *whole* next run (Main returns 2) - so such a line is dropped here
    instead of being appended to the task list.
    """
    probe = Task(id="T00", kind="doc", role=None, title="probe", lineNo=0)
    AddDetail(probe, line)
    unknown = [dep for dep in probe.after if dep not in knownIds]
    if unknown:
        return f"unknown task {', '.join(unknown)}"
    if probe.mode not in (MODE_STANDARD, MODE_FULL):
        return f"unknown mode '{probe.mode}'"
    errors = ResolveRefs(probe, projectDir) if probe.refs else []
    return "; ".join(error.replace("T00 ", "") for error in errors) or None


def ResolveOwnIds(line: str, own: dict[str, str | None]) -> tuple[str, str | None]:
    """A written task's detail line with the IDs of its own list replaced by the IDs those tasks really got.

    A [task] worker numbers its new tasks from the task list's next free ID at the start of the run (its prompt
    says so), but follow-ups added first, another [task] task's list or a skipped duplicate can shift the real IDs.
    own maps each ID the worker gave its list to the real one, or to None while that task is not added. Returns the
    line, and the reason to drop it when it points at a task of the list that is not added (or comes later in it).
    """
    directive = DIRECTIVE_PATTERN.match(line)
    key = directive["key"].lower() if directive else ""
    if not own or not (key == "after" or (key == "ref" and TASK_REF_PATTERN.match(directive["value"].strip()))):
        return line, None
    missing: list[str] = []

    def Swap(match: re.Match) -> str:
        expected = TaskId(int(match[1]))
        if expected not in own:  # an existing task's ID
            return match[0]
        if own[expected] is None:
            missing.append(expected)
            return match[0]
        return own[expected]

    value = OWN_ID_PATTERN.sub(Swap, directive["value"])
    if missing:
        return line, f"points at {', '.join(missing)} of the same list, which is not in the task list before it"
    return line[:directive.start("value")] + value, None


def AddTasks(
    owner: Task, items: list[FollowUp], known: dict[tuple[str, str], str], knownIds: set[str], source: TaskSource,
    projectDir: Path, log: logging.Logger, what: str, firstNumber: int | None = None,
) -> list[tuple[str, FollowUp]]:
    """Add new pending tasks to the end of the task list; returns (task ID, task) for each one added.

    A title already in the list is skipped, and a detail line that would stop the next run is dropped. With
    firstNumber (a [task] task's list), the IDs its worker gave its own tasks become the IDs they really get.
    """
    own: dict[str, str | None] = {TaskId(firstNumber + index): None for index in range(len(items))} if firstNumber else {}
    expected = list(own)
    added = []
    for index, item in enumerate(items):
        if item.key in known:
            log.info("[%s] %s already in the task list: [%s] %s", owner.id, what, item.kind, item.title)
            if own:  # "after:" on it means the task that is there already
                own[expected[index]] = known[item.key]
            continue
        details = []
        for line in item.details:
            line, problem = ResolveOwnIds(line, own)
            problem = problem or UnusableDirective(line, knownIds, projectDir)
            if problem:
                log.warning("[%s] %s '%s': detail line dropped (%s): %s", owner.id, what, item.title, problem, line)
                continue
            details.append(line)
        item.details = details
        try:
            taskId, where = source.AddTask(item)
        except (TaskFileError, OSError) as error:  # never let bookkeeping hide the run's results
            # With the title, run.log tells which one is missing from the task list
            log.warning("[%s] %s not added: [%s] %s: %s", owner.id, what, item.kind, item.title, error)
            continue
        known[item.key] = taskId
        knownIds.add(taskId)
        if own:
            own[expected[index]] = taskId
        added.append((taskId, item))
        log.info("[%s] %s added (%s): [%s] %s", owner.id, what, where, item.kind, item.title)
    return added


def AddedLabels(added: list[tuple[str, FollowUp]]) -> str:
    """"T05 [doc] title; T06 [code] title" - for summary.md and the progress report."""
    return "; ".join(f"{taskId} [{item.kind}] {item.title}" for taskId, item in added)


def AddWrittenTasks(
    tasks: list[Task], known: dict[tuple[str, str], str], knownIds: set[str], log: logging.Logger,
    source: TaskSource, projectDir: Path, firstNumber: int,
) -> None:
    """Add the task lists that [task] tasks wrote - whatever [tasks] follow_ups says, as writing them is their job.

    A [task] task none of whose tasks could be added fails; one whose tasks are all in the list already succeeds.
    """
    for task in tasks:
        if task.kind != "task" or not task.ok or not task.writtenTasks:
            continue
        added = AddTasks(task, task.writtenTasks, known, knownIds, source, projectDir, log, "task", firstNumber)
        if added:
            task.resultRef = f"added to the task list: {', '.join(taskId for taskId, _ in added)}"
            task.result = task.resultRef
            task.notes.append(f"task(s) added to the task list: {AddedLabels(added)}")
        elif all(item.key in known for item in task.writtenTasks):
            task.result = "every task it wrote is in the task list already"
        else:
            task.error = "none of the tasks it wrote could be added to the task list (see run.log)"


def AddFollowUpTasks(
    tasks: list[Task], known: dict[tuple[str, str], str], knownIds: set[str], log: logging.Logger,
    source: TaskSource, projectDir: Path,
) -> None:
    """Append the follow-up tasks this run's agents asked for to the task list, as new pending tasks.

    Only tasks that succeeded contribute: a failed or blocked task goes back to pending and runs again, which
    would propose the same follow-ups a second time. A title already in the task list is skipped for the same
    reason, so re-running a task does not pile up duplicates.
    """
    for task in tasks:
        if not task.ok or task.blocked:
            continue
        # Duplicates are dropped before the cap, so repeats of known titles cannot crowd out a new proposal
        proposed = FollowUpsOf(task)
        wanted = [followUp for followUp in proposed if followUp.key not in known]
        for followUp in (item for item in proposed if item.key in known):
            log.info("[%s] follow-up already in the task list: [%s] %s", task.id, followUp.kind, followUp.title)
        if len(wanted) > MAX_FOLLOWUPS:
            log.warning("[%s] %d follow-up tasks asked for; only the first %d are added", task.id, len(wanted), MAX_FOLLOWUPS)
            wanted = wanted[:MAX_FOLLOWUPS]
        added = AddTasks(task, wanted, known, knownIds, source, projectDir, log, "follow-up task")
        if added:  # summary.md and the progress report say what the next run will pick up
            task.notes.append(f"follow-up task(s) added to the task list: {AddedLabels(added)}")


def AddNewTasks(
    tasks: list[Task], allTasks: list[Task], log: logging.Logger, source: TaskSource, projectDir: Path,
    followUps: bool, firstNumber: int,
) -> None:
    """Add what this run wrote to the task list: the [task] tasks' lists first - their workers numbered them from
    firstNumber - then the follow-ups, when [tasks] follow_ups is on. A title is added once, whoever wrote it."""
    known = {TitleKey(task.kind, task.title): task.id for task in allTasks}
    knownIds = {task.id for task in allTasks}  # what an "after:" or "ref: Txx" may point at
    AddWrittenTasks(tasks, known, knownIds, log, source, projectDir, firstNumber)
    if followUps:
        AddFollowUpTasks(tasks, known, knownIds, log, source, projectDir)


async def RunPipeline(
    tasks: list[Task], allTasks: list[Task], source: TaskSource, config: OrchConfig, parallel: int, reason: str
) -> int:
    ctx = CreateContext(config, parallel)
    ctx.nextTaskNumber = source.NextNumber()  # [task] workers number the tasks they write from here
    ctx.log.info("%d task(s), up to %d concurrent agent(s) (%s)", len(tasks), parallel, reason)
    for line in AgentSummary(config):  # run.log records which provider/model each role ran on
        ctx.log.info("agents: %s", line)
    ctx.log.info("task list: %s", source.label)
    SettleEarlierTasks(allTasks)
    MarkTasks(tasks, lambda task: STATUS_RUNNING, ctx.log, source)
    if any(task.kind == "sys" for task in tasks):
        ctx.systemBase = CreateSnapshot(PROJECT_ROOT, ctx.worktreeDir / "_base")
    completed = False
    try:
        # Successful sys tasks are marked only once applied and [task] tasks once their tasks are added,
        # after every other task is done
        deferred = ("sys", "task")
        finish = lambda task: None if task.kind in deferred and task.ok else MarkTasks([task], FinalStatus, ctx.log, source)
        await asyncio.gather(*(RunTask(task, ctx, config.maxRounds, finish) for task in tasks))
        ApplySystemTasks(tasks, ctx, lambda task: MarkTasks([task], FinalStatus, ctx.log, source))
        completed = True
        # After the [sys] applies, so an applied TASKS.md never conflicts with the new lines; in a thread because a
        # Notion row is created over the network, and the reports run right after
        await asyncio.to_thread(
            AddNewTasks, tasks, allTasks, ctx.log, source, ctx.projectDir, config.followUps, ctx.nextTaskNumber
        )
        written = [task for task in tasks if task.kind == "task" and task.status == STATUS_RUNNING]
        MarkTasks(written, FinalStatus, ctx.log, source)
        await WriteProgressReports(tasks, ctx)  # every task is final now, [sys] applies and new tasks included
        # Every kind but doc (whose result is its document) leaves its report as the result document: the task
        # list's result line and "ref: Txx" point at it - a test leaves nothing else
        for task in tasks:
            if task.kind != "doc" and task.ok and task.reportRef:
                task.resultRef = task.reportRef
    finally:
        RemoveWorkspaces(tasks, ctx)
        # Aborted runs (and sys tasks never applied) go back to pending so the next run picks them up again
        MarkTasks([task for task in tasks if task.status == STATUS_RUNNING], lambda task: STATUS_PENDING, ctx.log, source)
        RecordResults(tasks, ctx.log, source)
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
    source, errors = CreateTaskSource(config, PROJECT_ROOT)  # TASKS.md, or the Notion database of [notion]
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors or source is None:
        return 2
    try:
        allTasks = source.Read()
    except TaskFileError as error:  # file missing, or the database unreachable or not shared with the integration
        print(f"error: {error}", file=sys.stderr)
        return 2
    for warning in source.Warnings():  # a typo in a task line or row would otherwise drop the task silently
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
        print(f"No pending tasks in {source.label}")
        return 0
    WarnMissingRoles(pending)
    parallel, reason = ResolveParallel(config, pending)
    if args.command == "preview":
        PrintPlan(pending, source, config, parallel, reason)
        return 0
    return asyncio.run(RunPipeline(pending, allTasks, source, config, parallel, reason))


if __name__ == "__main__":
    sys.exit(Main())
