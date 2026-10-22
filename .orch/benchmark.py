"""Turn a run's run.log into one CSV row per agent call, and check the per-task totals and times against its summary.md.

Usage (from the project root):
    python .orch/benchmark.py                 the latest finished run (with a summary.md) in .orch/runs/
                                              (the latest run.log when none finished; exit 0 when there is no run)
    python .orch/benchmark.py 20261022-120345 that run (a run ID, or the path of a run folder)
    python .orch/benchmark.py RUN -o out.csv  write the CSV there instead of <run folder>/calls.csv
Each call is a "[label] start (role on provider; waited Ns for a slot)" line and its "[label] ok/failed - ...
($cost; usage)" line (agents/runner.py RunOnce); a retry is a call of its own ("... retry1"). A failed line whose
error holds CLI stderr goes on over untimestamped lines up to its "($...)"; they are joined with spaces. run.log stamps
only HH:MM:SS, so the date comes from the run ID (YYYYMMDD-HHMMSS) and moves to the next day whenever the clock goes
back (past midnight).
The usage (TokenUsage.Summary) gives turns and token counts - exact integers (2012345), or the rounded form of older
runs (103k, 2.0M) - and for Claude calls the call's own wall-clock and API time ("time 6m12s, API 5m40s") and the
models that served it ("model a+b"). Older start lines without the slot wait leave waited_seconds empty.
Gemini calls report no cost and no usage on the finish line; their "[label]   tokens: input N (cached M), output K -
models" line fills in cache read (M), input (N - M, the uncached part, as Claude reports it) and output, a JSON
retry's line ("[label json retry]   tokens: ...") added in.
A task runs from its "[Txx] task start (title)" line (pipeline/taskflow.py RunTask, once its upstream tasks are done)
to its "[Txx] done -> ..." / "FAILED - ..." / "BLOCKED - ..." line. summary.md's Started and Duration columns
(reporting/report.py Clock, Elapsed) must agree with them within CLOCK_TOLERANCE, and show "-" where the log has no
start line (blocked) or no end line (aborted). Summaries without those columns (older runs) are compared by cost only.
Exit code 1 when a task's cost or times differ from summary.md; 0 with a notice when there is no run yet.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from core.paths import RUNS_DIR

LINE = re.compile(r"^(?P<time>\d\d:\d\d:\d\d) (?P<level>[A-Z]+) (?P<message>.*)$")
START = re.compile(
    r"^\[(?P<label>[^\]]+)\] start \((?P<role>\S+) on (?P<provider>[^\s;)]+)(?:; waited (?P<waited>\d+(?:\.\d+)?)s for a slot)?\)$")
# The error may hold anything, parentheses included: the cost group is the line's last "($...)"
FINISH = re.compile(r"^\[(?P<label>[^\]]+)\] (?P<status>ok|failed - .*) \(\$(?P<cost>\d+\.\d+)(?:; (?P<usage>[^()]*))?\)$")
FAILED_START = re.compile(r"^\[(?P<label>[^\]]+)\] failed - ")  # its "($...)" may come lines later (CLI stderr)
JSON_RETRY_SUFFIX = " json retry"  # gemini.py's second process within one call
GEMINI_TOKENS = re.compile(
    r"^\[(?P<label>[^\]]+)\]   tokens: input (?P<input>\d+) \(cached (?P<cached>\d+)\), output (?P<output>\d+) - (?P<models>.*)$")
# One item of TokenUsage.Summary: a count with no unit (exact) or k/M (older runs), a runner.Duration, or the models
USAGE_PART = re.compile(
    r"^(?:(?P<turns>\d+) turns"
    r"|(?P<name>peak context|cache read|cache write|input|output) (?P<count>\d+(?:\.\d+)?)(?P<unit>[kM]?)"
    r"|(?P<clock>time|API) (?:(?P<minutes>\d+)m)?(?P<seconds>\d+(?:\.\d+)?)s"
    r"|model (?P<models>\S+))$")
UNIT_SCALE = {"": 1, "k": 1000, "M": 1_000_000}
MODEL_SEPARATOR = "+"  # runner.TokenUsage.Summary joins model IDs with it; the CSV uses ", " as Gemini lines do
TASK_ID = re.compile(r"^T\d+")
TASK_START = re.compile(r"^\[(?P<id>T\d+)\] task start \(")  # taskflow.RunTask, once its upstream tasks are done
TASK_END = re.compile(r"^\[(?P<id>T\d+)\] (?:done -> |FAILED - |BLOCKED - )")  # a blocked task has no start line
SUMMARY_TASK_ROW = re.compile(r"^\| (?P<id>T\d+) ")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
SUMMARY_TOTAL = re.compile(r"^Total: \$(?P<cost>\d+\.\d+)$")
SUMMARY_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # report.TIME_FORMAT, the Started column
SUMMARY_SECONDS = re.compile(r"\((?P<seconds>\d+)s\)$")  # report.Elapsed: "0:12:34 (754s)"
CLOCK_TOLERANCE = 1  # seconds: run.log stamps truncate to the second and summary.md's Duration rounds
COST_DECIMALS = 3  # run.log and summary.md both print costs as %.3f
ROUNDING = 0.5 * 10 ** -COST_DECIMALS
COLUMNS = [
    "run_id", "label", "task", "part", "role", "provider", "status", "error", "start", "end", "seconds",
    "waited_seconds", "call_seconds", "api_seconds", "cost_usd",
    "turns", "peak_context", "cache_read", "cache_write", "input", "output", "models",
]
USAGE_COLUMNS = {"peak context": "peak_context", "cache read": "cache_read", "cache write": "cache_write",
                 "input": "input", "output": "output"}
CLOCK_COLUMNS = {"time": "call_seconds", "API": "api_seconds"}  # the call's own wall-clock and API time


@dataclass
class Call:
    label: str
    role: str
    provider: str
    start: datetime
    waitedSeconds: float | None = None  # time queued for an agent slot; None = an older start line without it
    end: datetime | None = None
    status: str = "unfinished"  # no finish line: the run was aborted mid-call
    error: str = ""
    costUsd: float = 0.0
    usage: dict[str, float] = field(default_factory=dict)  # token counts, turns and the clock columns
    models: str = ""

    @property
    def taskId(self) -> str:
        match = TASK_ID.match(self.label)
        return match.group(0) if match else ""

    @property
    def partId(self) -> str:
        first = self.label.split(" ", 1)[0]
        return first if "." in first else ""

    def Row(self, runId: str) -> dict[str, object]:
        return {
            "run_id": runId, "label": self.label, "task": self.taskId, "part": self.partId, "role": self.role,
            "provider": self.provider, "status": self.status, "error": self.error,
            "start": self.start.isoformat(sep=" "), "end": self.end.isoformat(sep=" ") if self.end else "",
            "seconds": int((self.end - self.start).total_seconds()) if self.end else "",
            "waited_seconds": "" if self.waitedSeconds is None else self.waitedSeconds,
            "cost_usd": f"{self.costUsd:.{COST_DECIMALS}f}", "models": self.models,
            **{column: self.usage.get(column, "") for column in ("turns", *USAGE_COLUMNS.values())},
            **{column: self.usage.get(column, "") for column in CLOCK_COLUMNS.values()},
        }


@dataclass
class TaskTimes:
    started: datetime | None = None  # its "task start" line; None = never started (blocked) or an older run.log
    finished: datetime | None = None  # its done/FAILED/BLOCKED line; None = the run was aborted mid-task


@dataclass
class TaskSpan:
    taskId: str
    calls: int = 0
    first: datetime | None = None  # the first call's start
    last: datetime | None = None  # the last call's finish
    costUsd: float = 0.0
    times: TaskTimes = field(default_factory=TaskTimes)

    @property
    def seconds(self) -> int | None:
        """The task's own duration, as summary.md's Duration has it; None without both its start and end line."""
        started, finished = self.times.started, self.times.finished
        return int((finished - started).total_seconds()) if started and finished else None


@dataclass
class SummaryTask:
    costUsd: float
    timed: bool = False  # the task table has Started and Duration columns (older summaries have neither)
    started: datetime | None = None  # "-": the task never started
    seconds: int | None = None  # "-": never started, or not finished when the run was aborted


def ParseUsage(text: str | None) -> tuple[dict[str, float], str]:
    """"40 turns, peak context 103456, ..., time 6m12s, API 5m40s, model a+b" ->
    ({"turns": 40, "peak_context": 103456, ..., "call_seconds": 372.0, "api_seconds": 340.0}, "a, b")."""
    usage: dict[str, float] = {}
    models = ""
    for item in (text or "").split(", "):
        match = USAGE_PART.match(item.strip())
        if not match:
            continue
        if match["turns"]:
            usage["turns"] = int(match["turns"])
        elif match["name"]:
            usage[USAGE_COLUMNS[match["name"]]] = round(float(match["count"]) * UNIT_SCALE[match["unit"]])
        elif match["clock"]:
            seconds = int(match["minutes"] or 0) * 60 + float(match["seconds"])
            usage[CLOCK_COLUMNS[match["clock"]]] = round(seconds, 1)
        else:
            models = ", ".join(match["models"].split(MODEL_SEPARATOR))
    return usage, models


def RunStart(runId: str) -> datetime:
    try:
        return datetime.strptime(runId, "%Y%m%d-%H%M%S")
    except ValueError:
        sys.exit(f"error: {runId} is not a run ID (YYYYMMDD-HHMMSS): its run.log times have no date")


def ParseLog(path: Path, runStart: datetime) -> tuple[list[Call], dict[str, TaskTimes]]:
    """Every agent call in run.log, in the order they started, and when each task started and ended."""
    calls: list[Call] = []
    tasks: dict[str, TaskTimes] = {}
    running: dict[str, Call] = {}
    geminiTokens: dict[str, tuple[dict[str, int], str]] = {}  # logged before the call's finish line
    pending: tuple[datetime, str] | None = None  # a failed line whose error (CLI stderr) goes on over more lines
    day, previous = runStart.date(), runStart.time()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stamp = LINE.match(line)
        if not stamp:  # a continuation of a multi-line message (CLI stderr)
            if pending:
                pending = (pending[0], f"{pending[1]} {line.strip()}")
                if Finish(pending[1], pending[0], running, geminiTokens):
                    pending = None
            continue
        pending = None  # a failed line never closed by "($...)" leaves its call unfinished
        clock = datetime.strptime(stamp["time"], "%H:%M:%S").time()
        if clock < previous:  # lines are written in order, so the clock going back means midnight passed
            day += timedelta(days=1)
        previous = clock
        moment, message = datetime.combine(day, clock), stamp["message"]
        if start := START.match(message):
            waited = float(start["waited"]) if start["waited"] else None
            call = Call(start["label"], start["role"], start["provider"], moment, waited)
            calls.append(call)
            running[call.label] = call
        elif tokens := GEMINI_TOKENS.match(message):
            AddGeminiTokens(tokens, geminiTokens)
        elif taskStart := TASK_START.match(message):
            tasks.setdefault(taskStart["id"], TaskTimes()).started = moment
        elif taskEnd := TASK_END.match(message):
            tasks.setdefault(taskEnd["id"], TaskTimes()).finished = moment
        elif not Finish(message, moment, running, geminiTokens):
            failed = FAILED_START.match(message)
            if failed and failed["label"] in running:
                pending = (moment, message)
    return calls, tasks


def AddGeminiTokens(tokens: re.Match, geminiTokens: dict[str, tuple[dict[str, int], str]]) -> None:
    """A JSON retry ("<label> json retry", gemini.py) is a second process inside the same call: its tokens are
    added to the call's own, as its finish line covers both."""
    label = tokens["label"].removesuffix(JSON_RETRY_SUFFIX)
    cached, total = int(tokens["cached"]), int(tokens["input"])
    usage = {"cache_read": cached, "input": max(total - cached, 0), "output": int(tokens["output"])}
    if label in geminiTokens:
        earlier, models = geminiTokens[label]
        usage = {key: usage[key] + earlier.get(key, 0) for key in usage}
        newModels = [model for model in tokens["models"].split(", ") if model not in models.split(", ")]
        geminiTokens[label] = (usage, ", ".join([models, *newModels]))
        return
    geminiTokens[label] = (usage, tokens["models"])


