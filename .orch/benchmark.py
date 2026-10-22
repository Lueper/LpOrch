"""Turn a run's run.log into one CSV row per agent call, and check the per-task totals against its summary.md.

Usage (from the project root):
    python .orch/benchmark.py                 the latest run in .orch/runs/
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
retry's line ("[label json retry]   tokens: ...") added in. Exit code 1 when a task's cost differs from summary.md;
0 with a notice when there is no run yet.
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
SUMMARY_TASK_ROW = re.compile(r"^\| (?P<id>T\d+) ")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
SUMMARY_TOTAL = re.compile(r"^Total: \$(?P<cost>\d+\.\d+)$")
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
class TaskSpan:
    taskId: str
    calls: int = 0
    first: datetime | None = None
    last: datetime | None = None
    costUsd: float = 0.0


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


def ParseLog(path: Path, runStart: datetime) -> list[Call]:
    """Every agent call in run.log, in the order they started."""
    calls: list[Call] = []
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
        elif not Finish(message, moment, running, geminiTokens):
            failed = FAILED_START.match(message)
            if failed and failed["label"] in running:
                pending = (moment, message)
    return calls


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


def TaskSpans(calls: list[Call]) -> dict[str, TaskSpan]:
    """Per task: how many calls, from the first start to the last finish, and their cost together."""
    spans: dict[str, TaskSpan] = {}
    for call in calls:
        span = spans.setdefault(call.taskId or "-", TaskSpan(call.taskId or "-"))
        span.calls += 1
        span.costUsd += call.costUsd
        end = call.end or call.start
        span.first = call.start if span.first is None else min(span.first, call.start)
        span.last = end if span.last is None else max(span.last, end)
    return spans


def SummaryCosts(path: Path) -> tuple[dict[str, float], float | None]:
    """summary.md's Cost (USD) per task row and its Total line; ({}, None) when the run wrote no summary."""
    if not path.is_file():
        return {}, None
    costs: dict[str, float] = {}
    costColumn, total = None, None
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in CELL_SPLIT.split(line)[1:-1]]
        if cells[:1] == ["Task"] and "Cost (USD)" in cells:
            costColumn = cells.index("Cost (USD)")
        elif costColumn is not None and SUMMARY_TASK_ROW.match(line) and len(cells) > costColumn:
            costs[cells[0].split(" ", 1)[0]] = float(cells[costColumn])
        elif match := SUMMARY_TOTAL.match(line):
            total = float(match["cost"])
        elif not line.strip():
            costColumn = None  # the task table ended; the part table below has a Cost column too
    return costs, total


def Matches(logged: float, summary: float, calls: int) -> bool:
    """Each call's cost is rounded in run.log and the sum once more in summary.md."""
    return abs(logged - summary) <= ROUNDING * (calls + 1) + 1e-9


def CompareWithSummary(spans: dict[str, TaskSpan], summaryPath: Path) -> list[str]:
    """One line per task (span, calls, cost from run.log vs summary.md) and the run's total; mismatches are marked."""
    costs, total = SummaryCosts(summaryPath)
    lines, mismatches = [], 0
    for taskId in sorted(set(spans) | set(costs)):
        span = spans.get(taskId, TaskSpan(taskId))
        seconds = int((span.last - span.first).total_seconds()) if span.first and span.last else 0
        logged = f"{span.first:%Y-%m-%d %H:%M:%S}-{span.last:%H:%M:%S} {seconds}s" if span.first else "no calls"
        summary = costs.get(taskId)
        if summary is None:
            verdict = "not in summary.md"
        elif Matches(span.costUsd, summary, span.calls):
            verdict = "matches summary.md"
        else:
            verdict, mismatches = f"MISMATCH: summary.md ${summary:.3f}", mismatches + 1
        lines.append(f"{taskId}: {logged}, {span.calls} calls, ${span.costUsd:.3f} - {verdict}")
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


def ResolveRunDir(target: str | None) -> Path | None:
    """The run folder to read; None when no target was given and no run has a run.log yet (nothing to benchmark)."""
    if target is None:
        runs = sorted(path for path in RUNS_DIR.iterdir() if (path / "run.log").is_file()) if RUNS_DIR.is_dir() else []
        return runs[-1] if runs else None
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
    calls = ParseLog(runDir / "run.log", RunStart(runId))
    output = args.output or runDir / "calls.csv"
    WriteCsv(output, calls, runId)
    print(f"{len(calls)} calls -> {output}")
    report = CompareWithSummary(TaskSpans(calls), runDir / "summary.md")
    print("\n".join(report))
    return 1 if "mismatch(es)" in report[-1] else 0


if __name__ == "__main__":
    sys.exit(Main())
