# Tasks

<!--
One task per line. Each pending task runs independently and in parallel with the others:
one agent does the task, a fresh agent reviews it, and the author revises what the review asks for
(a revision is re-checked only when the review found CRITICAL issues).
Add "mode: full" under a large task to split it among 1-4 parallel agents instead: the parts review
each other, every revision is re-checked, and the merged result is reviewed again as a whole (about 4-5x the tokens).

  - [ ] [doc] <title>           research / writing / analysis -> Markdown deliverable
  - [ ] [code] <title>          code change in an isolated git worktree (the project must be a git repo)
  - [ ] [sys] <title>           change this orchestrator itself (.orch/, .claude/, CLAUDE.md, TASKS.md, orch.toml, orch*.bat)
                                in a sandbox copy; applied automatically when the run ends, only if the
                                automated checks (compile, import, preview, JSON) and reviews pass
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
                      a glob (doc/refs/*.md) or a URL. Paths are relative to the project ("~\doc\x.md"
                      also works). Missing paths stop the run; small text files are put straight into
                      the prompt, and the reviewer checks the result against them.
  ref: T03            T03's result as base material, plus how it was reviewed (findings and the
                      author's responses, read from T03's run). Waits for T03 if it runs in the same run.
                      Preview shows a notice when a done task was built on an older result than the current one.
  lang: en            output language of a doc deliverable (default: Korean)
  verify: <command>   shell command that must pass for code/sys tasks (e.g. pytest -q)
  mode: full          multi-agent pipeline for large tasks (default: one agent + one review)
  → result: <location> (run <id>)  written by the orchestrator as the last line under a task when it
                      succeeds; "after:" and "ref: Txx" read it (leave it as is, or delete it before a re-run)

Status: [ ] pending, [~] running, [x] done, [!] failed. Set a task back to [ ] to retry it.
A task whose "after:" task did not succeed stays [ ] (blocked) and runs again next time.

Run:      orch            (PowerShell: .\orch)    or /orch in Claude Code
Preview:  orch preview    (PowerShell: .\orch preview)
Reset:    orch-reset      (PowerShell: .\orch-reset)  permanently deletes doc/*.md, report/*.md, run logs and
          this task list (the header stays) after showing counts and asking; "orch-reset --yes" skips the question
Results:  docs -> doc/[Txx]_<YYMMDD>_<Korean title>.md (one document per task, content only)
          code -> staged, uncommitted changes per task (review with git diff --cached)
          sys  -> applied to the system files; originals in .orch/runs/<run-id>/<Txx>/backup/
          progress report -> report/[Txx]_<YYMMDD>_<title>.md for every code and doc task that ran (Korean:
                  timeline, assumptions, review findings and how they were handled, follow-ups)
          logs, reviews, summary -> .orch/runs/<run-id>/
-->
