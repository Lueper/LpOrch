# LpOrch

`TASKS.md`에 작업을 한 줄씩 적어두면, `.orch` 오케스트레이터가 이를 읽어 여러 Claude Code 에이전트를 병렬로 실행하고 **Plan → Work → Review → Merge** 과정을 거쳐 문서·코드·시스템 변경 결과물을 만들어내는 자동화 시스템임.

```mermaid
flowchart LR
    A[orch 실행] --> B[TASKS.md 읽기]
    B --> C[Task별로 동시 실행]
    C --> D[Plan]
    D --> E[Work]
    E --> F[Review / Revise]
    F --> G[Merge]
    G --> H[결과 반영]
```

## 핵심 특징

- **병렬 실행** — `TASKS.md`의 각 줄이 독립된 Task가 되어 동시에 처리됨 (`after:`로 지정한 선행 Task가 있으면 대기). 동시 실행 개수는 CPU/메모리 기준으로 자동 결정됨
- **교차 리뷰** — 작업을 수행한 에이전트와는 다른 새 에이전트가 리뷰를 담당해 CRITICAL/WARNING을 판정하고, 필요한 수정은 원 작업자가 이어서(resume) 반영함
- **3가지 작업 종류** — `doc`(문서), `code`(격리된 git worktree 코드 변경), `sys`(오케스트레이터 자신의 변경, 샌드박스 사본 + 자동 체크 통과 시에만 반영)
- **대규모 작업 분할** — `mode: full` 지정 시 Planner가 작업을 1~4개 Part로 나눠 동시 처리 후 Ring Review와 Merge를 거침
- **안전장치** — 위험 Bash 명령 차단 훅(`protect-files.sh`), `.env`/secret 파일 수정 차단, destructive git 명령 deny 목록 등을 기본 내장

## 요구사항