def Finish(message: str, moment: datetime, running: dict[str, Call],
           geminiTokens: dict[str, tuple[dict[str, int], str]]) -> bool:
    """Closes the running call the message is the finish line of; False when it is none."""
    finish = FINISH.match(message)
    if not finish or finish["label"] not in running:
        return False
    call = running.pop(finish["label"])
    call.end, call.costUsd = moment, float(finish["cost"])
    call.status, _, call.error = finish["status"].partition(" - ")
    call.usage, call.models = ParseUsage(finish["usage"])
    if call.label in geminiTokens:
        usage, models = geminiTokens.pop(call.label)
        call.usage = {**usage, **call.usage}
        call.models = call.models or models
    return True


def TaskSpans(calls: list[Call], tasks: dict[str, TaskTimes]) -> dict[str, TaskSpan]:
    """Per task: its start and end lines, how many calls, from the first start to the last finish, and their cost
    together."""
    spans = {taskId: TaskSpan(taskId, times=times) for taskId, times in tasks.items()}
    for call in calls:
        span = spans.setdefault(call.taskId or "-", TaskSpan(call.taskId or "-"))
        span.calls += 1
        span.costUsd += call.costUsd
        end = call.end or call.start
        span.first = call.start if span.first is None else min(span.first, call.start)
        span.last = end if span.last is None else max(span.last, end)
    return spans


