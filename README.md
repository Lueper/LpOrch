# LpOrch

LpOrch는 문서·코드·시스템 변경 결과물을 만들어내는 자동화 시스템임. 작업 목록(`TASKS.md` 또는 Notion 데이터베이스)에 작업을 하나씩 적어두면, `.orch` 오케스트레이터가 이를 읽어 여러 AI 에이전트(Claude Code 또는 Gemini CLI, 역할별 선택)를 병렬로 실행하고 **Plan → Work → Review → Merge** 과정을 거쳐 결과물을 만들어냄.

```mermaid
flowchart LR
    A[orch 실행] --> B[작업 목록 읽기<br/>TASKS.md 또는 Notion]
    B --> C[Task별로 동시 실행]
    C --> D[Plan]
    D --> E[Work]
    E --> F[Review / Revise]
    F --> G[Merge]
    G --> H[결과 반영]
```

## 핵심 특징

- **병렬 실행** — 작업 목록의 각 항목이 독립된 Task가 되어 동시에 처리됨 (`after:`로 지정한 선행 Task가 있으면 대기). 동시 실행 개수는 CPU/메모리 기준으로 자동 결정됨
- **교차 리뷰** — 작업을 수행한 에이전트 대신 새 에이전트가 리뷰를 담당해 CRITICAL/WARNING을 판정하고 필요한 수정은 원 작업자가 이어서(resume) 반영함. 단, 작업 세션의 context가 60k token을 넘었고 CRITICAL이 없으면 새 세션이 작업 요약·diff 위치만 받아 수정함. 긴 세션은 요청마다 context 전체를 다시 읽어 비용이 큼 (`phases.FRESH_FIX_CONTEXT_TOKENS`)
- **5가지 작업 종류** — `doc`(문서), `code`(격리된 git worktree 코드 변경), `sys`(오케스트레이터 자신의 변경, 샌드박스 사본 + 자동 체크 통과 시에만 반영), `test`(프로젝트 임시 복사본에서 시험), `task`(다음 작업 작성)
- **작업 목록 소스 선택** — 작업을 `TASKS.md`에 적거나 Notion 데이터베이스의 행으로 관리함 (`[tasks] source`). Notion을 쓰면 `TASKS.md`는 그 캐시가 됨
- **후속 작업 자동 추가** — 작업 중 별도 Task가 필요한 일을 발견하면 에이전트가 요청하고, 성공한 Task에 한해 오케스트레이터가 작업 목록 끝에 pending으로 추가함 (`[tasks] follow_ups`, Task당 최대 5개)
- **대규모 작업 분할** — `mode: full` 지정 시 Planner가 작업을 1~4개 Part로 나눠 동시 처리 후 Ring Review와 Merge를 거침
- **안전장치** — 위험 Bash 명령 차단 훅(`protect-files.sh`), `.env`/secret 파일 수정 차단, destructive git 명령 deny 목록 등을 기본 내장

## 요구사항