- Python 3.11+
- [Claude Code](https://docs.claude.com/claude-code) CLI (native 설치 또는 npm 설치) — 로그인된 계정/API 키 필요
- Node.js + npx — `.mcp.json`에 등록된 MCP 서버(`context7`, `sequential-thinking`, `notion` 등) 실행용
- `code` 작업을 쓸 경우 프로젝트가 git 저장소여야 함

## 설치

```bash
pip install -r .orch/requirements.txt
```

`claude-agent-sdk`만 설치하면 됨. Claude Code CLI는 별도로 로그인되어 있어야 함.

## 빠른 시작

1. `TASKS.md`에 작업 줄 추가

   ```
   - [ ] [doc] API 변경 영향 분석 보고서
   ```

2. 실제 실행 없이 계획만 확인

   ```
   orch preview        # PowerShell: .\orch preview
   ```

3. 실행

   ```
   orch                # PowerShell: .\orch
   ```

   Claude Code 안에서는 `/orch`, `/orch preview`로도 실행 가능함.

4. 결과 확인 — `doc/[Txx]_<날짜>_<제목>.md`, `git diff --cached`(code), `report/[Txx]_<날짜>_<제목>.md`(code Task별 진행 보고), `.orch/runs/<run-id>/summary.md`

## TASKS.md 작성법

### 기본 형식

```
- [ ] [<kind>] <title>
- [ ] [<kind>:<role>] <title>
```

- `<kind>` : `doc` / `code` / `sys` 중 하나
- `<role>` : (선택) `.claude/skills/develop/<role>.md` 스킬 추가 주입 (예: `code:refactorer`, `doc:researcher`)
- 상태 마커: `[ ]` 대기, `[~]` 실행 중, `[x]` 완료, `[!]` 실패 — `[!]`/`[x]`를 `[ ]`로 되돌리면 재시도됨

| kind | 설명 | 결과물 위치 |
|---|---|---|
| `doc` | 리서치/작성/분석 → Markdown 산출물 | `doc/[Txx]_<YYMMDD>_<제목>.md` |
| `code` | 격리된 git worktree에서의 코드 변경 | 워킹 트리에 staged 상태로 반영 (`git diff --cached`) |
| `sys` | 오케스트레이터 자신(`.orch/`, `.claude/`, `CLAUDE.md`, `TASKS.md`, `orch*.bat`)의 변경 | 자동 체크·리뷰 통과 시 실제 파일에 적용, 원본은 `.orch/runs/<run-id>/<Txx>/backup/`에 백업 |

### 상세 지시(directive)

작업 줄 아래 2칸 이상 들여쓴 줄은 작업 지시문이며, 아래 키워드로 시작하면 설정으로 해석됨.

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
| `ref: <path\|URL>` | 작업의 근거 자료(파일/폴더/glob/URL). 작은 텍스트 파일은 프롬프트에 직접 삽입되고 리뷰어가 대조 검증함 |
| `lang: en` | doc 산출물 출력 언어 지정 (기본값: 한국어) |
| `verify: <command>` | code/sys 작업에서 반드시 통과해야 하는 쉘 커맨드 |
| `mode: full` | 대규모 작업을 1~4개 에이전트로 분할하는 멀티 에이전트 파이프라인 사용 (기본값: standard, 1에이전트+1리뷰) |
| `→ result: <location>` | 작업 성공 시 오케스트레이터가 자동 기록. 사람이 직접 쓸 필요 없음 |

## 실행 모드

- **standard (기본값)** — worker 1명 작업 → reviewer 1명 리뷰 → 동의하는 CRITICAL/WARNING만 수정 (WARNING만 있으면 재검증 생략, 토큰 절약)
- **mode: full** — Planner가 1~4개 Part로 분할 → Part별 동시 작업 → Ring Review(서로 돌림 리뷰) → 필요 시 재검증 → Merger가 통합 → 통합 결과 재리뷰 (standard 대비 토큰 약 4~5배 소모)

## 결과물 확인

- `doc` → `doc/[Txx]_<YYMMDD>_<제목>.md` (1작업당 1문서)
- `code` → 워킹 트리에 staged 변경 (`git diff --cached`로 리뷰)
- `sys` → 시스템 파일에 직접 적용, 원본 백업은 `.orch/runs/<run-id>/<Txx>/backup/`, 전체 diff는 `sys.diff`
- 진행 보고 → 실행된 code Task마다 `report/[Txx]_<YYMMDD>_<제목>.md` 생성 (doc/sys Task는 생성 안 함). run 종료 시 haiku가 run 기록(`run.log`, `notes`/`review`/`response`/`checks`, 변경 파일)을 읽고 개요·진행 경과·작업 내용·리뷰 지적사항 및 처리 결과·후속 조치를 한국어로 정리함 (요약 실패 시 원자료를 그대로 기록, `after:` 선행 실패로 blocked된 Task는 제외)
- 로그/리뷰/요약 → `.orch/runs/<run-id>/summary.md`에서 작업별 상태·파트 수·리뷰 결과·비용 확인 가능

## 디렉터리 구조

```
.
├── orch.bat              # orch 실행 런처 (PowerShell: .\orch)
├── orch-reset.bat         # doc/*.md, report/*.md, 런 로그, TASKS.md 작업 목록 초기화
├── TASKS.md               # 작업 목록 (사람이 직접 작성)
├── CLAUDE.md              # Claude Code 전역 규칙
├── doc/                   # doc 작업 결과물
├── report/                # code Task별 진행 보고
├── .orch/                 # 오케스트레이터 본체 (Python)
│   ├── orchestrate.py     # 진입점
│   ├── phases.py / pipeline.py / review.py / merge.py  # Plan-Work-Review-Merge 파이프라인
│   ├── planning.py        # mode: full Part 분할
│   ├── progress.py        # Task별 한국어 진행 보고 생성
│   ├── capacity.py        # 자동 병렬 수 결정
│   ├── gitops.py          # code 작업용 git worktree 관리
│   ├── sandbox.py         # sys 작업용 샌드박스 관리
│   ├── prompts/           # 역할별(worker/reviewer/merger/namer/reporter) 프롬프트
│   └── runs/              # 실행별 로그/리뷰/요약 (git에는 미포함)
└── .claude/
    ├── settings.json      # 권한(allow/deny), 안전 훅 설정
    ├── hooks/              # 위험 명령·sensitive 파일 차단 훅
    └── skills/
        ├── common/        # 모든 작업에 공통 적용되는 가이드라인
        └── develop/        # 역할별 스킬 (refactorer, reviewer, tester 등)
```