def SummaryTasks(path: Path) -> tuple[dict[str, SummaryTask], float | None]:
    """summary.md's task rows (Cost (USD), and Started and Duration when it has them) and its Total line;
    ({}, None) when the run wrote no summary."""
    if not path.is_file():
        return {}, None
    tasks: dict[str, SummaryTask] = {}
    columns: dict[str, int] | None = None
    total = None
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in CELL_SPLIT.split(line)[1:-1]]
        if cells[:1] == ["Task"] and "Cost (USD)" in cells:
            columns = {name: index for index, name in enumerate(cells)}
        elif columns is not None and SUMMARY_TASK_ROW.match(line) and len(cells) == len(columns):
            tasks[cells[0].split(" ", 1)[0]] = SummaryRow(cells, columns)
        elif match := SUMMARY_TOTAL.match(line):
            total = float(match["cost"])
        elif not line.strip():
            columns = None  # the task table ended; the part table below has a Cost column too
    return tasks, total


def SummaryRow(cells: list[str], columns: dict[str, int]) -> SummaryTask:
    """One task row; a Started or Duration of "-" stays None."""
    row = SummaryTask(float(cells[columns["Cost (USD)"]]))
    if "Started" not in columns or "Duration" not in columns:
        return row
    started, duration = cells[columns["Started"]], SUMMARY_SECONDS.search(cells[columns["Duration"]])
    row.timed = True
    row.started = datetime.strptime(started, SUMMARY_TIME_FORMAT) if started != "-" else None
    row.seconds = int(duration["seconds"]) if duration else None
    return row


