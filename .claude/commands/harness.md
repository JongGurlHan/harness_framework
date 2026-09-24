이 프로젝트는 Harness 프레임워크를 사용한다. 아래 워크플로우에 따라 작업을 진행하라.

---

## 워크플로우

### A. 탐색

`/docs/` 하위 문서(PRD, ARCHITECTURE, ADR 등)를 읽고 프로젝트의 기획·아키텍처·설계 의도를 파악한다. 필요시 Explore 에이전트를 병렬로 사용한다.

### B. 논의

구현을 위해 구체화하거나 기술적으로 결정해야 할 사항이 있으면 사용자에게 제시하고 논의한다.

### C. Step 설계

사용자가 구현 계획 작성을 지시하면 기능 단위 step으로 나뉜 초안을 작성해 피드백을 요청한다.

설계 원칙:

1. **검증 가능한 기능 단위** — step 하나는 "끝나면 테스트로 증명할 수 있는 동작 하나"를 만든다 (예: "API 레이어 작성" ✗ → "시세 수신부터 REST 조회까지 동작" ✓). 목표에 필요하면 여러 모듈을 함께 수정해도 된다. step이 너무 작으면 세션 사이 맥락 손실이 커지고, 너무 크면 실패 시 재시도 비용이 커진다. 한 세션에서 끝낼 수 있는 가장 큰 단위를 고른다.
2. **자기완결성** — 각 step 파일은 독립된 Claude 세션에서 실행된다. "이전 대화에서 논의한 바와 같이" 같은 외부 참조는 금지한다. 필요한 정보는 전부 파일 안에 적는다.
3. **맥락은 가리키기만** — 문서 목록과 이전 step 요약은 execute.py가 자동으로 넣는다. step 파일에는 이 step에서 특히 봐야 할 문서 섹션·파일만 적는다. 문서 전문을 복사해 넣지 않는다.
4. **시그니처 수준 지시** — 함수/클래스의 인터페이스만 제시하고 내부 구현은 에이전트 재량에 맡긴다. 시그니처는 설계 의도이지 계약이 아니다 — 에이전트가 실제로 맞지 않는다고 판단하면 조정하고 handoff에 이유를 남긴다. 반대로 바뀌면 안 되는 불변 조건(공개 API, 멱등성, 보안, 데이터 무결성 등)은 명시한다.
5. **AC는 실행 가능한 커맨드** — "~가 동작해야 한다" 같은 추상적 서술이 아닌 `npm run build`, `npm test` 같은 실제 커맨드를 index.json의 `ac`에 넣는다. execute.py가 세션 종료 후 직접 실행해 완료를 판정하므로, 오프라인에서 결정적으로 통과/실패하는 커맨드여야 한다.
6. **주의사항은 구체적으로** — "조심해라" 대신 "X를 하지 마라. 이유: Y" 형식으로 적는다.
7. **네이밍** — step name은 kebab-case slug로, 해당 step의 핵심 모듈/작업을 한두 단어로 표현한다 (예: `project-setup`, `api-layer`, `auth-flow`).

### D. 파일 생성

사용자가 승인하면 아래 파일들을 생성한다.

#### D-1. `phases/index.json` (전체 현황)

여러 task를 관리하는 top-level 인덱스. 이미 존재하면 `phases` 배열에 새 항목을 추가한다.

```json
{
  "phases": [
    {
      "dir": "0-mvp",
      "status": "pending"
    }
  ]
}
```

- `dir`: task 디렉토리명.
- `status`: `"pending"` | `"completed"` | `"error"` | `"blocked"`. execute.py가 실행 중 자동으로 업데이트한다.
- 타임스탬프(`completed_at`, `failed_at`, `blocked_at`)는 execute.py가 상태 변경 시 자동 기록한다. 생성 시 넣지 않는다.

#### D-2. `phases/{task-name}/index.json` (task 상세)

```json
{
  "project": "<프로젝트명>",
  "phase": "<task-name>",
  "model": "claude-opus-5-5",
  "timeout_sec": 3600,
  "steps": [
    { "step": 0, "name": "project-setup", "status": "pending", "ac": ["npm run build", "npm test"] },
    { "step": 1, "name": "market-feed", "status": "pending", "ac": ["npm run build", "npm test"] }
  ]
}
```

필드 규칙:

- `project`: 프로젝트명 (CLAUDE.md 참조).
- `phase`: task 이름. 디렉토리명과 일치시킨다.
- `steps[].step`: 0부터 시작하는 순번.
- `steps[].name`: kebab-case slug.
- `steps[].status`: 초기값은 모두 `"pending"`.
- `steps[].ac`: 완료 판정 커맨드 목록. 순서대로 실행하며 하나라도 실패하면 미완료. **필수** — 미완료 step에 `ac`가 없으면 execute.py가 시작 전에 설정 오류로 중단한다.
- `steps[].skip_ac` (예외): 검증할 커맨드가 정말 없는 step(문서만 수정 등)은 `"skip_ac": "사유"`를 명시한다. 이때도 세션이 정상 종료(exit 0, 타임아웃 아님)해야 완료로 인정한다.
- `model` (선택): `claude --model` 값. 생략하면 CLI 기본값. `--model` 인자가 우선한다.
- `timeout_sec` (선택): 세션 1회와 AC 커맨드 1개의 제한 시간. 기본 3600.
- `mcp` (선택): 기본 `false` — 세션에 MCP 서버를 로드하지 않는다(`--strict-mcp-config`). 무인 세션에 대화용 커넥터(claude.ai Drive 등)는 불필요한 도구 정의만 늘리기 때문이다. step이 MCP 도구를 써야 하면 `true`.

