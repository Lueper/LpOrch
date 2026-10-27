# Tasks

<!--
Quick reference - the full guide is README.md, section "작업 목록 작성법" (syntax, directives, follow-ups, Notion).

  - [ ] [<kind>] <title>          one task per line; IDs follow line order (T01, T02, ...), so add new tasks at the end
  - [ ] [<kind>:<role>] <title>   also injects .claude/skills/develop/<role>.md when it exists
        kind: doc (Markdown deliverable) | code (src/<project>/ named by Claude, no git) | sys (any project file, this orchestrator too)
              test (disposable project copy) | task (writes new tasks)

Detail lines, indented 2+ spaces under a task:
  after: T01, T02       wait for these tasks and build on their results
  ref: <path|URL|Txx>   base material, one per line ("ref: T01, T02" and "ref: T01, doc/x.md" also work)
  todo: <to-do>         what only a person can do; the task waits until the line is deleted or "todo: [x] ..."
  lang: en              output language of a doc deliverable (default: Korean)
  verify: <command>     must pass for code/sys tasks (code: run in its src/ folder)
  mode: full            split a large task among 1-4 parallel agents (about 4-5x the tokens)
  → result: ...         written by the orchestrator when the task succeeds

Status:   [ ] pending  [~] running  [x] done  [!] failed - runs again with the pending ones (a todo: line holds it back)
Run:      orch | orch preview | orch-reset    (PowerShell: .\orch, macOS/Linux: ./orch.sh)
Notion:   with [tasks] source = "notion" in orch.toml the list below is a cache of the database - edit tasks there
-->