def Matches(logged: float, summary: float, calls: int) -> bool:
    """Each call's cost is rounded in run.log and the sum once more in summary.md."""
    return abs(logged - summary) <= ROUNDING * (calls + 1) + 1e-9


def Near(logged, summary, tolerance) -> bool:
    """Both missing ("-"), or both there and at most the tolerance apart."""
    if logged is None or summary is None:
        return logged is None and summary is None
    return abs(logged - summary) <= tolerance


def Differences(span: TaskSpan, summary: SummaryTask) -> list[str]:
    """What summary.md says otherwise than run.log: the cost, and Started and Duration when it has those columns."""
    differences = [] if Matches(span.costUsd, summary.costUsd, span.calls) else [f"${summary.costUsd:.3f}"]
    if not summary.timed:
        return differences
    if not Near(span.times.started, summary.started, timedelta(seconds=CLOCK_TOLERANCE)):
        differences.append(f"started {summary.started or '-'}")
    if not Near(span.seconds, summary.seconds, CLOCK_TOLERANCE):
        differences.append(f"duration {'-' if summary.seconds is None else f'{summary.seconds}s'}")
    return differences


def Interval(first: datetime, last: datetime | None) -> str:
    """"2026-10-22 12:31:00-12:31:04 4s"; "2026-10-22 12:31:00- unfinished" when it has no end."""
    if last is None:
        return f"{first:%Y-%m-%d %H:%M:%S}- unfinished"
    return f"{first:%Y-%m-%d %H:%M:%S}-{last:%H:%M:%S} {int((last - first).total_seconds())}s"


