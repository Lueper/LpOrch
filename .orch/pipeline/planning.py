"""Plan phase: a "mode: full" task is split by a planner agent into 1..MAX_PARTS parts, one per parallel agent.
Standard tasks skip the planner and run as one part.
"""
from __future__ import annotations

import json

from agents.runner import AgentSpec, LoadPrompt, RunAgent, WriteText
from core.context import TaskContext
from core.runcontext import RunContext
from core.tasks import MAX_PARTS, Part, Task

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_PARTS,
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "brief": {"type": "string"}},
                "required": ["title", "brief"],
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["parts", "rationale"],
}


WHOLE_TASK = "Complete the whole task."


async def PlanTask(task: Task, ctx: RunContext) -> None:
    if not task.full:  # standard mode: one agent does the whole task, no planner call
        task.parts = [Part(id=f"{task.id}.1", task=task, title=task.title, brief=WHOLE_TASK)]
        return
    spec = AgentSpec(
        role="planner",
        cwd=ctx.repoRoot if task.kind == "code" and ctx.repoRoot else ctx.projectDir,
        systemAppend=LoadPrompt("planner"),
        addDirs=task.refDirs,
        schema=PLAN_SCHEMA,
    )
    result = await RunAgent(f"{task.id} plan", PlanPrompt(task), spec, ctx)
    task.costUsd += result.costUsd
    plan = result.structured if result.ok and isinstance(result.structured, dict) else {}
    briefs = [item for item in plan.get("parts", []) if isinstance(item, dict) and item.get("brief")]
    if not briefs:  # planning only optimizes; fall back to one agent doing the whole task
        briefs = [{"title": task.title, "brief": WHOLE_TASK}]
        task.notes.append(f"planner gave no parts ({result.error or 'empty plan'}); ran as one part")
    task.planNote = str(plan.get("rationale", ""))
    task.parts = [
        Part(id=f"{task.id}.{index}", task=task, title=str(item.get("title") or f"Part {index}"), brief=str(item["brief"]))
        for index, item in enumerate(briefs[:MAX_PARTS], 1)
    ]
    WriteText(ctx.TaskDir(task.id) / "plan.json", json.dumps(plan or {"parts": briefs}, ensure_ascii=False, indent=2))
    ctx.log.info("[%s] plan: %d part(s) - %s", task.id, len(task.parts), " | ".join(p.title for p in task.parts))


def PlanPrompt(task: Task) -> str:
    lines = TaskContext(task)  # includes upstream results, so the split can follow what already exists
    lines += ["", f"Split this task for 1 to {MAX_PARTS} agents working in parallel, one part per agent."]
    return "\n".join(lines)