상태 전이와 자동 기록 필드:

index.json은 **execute.py만 수정한다.** Claude 세션은 `step{N}-result.json`에 결과를 보고하고, execute.py가 AC를 실행해 확인한 뒤 index.json에 반영한다.

| 전이 | 조건 | 기록되는 필드 |
|------|------|-------------|
| → `completed` | 세션이 completed 보고 **그리고** AC 전부 통과 | `summary`, `handoff`, `completed_at`, `attempts` |
| → `error` | 최대 3회 시도 후에도 미완료 | `error_message`, `failed_at`, `attempts` |
| → `blocked` | 세션이 blocked 보고 (API 키, 외부 인증 등) | `blocked_reason`, `blocked_at`, `attempts` |

`summary`(한 줄)와 `handoff`(결정 사항·바뀐 인터페이스·남은 이슈, 10줄 이내)는 다음 step 프롬프트에 누적 전달된다.

`created_at`, `started_at`, `model`, `attempts`, `session_id`도 execute.py가 기록한다. 생성 시 넣지 않는다.

비용: step마다 `cost_usd`, `num_turns`를 시도·재시작에 걸쳐 누적하고, phase 완료 시 task 레벨에 합계를 기록한다. 타임아웃으로 강제 종료된 시도는 CLI가 결과를 내지 못해 비용이 집계되지 않는다(실제 비용보다 적게 나올 수 있다).

#### D-3. `phases/{task-name}/step{N}.md` (각 step마다 1개)

작업 규칙·종료 보고 방법·AC·문서 목록·이전 step 산출물은 execute.py가 프롬프트에 자동으로 붙인다. step 파일에는 **이 step 고유의 내용만** 적는다.

```markdown
# Step {N}: {이름}

## 목표

{이 step이 끝나면 무엇이 동작하는가. 테스트로 증명할 수 있는 결과로 서술.}

## 관련 파일

- {특히 먼저 봐야 할 문서 섹션이나 이전 step 산출물 경로. 없으면 생략}

## 작업

{구체적인 구현 지시. 파일 경로, 클래스/함수 시그니처, 로직 설명을 포함.
코드 스니펫은 인터페이스/시그니처 수준만 제시하고, 구현체는 에이전트에게 맡겨라.
바뀌면 안 되는 불변 조건은 명확히 박아넣어라.}

## 금지사항

- {이 step에서 하지 말아야 할 것. "X를 하지 마라. 이유: Y" 형식}
```

### E. 실행

phase 계획 파일은 커밋하지 않아도 된다(execute.py가 `chore: phase plan`으로 먼저 커밋). 단, `phases/` 밖에 커밋되지 않은 변경이 있으면 시작하지 않는다.

```bash
python scripts/execute.py {task-name}                  # 순차 실행
python scripts/execute.py {task-name} --push           # 완료 후 push
python scripts/execute.py {task-name} --model <id>     # 모델 지정
python scripts/execute.py {task-name} --reset-failed   # error/blocked step을 pending으로 되돌리고 재실행
```

execute.py가 자동으로 처리하는 것:

- `feat-{task-name}` 브랜치 생성/checkout
- 문서 색인 주입 — `docs/*.md`의 경로와 제목만 전달하고, 세션이 관련 문서를 골라 읽는다 (CLAUDE.md는 CLI가 자동 로드)
- 컨텍스트 누적 — 완료된 step의 summary + handoff를 다음 step에 전달
- 완료 검증 — 세션 종료 후 `ac` 커맨드를 직접 실행해 통과해야만 completed
- 자가 교정 — 세션 ID를 실행 전에 발급해 index.json에 저장한다. 실패·타임아웃 시 같은 세션을 `--resume`으로 이어서 AC 실패 출력을 전달 (최대 3회). 재개 자체가 실패하면 새 세션에 전체 프롬프트 + 실패 내용 전달
- 중단 복구 — 하네스가 도중에 죽었다면 그냥 다시 실행한다. pending step에 남은 `session_id`로 세션을 이어받는다
- 타임아웃 — 세션과 AC 커맨드가 `timeout_sec`을 넘으면 자식 프로세스(npm, 테스트 러너 등)까지 모두 종료한다
- 커밋 — 세션은 커밋하지 않는다. 코드(`feat`)와 메타데이터(`chore`)를 분리 커밋하고, error/blocked 시 중간 결과는 `wip`로 커밋
- 기록 — 시도별 CLI 출력을 `step{N}-attempt{K}-output.json`에 저장 (gitignore). step별·phase 전체 비용과 턴 수를 index.json에 기록

에러 복구:

- **error**: `error_message`와 `step{N}-attempt*-output.json`으로 원인을 확인하고, 필요하면 step 파일이나 코드를 고친 뒤 `--reset-failed`로 재실행한다.
- **blocked**: `blocked_reason`의 사유(API 키 등)를 해결한 뒤 `--reset-failed`로 재실행한다.
- `--reset-failed`는 `session_id`도 지운다. 사람이 고친 뒤에는 이전 세션의 맥락을 이어받지 않고 새 세션으로 시작한다.

Windows: 출력·파일은 UTF-8로 처리되고, npm 전역 설치의 `claude.cmd` 대신 실제 `claude.exe`를 자동으로 찾는다. 다른 실행 파일을 쓰려면 `HARNESS_CLAUDE_BIN` 환경변수로 지정한다.