def CompareWithSummary(spans: dict[str, TaskSpan], summaryPath: Path) -> list[str]:
    """One line per task (its own span from the task start/end lines, its calls' span, calls and cost from run.log
    vs summary.md) and the run's total; mismatches are marked."""
    summaries, total = SummaryTasks(summaryPath)
    lines, mismatches = [], 0
    for taskId in sorted(set(spans) | set(summaries)):
        span = spans.get(taskId, TaskSpan(taskId))
        logged = [f"task {Interval(span.times.started, span.times.finished)}"] if span.times.started else []
        logged.append(f"calls {Interval(span.first, span.last)}" if span.first else "no calls")
        summary = summaries.get(taskId)
        differences = Differences(span, summary) if summary else []
        if summary is None:
            verdict = "not in summary.md"
        elif not differences:
            verdict = "matches summary.md"
        else:
            verdict, mismatches = f"MISMATCH: summary.md {', '.join(differences)}", mismatches + 1
        lines.append(f"{taskId}: {', '.join(logged)}, {span.calls} calls, ${span.costUsd:.3f} - {verdict}")
    loggedTotal, callCount = sum(span.costUsd for span in spans.values()), sum(span.calls for span in spans.values())
    if total is None:
        lines.append(f"Total: ${loggedTotal:.3f} - no summary.md (run unfinished or aborted)")
    elif Matches(loggedTotal, total, callCount):
        lines.append(f"Total: ${loggedTotal:.3f} - matches summary.md")
    else:
        lines.append(f"Total: ${loggedTotal:.3f} - MISMATCH: summary.md ${total:.3f}")
        mismatches += 1
    if mismatches:
        lines.append(f"{mismatches} mismatch(es) with summary.md")
    return lines


def LatestRunDir() -> Path | None:
    """The latest finished run (run.log and summary.md); the latest run with a run.log when none finished yet,
    so a run in progress - one benchmarking from inside it, too - is not picked over a finished one. None when there
    is no run at all."""
    runs = sorted(path for path in RUNS_DIR.iterdir() if (path / "run.log").is_file()) if RUNS_DIR.is_dir() else []
    if not runs:
        return None
    finished = [path for path in runs if (path / "summary.md").is_file()]
    if finished:
        return finished[-1]
    print(f"note: no run in {RUNS_DIR} has a summary.md yet; using the latest run.log ({runs[-1].name})")
    return runs[-1]


def ResolveRunDir(target: str | None) -> Path | None:
    """The run folder to read; None when no target was given and no run has a run.log yet (nothing to benchmark)."""
    if target is None:
        return LatestRunDir()
    path = Path(target)
    runDir = path if path.is_dir() else RUNS_DIR / target
    if not (runDir / "run.log").is_file():
        sys.exit(f"error: no run.log in {runDir}")
    return runDir


def WriteCsv(path: Path, calls: list[Call], runId: str) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:  # utf-8-sig: Excel reads Korean errors right
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(call.Row(runId) for call in calls)


def Main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="benchmark", description="run.log -> one CSV row per agent call.")
    parser.add_argument("run", nargs="?", help="run ID or run folder (default: the latest run)")
    parser.add_argument("-o", "--output", type=Path, help="CSV path (default: <run folder>/calls.csv)")
    args = parser.parse_args(argv)
    runDir = ResolveRunDir(args.run)
    if runDir is None:  # a fresh checkout: no run to compare yet, which is not an error
        print(f"no run with a run.log in {RUNS_DIR} yet - nothing to benchmark")
        return 0
    runId = runDir.name
    calls, tasks = ParseLog(runDir / "run.log", RunStart(runId))
    output = args.output or runDir / "calls.csv"
    WriteCsv(output, calls, runId)
    print(f"{len(calls)} calls -> {output}")
    report = CompareWithSummary(TaskSpans(calls, tasks), runDir / "summary.md")
    print("\n".join(report))
    return 1 if "mismatch(es)" in report[-1] else 0


if __name__ == "__main__":
    sys.exit(Main())
