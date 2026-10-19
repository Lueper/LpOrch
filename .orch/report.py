"""Review rendering, gating, the per-run summary (summary.md), and the review history later tasks build on.

Parts and tasks are both "subjects": each has its own reviews, revisions and automated checks
(a task's are those of its integrated multi-part result).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Union

from tasks import NeedsRevision, Part, Task

if TYPE_CHECKING:  # type-only import keeps this module free of the SDK dependency
    from runner import RunContext

SEVERITIES = ("CRITICAL", "WARNING", "INFO")
Subject = Union[Part, Task]
MAX_HISTORY_CHARS = 8_000  # per upstream task; the full artifacts stay readable in its run folder
ROUND_NUMBER = re.compile(r"\.r(\d+)\.")


def Cell(value: object) -> str:
    text = "" if value is None else str(value)  # keep 0 visible; only None renders empty
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def Row(cells: list[object]) -> str:
    return "| " + " | ".join(Cell(cell) for cell in cells) + " |"


def RenderReview(subject: Subject, review: dict) -> str:
    lines = [
        f"# Review of {subject.id}: {subject.title}",
        "",
        f"- Round: {review.get('round')} ({review.get('mode', 'review')})",
        f"- Reviewer: {review.get('reviewer')}",
        f"- Verdict: {review.get('verdict')}",
        "",
        review.get("summary") or "",
    ]
    findings = review.get("findings", [])
    if findings:
        lines += ["", "| Severity | Location | Issue | Suggestion |", "|---|---|---|---|"]
        lines += [Row([f.get("severity"), f.get("location"), f.get("issue"), f.get("suggestion")]) for f in findings]
    return "\n".join(lines) + "\n"


def FindingLines(review: dict) -> str:
    return "\n".join(
        f"- [{item.get('severity')}] {item.get('location')}: {item.get('issue')} -> {item.get('suggestion')}"
        for item in review.get("findings", [])
    )


def CheckText(subject: Subject) -> str:
    if subject.checkOk is None:
        return ""
    state = "PASSED" if subject.checkOk else "FAILED - treat as CRITICAL"
    return f"Automated checks run by the orchestrator: {state}\n{subject.checkOutput}"


def ReviewState(subject: Subject) -> str:
    if not subject.reviews:
        return "not reviewed"
    last = subject.reviews[-1]
    if last.get("verdict") == "error":
        return "review error"
    if not NeedsRevision(last):
        return "approved" if subject.revisions == 0 else "approved after fix"
    return "fixed, not re-reviewed" if subject.revisions >= last["round"] else "changes requested"


def OpenFindings(subject: Subject, severities: tuple[str, ...] = SEVERITIES[:2]) -> list[dict]:
    """Findings of the last review that no successful revision has addressed."""
    if ReviewState(subject) != "changes requested":
        return []
    return [f for f in subject.reviews[-1].get("findings", []) if f.get("severity") in severities]


def OpenIssues(subject: Subject) -> str:
    items = [f"- [{f.get('severity')}] {f.get('location')}: {f.get('issue')}" for f in OpenFindings(subject)]
    return "Unresolved review findings:\n" + "\n".join(items) if items else ""


def BlockingIssues(task: Task) -> list[str]:
    """Why a task's result must not be applied: unresolved CRITICAL findings or failed automated checks."""
    issues = [f"{s.id}: unresolved CRITICAL findings" for s in [*task.parts, task] if OpenFindings(s, ("CRITICAL",))]
    checked = [task] if len(task.parts) > 1 else task.parts  # split parts are checked together after merging
    return issues + [f"{s.id}: automated checks failed" for s in checked if s.checkOk is False]


def SeverityCounts(subjects: list[Subject]) -> list[int]:
    severities = [f.get("severity") for s in subjects if s.reviews for f in s.reviews[-1].get("findings", [])]
    return [severities.count(level) for level in SEVERITIES]


def CheckState(subject: Subject) -> str:
    return {None: "-", True: "passed", False: "FAILED"}[subject.checkOk]


def TaskStatus(task: Task) -> str:
    if not task.finished:
        return "reset to pending"
    if task.blocked:
        return "blocked"
    return "done" if task.ok else "failed"


