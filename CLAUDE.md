# Claude Rules

<!-- ---------------------------------------- -->

## [📌 Global Rules]

### 1. Response Style
- Respond in Korean
- Keep technical terms in English as-is (e.g., API, middleware, callback)
- **Tone rule (MUST follow in every response):**
  - End every sentence with concise verb endings: "~함", "~필요", "~완료", "~없음", "~있음", "~됨", "~가능"
  - NEVER use formal/polite endings: "~합니다", "~입니다", "~습니다", "~세요", "~겠습니다"
  - NEVER use casual-polite endings: "~해요", "~예요", "~네요"
  - Examples:
    - ✅ "파일 수정 완료함" / "해당 함수 확인 필요" / "버그 원인 파악됨"
    - ❌ "파일을 수정했습니다" / "확인해 주세요" / "파악되었습니다"

### 2. Output Format
- Concise — answer first, explain only if needed
- Use structured format — bullet points, tables, code blocks
- Context-aware — adjust detail level based on content complexity
- **All .md files MUST be written in English** (except content that can only be expressed in Korean, e.g., Korean-specific examples in CLAUDE.md tone rules, orchestrator doc deliverables `doc/[Txx]_*.md`, which follow the task's output language — see Orchestrated Agent Rules, and orchestrator progress reports `report/[Txx]_*.md`, which are Korean)
- **Plan files MUST be written in Korean** — plans are for user communication, not code documentation

### 3. Workflow

#### 3-1. Before Writing Code
- Read and understand existing files before editing
- Ask clarifying questions if requirements are ambiguous
- Present options before creating files
- Show list of affected files before making changes

#### 3-2. While Writing Code
- Use parallel tool calls when possible for speed
- Add brief comment on reason for each change

#### 3-3. After Writing Code
- Summarize what was changed and why
- Do not auto-commit — wait for explicit instruction
- Show diff of changes
- List added/modified/removed files

### 4. Agent Behavior
- Use Explore agent for codebase-wide searches
- Run parallel agents for independent tasks
- Use background agents to minimize wait time
- Use Plan agent for large-scale task design
- Remember preferred tech stack and patterns
- Update memory immediately when corrected
- Ask before saving to memory

### 5. Restrictions

#### 5-1. 🟢 Do
- Check existing code before suggesting changes
- Ask before creating new files
- Use dedicated tools (Read, Edit, Grep) over Bash equivalents

#### 5-2. 🔴 Do Not
- Overwrite uncommitted changes without asking
- Commit .env or secret files
- Modify large number of files without confirmation

### 6. Tool Usage
- Use sequential-thinking for complex debugging
- Use context7 before implementing library features
- Use tavily-mcp for searching latest docs or solutions

### 7. Dangerous Operations
- Ask before deleting files or directories
- Ask before git push or force operations
- Ask before modifying CI/CD or deployment configs
- Do not run commands that could affect production
- Ask before installing/removing packages
- Ask before DB schema changes or data manipulation

### 8. Skills Reference
- Guideline and role skill files are optional and not shipped with this repo; add them locally under `.claude/skills/`:
  - `skills/common/*.md` — shared guidelines (e.g., conventions, security, performance)
  - `skills/develop/{role}.md` — role-specific instructions (e.g., `debugger`, `reviewer`)
- If these files exist, read the relevant ones before starting a task; otherwise skip this step
- The orchestrator injects `develop/{role}.md` into `[kind:role]` tasks, and adds `develop/reviewer.md` and `common/{conventions,security,performance}.md` to the code review rubric when present
- `skills/orch/` is the `/orch` command that runs the orchestrator

### 9. Orchestrated Agent Rules
- Applies only to headless agents launched by the `.orch` orchestrator (their system prompt - or, for Gemini agents, the first message of the session - contains a `# Role:` section from `.orch/prompts/`); interactive sessions ignore this section
- For those agents, the orchestrator prompt takes precedence over §1 (response style), §2 (.md language), §3 (workflow confirmations), §4 (agent behavior), §5-1 (asking before creating files in task scope) and §8 (skill reading — the needed role skill is already injected)
- Output language of doc deliverables comes from the task: Korean by default, `lang:` in `TASKS.md` overrides it; established technical terms stay in English
- §5-2 and §7 (dangerous operations) still apply in full

<!-- ---------------------------------------- -->

## [📁 Project Rules]



<!-- ---------------------------------------- -->