- Windows 또는 macOS (Linux도 macOS와 같은 `.sh` 런처로 동작) — OS별 차이는 아래 "Windows / macOS" 참조
- Python 3.11+ — macOS 기본 `/usr/bin/python3`(3.9)는 사용 불가. `brew install python` 또는 python.org 설치판 필요. 3.11 미만으로 실행하면 `error: LpOrch needs Python 3.11 or newer ...`로 중단됨
- 사용하는 provider의 CLI (역할 하나라도 그 provider를 쓰는 경우에만 필요)
  - claude: [Claude Code](https://docs.claude.com/claude-code) CLI (native 설치 또는 npm 설치) + `claude-agent-sdk` — 로그인된 계정/API 키 필요
  - gemini: [Gemini CLI](https://github.com/google-gemini/gemini-cli) — 아래 "Gemini provider" 참조
- Node.js + npx — `.mcp.json`에 등록된 MCP 서버(`context7`, `sequential-thinking`, `notion` 등) 실행용
- jq — 안전 훅 `.claude/hooks/protect-files.sh`가 사용함. 없으면 훅이 아무것도 차단하지 못함. macOS 15+는 기본 포함, 그 이전은 `brew install jq`. Windows는 Git Bash에서 찾을 수 있게 설치(예: `winget install jqlang.jq`)
- `code` 작업을 쓸 경우 프로젝트가 git 저장소여야 함
- Notion 작업 목록(`[tasks] source = "notion"`)을 쓸 경우 — Notion integration token을 담은 환경변수. Notion REST API를 표준 라이브러리(`urllib`)로 직접 호출하므로 추가 패키지나 MCP 서버는 필요 없음 (아래 "작업 목록을 Notion 데이터베이스로 쓰기" 참조)

## 설치

프로젝트 루트의 `.venv`에 설치하는 것을 권장함. 런처(`orch.bat`/`orch.sh`)는 `.venv`가 있으면 그 Python을 자동으로 쓰므로 activate할 필요 없음.

```powershell
# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\python -m pip install -r .orch\requirements.txt
```

```bash
# macOS/Linux
python3 -m venv .venv
.venv/bin/python -m pip install -r .orch/requirements.txt
```

`.venv`가 없으면 런처는 PATH의 `python`(Windows) / `python3`(macOS/Linux)을 사용함. Windows는 `pip install -r .orch/requirements.txt` 전역 설치도 가능하지만, macOS의 Homebrew Python은 전역 `pip install`을 막으므로(PEP 668) venv가 필요함.

`claude-agent-sdk`만 설치하면 됨(claude provider용). Claude Code CLI는 별도로 로그인되어 있어야 함. SDK는 claude 역할이 처음 실행될 때 import되므로, gemini만 쓰거나 `orch preview`만 실행할 때는 없어도 동작함.

## 빠른 시작

1. 작업 추가 — `TASKS.md`에 한 줄 적거나(기본), Notion을 쓰면 데이터베이스에 행 하나 추가

   ```
   - [ ] [doc] API 변경 영향 분석 보고서
   ```

2. 실제 실행 없이 계획만 확인

   ```
   orch preview          # Windows (PowerShell: .\orch preview)
   ./orch.sh preview     # macOS/Linux
   ```

3. 실행

   ```
   orch                  # Windows (PowerShell: .\orch)
   ./orch.sh             # macOS/Linux
   ```

   Claude Code 안에서는 `/orch`, `/orch preview`로도 실행 가능함 (OS에 맞는 런처를 호출함). macOS에서 `permission denied`가 나오면 실행 권한이 없는 것임 → `chmod +x orch.sh orch-reset.sh` 또는 `sh orch.sh`로 실행

   주기 실행 — 숫자(분)를 주면 Ctrl+C로 멈출 때까지 그 간격으로 반복함. 다른 옵션도 그대로 붙일 수 있음 (`orch 60 --max-rounds 2`)

   ```
   orch 60               # Windows (PowerShell: .\orch 60) - 60분마다 run
   ./orch.sh 60          # macOS/Linux
   ```

   - 간격은 run **시작** 기준. run이 간격보다 오래 걸리면 끝나자마자 다음 run 시작 (겹쳐 돌지 않음)
   - 매 run은 새 프로세스로 실행됨 → 작업 목록·`orch.toml`·`[sys]` 작업이 바꾼 코드가 다음 run에 반영됨
   - pending 작업이 없거나 전부 `after:` 선행 실패로 blocked면 run을 만들지 않고 다음 주기까지 대기함
   - 설정·작업 목록 오류(`error: ...`)가 나도 멈추지 않고 다음 주기에 다시 확인함 (시작 시점의 `orch.toml` 오류만 즉시 종료)
   - run 도중 Ctrl+C → 해당 run이 작업을 pending으로 되돌린 뒤 종료됨
   - 후속 작업(follow-up)·`[task]` 작업이 추가한 Task도 다음 주기에 실행됨 → 연쇄 실행 비용이 걱정되면 `[tasks] follow_ups = false`

4. 결과 확인 — `doc/[Txx]_<날짜>_<제목>.md`, `git diff --cached`(code), `report/[Txx]_<날짜>_<제목>.md`(Task별 진행 보고), `.orch/runs/<run-id>/summary.md`

## Windows / macOS

같은 저장소와 작업 목록(`TASKS.md` 또는 Notion)을 두 OS에서 그대로 사용함. 다른 점은 아래뿐임.

| 항목 | Windows | macOS/Linux |
|---|---|---|
| 런처 | `orch.bat`, `orch-reset.bat` (PowerShell: `.\orch`, `.\orch-reset`) | `./orch.sh`, `./orch-reset.sh` |
| Python | `.venv\Scripts\python.exe`, 없으면 PATH의 `python` | `.venv/bin/python`, 없으면 PATH의 `python3` |
| `verify:` 명령 실행 shell | `cmd.exe` | `/bin/sh` |
| Gemini agent의 shell | PowerShell | bash |
| 안전 훅(`protect-files.sh`) 실행 | Git Bash | `/bin/bash` |
| 자동 병렬 수의 메모리 기준 | 가용 물리 메모리 | `vm_stat`의 free + inactive + speculative 페이지 |

- **작업 목록 공유** — `ref:` 경로는 `/`와 `\` 모두 양쪽 OS에서 인식됨. `verify:`는 두 shell에서 모두 도는 명령으로 작성함(예: `pytest -q`). macOS에는 `python` 명령이 없는 경우가 많으므로 `python ...` 형태는 피함
- **줄바꿈·실행 권한** — `.gitattributes`가 `*.sh`는 LF, `*.bat`은 CRLF로 고정함. Windows에서 `.sh`를 처음 커밋할 때 `git add --chmod=+x orch.sh orch-reset.sh`로 실행 권한을 기록해야 macOS에서 `./orch.sh`로 바로 실행됨
- **OS 메타데이터 파일** — macOS의 `.DS_Store`·AppleDouble(`._*`), Windows의 `Thumbs.db`·`desktop.ini`는 폴더/glob `ref:`와 sys 작업 대상에서 제외됨
- **런처 수정** — 두 런처는 쌍으로 유지함(`orch.bat` ↔ `orch.sh`, `orch-reset.bat` ↔ `orch-reset.sh`). sys 작업 worker에게도 같은 규칙이 지시됨

## 설정 (orch.toml)

프로젝트 루트의 `orch.toml`에서 실행 옵션 기본값과 역할별 provider/model을 지정함. 우선순위는 **CLI 옵션 > `orch.toml` > 내장 기본값**이며, 파일이나 키가 없으면 내장 기본값을 사용함. 알 수 없는 섹션·키, 잘못된 타입·범위는 실행 전에 `error: orch.toml: ...`로 중단됨.

| 섹션 | 키 | 기본값 | CLI 옵션 | 의미 |
|---|---|---|---|---|
| `[run]` | `max_parallel` | `0` | `--max-parallel` | 전체 동시 에이전트 수, `0` = 자동(CPU·메모리·작업 수 기준) |
| | `max_rounds` | `1` | `--max-rounds` | 리뷰/수정 라운드, `0` = 리뷰 없음 |
| | `budget_per_agent` | `2.0` | `--budget-per-agent` | 에이전트 호출당 USD 상한 |
| | `max_turns` | `40` | `--max-turns` | 에이전트 호출당 turn 상한 |
| `[agent]` | `provider` | `"claude"` | `--provider` | 역할별 지정이 없는 모든 역할의 provider (`claude` \| `gemini`) |
| `[tasks]` | `source` | `"file"` | `--source` | 작업 목록 위치 (`file` = `TASKS.md`, `notion` = `[notion]`의 데이터베이스) |
| | `follow_ups` | `true` | — | 성공한 Task가 에이전트들이 요청한 후속 작업을 목록에 추가함(Task당 최대 5개). `false` = 에이전트에게 요청 자체를 안 하고 목록에 아무것도 쓰지 않음 |
| `[notion]` | `database` | `""` | — | 데이터베이스 링크("Copy link") 또는 32자 id (`?v=` 뷰 id는 무시됨). `source = "notion"`일 때 필수 |
| | `token_env` | `"NOTION_TOKEN"` | — | integration token이 담긴 환경변수 **이름** (토큰 자체는 `orch.toml`에 적지 않음) |
| `[claude]` | `model` | `""` | `--model` | 주 모델, `""` = CLI 기본 모델 |
| | `small_model` | `"haiku"` | — | namer·reporter용 모델, `""` = `model`과 동일 |
| | `cli_path` | `""` | `--cli-path` | native claude 실행 파일, `""` = 자동 탐지 |
| | `max_retries` | (CLI 기본 10) | — | API 요청 실패(429·529·네트워크) 재시도 횟수. 지정 시 `CLAUDE_CODE_MAX_RETRIES`로 전달 |
| `[gemini]` | `model` / `small_model` | `""` / `"flash-lite"` | `--model` | `[claude]`와 동일한 의미 (`""` = CLI 기본 `auto`) |
| | `cli_path` | `""` | `--cli-path` | gemini 실행 파일 또는 `gemini.js`, `""` = 자동 탐지 |
| | `max_retries` | `0` | — | API 요청 실패(429 quota·네트워크) 재시도 횟수(0~9). `0` = 즉시 실패 (CLI 기본은 요청당 10회) |
| `[roles.<역할>]` | `provider`, `model` | — | — | 역할별 override |

- 역할: `doc-worker`, `code-worker`, `sys-worker`, `test-worker`, `task-worker`, `reviewer`, `doc-merger`, `code-merger`, `planner`, `namer`, `reporter`
- 역할의 model 결정 순서: `[roles.<역할>] model` → (namer·reporter만) provider의 `small_model` → provider의 `model` → CLI 기본 모델
- `--model`·`--cli-path`는 기본 provider(`--provider` 또는 `[agent] provider`)의 `model`·`cli_path`만 바꾸며 `[roles.*]` override와 다른 provider 설정은 그대로 유지됨
- `orch preview`에 적용된 설정 파일, 작업 목록 소스, follow-up 설정, 역할별 provider/model이 표시됨

```toml
# 예: 작업은 Claude, 리뷰는 Gemini로 교차 검증
[roles.reviewer]
provider = "gemini"
model = "pro"
```

### Provider 구조

claude와 gemini는 interface가 같은 backend로 대칭 구성됨.

- **공통 호출**: 모든 phase는 `agents/runner.py`의 `RunAgent`만 호출함. `RunAgent`는 역할의 provider를 골라 그 backend의 `Run(label, prompt, spec, ctx, model) -> AgentResult`를 실행함
- **일시적 오류 재시도**: 연결 끊김·timeout·API 과부하(5xx)처럼 일시적인 실패는 15초 후 1회 재시도함 (`runner.py`의 `TRANSIENT_RETRIES`, `RETRY_DELAY_SECONDS`)
  - 실패한 세션이 저장돼 있으면 그 세션을 이어서 답만 다시 요청함 → 이미 끝낸 검색·편집은 유지됨. 세션이 없으면 같은 호출을 처음부터 다시 실행함
  - 인증·예산·turn 상한·rate limit(429) 실패는 다시 해도 같으므로 재시도 안 함. 판정 기준은 backend별 `TRANSIENT_ERROR`
  - run.log에는 `<단계> retry1`로 기록되고 실패한 시도의 비용도 합산됨
- **token 기록**: Claude 호출은 종료 줄에 비용과 함께 token 사용량이 남음. 예: `ok ($2.572; 31 turns, peak context 103k, cache read 2.0M, cache write 103k, input 1k, output 40k, time 6m12s, API 5m40s, model claude-opus-5-5)`. `peak context`(가장 큰 요청)와 `cache read`가 크면 세션이 길게 늘어진 것임 (Gemini는 별도 `tokens:` 줄)
  - token 합계는 result message의 usage 합계와 응답별 usage 합계 중 항목마다 큰 값임 (응답별 usage는 스트리밍 중간 값이라 output이 빠질 수 있음)
  - `time`·`API`는 result message의 `duration_ms`·`duration_api_ms`, `model`은 `model_usage`의 model ID(subagent 포함, `+`로 연결)
- **context 절약**: worker는 파일 전체 `cat` 대신 Grep + 필요한 범위만 Read하고 관련 수정은 한 번에 Edit하도록 지시받음 (`prompts/worker.md` "Working Economically"). reviewer 입력의 diff는 30k자까지만 넣고, 넘으면 변경 파일 목록 + 앞부분 + 나머지 파일 위치를 줌 (`review.REVIEW_DIFF_CHARS`)
- **backend**: `agents/claude.py`(claude_agent_sdk), `agents/gemini.py`(Gemini CLI headless). 처음 호출될 때 import되므로 쓰지 않는 provider의 SDK·CLI는 필요 없음
- **공통 규격**: 역할별 도구(`ROLE_TOOLS`, Claude Code 도구 이름 기준), structured output용 JSON schema(`AgentSpec.schema`), tool 진행 로그 형식은 `runner.py`에서 정의하고, 각 backend가 자기 방식으로 변환함
- **등록**: `core/providers.py`에 provider별 기본값(`small_model`, `max_retries`), `max_retries` 상한, preview 안내문, backend 모듈을 등록함. `orch.toml`의 `[<provider>]` 섹션 검증·기본값은 여기서 자동으로 따라옴
- provider 추가 = `core/providers.py` 항목 1개 + `agents/<provider>.py` backend 1개 (`Run`, `TRANSIENT_ERROR`)

### Gemini provider

`provider = "gemini"`인 역할은 Gemini CLI를 headless(`stream-json`)로 실행함 (`.orch/agents/gemini.py`). ring review(`mode: full`)에서 리뷰어와 작업자의 provider가 다르면 세션을 이어받을 수 없어 fresh reviewer로 대체됨.

- **준비**: Gemini CLI 설치 후 인증. API key(OS keychain에 저장된 key 또는 `GEMINI_API_KEY` 환경변수)나 Vertex 환경변수 방식 권장
- **실행 격리**: 실행마다 `.orch/runs/<run-id>/gemini/home`을 `GEMINI_CLI_HOME`으로 사용함
  - 사용자의 `~/.gemini/settings.json`을 복사한 뒤 CLAUDE.md context, `max_turns`, guard hook을 덮어씀
  - 세션도 이 폴더에 저장되며 사용자의 `~/.gemini`는 건드리지 않음
  - 인증(OAuth 로그인 캐시, `~/.gemini/.env`의 key)이 `~/.gemini`의 파일에만 있으면 이 폴더에서 보이지 않음
- **도구 제한**: Claude 역할별 도구를 Gemini built-in으로 변환해 policy(`--policy`)로 적용함. 나머지 도구와 MCP는 전부 차단됨
- **보안장치**: `write_file`/`replace`/`run_shell_command` 호출 전에 `.orch/agents/gemini_guard.py`가 실행됨
  - 프로젝트 `.claude/settings.json`의 `permissions.deny` 규칙과 PreToolUse hook(`protect-files.sh` 등)을 그대로 적용함
  - 검사 자체를 할 수 없으면 차단함(fail closed)
- **역할 프롬프트**: system prompt에 덧붙일 수 없어 세션 첫 메시지 앞에 넣음
- **structured output**: reviewer, planner, namer의 JSON 응답은 프롬프트로 요청하고 파싱함. 실패하면 같은 세션에서 1회 재요청함
- **API 실패**: Gemini CLI는 기본적으로 요청 하나를 최대 10회 재시도함. 분당 한도 초과(429)는 약 1분 간격으로 재시도해 수십 분씩 멈춘 것처럼 보일 수 있음
  - LpOrch는 `[gemini] max_retries`(기본 `0`)로 이를 제한함
  - 네트워크 오류·5xx는 이와 별개로 LpOrch가 1회 재시도함 (위 "일시적 오류 재시도"). quota 초과(429)는 재시도 안 함
  - 실패한 시도와 quota 내역(`Quota exceeded for metric: ...`)은 콘솔에 WARNING으로 표시됨
  - 무료 tier key는 모델별 분당 요청 한도가 낮음(예: 5회). 에이전트 하나가 도구를 몇 번만 써도 한도에 닿으므로 결제가 연결된 key 권장
- **제약**:
  - 비용(USD)이 보고되지 않음 → summary에는 `$0`으로 집계되고, 호출별 token 수와 실제 사용 model은 `run.log`에 기록됨
  - `budget_per_agent`는 적용되지 않음. `max_turns`만 적용됨
  - headless에서는 build 파일(`CMakeLists.txt`, `*.cmake`, `package.json`, `pyproject.toml` 등) 수정이 Gemini CLI 자체 정책으로 차단됨 → build 설정을 바꾸는 code 작업은 Claude 권장
  - Windows에서 Gemini의 shell은 PowerShell임. deny 규칙은 명령 문자열로 비교하므로 bash 기준 규칙이 PowerShell 명령(`Remove-Item` 등)까지 막지는 않음. macOS/Linux는 bash라 해당 없음

## 작업 목록 작성법

작업 목록은 `TASKS.md`의 줄(기본) 또는 Notion 데이터베이스의 행으로 적음. Notion 모드에서도 행이 `TASKS.md` 줄로 변환돼 같은 파서를 통과하므로 어느 쪽이든 아래 형식과 directive가 그대로 적용됨.

### 기본 형식

```
- [ ] [<kind>] <title>
- [ ] [<kind>:<role>] <title>
```

- `<kind>` : `doc` / `code` / `sys` / `test` / `task` 중 하나
- `<role>` : (선택) `.claude/skills/develop/<role>.md` 스킬 추가 주입 (예: `code:refactorer`, `doc:researcher`)
- 상태 마커: `[ ]` 대기, `[~]` 실행 중, `[x]` 완료, `[!]` 실패. `[!]`/`[x]`를 `[ ]`로 되돌리면 재시도됨
- 구분 공백과 checkbox는 생략 가능하고 대소문자는 무시됨: `-[doc]제목`, `- [][Doc] 제목`, `- [ ] [doc] 제목`은 모두 같은 대기 작업. 실행되는 작업 줄은 표준 형식(`- [~] [doc] 제목`)으로 다시 쓰임
- 작업처럼 보이지만 인식되지 않는 줄(`- [ ] [docs] x`, `* [ ] [doc] x`, 제목 없는 `- [ ] [doc]` 등)도 있음. 이런 줄은 `orch preview`·`orch` 실행 시 `warning: TASKS.md line N is not read as a task ...`로 표시됨. 실행은 계속됨. Notion 모드에서는 읽히지 않은 **행**이 같은 방식으로 경고됨

| kind | 설명 | 결과물 위치 |
|---|---|---|
| `doc` | 리서치/작성/분석 → Markdown 산출물 | `doc/[Txx]_<YYMMDD>_<제목>.md` |
| `code` | 격리된 git worktree에서 하는 코드 변경 | 워킹 트리에 staged 상태로 반영 (`git diff --cached`) |
| `sys` | 오케스트레이터 자신(`.orch/`, `.claude/`, `CLAUDE.md`, `README.md`, `TASKS.md`, `orch.toml`, `orch*.bat`, `orch*.sh`)의 변경 | 자동 체크·리뷰 통과 시 실제 파일에 적용, 원본은 `.orch/runs/<run-id>/<Txx>/backup/`에 백업 |
| `test` | 프로젝트 전체의 임시 복사본(미커밋 변경 포함, `.git`·run 기록 제외)에서 파일 수정·명령 실행으로 시험 | 진행 보고만 남음 (복사본은 run 종료 시 삭제, 실제 프로젝트는 변경 없음) |
| `task` | 다음 작업 작성 — 결과물이 작업 목록 문법의 새 task 목록이며 다른 결과물처럼 리뷰됨 (최대 20개) | run 종료 시 작업 목록 끝에 pending으로 추가, 다음 run에서 실행. 새 ID는 다음 빈 번호부터 매겨지고 agent에게 미리 알려줘서 `after:`로 서로 연결 가능 |

### 상세 지시(directive)

작업 줄 아래 2칸 이상 들여쓴 줄은 작업 지시문이며, 아래 키워드로 시작하면 설정으로 해석됨. Notion 모드에서는 `ref` 속성의 각 줄이 `ref:` directive가 되고 `details` 속성의 각 줄이 나머지 directive·지시문이 됨.

```
- [ ] [doc] 예시 작업 제목
  일반 설명 텍스트는 그대로 작업 지시문으로 전달됨
  after: T04, T05
  ref: doc/refs/spec.md
  lang: en
  verify: pytest -q
  mode: full
```

| directive | 의미 |
|---|---|
| `after: Txx, Tyy` | 지정한 작업들이 끝날 때까지 대기 후 그 결과를 입력으로 사용 |
| `ref: <path\|URL>` | 작업의 근거 자료(파일/폴더/glob/URL). 작은 텍스트 파일은 프롬프트에 직접 삽입되고 리뷰어가 대조 검증함. 경로 구분자는 OS와 무관하게 `/`, `\` 모두 가능 |
| `lang: en` | doc 산출물 출력 언어 지정 (기본값: 한국어) |
| `verify: <command>` | code/sys 작업에서 반드시 통과해야 하는 쉘 커맨드. Windows는 `cmd.exe`, macOS/Linux는 `/bin/sh`에서 실행됨 |
| `mode: full` | 대규모 작업을 1~4개 에이전트로 분할하는 멀티 에이전트 파이프라인 사용 (기본값: standard, 1에이전트+1리뷰) |
| `→ result: <location>` | 작업 성공 시 오케스트레이터가 자동 기록 — `doc`은 `doc/` 문서, 그 외 kind는 `report/` 진행 보고 경로. `after:`·`ref: Txx`가 이 값을 읽음. 사람이 직접 쓸 필요 없음 |

### 후속 작업(follow-up) 자동 추가

작업 중 **별도 Task가 필요한 일**(이 작업의 범위 밖, 다른 사람의 결정 대기, 여기서 고쳐선 안 되는 문제)을 발견하면 에이전트는 그 일을 직접 하지 않고 최종 메시지 끝의 `=== FOLLOW-UP TASKS ===` 줄 아래에 작업 목록 문법으로 적음. 오케스트레이터가 run 종료 시 이를 작업 목록 끝에 pending으로 추가하고, 다음 run이 실행함.

- **설정** — `[tasks] follow_ups`(기본 `true`). `false`면 에이전트 프롬프트에 요청 자체가 들어가지 않으므로 아무것도 추가되지 않음. `orch preview`가 켜져 있는지와 상한을 미리 알려줌
- **상한** — 한 Task가 추가할 수 있는 후속 작업은 최대 5개(`MAX_FOLLOWUPS`). 그보다 많이 요청하면 앞의 5개만 추가되고 run.log에 WARNING이 남음
- **리뷰 대상** — 제안된 후속 작업은 결과물과 함께 리뷰어에게 전달됨. 리뷰어는 이를 결과물의 품질로 판정하지 않되, 이 Task가 끝냈어야 할 일을 떠넘기거나 결과물과 중복되거나 작성자의 세션 없이는 실행 불가능할 만큼 모호하면 WARNING으로 지적함. 작성자는 수정 때 문구를 고치거나 뺄 수 있음
- **추가 규칙**
  - 이미 목록에 같은 제목(kind + 제목)이 있으면 추가하지 않음
  - 다음 run을 중단시킬 detail 줄은 버리고 run.log에 기록함. 존재하지 않는 `after:` ID, 아무것도 매칭하지 않는 `ref:`, 알 수 없는 `mode:`가 이에 해당함
  - 실패한 Task는 아무것도 추가하지 않음 (해당 Task 자체가 다시 실행됨)
  - `task` kind가 작성한 작업 목록은 그 Task의 결과물이므로 `follow_ups` 설정과 무관하게 추가됨
- **기록** — 추가된 ID와 제목은 `run.log`, `summary.md`, 그리고 Task별 진행 보고의 "남은 이슈 및 후속 조치"에 남음. 원치 않는 항목은 다음 run 전에 작업 목록에서 지우면 됨

### 작업 목록을 Notion 데이터베이스로 쓰기

`[tasks] source = "notion"`으로 두면 작업 목록이 Notion 데이터베이스가 되고, 행 하나가 Task 하나가 됨. Notion REST API를 `urllib`로 직접 호출하므로 추가 패키지나 MCP 서버는 필요 없음 (`.orch/core/notion.py`).

**준비**

1. [notion.so/my-integrations](https://www.notion.so/my-integrations)에서 internal integration 생성
2. 대상 데이터베이스에 그 integration 초대 (Share → Connections)
3. integration secret을 환경변수에 넣음 — 기본 `NOTION_TOKEN`, 이름은 `[notion] token_env`로 변경 가능. **`orch.toml`은 커밋되는 파일이므로 토큰 자체를 적지 않음**
   - Windows: `setx NOTION_TOKEN "<token>"` 후 새 터미널에서 실행
   - macOS: `~/.zshrc`에 `export NOTION_TOKEN="<token>"` 추가 후 새 터미널에서 실행
   - macOS에서 python.org 설치판 Python을 쓰면 처음 한 번 `/Applications/Python 3.x/Install Certificates.command` 실행 필요. 안 하면 인증서 검증에 실패하고, 에러 메시지에 이 안내가 붙음
4. `orch.toml`에 소스와 데이터베이스 지정

   ```toml
   [tasks]
   source = "notion"

   [notion]
   database = "https://www.notion.so/<workspace>/<32자-id>"   # "Copy link" 결과 또는 32자 id
   token_env = "NOTION_TOKEN"
   ```

5. 첫 run 전에 `orch preview`로 확인 — 토큰·초대·속성 이름 문제는 에이전트가 하나도 시작되기 전에 `error: ...`로 중단되고, 무엇을 고쳐야 하는지 Notion의 원문 메시지와 함께 표시됨

**데이터베이스 속성** — 이름은 `.orch/core/notion.py`에 고정돼 있으므로 아래와 정확히 같아야 함 (모두 소문자, 대소문자 구분).

| 속성 | Notion 타입 | 내용 |
|---|---|---|
| `task` | Title | 작업 ID — `T01`, `T02` … `after: T01`·`ref: T01`이 가리키는 값 |
| `title` | Text | 작업 제목 |
| `kind` | Select | `doc` \| `code` \| `sys` \| `test` \| `task` (역할 스킬은 `code:refactorer`처럼) |
| `status` | Status | `pending` / `running` / `done` / `failed` 옵션 필요 → `[ ]` `[~]` `[x]` `[!]`에 대응 |
| `ref` | Text | 근거 자료, 한 줄에 하나 (경로/glob/URL/작업 ID) — 각 줄이 `ref:` directive가 됨 |
| `details` | Text | 그 외 detail 줄, 한 줄에 하나 (`after: T01`, `lang: en`, `verify: ...`, `mode: full`, 일반 지시문) |
| `result` | Text | 오케스트레이터가 성공 시 기록 — `doc/x.md (run 20261008-101500)` (doc 외 kind는 `report/` 진행 보고 경로) |

- API로는 Status 옵션을 만들 수 없으므로 네 옵션은 미리 만들어 둬야 함
- 속성이 없거나 타입이 다르면 run이 시작되지 않고 무엇이 어긋났는지 나열됨

**읽기·쓰기 동작**

- **ID 순서** — 행은 작업 ID 순으로 읽힘. 같은 ID가 둘이면 먼저 만들어진 행이 ID를 가져가고 나머지는 경고로 빠짐. ID가 없거나 제목·kind·status가 비정상인 행은 읽히지 않은 줄처럼 경고로 보고됨 (Notion 데이터베이스 끝의 빈 행은 조용히 무시)
- **행 수 상한** — 앞 500행까지만 읽고, 초과하면 경고가 표시됨
- **`TASKS.md`는 캐시** — 모든 run·preview가 `TASKS.md`의 작업 목록 부분을 행 내용으로 덮어씀(상단 주석 헤더는 유지). 이후 run은 그 파일을 평소처럼 읽고 씀. **작업은 Notion에서 편집해야 하며, 캐시된 줄을 고쳐도 다음 run이 덮어씀**
- **쓰기 경로** — 상태 마커, `result`, 후속/`task` 작업 추가는 **행에 먼저** 쓰고 그다음 파일에 반영함. 각각 동기 API 호출 1회이며, 상태·결과가 바뀔 때와 작업이 추가될 때만 호출함. 실패한 호출은 429·5xx와 네트워크 오류(timeout·깨진 응답 포함)를 최대 3회 시도한 뒤 포기하고 run.log에 남음. 결과물 자체는 영향받지 않음
- **후속 작업** — 새 행이 다음 빈 ID로 생성되므로 기존 ID는 움직이지 않음. 새 행의 Notion 페이지 URL은 `run.log`에 기록됨 (`summary.md`·진행 보고에는 ID와 제목만)
- **파일로 일회성 실행** — `orch --source file`은 Notion을 건드리지 않고 `TASKS.md`를 그대로 실행함 (이때 ID는 다시 줄 순서를 따름). sys 작업의 자동 체크도 이 방식으로 돌아 네트워크를 타지 않음
- **`orch-reset`** — 로컬 파일만 지움. Notion 행은 상태·결과를 그대로 유지하고 다음 run·preview에 `TASKS.md`로 다시 들어오므로, 정말 지우려면 Notion에서 지워야 함. reset이 이 사실을 안내 문구로 알려줌

## 실행 모드

- **standard (기본값)** — worker 1명 작업 → reviewer 1명 리뷰 → 동의하는 CRITICAL/WARNING만 수정 (WARNING만 있으면 재검증 생략, 토큰 절약)
- **mode: full** — Planner가 1~4개 Part로 분할 → Part별 동시 작업 → Ring Review(서로 돌림 리뷰) → 필요 시 재검증 → Merger가 통합 → 통합 결과 재리뷰 (standard 대비 토큰 약 4~5배 소모)

## 결과물 확인

- `doc` → `doc/[Txx]_<YYMMDD>_<제목>.md` (1작업당 1문서). 문서에는 본문만 들어가고, 작성자의 가정 사항·병합 메모·미해결 리뷰 지적은 진행 보고로 감
- `code` → 워킹 트리에 staged 변경 (`git diff --cached`로 리뷰)
- `sys` → 시스템 파일에 직접 적용, 원본 백업은 `.orch/runs/<run-id>/<Txx>/backup/`, 전체 diff는 `sys.diff`
- `test` → 진행 보고만 남음 (테스트 결과·복사본에서 바꾼 파일 목록 포함)
- `task` → 작업 목록에 새 pending task 추가 (추가된 ID는 진행 보고와 `summary.md`에 기록)
- 후속 작업 → 성공한 Task가 요청한 만큼 작업 목록 끝에 pending으로 추가됨 (`TASKS.md`의 줄 또는 새 Notion 행). 추가된 ID·제목은 진행 보고의 "남은 이슈 및 후속 조치"와 `summary.md`에 기록됨
- 진행 보고 → 실행된 Task마다 `report/[Txx]_<YYMMDD>_<제목>.md` 생성 (code·doc·sys 모두). sys는 적용 여부와 원본 백업 위치를 포함함. run 종료 시 `small_model`(기본 haiku)이 run 기록(`run.log`, `notes`/`review`/`response`/`checks`, 변경 파일 또는 문서 목차)을 읽고 개요·진행 경과·작업 내용·가정 사항·리뷰 지적사항 및 처리 결과·후속 조치를 한국어로 정리함. 요약 실패 시 원자료를 그대로 기록함. `after:` 선행 실패로 blocked된 Task는 제외됨. `doc` 외 kind는 이 보고서가 결과 문서가 되어 작업 목록의 결과 줄(`→ result:`, Notion `result`)이 이를 가리킴
- 로그/리뷰/요약 → `.orch/runs/<run-id>/summary.md`에서 작업별 상태·파트 수·리뷰 결과·비용 확인 가능

## 디렉터리 구조

```
.
├── orch.bat / orch.sh              # 실행 런처 (Windows / macOS·Linux)
├── orch-reset.bat / orch-reset.sh  # 실행 기록·작업 목록 초기화
├── TASKS.md                        # 작업 목록
├── orch.toml                       # Config 설정
├── CLAUDE.md                       # Claude Code 전역 규칙
├── doc/                            # doc 작업 결과물
├── report/                         # Task별 진행 보고
└── .orch/                          # 오케스트레이터 패키지
   ├── core/                       # 설정, 작업 목록, Task 모델
   ├── workspace/                  # git worktree, sys 샌드박스
   ├── agents/                     # provider별 에이전트 호출 (Claude, Gemini)
   ├── reporting/                  # 리뷰 집계, summary, 진행 보고
   ├── pipeline/                   # Plan → Work → Review → Merge
   ├── prompts/                    # 역할별 프롬프트
   └── runs/                       # 실행별 로그 (git 미포함)
```
