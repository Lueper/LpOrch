# Tasks

<!--
One task per line. Each pending task runs independently and in parallel with the others:
one agent does the task, a fresh agent reviews it, and the author revises what the review asks for
(a revision is re-checked only when the review found CRITICAL issues).
Add "mode: full" under a large task to split it among 1-4 parallel agents instead: the parts review
each other, every revision is re-checked, and the merged result is reviewed again as a whole (about 4-5x the tokens).

  - [ ] [doc] <title>           research / writing / analysis -> Markdown deliverable
  - [ ] [code] <title>          code change in an isolated git worktree (the project must be a git repo)
  - [ ] [sys] <title>           change this orchestrator itself (.orch/, .claude/, CLAUDE.md, README.md, TASKS.md, orch.toml, orch*.bat, orch*.sh)
                                in a sandbox copy; applied automatically when the run ends, only if the
                                automated checks (compile, import, preview, JSON) and reviews pass
  - [ ] [test] <title>          try something out in a disposable copy of the whole project (files may be
                                changed and commands run there); the copy is deleted when the run ends and
                                only the progress report remains - the project is never touched
  - [ ] [task] <title>          write new tasks: the agent's result is a list of tasks in this file's syntax,
                                reviewed like any result and added here as pending tasks when the run ends
                                (up to 20; a later run executes them). They are numbered from the next free ID,
                                which the agent is told, so "after:" between them names those IDs
  - [ ] [<kind>:<role>] <title> also injects .claude/skills/develop/<role>.md when that file exists
                                (role skills are not shipped; add your own, e.g. develop/refactorer.md)

Spaces between the parts and the checkbox are optional, and case does not matter: "-[doc]title",
"- [][Doc] title" and "- [ ] [doc] title" are the same pending task. A task that runs is rewritten in the
last, standard form. Lines that look like a task but cannot be read ("- [ ] [docs] x", "* [ ] [doc] x",
"- [ ] [doc]" with no title) are listed as warnings by preview and run.

Indent lines under a task (2+ spaces) to add detailed instructions. These detail lines are settings:
  after: T04, T05     wait for these tasks and build on their results (IDs follow line order:
                      T01, T02, ... - add new tasks at the end so existing IDs stay stable)
  ref: <path or URL>  base material for the task; one per line. A file, a folder (all files in it),
                      a glob (doc/refs/*.md) or a URL. Paths are relative to the project, with / or \ on
                      Windows and macOS alike ("~\doc\x.md" also works). Missing paths stop the run; small
                      text files are put straight into the prompt, and the reviewer checks the result against them.
  ref: T03            T03's result as base material, plus how it was reviewed (findings and the
                      author's responses, read from T03's run). Waits for T03 if it runs in the same run.
                      Task IDs may share a line and sit next to paths: "ref: T03, T04", "ref: T03, doc/x.md".
                      Preview shows a notice when a done task was built on an older result than the current one.
  ref: T01 (origin)   written by the orchestrator under a follow-up: T01 is the task a person wrote that the
                      follow-up chain started from - its goal - and works as "ref: T01" otherwise
  todo: <to-do>       what only a person can do (merge, commit, decide, ...). While it is there, this task and
                      every task built on it ("after:", "ref: Txx") wait. Delete the line or mark it
                      "todo: [x] <to-do>" once done. Agents and the orchestrator (a code result it could not
                      apply) write these lines too
  lang: en            output language of a doc deliverable (default: Korean)
  verify: <command>   shell command that must pass for code/sys tasks (e.g. pytest -q); it runs in cmd.exe on
                      Windows and /bin/sh on macOS, so a task list shared by both needs a command both run
  mode: full          multi-agent pipeline for large tasks (default: one agent + one review); not for test or task
  → result: <location> (run <id>)  written by the orchestrator as the last line under a task when it
                      succeeds: a doc task's document, or the progress report for every other kind;
                      "after:" and "ref: Txx" read it (leave it as is, or delete it before a re-run)

Status: [ ] pending, [~] running, [x] done, [!] failed. Set a task back to [ ] to retry it.
A task whose "after:" task did not succeed stays [ ] (blocked) and runs again next time; one held back by an
open "todo:" is shown as "waiting:" and does not start until the to-do is done.

A task that succeeds may also add tasks here itself: when its agents found work that needs a task of its own
(outside the task's scope, a problem they must not fix there), the orchestrator appends it as a new pending task
at the end of the list, with the detail lines the agent asked for - up to 5 per task. Each follow-up gets
"ref: <origin> (origin)" (the task a person wrote that the chain started from) and "after: <the task that
proposed it>"; a chain grows at most max_followup_depth generations and max_followups_per_origin follow-ups
([tasks] in orch.toml), and what a limit stops is noted in the reports instead. What only a person can do is
written as a "todo:" line under the task instead of a new task. Each proposal is reviewed with the task's result,
and a detail line that would stop the next run (an unknown "after:" task, a "ref:" that matches nothing) is
dropped. The run log and the progress report say what was added; review the new lines like your own and delete
what you do not want. Failed tasks add nothing (they run again), and a title already in the list is not added
twice. Set follow_ups = false under [tasks] in orch.toml to turn this off: agents are then not asked for
follow-ups and nothing is written here. When an origin's chain has ended or waits on a person, a Korean goal
report says what it achieved against the origin's goal (report/[Txx]_<YYMMDD>_목표_보고_<title>.md).

The task list can also live in a Notion database instead of this file: set source = "notion" under [tasks]
in orch.toml and the database link under [notion]. One row is then one task, with the properties task (the ID,
T01), title, kind (doc|code|sys|test|task), status (pending|running|done|failed), ref (one "ref:" per line), details (the
other detail lines above, one per line) and result (written by the orchestrator) - the names are lower case and
case-sensitive; see orch.toml for the setup.
This file becomes the database's cache: every run and preview rewrites the task list below from the rows (this
header stays), the run works on it as usual, and status, results and follow-up tasks are written to the row and
to this file alike. Edit the tasks in Notion; edits here are overwritten. "orch --source file" runs this file as
it is, without Notion (its IDs then follow line order again).

Run:      orch            (PowerShell: .\orch, macOS/Linux: ./orch.sh)    or /orch in Claude Code
Preview:  orch preview    (PowerShell: .\orch preview, macOS/Linux: ./orch.sh preview)
Reset:    orch-reset      (PowerShell: .\orch-reset, macOS/Linux: ./orch-reset.sh)  permanently deletes doc/*.md,
          report/*.md, run logs and this task list (the header stays) after showing counts and asking;
          "orch-reset --yes" skips the question
Results:  docs -> doc/[Txx]_<YYMMDD>_<Korean title>.md (one document per task, content only)
          code -> staged, uncommitted changes per task (review with git diff --cached); a run's code results are
                  merged one after another, and one that cannot be applied leaves its patch and a "todo:"
          sys  -> applied to the system files; originals in .orch/runs/<run-id>/<Txx>/backup/
          test -> the progress report only
          task -> new pending tasks at the end of this list (the progress report names their IDs)
          progress report -> report/[Txx]_<YYMMDD>_<title>.md for every task that ran, sys included (Korean:
                  timeline, assumptions, review findings and how they were handled, follow-ups); for every
                  kind but doc it is the task's result: the result line points at it
          logs, reviews, summary -> .orch/runs/<run-id>/
-->