def TaskRow(task: Task) -> str:
    reviewed = [part for part in task.parts if part.reviews]
    approved = sum(ReviewState(part).startswith("approved") for part in task.parts)
    return Row([
        f"{task.id} {task.title}",
        task.kind + (f":{task.role}" if task.role else ""),
        TaskStatus(task),
        len(task.parts),
        f"{approved}/{len(task.parts)}" if reviewed else "-",
        "/".join(map(str, SeverityCounts(task.parts))) if reviewed else "-",
        sum(part.revisions for part in task.parts),
        ReviewState(task) if task.reviews else "-",
        CheckState(task) if len(task.parts) > 1 else CheckState(task.parts[0]) if task.parts else "-",
        f"{task.totalCostUsd:.3f}",
        "; ".join(item for item in [task.error, task.result, *task.notes] if item),
    ])


def PartRow(part: Part) -> str:
    return Row([
        part.id,
        part.title,
        ReviewState(part),
        "/".join(map(str, SeverityCounts([part]))) if part.reviews else "-",
        part.revisions,
        CheckState(part),
        f"{part.costUsd:.3f}",
        part.error or "; ".join(part.notes),
    ])


def WriteRunSummary(tasks: list[Task], ctx: RunContext, completed: bool) -> str:
    lines = [
        f"# Run {ctx.runId}",
        "",
        f"- Completed: {'yes' if completed else 'NO - aborted, unfinished tasks reset to pending'}",
        f"- Project: {ctx.projectDir}",
        f"- Artifacts: {ctx.runDir}",
        "",
        "| Task | Kind | Status | Parts | Approved | Findings C/W/I | Fixes | Integration | Checks | Cost (USD) | Result / Note |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        *(TaskRow(task) for task in tasks),
        "",
        "| Part | Title | Review | Findings C/W/I | Fixes | Checks | Cost (USD) | Note |",
        "|---|---|---|---|---|---|---|---|",
        *(PartRow(part) for task in tasks for part in task.parts),
        "",
        *ReportLines(tasks),
        f"Total: ${sum(task.totalCostUsd for task in tasks):.3f}",
    ]
    text = "\n".join(lines) + "\n"
    (ctx.runDir / "summary.md").write_text(text, encoding="utf-8")
    return text


def ReportLines(tasks: list[Task]) -> list[str]:
    """Where each code task's Korean progress report went; none for blocked tasks or aborted runs."""
    reports = [f"- {task.id}: {task.reportRef}" for task in tasks if task.reportRef]
    return ["Progress reports (Korean):", *reports, ""] if reports else []


# ---------- history for later tasks ----------

def UpstreamHistory(task: Task, runsDir: Path) -> str:
    """Review findings and author responses from the runs that produced the task's upstream results."""
    blocks = [TaskHistory(up, runsDir / up.resultRun) for up in task.upstream if up.resultRun]
    return "\n\n".join(block for block in blocks if block)


def RoundOf(path: Path) -> int:
    match = ROUND_NUMBER.search(path.name)
    return int(match[1]) if match else 0


def TaskHistory(up: Task, runDir: Path) -> str:
    """One upstream task's reviews, round by round, each followed by the author's response to it."""
    if not runDir.is_dir():  # the run folder was deleted (e.g. a history reset)
        return ""
    folders = sorted(path for path in runDir.iterdir() if path.is_dir() and path.name.split(".")[0] == up.id)
    lines = []
    for folder in folders:
        for reviewFile in sorted(folder.glob("review.r*.json"), key=RoundOf):
            try:
                review = json.loads(reviewFile.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            roundNo = review.get("round", RoundOf(reviewFile))
            lines.append(f"{folder.name} {review.get('mode', 'review')} r{roundNo} ({review.get('verdict')}): {review.get('summary', '')}")
            lines.append(FindingLines(review))
            response = folder / f"response.r{roundNo}.md"
            if response.is_file():
                lines += [f"Author response r{roundNo}:", response.read_text(encoding="utf-8").strip()]
    text = "\n".join(line for line in lines if line)
    if not text:
        return ""
    if len(text) > MAX_HISTORY_CHARS:
        text = text[:MAX_HISTORY_CHARS] + "\n[truncated]"
    header = f"=== HISTORY {up.id} {up.title} (run {up.resultRun}; all artifacts in {runDir}) ==="
    return f"{header}\n{text}\n=== END HISTORY ==="
