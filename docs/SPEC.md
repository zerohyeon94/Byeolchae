# ZeroSync v2 봇 설계서 (SPEC)

> 작성: 2026-09-30 (claude.ai 초안) | 갱신: 2026-10-28 (ADR-007 회사 재정의 반영) | 상태: Phase 3~7 구현 (`feat/0-company-system`), Phase 8 미정
> 상위 문서: [운영 규약](운영-규약.md). **이 문서와 운영 규약이 다르면 운영 규약을 따른다.**
> 관련: [ADR-005](아키텍처%20결정/ADR-005-Discord-오케스트레이터-전환.md), [ADR-006](아키텍처%20결정/ADR-006-회사형-구조.md), [ADR-007](아키텍처%20결정/ADR-007-회사-재정의.md), [회사 재정의 문서](회사/README.md), [AGENTS.md](../AGENTS.md)
> 구현 상태: 팀 구조, 제품 채팅 채널, 파이프라인 선언(기능 개발·버그 수정·출시·정기 테스트), 일정·보고는 `feat/0-company-system`에서 구현했다 (12장). 실제 Discord 서버 확인은 아직이다. v6 이전에 시작한 작업은 `legacy` 흐름(ADR-005 단계를 새 역할로)으로 이어 간다

---

## 0. 이 문서의 역할과 표기

운영 규약은 **무엇을 지켜야 하는가**(역할, 권한, 대화 형태, 기록 위치, GitHub 규칙)를 정한다.
이 문서는 **봇을 어떻게 만드는가**(상태 머신, 명령어, 모듈, 데이터 모델, 에이전트 호출 규약, 운영 방식)를 정한다.
운영 규약에 이미 있는 내용은 절 번호로 참조만 하고 반복하지 않는다.

| 표기 | 뜻 |
|---|---|
| [확정] | 운영 규약 또는 Zero의 결정으로 정해짐 |
| [제안] | 이 문서가 제안하는 값. Zero의 확인 전까지 구현 기본값으로만 쓴다 |
| [미결] | 결정이 필요함. 11장에 모아 둠 |
| [검증 필요] | 맥에서 실제로 확인해야 하는 가정. 10장에 모아 둠 |

이 문서를 바탕으로 구현할 때, [제안]과 [미결] 항목을 임의로 확정하지 않는다. 구현에 필요한 순간 Zero에게 묻는다.

---

## 1. 목표와 범위

### 1.1 완료 기준 (프로젝트 목표)
ZeroSync는 Zero 한 사람을 위한 회사다 (ADR-007). 회사는 교육·비서·제품 세 부서로 나뉘고, 이 문서는 지금 **제품 부서**를 다룬다. 제품 부서는 Zero가 부서장을 겸임하고 기획팀(Beta), 디자인팀(Gamma), 개발팀(Alpha), QA팀(Delta)이 가게씨·제이데이·오롯을 맡는다. 교육 부서는 ADR-009로 시작했다(교육팀장 Eta, 매일 Daily와 개념 노트, Discord에서 푸는 문제, 일요일 주간 학습 보고와 주간 문제). 비서 부서는 Phase 8에서 정한다.

완료 기준은 다음과 같다.

1. 가게씨에서 기능 개발 흐름 "채팅 의견 → `/task` → Beta 기획 → Zero 승인 → Gamma 화면 디자인 → Zero 승인 → Alpha 설계 → Epsilon 구현(Alpha 확인) → 봇 테스트 → Delta 검증(최대 3회) → Zero merge → 문서 반영" 한 사이클을 실제로 완주한다
2. 버그 수정·출시 흐름으로 가게씨를 안정화하고 첫 출시 준비물을 만든다
3. 파이프라인을 YAML로 선언해 흐름을 코드 수정 없이 추가할 수 있다
4. 봇 일정으로 정기 업무(아침·주간 보고, 정기 테스트)가 돈다
5. 여러 제품·흐름의 현황과 지표를 대시보드 한 화면에서 본다
6. 봇이 맥에서 상시 운영된다

### 1.2 범위 밖
- 봇이 내용을 판단하거나 방향을 정하는 기능 (봇은 LLM이 아니다. 운영 규약 1.1-3)
- v1의 일정·번아웃 관리 기능 (운영 규약 1.2)
- main 브랜치 관련 작업 (운영 규약 4.1)
- 여러 사용자 지원. 명령 권한자는 Zero 한 명이고, 다른 사람이 요청을 넣는 경로는 없다 (ADR-007)
- 에이전트가 조직(팀, 역할)을 바꾸는 기능. 에이전트는 채용을 제안만 한다 (운영 규약 1.12)
- 봇이 직접 배포하거나 스토어에 제출하는 기능. 봇은 출시 준비물까지 만들고, 제출은 Zero가 직접 한다 (ADR-007)
- 직장의 현장 업무 지원(직장 Slack, Jenkins, 테스트 베드)과 그 입력 경로 (ADR-007)
- 메일 접근. 비서 부서의 메일 정리는 보류다 (ADR-007)

---

## 2. 실행 구조

### 2.1 프로세스 구성 [확정]

```
launchd (LaunchAgent, 로그인 사용자 세션)
  └─ caffeinate -is
       └─ python -m bot          ← 상시 실행되는 유일한 프로세스 (asyncio 이벤트 루프 하나)
            ├─ Discord Gateway WebSocket (맥에서 밖으로 나가는 연결. 포트 개방 불필요)
            ├─ 백그라운드 태스크: PR merge 폴링, 방치 작업 재멘션, 요청 대기열 처리(한 번에 하나, 6.5), 일정 실행(6.6)
            ├─ SQLite (상태, 이벤트, 에이전트 출력 원본)
            └─ 필요할 때만 띄우는 자식 프로세스
                 ├─ claude -p ...      (헤드리스, 끝나면 종료. 에이전트 엔진이 Claude일 때)
                 ├─ codex exec ...     (헤드리스, 끝나면 종료. 에이전트 엔진이 Codex일 때)
                 ├─ <projects.yaml의 test 명령>  (전역 락 아래 하나씩)
                 └─ git, gh
```

- 봇 밖에서 요청을 넣는 프로세스는 두지 않는다. 입력은 Discord와 봇 일정뿐이다. 나중에 경로가 필요해지면 같은 대기열에 넣는 로컬 접수 명령(`python -m bot.intake`)만 더한다 (ADR-007, 6.5)
- 에이전트 자식 프로세스(`claude`, `codex`)는 한 번에 하나만 실행한다. test 명령도 전역 락으로 하나씩이다 (ADR-007)
- 봇은 자식 프로세스를 `asyncio.create_subprocess_exec`로 띄운다. 셸 문자열(`shell=True`)은 쓰지 않는다 (프롬프트에 섞인 문자가 셸 명령으로 해석되는 것을 막기 위해)
- 자식 프로세스가 실행되는 동안에도 이벤트 루프는 `/stop`, `/status`, `/ping`에 응답해야 한다
- 같은 맥에서 봇 인스턴스는 하나만 실행한다. 시작 시 lock 파일(`paths.db`와 같은 폴더의 `bot.lock`, 기본 `~/.zerosync/bot.lock`)을 `flock`으로 잡지 못하면 종료한다. 프로세스가 끝나면 운영체제가 락을 푼다 (2026-10-07 구현)

### 2.2 사람이 쓰는 화면
봇 자체에는 UI가 없다. Zero는 Discord(제품별 채팅에서 의견, 포럼에서 진행·보고·승인, `#대표실`에서 정기 보고, 6.1), 로컬 대시보드(여러 프로젝트·파이프라인 현황과 지표, 9.3-1), GitHub(PR 확인·merge), Obsidian(결정·기능 문서)을 쓴다. 대시보드는 SQLite만 읽는다 (운영 규약 1.11).

---

## 3. 상태 머신

### 3.1 상태 (기능 개발 파이프라인) [제안]

운영 규약 1.5의 단계를 "봇이 일하는 중"과 "Zero를 기다리는 중"으로 나눠 상태로 만든다. Zero 대기 상태는 운영 규약 2.5의 멘션·재멘션 대상이다. 아래는 기본 파이프라인인 기능 개발(`pipelines/feature.yaml`)의 상태다. 다른 파이프라인은 3.1-1과 3.5의 단계 종류를 조합해 같은 방식으로 상태를 만든다.

| 상태 | 실행 주체 | 종류 | 설명 |
|---|---|---|---|
| `PLANNING` | Beta | 실행 중 | worktree·브랜치 생성, 기획서 작성, 기획 요약 JSON 출력(화면 변경 여부 포함) |
| `AWAIT_PLAN_APPROVAL` | - | Zero 대기 | `/approve` 또는 `/revise` |
| `UI_DESIGNING` | Gamma | 실행 중 | 화면 디자인 명세 작성, Claude 디자인 링크, 디자인 요약 JSON 출력. 기획 요약의 화면 변경이 없으면 건너뛴다 |
| `AWAIT_UI_APPROVAL` | - | Zero 대기 | `/approve` 또는 `/revise` |
| `DESIGNING` | Alpha | 실행 중 | 기술 설계 문서 작성, 설계 요약 JSON 출력. 승인 없이 구현으로 이어진다 |
| `IMPLEMENTING` | Epsilon (Alpha 확인) | 실행 중 | 작업 브랜치 로컬 커밋. Alpha가 확인을 통과시켜야 테스트로 간다 (ADR-008) |
| `TESTING` | 봇 | 실행 중 | test 명령 실행 (전역 락) |
| `REVIEWING` | Delta (새 세션, 읽기 전용) | 실행 중 | diff + 기획·디자인·설계 요약 + 테스트 결과 → 판정 |
| `FIXING` | Epsilon (Alpha 확인) | 실행 중 | [필수] 지적 또는 테스트 실패 대응 |
| `AWAIT_MERGE` | - | Zero 대기 | APPROVED, draft 해제됨. GitHub에서 merge |
| `DOC_DRAFTING` | Beta | 실행 중 | DOC_SYNC 수정안 작성 (읽기 전용) |
| `AWAIT_DOC_APPROVAL` | - | Zero 대기 | `/approve`, `/revise`, `/skip` |
| `DONE` | - | 종료 | |
| `STOPPED` | - | 종료 | `/stop` |
| `NEEDS_HUMAN` | - | Zero 대기 | 자동 진행 한도·예산 초과 |

작업(task)은 Zero가 제품 채팅 채널에서 `/task`·`/release`를 쓰거나 일정(6.6)이 요청을 넣는 순간 생성된다. 봇은 그 제품의 포럼에 포스트를 열고 채팅의 의견 요약을 첫머리에 옮긴다. 첫 상태는 파이프라인 선언의 첫 단계다 (기능 개발은 `PLANNING`).

- ADR-005 기준의 `OPINIONS` 상태는 없어진다. 의견은 제품 채팅 채널의 대화(상태 머신 없음, 6.1)에서 나누고, `/task`가 `/decide`를 대신한다 (ADR-007). enum 값은 옛 기록을 읽기 위해 남긴다
- ADR-005 기준의 `DESIGNING`(Beta 설계) → `AWAIT_DESIGN_APPROVAL` 흐름은 `PLANNING`(Beta 기획) → `AWAIT_PLAN_APPROVAL` → (`UI_DESIGNING`(Gamma) → `AWAIT_UI_APPROVAL`) → `DESIGNING`(Alpha 기술 설계)로 바뀐다. Q3("설계 승인은 항상 `/approve`")의 승인 지점은 기획 승인과 화면 디자인 승인으로 옮긴다 (2026-10-26, 2026-10-28 Zero 결정)

### 3.1-1 다른 파이프라인의 상태 [확정] (ADR-007, 구현)

| 파이프라인 | 상태 순서 |
|---|---|
| 버그 수정 (`bugfix.yaml`) | `DIAGNOSING`(Zeta, 읽기 전용, Delta 확인) → `AWAIT_FIX_APPROVAL` → `IMPLEMENTING`(Epsilon, Alpha 확인) → `TESTING` → `REVIEWING`(Delta) ⇄ `FIXING` → `AWAIT_MERGE` → `DONE`. 문서 반영은 하지 않는다. 기능 정의서를 고칠 일이면 `/task feature`로 연다. 작업 브랜치와 Issue 제목은 진단의 변경 필드(change_type, slug 등)로 만든다 |
| 출시 (`release.yaml`) | `TESTING`(전체 테스트·빌드) → `RELEASE_PREPARING`(Beta, 배포 겸임) → `AWAIT_RELEASE_APPROVAL` → `/approve`로 `DONE`. 스토어 제출은 승인 뒤 Zero가 직접 한다 |
| 정기 테스트 (`nightly-test.yaml`) | `TESTING` → 통과면 `DONE`(Discord 알림 없음). 실패면 `DIAGNOSING`(Zeta, Delta 확인) → 포럼에 보고 포스트(`확인필요` 태그) → `DONE`. 수정은 Zero가 `/task bug`로 시작한다. 실행 중에는 `sched:` 가짜 스레드를 쓰고 보고할 때 실제 포스트를 연다. [미구현] 같은 실패를 기존 보고 포스트에 덧붙이기, `확인필요` 태그 |

- 기획이 없는 파이프라인(버그 수정)은 진단 뒤 `gate`를 둔다 (3.5 규칙 R2)

### 3.2 전이

```
(/task feature) ─▶ PLANNING ──▶ AWAIT_PLAN_APPROVAL
                     ▲              │ /approve
                     └──/revise─────┤
                                    ├─(화면 변경 있음)─▶ UI_DESIGNING ──▶ AWAIT_UI_APPROVAL
                                    │                      ▲                │ /approve
                                    │                      └────/revise─────┤
                                    ▼                                       │
                                DESIGNING ◀─────────────────────────────────┘
                                                         │
                                                         ▼
                               IMPLEMENTING ──▶ TESTING ──(통과)──▶ REVIEWING
                                    ▲              │(실패)            │
                                    │              ▼                  │ CHANGES_REQUESTED
                                    └────────── FIXING ◀──────────────┘
                                                                      │ APPROVED
                                                                      ▼
           DONE ◀── AWAIT_DOC_APPROVAL ◀── DOC_DRAFTING ◀──(merge 감지)── AWAIT_MERGE
                     │ /revise ─▶ DOC_DRAFTING
                     │ /skip   ─▶ DONE (결정 노트만 완료 처리, worktree 정리)

어느 상태에서든 /stop ─▶ STOPPED
한도·예산 초과 ─▶ NEEDS_HUMAN
```

| 현재 | 이벤트 | 다음 | 부수 효과 |
|---|---|---|---|
| (없음) | 채팅에서 `/task feature <제목>` | PLANNING | 포럼 포스트 생성(채팅 의견 요약), Issue 생성(운영 규약 1.8), 볼트 결정 노트·논의 목록(운영 규약 3.5) |
| PLANNING | 기획 JSON 검증 통과 | AWAIT_PLAN_APPROVAL | 브랜치 생성·push(운영 규약 4.3), Issue 코멘트, 멘션 |
| AWAIT_PLAN_APPROVAL | `/approve` | UI_DESIGNING (화면 변경 있음) 또는 DESIGNING | 라벨 `type:*`, 볼트 갱신(운영 규약 3.5) |
| UI_DESIGNING | 디자인 JSON 검증 통과 | AWAIT_UI_APPROVAL | 디자인 명세 커밋 push, Claude 디자인 링크 게시, 멘션 |
| AWAIT_UI_APPROVAL | `/approve` | DESIGNING | Issue 코멘트(무음) |
| AWAIT_UI_APPROVAL | `/revise <요청>` | UI_DESIGNING | 같은 명세 파일 수정 커밋 |
| AWAIT_PLAN_APPROVAL | `/revise <요청>` | PLANNING | 같은 기획 파일 수정 커밋 |
| DESIGNING | 설계 JSON 검증 통과 | IMPLEMENTING | 설계 커밋 push, Issue 코멘트(무음) |
| IMPLEMENTING, FIXING | 커밋 완료 | TESTING | 커밋 메시지 형식 검사(운영 규약 4.4) 후 push |
| TESTING | 통과 | REVIEWING | 첫 회에 draft PR 생성, 이후 PR 본문 갱신 |
| TESTING | 실패 | FIXING | `test_retry += 1`. 리뷰 담당 호출 생략 (운영 규약 1.7) |
| REVIEWING | CHANGES_REQUESTED | FIXING | `review_round += 1`, PR 코멘트 |
| REVIEWING | APPROVED | AWAIT_MERGE | draft 해제, 남은 [권장] 개수 표시, 멘션 |
| AWAIT_MERGE | `/fix 권장 반영` | FIXING | [권장] 항목을 대상으로 1회 수정 (운영 규약 2.3) |
| AWAIT_MERGE | PR merge 감지 | DOC_DRAFTING | Issue 코멘트 후 종료 (운영 규약 4.6) |
| AWAIT_DOC_APPROVAL | `/approve` | DONE | 볼트 쓰기·커밋, worktree 정리 |
| AWAIT_DOC_APPROVAL | `/skip` | DONE | 결정 노트만 완료 처리, worktree 정리 (운영 규약 3.9-6, 4.6) |
| 모든 상태 | `/stop` | STOPPED | 실행 중 CLI 종료, Issue·브랜치·볼트 처리 (운영 규약 1.8, 3.3, 3.5) |
| 실행 중 상태 | 한도·예산 초과 | NEEDS_HUMAN | `needs-human` 라벨, 멘션 |
| NEEDS_HUMAN | `/resume` | 직전 상태 | 카운터는 유지하고 초과한 한도만 1회 늘린다 [확정] (Q4) |

### 3.3 한도 [확정 + 제안]

| 항목 | 값 | 근거 |
|---|---|---|
| 리뷰 라운드 | 3회 초과 시 NEEDS_HUMAN | 운영 규약 1.7, 2.5 |
| 테스트 실패 재시도 | 2회 초과 시 NEEDS_HUMAN | 운영 규약 1.7, Q2 [확정] |
| 형식 오류 | 재요청 1회 후에도 실패하면 원문 게시 + NEEDS_HUMAN. 채팅의 답(의견)은 원문을 "형식 오류" 표시와 함께 게시하고 멘션한 뒤 끝낸다 [확정] (2026-10-10 Zero 결정, ADR-007로 채팅에 적용) | 운영 규약 2.1, 2.5 |
| CLI 타임아웃 | 3,600초 | `projects.yaml`의 `cli_timeout_sec` |
| 채팅 호출 | 따로 세지 않고 팀장별 일일 호출에 포함한다. ADR-005 기준 구현의 "OPINIONS 질문 5회 초과 알림"은 채팅으로 옮기면서 없앤다 [확정] (ADR-007) | 운영 규약 1.6 |
| 재멘션 | Zero 대기 24시간 초과 시 1회 | 운영 규약 2.5 |
| 파이프라인 예산 | 1회당 에이전트 호출 수·실행 시간. 넘으면 NEEDS_HUMAN + #zerosync-ops 알림 | 파이프라인 YAML의 `budget` (Q18) |
| 에이전트 일일 호출 | 에이전트별 하루 호출 수. 넘으면 그 에이전트가 맡은 단계는 NEEDS_HUMAN, 채팅 답은 "오늘 한도 초과" 안내만 | `org.yaml`의 `daily_calls` (Q18) |
| 예산 시작값 | 팀장마다 하루 30회. 기능 개발 25회·240분, 버그 수정 12회·120분, 출시 8회·60분, 정기 테스트 진단 3회·30분. 2주 운영 뒤 주간 보고를 보고 조정 [확정] (ADR-007) | 회사 재정의 05-운영 4장 |
| 동시 실행 | 에이전트 1개, test 명령 1개 [확정] (ADR-007) | 6.5 |
| 구독 사용량 한도 | CLI가 한도 오류를 내면 대기열 전체를 멈추고 #zerosync-ops에 알린다. 한도가 풀리는 시각에 다시 시작한다 [제안] [검증 필요] (V15) | ADR-007 |

### 3.4 구현 원칙
- 전이 규칙은 `bot/workflow/state.py`에 **순수 함수**로 둔다: `(현재 상태, 이벤트) → (다음 상태, 실행할 효과 목록)`. I/O 없이 단위 테스트로 전부 고정한다. 파이프라인 선언(3.5)을 쓰게 되면 `(파이프라인, 현재 상태, 이벤트)`를 입력으로 받는다
- 효과(에이전트 호출, GitHub, 볼트, 게시)는 `workflow/engine.py`가 인터페이스를 통해 실행한다
- 상태 변경은 SQLite 트랜잭션 안에서 이벤트 기록과 함께 저장한다. 봇이 재시작되면 "실행 중" 상태의 작업은 NEEDS_HUMAN으로 돌리고 Zero에게 알린다 (중간 결과를 신뢰하지 않음) [확정] (Q8)
  - 끝나지 않은 에이전트 실행은 `error`로 닫는다. ADR-005 기준 구현의 OPINIONS 작업은 NEEDS_HUMAN으로 보내지 않고 "재시작으로 중단" 안내(멘션) 후 그대로 둔다 [확정] (2026-10-10 Zero 결정). 구현: `Engine.recover_interrupted`. 채팅으로 옮긴 뒤에는 끝나지 않은 채팅 답에 같은 안내를 하고 끝낸다. 다시 부르면 된다


### 3.5 파이프라인 선언 [확정 원칙, 형식은 제안] (Q13)

파이프라인은 비공개 회사 저장소의 `pipelines/<이름>.yaml`에 선언한다 (Q17). YAML은 단계 순서, 담당, 승인 게이트, 상한만 적는다. 단계의 동작은 엔진에 구현하고 단위 테스트로 고정한다. 봇은 YAML을 해석해 판단하지 않고, 정해진 단계 종류를 순서대로 실행할 뿐이다.

```yaml
# pipelines/feature.yaml (기능 개발)
name: 기능 개발
command: /task feature          # 이 파이프라인을 시작하는 채팅 명령 (4장)
triggers:                       # 시작할 수 있는 출처 (6.5, 6.6)
  - discord_task
priority: feature               # 대기열 순서: bugfix > release > feature (6.5)
stages:
  - {id: plan,       kind: plan,      agent: beta}
  - {id: approve,    kind: gate}                                   # Zero /approve
  - {id: ui,         kind: ui_design, agent: gamma, when: has_ui_change}
  - {id: approve_ui, kind: gate,                  when: has_ui_change}
  - {id: design,     kind: design,    agent: alpha}                # 기술 설계
  - {id: implement,  kind: implement, agent: epsilon, check: alpha} # 팀원 구현, 팀장 확인 (ADR-008)
  - {id: test,       kind: test,      retries: 2}                  # 봇이 실행
  - {id: review,     kind: review,    agent: delta, rounds: 3}
  - {id: merge,      kind: merge_gate}                             # Zero가 GitHub에서 merge
  - {id: docs,       kind: doc_sync,  agent: beta}
budget: {max_calls: 35, max_minutes: 300}                          # 파이프라인 1회당 (Q18, ADR-007, ADR-008)
```

```yaml
# pipelines/bugfix.yaml (버그 수정)
name: 버그 수정
command: /task bug
triggers: [discord_task]
priority: bugfix
stages:
  - {id: diagnose,  kind: diagnose,  agent: zeta, check: delta}
  - {id: approve,   kind: gate}                       # 수정 방향 승인
  - {id: implement, kind: implement, agent: epsilon, check: alpha}
  - {id: test,      kind: test,      retries: 2}
  - {id: review,    kind: review,    agent: delta, rounds: 3}
  - {id: merge,     kind: merge_gate}
budget: {max_calls: 18, max_minutes: 150}
```

```yaml
# pipelines/release.yaml (출시)
name: 출시
command: /release
triggers: [discord_task]
priority: release
stages:
  - {id: check,   kind: test,         scope: full}        # 전체 테스트·빌드
  - {id: prepare, kind: release_prep, agent: beta}       # 배포 겸임
  - {id: approve, kind: gate}                            # 출시 승인. 스토어 제출은 Zero가 직접
budget: {max_calls: 8, max_minutes: 60}
```

```yaml
# pipelines/nightly-test.yaml (정기 테스트)
name: 정기 테스트
triggers: [schedule]
schedule: "0 3 * * *"
priority: bugfix
stages:
  - {id: test,     kind: test}
  - {id: diagnose, kind: diagnose, agent: zeta, check: delta, when: test_failed}
  - {id: report,   kind: report}                         # 실패 때만 포럼에 보고 포스트
budget: {max_calls: 6, max_minutes: 45}
```

- `check`는 팀장 확인이다 (ADR-008). 구현·진단 단계에만 둔다. 팀원의 결과를 팀장이 리뷰 판정 형식으로 확인하고, [필수] 지적이 있으면 팀원이 고친 뒤 다시 확인한다. `check_rounds`(기본 `limits.max_check_rounds` 2회) 안에 통과하지 못하면 단계 실패(`NEEDS_HUMAN`)다. 확인은 단계 안에서 일어나므로 상태를 늘리지 않는다. 구현 뒤의 수정(`FIXING`)도 같은 팀장이 확인한다. 승인 게이트 안내는 확인을 통과한 뒤에 붙는다
- `when`은 앞 단계 출력 JSON의 정해진 불리언 필드(`has_ui_change`)나 봇이 아는 결과(`test_failed`)만 받는다. 봇은 조건을 해석하지 않고 값을 읽을 뿐이다 [제안]
- 채팅의 대화는 파이프라인이 아니다. 아무도 부르지 않았을 때 답할 팀장은 회사 설정의 제품 채널 설정(`chat_default: [beta, alpha]`)에 둔다 (6.1, 9.1-1). 구현은 제품별이 아니라 `org.yaml`의 `roles.opinions` 하나를 모든 제품에 쓴다

단계 종류 [제안]

| kind | 실행 주체 | 동작 | 대응 상태 |
|---|---|---|---|
| `plan` | 에이전트 | 기획서 + 기획 요약 JSON | PLANNING |
| `ui_design` | 에이전트 | 화면 디자인 명세 + Claude 디자인 링크 + 디자인 요약 JSON (ADR-007) | UI_DESIGNING |
| `design` | 에이전트 | 기술 설계 문서 + 설계 요약 JSON | DESIGNING |
| `implement` | 에이전트 | 구현 커밋 | IMPLEMENTING, FIXING |
| `test` | 봇 | test 명령 실행 | TESTING |
| `review` | 에이전트 (새 세션, 읽기 전용) | 판정 + 지적, 라운드 반복 | REVIEWING |
| `gate` | Zero | `/approve`, `/revise` 대기 | AWAIT_<앞 단계>_APPROVAL |
| `merge_gate` | Zero | PR merge 감지 대기 | AWAIT_MERGE |
| `doc_sync` | 에이전트 → Zero | 문서 반영안, `/approve`·`/revise`·`/skip` | DOC_DRAFTING, AWAIT_DOC_APPROVAL |
| `diagnose` | 에이전트 (읽기 전용) | 재현 방법, 원인 분류, 수정 방향을 보고만 한다. 외부에 쓰지 않는다 | DIAGNOSING |
| `release_prep` | 에이전트 | 출시 점검 목록, 버전 번호, 릴리스 노트·스토어 설명 초안, 인증서·프로비저닝 만료 확인 (ADR-007) | RELEASE_PREPARING |
| `report` | 봇 | 앞 단계 결과를 포럼 보고 포스트로 게시. 같은 실패가 이어지면 기존 포스트에 덧붙인다 (ADR-007) | - |
| `external` | 봇 | 허용된 외부 명령 실행. gate 뒤에서만. 지금 쓰는 파이프라인 없음 (ADR-007에서 Jenkins 제외) | RUNNING_EXTERNAL |

불러올 때 규칙 검사 (하나라도 어기면 봇이 그 파이프라인을 불러오지 않고 #zerosync-ops에 알린다)

| # | 규칙 |
|---|---|
| R1 | `review`의 에이전트는 같은 작업의 `plan`·`design`·`implement` 에이전트와 다르다 (운영 규약 1.1-2) |
| R2 | `implement` 앞에 `gate`가 최소 1개 있다 |
| R3 | 외부에 쓰는 단계(`implement` 이후 push·PR, `external`) 뒤의 merge·배포는 `merge_gate` 또는 `gate`를 거친다 |
| R4 | 에이전트는 `org.yaml`에 있는 이름이다. 엔진이 Codex인 에이전트는 읽기 전용 일(채팅 답, `review`, `diagnose`)에만 둔다 |
| R5 | `triggers`의 출처는 6.5·6.6에서 정의한 값(`discord_task`, `schedule`)이다. 입력 계층은 여기 적힌 파이프라인만 받는다 |
| R6 | `check`(팀장 확인)는 `implement`·`diagnose`에만 두고, 그 단계 담당이 속한 팀의 팀장이 맡는다. 담당 자신은 맡지 않는다 (ADR-008) |

- R1은 리뷰를 Delta가 맡으면서 지켜진다. 2026-10-26판 예시는 기획과 리뷰를 모두 Beta에게 두어 R1과 충돌했다 (ADR-007에서 정리)
- R1에서 작성 단계의 `check` 담당도 작성 담당으로 본다 (ADR-008). 구현을 확인하는 Alpha와 리뷰하는 Delta가 다르므로 지켜진다
- 상태 머신은 파이프라인 선언을 입력으로 받는 순수 함수다 (`bot/workflow/state.py`, 3.4). 선언은 회사 저장소 `pipelines/*.yaml`에서 읽고, 폴더가 없으면 코드의 기본 선언(`bot/pipeline.py` `default_pipelines`)을 쓴다
- 추가 규칙(구현): 각 단계 종류(`gate` 제외)는 흐름마다 한 번만 쓴다. `gate`는 기획·화면 디자인·설계·진단·출시 준비 바로 뒤에만 둔다

---

## 4. Discord 명령어

권한 검사: 모든 명령과 메시지는 Zero의 Discord 사용자 ID일 때만 처리한다. 그 밖의 입력은 무시하고 로그만 남긴다 (운영 규약 1.3).

| 명령 | 가능한 상태 | 동작 | 상태 |
|---|---|---|---|
| (제품 채팅 채널에 메시지) | - | 대화. `@beta`처럼 `@이름`으로 시작하면 그 팀장만, 아니면 제품 채널의 `chat_default`(Beta, Alpha)가 답한다. 상태 머신 없음 | [확정] (ADR-007) |
| `/debate` | 제품 채팅 채널 | 직전에 답한 두 팀장이 상대 답에 반박 1회씩 | [확정] (구현) |
| `/task <feature\|bug> <제목>` | 제품 채팅 채널 | 그 제품 포럼에 포스트를 열고 파이프라인 시작. 채팅의 의견 요약을 첫머리에 옮긴다. ADR-005의 `/decide`를 대신한다 | [확정] (ADR-007) |
| `/release <버전>` | 제품 채팅 채널 | 출시 파이프라인 시작 | [확정] (ADR-007) |
| `/idea <내용>` | 제품 채팅 채널 | 그 제품의 아이디어 목록에 기록. 포스트를 열지 않는다 | [확정] (ADR-007) |
| `/approve` | `gate` 대기 상태(AWAIT_PLAN_APPROVAL 등), AWAIT_DOC_APPROVAL | 현재 단계 승인 | [확정] |
| `/revise <요청>` | `gate` 대기 상태, AWAIT_DOC_APPROVAL | 앞 단계 산출물 다시 작성 | [확정] (Q7, 기획 단계로 이동) |
| `/skip` | AWAIT_DOC_APPROVAL | 기능 정의서 반영 생략 | [확정] |
| `/fix 권장 반영` | AWAIT_MERGE | [권장] 항목 1회 추가 수정 | [확정] |
| `/stop` | 종료 상태 외 전부 | 긴급 정지 | [확정] |
| `/resume` | NEEDS_HUMAN | 직전 상태에서 재개 | [확정] (Q4) |
| `/status` | 전부 | 상태, 라운드 수, 재시도 수, 대기 시간 | [확정] |
| `/ping` | 전부 (#zerosync-ops에서도) | 봇 생존, 실행 중 CLI, 대기열 길이 | [확정] |
| `/daily` | 전부 | 오늘 개념 노트와 문제를 지금 낸다. 이미 냈으면 다시 내지 않는다 (ADR-009) | [확정] (구현) |
| `/weekly` | 전부 | 이번 주 학습 보고와 주간 문제를 지금 만든다. 문제는 주마다 한 번 (ADR-009) | [확정] (구현) |
| (교육 "다 읽었다" 버튼) | `#교육` | 오늘 개념을 완료로 표시한다. 누르지 않으면 다음 날 이어 한다. Zero만 누를 수 있다 (ADR-009) | [확정] (구현) |
| (교육 문제 A~D 버튼) | `#교육` | Zero만 답한다. 정답과 해설은 Zero에게만 보이고, 처음 고른 답만 기록한다 (ADR-009) | [확정] (구현) |
| `/hire <수락\|거절> <제안 번호> [메모]` | 전부 (#대표실, #zerosync-ops에서도) | 팀장의 채용 제안에 답한다. 수락은 기록만 하고 `org.yaml` 변경은 Zero가 한다. 거절 기록은 다음 프롬프트에 들어간다 (운영 규약 1.12, Q19) | [제안] |

- 명령은 discord.py의 앱 명령(슬래시 명령)으로 등록한다. 시작 명령(`/task`, `/release`, `/idea`, `/debate`)은 제품 채팅 채널에서, 진행 명령(`/approve`, `/revise` 등)은 해당 포스트(스레드) 안에서만 동작한다. 다른 곳에서 쓰면 짧은 오류만 보낸다
  - 서버(guild) 명령으로 등록해 바로 반영되게 한다. Zero가 아니거나 작업이 없는 게시글이면 Zero에게만 보이는(ephemeral) 오류를 보낸다
  - `/task`, `/release`, `/debate`, `/stop`은 명령 원문을 공개로 남기고(무음), 엔진 작업은 백그라운드에서 실행한다. Discord가 3초 안에 응답을 요구하기 때문이다. `/status`, `/ping`은 Zero에게만 보인다 [확정] (2026-10-10 Zero 결정)
- 상태와 맞지 않는 명령은 상태를 바꾸지 않고 현재 상태와 가능한 명령을 알려 준다

---

## 5. 에이전트 호출 규약

### 5.1 AgentRunner 인터페이스 [확정 원칙, 시그니처는 제안]

```python
class AgentRunner(Protocol):
    name: Literal["claude", "codex"]     # 엔진 이름. 에이전트 이름(Beta, Alpha)과 다르다

    async def run(self, request: AgentRequest) -> AgentResult: ...
    async def cancel(self, run_id: str) -> None: ...

@dataclass(frozen=True)
class AgentRequest:
    run_id: str
    task_id: int
    stage: Stage                 # OPINION(채팅 답), DEBATE, PLAN, UI_DESIGN, DESIGN, IMPLEMENT, FIX, REVIEW, DIAGNOSE, RELEASE_PREP, DOC_SYNC
    prompt: str
    cwd: Path                    # 앱 저장소 또는 작업 worktree
    mode: Literal["read_only", "write_worktree"]
    timeout_sec: int
    resume_session: str | None = None   # Claude 세션 이어가기 (최적화, 필수 아님)
    output_schema: Mapping[str, Any] | None = None  # 응답 JSON 스키마. 주면 CLI 구조화 출력으로 강제

@dataclass(frozen=True)
class AgentResult:
    status: RunStatus            # ok | error | timeout | cancelled
    exit_code: int | None        # 프로세스를 띄우지 못하면 None
    stdout: str                  # parse_agent_output()에 넘기는 원문
    stderr: str
    session_id: str | None
    duration_sec: float
    log_path: Path               # ~/.zerosync/runs/<run_id>.log
```

- 실제 구현: `ClaudeCliRunner`, `CodexCliRunner`. 테스트: `FakeAgentRunner`(정해 둔 응답을 돌려줌)
- 에이전트와 러너: 엔진은 `org.yaml`의 에이전트별 `engine`으로 정하고, 봇이 그 엔진의 러너를 고른다. 러너는 에이전트를 모른다. 인격·직책은 프롬프트(5.3)로만 들어간다 (ADR-006 4절) [확정 원칙]
- `status`는 종료 코드만으로 구분할 수 없는 타임아웃·취소를 나타낸다. 8장 `agent_runs.status`의 `running`·`invalid_output`은 저장 계층과 출력 검증이 정한다 (2026-10-07 Zero 승인으로 추가)
- 구현됨: `bot/agents/base.py`(인터페이스), `bot/agents/process.py`(자식 프로세스 실행, 타임아웃·취소, 실행 로그), `bot/agents/fake.py`, `bot/agents/claude.py`, `bot/agents/codex.py`
- `output_schema`는 2026-10-10 추가 (Zero 결정: 스키마 강제 우선). 강제 출력도 `parse_agent_output`으로 다시 검증한다
- 러너는 출력을 해석하지 않는다. JSON 추출과 검증은 `bot/schemas/extract.py`의 `parse_agent_output`가 한다 (구현됨)

### 5.2 단계별 명령 [확정, 2026-10-10 시험 호출로 확인]

| 단계 | 에이전트 | 명령 골격 | 권한 |
|---|---|---|---|
| OPINION(채팅 답), DEBATE, RELEASE_PREP | Claude | `claude -p --permission-mode plan` | 읽기 전용 (출시 준비물은 JSON으로만) |
| OPINION, DEBATE, REVIEW | Codex | `codex exec --cd <cwd> --sandbox read-only --ephemeral --ignore-user-config -o <runs>/<run_id>.last.txt -` | 읽기 전용 |
| PLAN | Claude | DESIGN과 같고 허용 경로만 `docs/기획/**` [제안] | worktree의 `docs/기획/`만 |
| UI_DESIGN | Claude | DESIGN과 같고 허용 경로만 `docs/디자인/**`. Claude 디자인 생성 도구 허용은 V14 확인 뒤 정한다 [제안] | worktree의 `docs/디자인/`만 |
| DESIGN | Claude | `claude -p --permission-mode dontAsk --allowedTools Read Glob Grep "Edit(docs/설계/**)" "Write(docs/설계/**)" <로컬 git>` | worktree의 `docs/설계/`만 |
| REVIEW, DIAGNOSE | Claude | `claude -p --permission-mode plan` (세션 이어가기 없음) [제안] | 읽기 전용 |
| IMPLEMENT, FIX | Claude | `claude -p --permission-mode acceptEdits --allowedTools Read Glob Grep Edit Write <로컬 git>` (+ `--resume <세션ID>`) | 작업 worktree |
| DOC_SYNC | Claude | `claude -p --permission-mode plan` | 읽기 전용 (수정안은 JSON으로만) |

- Claude 공통: `--output-format json --permission-prompts none --strict-mcp-config --disallowedTools "Bash(git push:*)" "Bash(git pull:*)" "Bash(git fetch:*)" "Bash(git remote:*)" "Read(**/.env)" "Edit(**/.env)" "Write(**/.env)"`. `<로컬 git>`은 `Bash(git status|diff|log|show|add|commit:*)`
  - `--permission-prompts none`: 허용 목록에 없어 승인이 필요한 동작은 자동 거부되고 결과 JSON의 `permission_denials`에 남는다. `ls` 같은 읽기 전용 명령은 목록에 없어도 실행된다
  - `--strict-mcp-config`: 사용자 MCP 서버(Notion, Slack 등)를 불러오지 않는다. `--bare`는 OAuth 로그인을 읽지 않아 구독 로그인과 함께 쓸 수 없다
- 프롬프트는 두 CLI 모두 stdin으로 넘긴다. 실행 로그의 argv에 프롬프트가 남지 않는다
- 단계별 모델은 지금 `projects.yaml`의 `cli.claude_models`·`cli.codex_models`로 정한다 (9.1, 2026-10-10 Zero 결정). Phase 3부터는 `org.yaml`의 에이전트별 `models`로 옮긴다 (Q12)
- 리뷰는 구현 단계의 세션을 이어받지 않는다. 엔진이 같아도 새 세션으로 읽기 전용 실행한다 (운영 규약 1.1-2)
- Codex는 쓰기 단계를 맡지 않으므로 러너가 쓰기 모드 요청을 거부한다

- 구조화 출력: `AgentRequest.output_schema`가 있으면 Claude는 `--json-schema`, Codex는 `--output-schema <파일>`로 강제한다. 없으면 프롬프트로 ```json 블록을 요청한다 (운영 규약 2.1). pydantic `model_json_schema()` 결과를 두 CLI 모두 그대로 받는다 (확인됨)
  - Claude 결과 JSON: `structured_output`(스키마 강제 시)을 우선하고, 없으면 `result` 문자열을 응답 본문으로 쓴다. `is_error: true`면 종료 코드가 0이어도 실패로 본다. 세션 ID는 `session_id`
  - Codex: `-o` 파일의 마지막 메시지를 응답 본문으로 쓴다. 진행 기록은 stderr로 나와 실행 로그에만 남는다. 세션 ID는 쓰지 않는다(`--ephemeral`)
- Claude의 쓰기 단계 권한은 허용 도구 목록으로 좁힌다. `git push`, 원격 관련 git 명령, `.env` 접근은 금지 목록에 넣는다 (운영 규약 1.3). 시험 호출에서 push, `.env` 읽기, 작업 폴더 밖 쓰기, 설계 단계의 `docs/설계/` 밖 쓰기가 모두 거부됨을 확인했다
- 맥락은 매번 SQLite 기록으로 재구성해 프롬프트에 넣는다. 세션 이어가기는 실패해도 동작에 영향이 없어야 한다 (운영 규약 1.6)
- PATH는 launchd 환경에서도 `claude`, `codex`를 찾도록 `projects.yaml`의 `cli.claude`·`cli.codex`에 절대 경로로 지정할 수 있다 (Homebrew 설치 시 `/opt/homebrew/bin/claude`, `/opt/homebrew/bin/codex`)

### 5.3 프롬프트 [제안]
- 위치: `bot/prompts/<stage>.md` (템플릿 문자열, `string.Template`의 `$이름`). 코드에 긴 문자열을 넣지 않는다
- 구현됨: `opinion.md`, `debate.md` (2026-10-10). 스냅샷은 `tests/snapshots/`
- 공통 구성: 인격 파일(`agents/<이름>.md`: 이름, 직책, MBTI, 역량, 관점. Q12)과 관점(운영 규약 1.2, 1.4) → 거절된 채용 제안 목록(운영 규약 1.12, 해당할 때) → 작업 맥락(제목, 원문, 결정 원문, 이전 출력) → 단계별 지시 → 출력 스키마(`Model.model_json_schema()`로 생성) → 금지 사항
- 채팅 답에는 그 채널의 최근 대화를 넣는다 [제안: 최근 20개]. 아무도 부르지 않아 두 팀장이 함께 답할 때는 서로의 답을 넣지 않는다 (운영 규약 1.6의 독립 의견 원칙)
- 프롬프트 렌더링은 순수 함수로 두고 스냅샷 테스트로 고정한다

### 5.4 출력 스키마

| 단계 | 스키마 | 상태 |
|---|---|---|
| OPINION (채팅 답) | `Opinion` (stance, conclusion, reasons, risks, proposal) | 구현됨 |
| DEBATE | `Opinion` 재사용. stance는 상대 의견에 대한 입장 | [제안] |
| REVIEW | `Review` (verdict, summary, findings) | 구현됨 |
| PLAN | `PlanSummary` (아래) | [제안] |
| UI_DESIGN | `UiDesignSummary` (아래) | [제안] (ADR-007) |
| DESIGN | `DesignSummary` (change_type, target_feature, slug + 아래 추가 필드) | 구현됨 (2026-10-11, ADR-005 기준) |
| DIAGNOSE | `DiagnosisReport` (재현 방법, 원인 분류, 근거, 수정 방향, 확신도) | [제안] |
| RELEASE_PREP | `ReleasePrep` (아래) | [제안] (ADR-007) |
| 단계 종료 보고 전체 | 선택 필드 `hiring_proposal` (아래) | [제안] (Q19) |
| IMPLEMENT, FIX | `ImplementationReport` | 구현됨 (2026-10-11) |
| DOC_SYNC | `DocSyncProposal` | [제안] (형태는 운영 규약 3.9-3) |

`DesignSummary` 추가 필드 [확정] (Q1, 대상 기능은 1개)

| 키 | 타입·제약 | 쓰임 |
|---|---|---|
| `title` | 한 줄, 80자 | PR 제목 `<type>(<영역>): <요약>`의 요약 |
| `scope` | 영문 소문자 영역 이름 또는 null | 커밋·PR 제목의 영역 (4.4) |
| `summary` | 500자 | Discord·Issue 코멘트·결정 노트의 설계 요약 |
| `changes` | 1~8개, 항목 200자 | 바꿀 파일·구성 요소 |
| `test_plan` | 1~6개, 항목 200자 | 추가·수정할 테스트 |
| `risks` | 0~3개, 항목 200자 | 설계상 위험 |
| `design_doc` | `docs/설계/<Issue번호>-<slug>.md` 형식 | 봇이 파일 존재를 확인 |

`target_feature` 검증 규칙 (구현됨, PR #10): 봇이 넘긴 `기능/` 파일 이름 목록을 기준으로, `feature_add`는 목록에 없는 이름, 나머지 변경 유형은 목록에 있는 이름이어야 한다 (운영 규약 3.8, 3.9-4).

`PlanSummary` [제안]: 기능 개발 파이프라인에서는 변경 유형·대상 기능·slug를 기획 단계가 정한다. 그래서 `change_type`, `target_feature`, `slug`, `title`, `scope`가 `DesignSummary`에서 `PlanSummary`로 옮겨 오고, 기획 승인(`/approve`)으로 함께 승인된다 (운영 규약 3.8은 이후 정리). 기획이 없는 버그 수정은 `DiagnosisReport`가 이 필드를 갖고, 수정 방향 승인으로 함께 승인된다 [제안].

| 키 | 타입·제약 | 쓰임 |
|---|---|---|
| `change_type`, `target_feature`, `slug`, `title`, `scope` | `DesignSummary`와 같은 규칙 | 브랜치·커밋·PR 제목, 결정 노트 |
| `problem` | 300자 | 누구의 어떤 문제인가 |
| `summary` | 500자 | Discord·Issue 코멘트·결정 노트의 기획 요약 |
| `in_scope`, `out_of_scope` | 각 1~6개, 항목 200자 | 이번에 할 것과 하지 않을 것 |
| `acceptance` | 1~6개, 항목 200자 | 완료 기준. 리뷰 담당이 확인한다 |
| `edge_cases` | 0~5개, 항목 200자 | 예외 상황 |
| `plan_doc` | `docs/기획/<Issue번호>-<slug>.md` 형식 | 봇이 파일 존재를 확인 |
| `has_ui_change` | 불리언 | 화면이 바뀌는지. 거짓이면 화면 디자인 단계를 건너뛴다 (3.5 `when`, ADR-007) |

`UiDesignSummary` [제안] (ADR-007): `summary`(500자), `screens`(1~8개: 화면 이름, 상태 목록(빈 화면·로딩·오류 등)), `components`(0~10개), `design_system_changes`(0~3개, 사유 포함), `out_of_plan`(기획 밖 제안, 0~3개), `design_url`(Claude 디자인 링크 또는 null), `design_doc`(`docs/디자인/<Issue번호>-<slug>.md` 형식, 봇이 파일 존재를 확인). 명세(`design_doc`)가 원본이고 링크는 명세 안에도 적는다.

`ReleasePrep` [제안] (ADR-007): `version`, `checklist`(1~15개: 항목, 상태 `ok`/`todo`/`blocked`), `release_notes`(1,000자), `store_description`(4,000자 또는 null), `certificate_expiry`(인증서·프로비저닝 프로필별 만료일), `blockers`(0~5개). 릴리스 노트와 스토어 설명은 외부로 나가는 글이므로 출시 승인으로 함께 승인된다.

`HiringProposal` [제안] (Q19): `kind`(`team`/`member`), `team`, `role`, `reason`(300자), `cases`(관련 작업 1~5개), `duties`(200자), `mbti`, `competencies`(1~5개), `if_not_hired`(200자). 봇은 이 필드를 검증해 `hiring_proposals`(8장)에 저장하고, 대기 시간·실패 수·한도 초과 수를 DB에서 계산해 붙여 `#대표실`에 게시하고 주간 보고에 모은다 (ADR-007).

`ImplementationReport` [확정] (2026-10-11 구현): `changes_summary`(500자), `design_intent`(300자), `check_points`(0~5개), FIX 단계에서는 `responses`(지적 번호별 `fixed` / `declined` + 사유). PR 본문의 구현 담당 항목(운영 규약 1.9)이 여기서 나온다.

`DocSyncProposal` [제안]: `summary`(300자), `files`(1~5개: `path`, `action`(`create`/`update`), `content` 전체).

---

## 6. 외부 연동

### 6.1 Discord (`bot/discord_io/`)
- 워크플로는 `ChatIO` 인터페이스만 안다: `post(thread, author, text, *, mention, attachments) -> 메시지 ID`, `notify_ops(text, *, mention)`. 인터페이스는 `bot/workflow/chat.py`에 두고 `bot/discord_io/`가 구현한다 (의존 방향, 7장). `set_tags`는 태그 이름이 정해지면 추가한다 [제안]
- `mention=False`면 알림 없는 메시지로 보낸다. 멘션 문자열(`<@Zero ID>`)과 1,900자 분할은 구현 쪽 책임이다
- 채널 구성 [확정] (ADR-007): 제품마다 포럼 `#<제품>`(정식 진행, 팀 보고)과 채팅 `#<제품>-chat`(의견 나누기), Zero 보고용 `#대표실`, 봇 상태용 `#zerosync-ops`. 교육 부서는 `#교육`(아침 학습 알림과 완료 버튼, 버튼으로 푸는 오늘 문제·복습·주간 문제, 주간 학습 보고, 없으면 `#대표실`, ADR-009). 비서 채널은 Phase 8에서 더한다. 채널 이름은 회사 설정에서 바꿀 수 있다
- 채팅 답·반박은 답할 팀장들의 응답이 모두 끝난 뒤 `chat_default` 순서(Beta, Alpha)로 게시하고, 마지막 게시에서 멘션한다. 실행 실패한 쪽은 그 자리에 [ZeroSync] 안내를 게시하고 #zerosync-ops에 알린다 (2026-10-10 구현, ADR-005 기준은 포럼 의견)
- 게시 이름: `[이름 · 직책]`(예: `[Beta · 기획팀장]`, `[Gamma · 디자인팀장]`, `[Alpha · 개발팀장]`, `[Delta · QA팀장]`, `[Epsilon · 개발팀원]`, `[Zeta · QA팀원]`)과 `[ZeroSync]` (운영 규약 1.2, Q16). ADR-005 기준 구현은 `[Beta · Claude]`, `[Alpha · Codex]`다. 에이전트 메시지는 웹훅의 username 지정으로 게시한다
- 웹훅은 봇이 시작할 때 각 포럼·채팅 채널과 `#대표실`에서 찾거나 만든다(Manage Webhooks 권한). 웹훅 URL을 `.env`에 두지 않는다 [확정] (Q6)
  - 이름이 `ZeroSync`이고 이 봇이 만든 웹훅을 다시 쓴다. `[ZeroSync]` 안내도 같은 웹훅으로 보내 모든 게시를 한 경로로 처리한다 (discord.py 2.7.1의 `Webhook.send`가 `thread`, `silent`를 지원)
- 입력 판정 (`bot/discord_io/rules.py`): Zero가 제품 채팅 채널에 쓴 메시지와 제품 포럼의 스레드에 쓴 메시지만 처리한다 (ADR-007로 채팅 추가. ADR-005 기준 구현은 포럼만). 봇·웹훅 메시지는 무시한다(자기 게시 반복 방지). 포럼 게시글의 첫 메시지는 ID가 스레드 ID와 같으므로 이것으로 게시글과 추가 질문을 구분한다. 제목은 스레드 이름, 본문은 첫 메시지 내용
- 백그라운드 작업에서 예외가 나면 #zerosync-ops에 멘션해 알린다
- 멘션은 이벤트 종류의 `ACTION_REQUIRED` 집합으로만 결정한다. 나머지는 silent 메시지 (운영 규약 2.5) [검증 필요: 웹훅 silent]
- 길이: 게시용 요약 1,500자 이내, 넘는 전문은 파일 첨부. 1,900자 분할은 안전장치 (운영 규약 2.6)
- render 계층(`bot/render/`)이 스키마 객체를 텍스트로 바꾼다. Discord에 의존하지 않으므로 Issue·PR·볼트 문서도 같은 계층에서 만든다
- 포럼 태그: `기획` `디자인` `설계` `구현` `리뷰` `merge대기` `문서반영` `진단` `출시준비` `완료` `중지` `확인필요` [제안] (ADR-007로 `의견중` `결정됨` 제거)

### 6.2 GitHub (`bot/github/`)
- `gh` CLI를 감싼 `GitHubClient` 인터페이스: Issue 생성·코멘트·종료·라벨, draft PR 생성·본문 갱신·코멘트·draft 해제, PR 상태 조회 (2026-10-11 구현, `bot/github/client.py`)
- 설계 단계: 봇이 `<paths.worktrees>/<project>-<Issue>`에 develop 최신의 detached worktree를 만들고, Claude가 설계 파일을 커밋하면 봇이 설계 JSON·파일 존재·커밋 메시지를 확인한 뒤 브랜치를 만들어 push한다. `/revise`로 slug·변경 유형이 바뀌면 로컬 브랜치 이름을 바꾸고 새 이름으로 push한 뒤 옛 원격 브랜치를 지운다
- 에이전트 커밋 확인(설계·구현·수정 공통): 커밋하지 않은 변경 없음, 새 커밋 있음, 모든 새 커밋 메시지가 4.4 형식. 어긋나면 형식 오류와 같이 내용을 붙여 1회 재요청하고, 다시 어긋나면 NEEDS_HUMAN
- 설계 대상 기능 검증은 볼트 `기능/` 폴더를 읽기만 해서 한다. 볼트 쓰기는 Phase 3
- /stop: Issue는 not planned로 닫고, PR이 있으면 닫고, 없으면 원격 작업 브랜치를 지운 뒤 worktree를 정리한다
- merge 감지: Issue에 merge 완료 코멘트 후 종료, worktree 정리, DONE. DOC_DRAFTING은 Phase 3 (2026-10-11 Zero 결정)
- 봇의 git 작업은 `GitOps` 인터페이스: worktree 생성·삭제, 브랜치 생성, 커밋 메시지 검사, push. push 전 두 조건 검사(운영 규약 4.7)는 단위 테스트로 고정
- merge 감지: `AWAIT_MERGE` 상태의 PR을 5분 간격으로 조회 (운영 규약 3.9-1)
- 첫 실행 시 라벨이 없으면 만든다 (운영 규약 4.8)

### 6.3 Obsidian 볼트 (`bot/vault/`)
- 쓰기 허용 경로를 코드에서 강제한다: `결정/` 전체, 그리고 DOC_SYNC 승인 시의 `기능/*.md`와 허브 파일 (운영 규약 3.4)
- 논의 목록은 SQLite 기록으로 매번 통째로 다시 만든다 (3.6). 결정 노트는 `source_id`로 찾아 갱신한다 (3.7)
- DOC_SYNC 승인 직전에 대상 파일 해시를 비교한다 (3.9-7)
- 볼트 git 커밋은 자기 파일만 경로를 지정해 커밋한다 (3.10)

### 6.4 테스트 실행 (`bot/testrun.py`)
- `projects.yaml`의 `test` 명령을 전역 락 아래에서 실행한다 (운영 규약 1.7)
- 결과 요약: 종료 코드, 통과·실패 수, 실패 테스트명, 소요 시간. 전체 로그는 `~/.zerosync/runs/`
- xcodebuild 결과 파싱은 출력 텍스트에서 한다 (2026-10-11 구현): XCTest의 마지막 `Executed N tests, with M failures`, Swift Testing의 `Test run with N tests ... with M issues`, 실패 테스트명, `error:` 줄(빌드 오류). 결과 번들 파싱은 필요해지면 검토


### 6.5 요청 대기열 (`bot/intake/`) [확정] (Q14, ADR-007로 축소)

입력은 **Discord와 봇 일정** 두 가지다 (ADR-007). 2026-10-26의 Slack 어댑터, Jenkins 연동, 테스트 베드 SSH 강제 명령은 직장 업무 지원을 위한 것이라 만들지 않는다.

```
Discord 제품 채팅의 /task, /release ─┐
                                    ├─→ requests 대기열 ─→ 엔진 (한 번에 하나)
일정 (6.6) ─────────────────────────┘
Discord 채팅의 대화 ──────────────────→ 대기열 (가장 높은 순위)
```

요청 형식 [제안]

| 필드 | 내용 |
|---|---|
| `source` | `discord_task`, `discord_chat`, `schedule` |
| `project` | 제품 키 |
| `pipeline` | `pipelines/`의 파이프라인 이름 (대화는 null). 그 파이프라인의 `triggers`에 `source`가 있어야 한다 (3.5 R5) |
| `requester` | Zero의 Discord ID 또는 `schedule` |
| `title`, `body` | 요청 제목과 원문 |
| `received_at` | 접수 시각 |

대기열 순서 [확정] (ADR-007)

1. Zero가 방금 답한 일(승인, 질문에 대한 답)과 채팅 대화
2. 제품 우선순위: 가게씨 → 제이데이 → 오롯 (회사 설정의 제품 순서)
3. 같은 제품 안에서 파이프라인의 `priority`: `bugfix` → `release` → `feature`
4. 접수 순서

- 에이전트 실행은 한 번에 하나다. 실행 중인 단계가 끝나야 다음 요청이나 다음 단계를 꺼낸다. test 명령은 전역 락으로 따로 하나씩이다
- 같은 요청이 반복되면(같은 출처·파이프라인·제목이 처리 중) 새 작업을 만들지 않고 기존 작업에 이벤트로 붙인다 [제안]
- 나중에 다른 입력 경로가 필요해지면 같은 대기열에 넣는 로컬 접수 명령(`python -m bot.intake`)만 더한다. 이때도 명령 권한은 없고 승인은 Zero만 Discord에서 한다

### 6.6 일정 (`bot/scheduler.py`) [확정 원칙, 세부는 제안] (Q15)

- 파이프라인 YAML의 `schedule`(cron 형식, 이 맥의 시간대)에 적는다. 예: `schedule: "0 3 * * *"` (매일 03:00)
- 봇의 백그라운드 태스크가 시각이 되면 `source: schedule` 요청을 대기열에 넣는다
- 봇이 꺼져 있어 놓친 실행은 켜질 때 파이프라인마다 한 번만 따라잡는다. 마지막 실행 시각은 DB에 둔다
- 일정으로 시작한 작업도 승인 게이트·예산 규칙은 같다
- 봇에 내장된 일정 [확정] (ADR-007): 아침 보고 매일 08:00, 주간 보고 월요일 08:00. 둘 다 `#대표실`에 게시하고 DB에서 만든다(에이전트 호출 없음). 아침 보고는 Zero가 할 일이 있을 때만 멘션한다
  - 아침 보고: 승인 대기 목록(대기 시간), 진행 중인 일, 어제 끝난 일, 어제 예산 사용
  - 주간 보고: 제품별 진행, 성공 기준 지표(9.3-1), 예산 사용 추이, 채용 제안
- 정기 테스트는 제품마다 `nightly-test.yaml`의 `schedule`로 돈다 (3.5)

---

## 7. 모듈 구조

`feat/0-company-system` 기준 구조다. `bot/__main__.py`는 설정 → 락 → DB → 러너 → 엔진 → Discord 순으로 연결한다. 목표 구조에서 따로 두려던 `org/`·`intake/`·`budget.py`·`pipeline/`은 파일 하나씩(`org.py`, 엔진의 `AgentGate`, 엔진의 예산 검사, `pipeline.py`)으로 구현했다.

```
bot/
  __main__.py          # 진입점: 설정 로드, 단일 인스턴스 락, 이벤트 루프 시작
  config.py            # projects.yaml + .env 로더 (pydantic 모델)
  schemas/             # 에이전트 출력 스키마 (의견, 리뷰, 설계, 기획·화면 디자인·진단·출시 준비·문서 반영)
  prompts/             # 단계별 프롬프트 템플릿 (.md)
  agents/
    base.py            # AgentRunner, AgentRequest, AgentResult
    process.py         # 자식 프로세스 실행 (셸 미사용, 타임아웃·취소, 실행 로그)
    claude.py          # ClaudeCliRunner
    codex.py           # CodexCliRunner
    fake.py            # 테스트용 FakeAgentRunner
  org.py               # org.yaml·인격 파일 로더, 에이전트 → 엔진 러너 선택
  scheduler.py         # 내장 일정(아침·주간 보고)과 파이프라인 schedule, 놓친 실행 따라잡기
  reports.py           # 아침·주간 보고 만들기 (DB에서, 에이전트 호출 없음)
  pipeline.py          # 파이프라인 YAML 로더, 규칙 검사 R1~R6, `when` 조건, 기본 선언과 legacy 흐름
  workflow/
    state.py           # 상태 enum, 이벤트, 전이 규칙 (순수 함수)
    engine.py          # 효과 실행, 예산·사용량 한도, 채팅 대화
    gate.py            # AgentGate: 에이전트 실행 대기열 (우선순위, 동시 실행 수)
    work.py            # 단계별 작업 (기획~문서 반영, 보고)
    chat.py            # ChatIO 인터페이스, 테스트용 FakeChatIO
    mentions.py        # ACTION_REQUIRED 집합, 재멘션 판정
  store/
    db.py              # SQLite 연결, 마이그레이션
    repo.py            # tasks, events, agent_runs, chat_turns, ideas, hiring 접근
  render/              # 스키마 → Discord·Issue·PR·볼트 텍스트
  discord_io/          # ChatIO 구현, 슬래시 명령, 웹훅
  github/              # GitHubClient(gh), GitOps(git), VaultGit(볼트 커밋)
  # 볼트 쓰기(DOC_SYNC)는 work.py가 허용 경로에만 하고 VaultGit으로 커밋한다. 결정 노트·논의 목록은 [미구현]
  testrun.py           # 테스트 명령 실행과 결과 요약
  locks.py             # 전역 빌드 락, 작업별 실행 락
  clock.py             # 현재 시각 (테스트에서 고정)
tests/
deploy/
  com.zerosync.orchestrator.plist
```

의존 방향: `discord_io`, `github`, `vault`, `agents`, `intake` → (인터페이스) ← `workflow`. `workflow`는 discord.py를 import하지 않는다 (운영 규약 1.1-5). 회사용 어댑터는 만들지 않는다 (ADR-007). 회사 설정만 비공개 저장소에 둔다 (Q17).

---

## 8. 데이터 모델 (SQLite) [제안]

파일: `~/.zerosync/zerosync.db`. 시각은 UTC ISO 8601 문자열.

```sql
CREATE TABLE tasks (
  id               INTEGER PRIMARY KEY,
  project          TEXT NOT NULL,                 -- projects.yaml 키
  discord_thread   TEXT NOT NULL UNIQUE,
  title            TEXT NOT NULL,
  state            TEXT NOT NULL,                 -- 3.1의 상태
  prev_state       TEXT,                          -- NEEDS_HUMAN에서 돌아갈 상태
  review_round     INTEGER NOT NULL DEFAULT 0,  -- v2부터 review_limit_bonus, test_limit_bonus(/resume으로 늘린 한도), recommended_fixed(/fix 사용 여부) 추가
  test_retry       INTEGER NOT NULL DEFAULT 0,
  opinion_rounds   INTEGER NOT NULL DEFAULT 0,
  issue_number     INTEGER,
  pr_number        INTEGER,
  branch           TEXT,                          -- 봇이 만든 브랜치 (push 보호 4.7-2)
  worktree         TEXT,
  claude_session   TEXT,
  waiting_since    TEXT,                          -- Zero 대기 시작 시각
  reminded_at      TEXT,                          -- 재멘션 1회 기록
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE events (
  id               INTEGER PRIMARY KEY,
  task_id          INTEGER NOT NULL REFERENCES tasks(id),
  actor            TEXT NOT NULL,   -- zero | claude | codex | bot
  kind             TEXT NOT NULL,   -- message | opinion | debate | decision | design | impl
                                    -- | test | review | fix | doc_sync | command | error
  payload          TEXT NOT NULL,   -- JSON: 검증된 스키마 원본 또는 원문
  discord_message  TEXT,
  created_at       TEXT NOT NULL
);

CREATE TABLE agent_runs (
  id               TEXT PRIMARY KEY,               -- run_id
  task_id          INTEGER NOT NULL REFERENCES tasks(id),
  agent            TEXT NOT NULL,                  -- claude | codex
  stage            TEXT NOT NULL,
  exit_code        INTEGER,
  status           TEXT NOT NULL,                  -- running | ok | invalid_output | timeout
                                                   -- | cancelled | error
  log_path         TEXT,
  started_at       TEXT NOT NULL,
  finished_at      TEXT
);
```

- 비동기 접근은 `aiosqlite`를 쓴다. 스키마 변경은 `PRAGMA user_version`으로 버전을 관리한다
- 에이전트 출력 원본(검증된 JSON)은 `events.payload`에 둔다. 운영 규약 3.2의 "에이전트 출력 원본 JSON은 SQLite"에 해당

회사 구조 추가분 [제안] (Phase 3~5에서 스키마 버전을 올리며 추가한다)

```sql
-- tasks에 추가
--   pipeline     TEXT NOT NULL DEFAULT 'feature'   -- pipelines/의 이름
--   request_id   INTEGER REFERENCES requests(id)   -- 대기열로 시작한 작업
--   discord_thread는 /task·/release·정기 테스트 보고 때 봇이 연 포럼 포스트
-- tasks.opinion_rounds는 의견이 채팅으로 옮겨 가면서 쓰지 않는다 (ADR-007)
-- events.actor, agent_runs.agent는 엔진 이름 대신 에이전트 이름(beta, gamma, alpha, delta …)을 쓰고,
-- agent_runs에 engine TEXT 열을 추가한다 (어느 엔진으로 실행했는지)
-- agent_runs.task_id는 채팅 답이면 NULL을 허용하고 chat_turn_id를 둔다

CREATE TABLE requests (
  id               INTEGER PRIMARY KEY,
  source           TEXT NOT NULL,   -- discord_task | discord_chat | schedule (6.5)
  project          TEXT NOT NULL,
  pipeline         TEXT,            -- 대화는 NULL
  requester        TEXT NOT NULL,
  title            TEXT NOT NULL,
  body             TEXT NOT NULL,
  status           TEXT NOT NULL,   -- queued | started | rejected | merged_into
  task_id          INTEGER REFERENCES tasks(id),
  received_at      TEXT NOT NULL
);

CREATE TABLE chat_turns (           -- 제품 채팅 채널의 대화 (ADR-007)
  id               INTEGER PRIMARY KEY,
  project          TEXT NOT NULL,
  actor            TEXT NOT NULL,   -- zero | 에이전트 이름 | bot
  kind             TEXT NOT NULL,   -- message | opinion | debate | command | error
  payload          TEXT NOT NULL,   -- 원문 또는 검증된 Opinion JSON
  discord_message  TEXT,
  task_id          INTEGER REFERENCES tasks(id),   -- /task로 옮겨 간 경우
  created_at       TEXT NOT NULL
);

CREATE TABLE ideas (                -- /idea 목록 (ADR-007)
  id               INTEGER PRIMARY KEY,
  project          TEXT NOT NULL,
  body             TEXT NOT NULL,
  status           TEXT NOT NULL,   -- open | taken | dropped
  task_id          INTEGER REFERENCES tasks(id),   -- /task로 이어간 경우
  created_at       TEXT NOT NULL
);

CREATE TABLE hiring_proposals (
  id               INTEGER PRIMARY KEY,
  task_id          INTEGER REFERENCES tasks(id),
  proposed_by      TEXT NOT NULL,   -- 제안한 팀장 이름
  payload          TEXT NOT NULL,   -- HiringProposal JSON (5.4)
  evidence         TEXT NOT NULL,   -- 봇이 계산한 숫자 근거 JSON
  status           TEXT NOT NULL,   -- open | accepted | rejected
  decided_note     TEXT,            -- /hire 메모
  created_at       TEXT NOT NULL,
  decided_at       TEXT
);

CREATE TABLE schedule_runs (
  job              TEXT PRIMARY KEY, -- 'morning_report', 'weekly_report', 'education_daily', 'education_weekly', '<제품>:<파이프라인>'
  last_run_at      TEXT NOT NULL     -- 놓친 실행 따라잡기 기준 (6.6)
);

CREATE TABLE study_days (           -- 교육 학습 기록 (ADR-009). 하루에 개념 1개
  day              TEXT PRIMARY KEY, -- YYYY-MM-DD
  target           TEXT NOT NULL,   -- 개념 노트 경로 (Learning/개념/날짜 주제)
  label            TEXT NOT NULL,
  carried          INTEGER NOT NULL, -- 전날 개념을 이어 하는가
  done_at          TEXT,            -- 완료 버튼을 누른 시각
  created_at       TEXT NOT NULL
);

CREATE TABLE quizzes (              -- 교육 문제 (ADR-009). 볼트에는 쓰지 않는다
  id               INTEGER PRIMARY KEY,
  kind             TEXT NOT NULL,   -- note | review | weekly
  key              TEXT NOT NULL UNIQUE, -- 'note:<노트 경로>', 'review:2026-11-05', 'week:2026-W45'
  payload          TEXT NOT NULL,   -- QuizQuestion JSON 목록
  created_at       TEXT NOT NULL
);

CREATE TABLE quiz_answers (         -- Zero가 처음 고른 답만
  quiz_id          INTEGER NOT NULL REFERENCES quizzes(id),
  number           INTEGER NOT NULL,
  choice           TEXT NOT NULL,   -- A | B | C | D
  correct          INTEGER NOT NULL,
  answered_at      TEXT NOT NULL,
  PRIMARY KEY (quiz_id, number)
);

CREATE TABLE bot_state (            -- 사용량 한도 정지 등 봇 전체 상태 (ADR-007)
  key              TEXT PRIMARY KEY, -- 예: 'queue_paused_until'
  value            TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
```

- 예산(Q18)은 별도 테이블 없이 `agent_runs`에서 계산한다: 에이전트별 오늘 호출 수, 작업별 호출 수·실행 시간
- 대시보드 지표(도달, 채택, 자동 제외, 시스템 실패, 병목)도 `tasks`·`events`·`agent_runs`·`requests`에서 계산한다. 정의는 9.3-1 (ADR-007)

---

## 9. 설정과 운영

### 9.1 `bot/projects.yaml` [제안]

지금 구현의 설정이다. Phase 3부터 프로젝트 설정은 비공개 회사 저장소의 `projects.yaml`로 옮기고, `cli.*_models`는 `org.yaml`의 에이전트별 `models`로 옮긴다 (9.1-1). 옮길 때 제품마다 `chat_channel_id`와 `chat_default`를 더하고, 제품 목록의 순서를 대기열 우선순위로 쓴다 (ADR-007, 6.5).

저장소에는 값을 비운 `bot/projects.example.yaml`만 커밋하고, 실제 값이 든 `bot/projects.yaml`은 `.gitignore`에 넣는다 [확정] (Q11, 공개 저장소)

```yaml
projects:
  gagaessi:
    forum_channel_id: "..."
    repo: ~/Developer/GagaeSsi
    github: zerohyeon94/GagaeSsi
    base_branch: develop
    test: xcodebuild test -scheme GagaeSsi -destination 'platform=iOS Simulator,name=iPhone 16'
    vault_dir: 일일 예산 관리 앱
    vault_hub: 가게씨.md
    vault_tag: brain/gagaessi
    vault_slug: gagaessi
  # orot, jday도 같은 형식 (운영 규약 3.4 표)

paths:
  vault_projects: ~/Library/Mobile Documents/iCloud~md~obsidian/Documents/zerohyeon-labs/Projects
  worktrees: ~/.zerosync/worktrees
  runs: ~/.zerosync/runs
  db: ~/.zerosync/zerosync.db

cli:
  claude: claude        # launchd PATH 문제 시 절대 경로
  codex: codex
  claude_models:        # 단계별 모델. 적지 않은 단계는 CLI 기본값 (2026-10-10 Zero 결정)
    opinion: sonnet
    design: opus
  codex_models:         # opinion, debate, review만
    review: <모델>

limits:
  max_review_rounds: 3
  max_test_retries: 2   # Q2 확정
  cli_timeout_sec: 3600
  concurrent_builds: 1
  merge_poll_sec: 300
  remind_after_hours: 24
```

- 프로젝트의 GitHub 저장소 이름, 채널 ID는 [미결]이 아니라 실제 값 확인이 필요한 항목이다 (맥에서 채움)
- 로더 규칙 (`bot/config.py`, 2026-10-07 구현)
  - 모르는 키는 오류로 처리한다 (오타를 조용히 무시하지 않기 위해)
  - `paths`의 `vault_projects`는 필수, 나머지와 `cli`·`limits`는 위 값이 기본값이다. 경로는 절대 경로이거나 `~`로 시작해야 한다
  - `base_branch`는 생략하면 `develop`이고, `main`은 거부한다 (9.5)
  - `test`는 문자열이면 셸 문법(`shlex`)으로 인자 목록으로 나눠 저장하고, 실행은 셸 없이 한다 (2.1). 목록으로 적어도 된다
  - 프로젝트 키는 영문 소문자·숫자·`-`, 포럼 채널 ID는 프로젝트끼리 겹치면 안 된다
  - 오류 메시지에는 위치와 이유만 담고 값은 넣지 않는다

### 9.1-1 회사 설정 (비공개 저장소) [확정 원칙, 형식은 제안] (Q12, Q17)

회사 업무용 설정은 공개 저장소에 두지 않는다. 별도 비공개 저장소(예: `zerosync-company`)를 로컬 `~/.zerosync/company`에 두고 봇이 그 경로를 읽는다. 공개 저장소에는 값을 비운 예시(`examples/company/`)만 둔다.

```
~/.zerosync/company/          (비공개 저장소)
  org.yaml                    팀, 에이전트, 채용 기록
  projects.yaml               프로젝트 설정 (지금의 bot/projects.yaml을 옮김)
  agents/<이름>.md            인격·역량 (프롬프트에 들어감)
  pipelines/<이름>.yaml       파이프라인 선언 (3.5)
```

```yaml
# org.yaml
departments:
  product: {name: 제품 부서, head: zero}       # Zero가 부서장 겸임 (ADR-007)
  # education, secretary는 Phase 8

teams:
  planning: {department: product, name: 기획팀,   lead: beta}
  design:   {department: product, name: 디자인팀, lead: gamma}
  dev:      {department: product, name: 개발팀,   lead: alpha}
  qa:       {department: product, name: QA팀,     lead: delta}

agents:
  beta:
    display: Beta
    team: planning
    title: 팀장                # 표시 이름 [Beta · 기획팀장]
    duties: [plan, doc_sync, release_prep]   # 배포 겸임 (ADR-007)
    engine: claude             # claude | codex
    mbti: INFJ
    persona: agents/beta.md
    models: {opinion: opus, plan: opus, doc_sync: sonnet, release_prep: sonnet}
    daily_calls: 30            # 시작값 (ADR-007)
    hired: 2026-10-26
    proposed_by: null
    reason: 초기 구성 (ADR-006)
  gamma:
    display: Gamma
    team: design
    title: 팀장
    engine: claude
    mbti: ISFP
    persona: agents/gamma.md
    models: {opinion: opus, ui_design: opus}
    daily_calls: 30
    hired: 2026-10-28
    proposed_by: null
    reason: 화면 디자인 담당 (ADR-007)
  alpha:
    display: Alpha
    team: dev
    title: 팀장
    engine: claude
    mbti: INTJ
    persona: agents/alpha.md
    models: {opinion: opus, design: opus, implement: sonnet, fix: sonnet}
    daily_calls: 30
    hired: 2026-10-26
    proposed_by: null
    reason: 초기 구성 (ADR-006)
  delta:
    display: Delta
    team: qa
    title: 팀장
    engine: claude
    mbti: ISTJ
    persona: agents/delta.md
    models: {opinion: opus, review: opus, diagnose: opus}
    daily_calls: 30
    hired: 2026-10-28
    proposed_by: null
    reason: 검증과 테스트 진단, R1 충족 (ADR-007)
```

```yaml
# projects.yaml (회사 설정 쪽, 순서 = 대기열 우선순위)
products:
  gagaessi:
    forum_channel_id: "..."
    chat_channel_id: "..."
    chat_default: [beta, alpha]     # 아무도 부르지 않았을 때 답할 팀장
    design_system_url: null         # Claude 디자인 시스템 링크 (ADR-007)
    # repo, github, test, vault_* 는 9.1과 같음
  jday: {...}
  orot: {...}
```

- 회사 설정 경로는 공개 저장소 쪽 설정(`paths.company`, 기본 `~/.zerosync/company`)으로 지정한다
- 로더는 지금 `config.py`와 같은 규칙(모르는 키 오류, 값 없는 오류 메시지)을 따르고, 불러올 때 3.5의 규칙 R1~R6을 검사한다
- 수치(`daily_calls`, 파이프라인 `budget`)는 ADR-007의 시작값이다. 2주 운영 뒤 주간 보고를 보고 고친다

### 9.2 `.env` (커밋 금지)

| 키 | 내용 |
|---|---|
| `DISCORD_TOKEN` | 봇 토큰 |
| `DISCORD_GUILD_ID` | 비공개 서버 ID |
| `ZERO_USER_ID` | 명령 권한자 |
| `OPS_CHANNEL_ID` | #zerosync-ops |
| `REPORT_CHANNEL_ID` | #대표실 (Phase 4, ADR-007) [제안] |

웹훅 URL은 두지 않는다. 봇이 시작할 때 찾거나 만든다 (6.1, Q6).

실제 환경변수가 있으면 `.env`보다 우선한다. `.env` 파일은 없어도 되지만 위 키(Phase 4 전에는 앞의 네 키)는 어느 쪽에든 있어야 한다. ID는 숫자 문자열로 검증하고, 토큰은 `SecretStr`로 담아 출력·repr에 드러나지 않게 한다. 예시는 `.env.example`.

### 9.3 실행
- 개발: `tmux` 안에서 `caffeinate -is python -m bot`
- 운영: `scripts/install_launchd.sh`가 `deploy/com.zerosync.orchestrator.plist` 템플릿의 저장소·홈 경로를 채워 `~/Library/LaunchAgents/`에 두고 `launchctl bootstrap gui/$(id -u) <plist>`한다. `--uninstall`로 해제. `RunAtLoad`, 비정상 종료 때만 재시작(`KeepAlive.SuccessfulExit=false`, 60초 간격), 로그 `~/.zerosync/bot.{out,err}.log`, Homebrew PATH (2026-10-28 구현)
- LaunchDaemon(root)으로 실행하지 않는다. CLI 구독 로그인이 사용자 계정에 있기 때문이다
- 운영 맥: 전원 상시 연결, 시스템 잠자기 방지, 상시 켜 두는 맥 한 대에서만 실행

### 9.3-1 로컬 대시보드 (`bot/dashboard/`) [확정] (2026-10-11 Zero 결정)
- 목적: 여러 프로젝트의 시도 수와 에이전트 실행·로그를 한 화면에서 본다
- 실행: `python -m bot.dashboard [--port 8765]` → 브라우저에서 `http://127.0.0.1:8765`. 봇과 따로 실행하며 봇이 꺼져 있어도 기록을 볼 수 있다
- 화면: 요약(`/`, 프로젝트별 작업·상태·실행 결과, 에이전트·단계별 실행 수와 성공 실행의 평균 소요), 작업 목록(`/tasks`, 프로젝트·상태 필터, Discord 게시글 링크), 작업 상세(`/tasks/<id>`, 대화·의견·결정·오류 흐름과 실행 목록), 실행 기록(`/runs`, 프로젝트·결과 필터, 최근 200건), 실행 로그 전문(`/runs/<id>/log`)
- 표준 라이브러리만 쓴다(`http.server`, `sqlite3`). 새 의존성 없음
- 안전: `127.0.0.1`에만 연결(로그에 프롬프트·앱 코드가 있다), DB는 읽기 전용(`mode=ro`), 로그는 `paths.runs` 안의 파일만, 모든 값은 HTML 이스케이프
- 확장 (Phase 4, ADR-007) [확정]: 홈(승인 대기 목록을 맨 위에, 실행 중인 일, 대기열, 오늘 예산), 제품별 작업판(흐름 카드: 단계, 승인 대기, 걸린 시간), 지표, 병목(단계별 평균 대기. Zero 승인 대기와 에이전트 실행 대기를 나눔), 아이디어, 예산(팀장별·흐름별 추이)
- 지표 정의 (ADR-007): 도달 = 시작된 흐름 수(`/task`, `/release`, 정기 테스트 실패 보고), 채택 = merge·출시된 흐름 수, 자동 제외 = Zero가 닫거나 반려한 흐름 수, 시스템 실패 = 실행 오류·형식 오류·예산 초과로 NEEDS_HUMAN이 된 수. 성공 기준 지표(회사 헌장 5장): 출시·업데이트 수, 자동 처리 비율, merge 후 되돌린 변경 수, 테스트 통과율

### 9.4 사전 준비 체크리스트
- Python 3.11+, git, `gh`(인증), Xcode(시뮬레이터 런타임 포함), `claude`·`codex` CLI(구독 로그인)
- 각 앱 저장소에서 CLI를 한 번 수동 실행해 폴더 신뢰·권한 질문 처리. 2026-10-10 시험 호출에서는 신뢰 처리하지 않은 임시 폴더에서도 `claude -p`, `codex exec`가 질문 없이 실행됐다. 실제 저장소에서 다시 확인한다
- Discord: 비공개 서버, 제품마다 포럼 채널과 채팅 채널, `#대표실`, `#zerosync-ops` (ADR-007), 봇 초대(View Channels, Send Messages, Send Messages in Threads, Create Public Threads, Read Message History, Manage Webhooks, Manage Threads, Attach Files), Message Content Intent
  - 초대 URL의 scope는 `bot`과 `applications.commands` 둘 다 (슬래시 명령 등록)
  - 포럼 채널은 커뮤니티 서버에서만 만들 수 있다. 서버 설정에서 커뮤니티를 켠다
  - Public Bot을 끄려면 먼저 Installation 탭의 Install Link를 None으로 바꾼다 (Discord Provided Link와 함께 둘 수 없다). Installation Contexts는 Guild Install만
  - ID는 Discord 앱의 사용자 설정 > 고급 > 개발자 모드를 켠 뒤 오른쪽 클릭 > ID 복사로 얻는다
  - Attach Files는 긴 의견의 첨부에 필요해 2026-10-11 추가
- GitHub: 앱 저장소 Merge commit만 허용, head 브랜치 자동 삭제 켬 (트러블슈팅 2026-10-05 참고)

### 9.5 보안 [확정]
- 봇은 사실상 원격 셸이다. Zero의 ID 외 입력 무시, 비공개 서버·채널에서만 운영
- 금지: main 접근, develop 직접 push, force push, merge, `.env` 수정, 지정 저장소·볼트 허용 경로 밖 쓰기
- 로그와 Discord 게시물에 토큰이 섞이지 않도록 출력 전 마스킹 [제안]
- 대기열(6.5)은 Discord의 Zero 입력과 봇 일정만 받는다. 외부 입력 경로는 두지 않는다 (ADR-007)
- 회사 설정·회사 프로젝트 정보·디자인 시스템 링크는 공개 저장소에 커밋하지 않는다 (Q17). 공개 저장소의 예시 파일은 값을 비운다
- 어떤 에이전트도 메일에 접근하지 않는다. 비서 부서의 메일 정리를 시작할 때 다시 정한다 (ADR-007)

---

## 10. 검증 필요 목록

맥에서 Claude Code로 개발을 시작하기 전후에 확인한다. 결과는 개발 일지와 이 절에 기록한다.

| # | 항목 | 확인 방법 | 영향 | 결과 |
|---|---|---|---|---|
| V1 | `claude -p`의 JSON 출력·스키마 강제 옵션, 세션 ID 반환 | `claude --help`, 짧은 호출 | 5.2, 2.1 | 확인 (2026-10-10, v2.1.285): `--output-format json`, `--json-schema`, `session_id`, `--resume` 동작 |
| V2 | `codex exec`의 출력 스키마 지정·마지막 메시지 파일 출력 옵션 | `codex exec --help` | 5.2 | 확인 (2026-10-10, v0.161.0): `--output-schema`, `-o`, read-only 샌드박스 쓰기 차단 |
| V3 | Claude 쓰기 단계의 허용·금지 도구 지정 방식 | 공식 문서, 시험 호출 | 5.2 권한 | 확인 (2026-10-10): `--allowedTools`·`--disallowedTools`·`--permission-mode`·`--permission-prompts none` |
| V4 | Codex read-only 샌드박스에서 `xcodebuild` 실패 여부 | 시험 호출 | 1.7 (봇이 테스트 실행하는 근거) |  |
| V5 | 웹훅 메시지의 silent(알림 억제) 지원 | 시험 게시 | 2.5 멘션 규칙 | 확인 (2026-10-11): 멘션 없는 웹훅·운영 채널 메시지는 알림 없이, 멘션 붙은 마지막 의견만 알림 |
| V6 | 개인 private 저장소에서 브랜치 보호(rulesets) 사용 가능 여부 | GitHub 설정 | 4.7 | GagaeSsi는 공개 저장소. 규칙 1개가 있으나 꺼져 있음 (2026-10-11). 봇은 push 보호(4.7)로 막고, 규칙 활성화는 Zero 판단 |
| V7 | `gh` 인증 계정의 Issue·PR 쓰기 권한 | `gh auth status`, 시험 Issue | 1.8, 1.9 | 확인 (2026-10-11): `zerohyeon94`, scope `repo` |
| V8 | 볼트 루트가 git 저장소인지, iCloud 동기화 중 쓰기 안정성 | `git -C <볼트> status` | 3.10 |  |
| V9 | xcodebuild CoreDevice 오류(`_XPCTypeBool`) 해결 | `xcodebuild test` | 1.7 전체. Phase 2 실제 사이클과 가게씨 흐름(Phase 5~6) 차단 요인 |  |
| V10 | launchd 환경에서 CLI 실행(PATH, 키체인 접근) | plist로 시험 실행 | 9.3 |  |
| V11 | SSH `authorized_keys` 강제 명령으로 접수 명령만 실행되는지 | - | - | 해당 없음 (ADR-007에서 SSH 입력 제외) |
| V12 | Slack Socket Mode 앱을 회사 워크스페이스에 설치할 수 있는지 | - | - | 해당 없음 (ADR-007에서 Slack 입력 제외) |
| V13 | Claude 리뷰를 새 세션·읽기 전용으로 돌렸을 때 구현 세션과 다른 지적이 나오는지 (같은 엔진 검증의 실효) | 실제 PR로 비교 | 운영 규약 1.1-2, ADR-006 5절 |  |
| V14 | `claude -p`(비대화형)에서 Claude 디자인(Artifact Design 형식)과 디자인 시스템을 만들 수 있는지, 링크를 얻을 수 있는지 | 시험 호출 | 3.5 `ui_design`, 5.2 UI_DESIGN. 안 되면 명세(md)만으로 시작 (ADR-007) |  |
| V15 | 구독 사용량 한도에 걸렸을 때 CLI가 무엇을 내는지(종료 코드, 오류 문구, 풀리는 시각) | 한도 근처에서 관찰, 문서 | 3.3 사용량 한도 정지 |  |

---

## 11. 미결 사항

2026-10-07에 Zero가 Q1~Q11을 모두 결정했다. Q9를 빼면 이 문서의 제안대로다.

| # | 질문 | 결정 |
|---|---|---|
| Q1 | 설계 요약 JSON의 나머지 필드 구성, 대상 기능이 여러 개일 수 있는가 | 5.4 표. 대상 기능은 1개로 시작 |
| Q2 | 테스트 실패 재시도 한도 | 2회 초과 시 NEEDS_HUMAN |
| Q3 | 설계 승인 생략 옵션 (초안의 "승인 지점 2, 생략 가능") | 두지 않음. 운영 규약 1.5대로 항상 `/approve` |
| Q4 | NEEDS_HUMAN에서 재개하는 방법 | `/resume`. 카운터는 유지하고 한도를 1회만 늘림 |
| Q5 | 토론 라운드 기본값 | `/debate`로만 실행 (사용량 절약) |
| Q6 | 웹훅 URL을 `.env`에 둘지, 봇이 만들지 | 봇이 시작 시 찾거나 만든다 |
| Q7 | `/revise`를 설계 승인 단계에서도 쓸지 | 쓴다 (운영 규약 3.3의 "설계 수정 요청"을 명령으로) |
| Q8 | 봇 재시작 시 실행 중이던 작업 처리 | NEEDS_HUMAN으로 전환하고 알림 |
| Q9 | ZeroSync 저장소 자체의 기준 브랜치 표기 정리 | 저장소는 반영 완료: AGENTS.md와 운영 규약 4.1이 develop 기준 (PR #2). claude.ai 프로젝트 지침은 Zero가 직접 수정 |
| Q10 | Obsidian 동기화 방식과 에이전트별 폴더 분리 (초안 10-4) | 운영 규약 3.4로 대부분 정해짐. 동기화는 iCloud 유지 |
| Q11 | 저장소가 공개(public)이므로 `projects.yaml`(9.1)의 Discord 채널 ID, 로컬 경로를 커밋할지 | 실제 값 파일은 커밋하지 않고 `projects.example.yaml`(값 비움)만 둔다 |

### 11.1 회사 구조 재설계 미결 사항 (ADR-006, 2026-10-26)

[ADR-006](아키텍처%20결정/ADR-006-회사형-구조.md)의 방향을 구현하려면 아래를 정해야 했다. 2026-10-26에 Zero가 Q12~Q19를 모두 결정했고, 같은 날 이 문서의 본문(1~9장, 12장)에 반영했다.

| # | 질문 | 결정 |
|---|---|---|
| Q12 | 팀·에이전트(이름, 팀, 직책, 엔진, 모델, 성격, 역량) 설정 형식과 위치. `projects.yaml`에 넣을지 별도 파일로 둘지 | `org.yaml`로 분리한다(teams, agents. 에이전트마다 team, title, engine, mbti, persona, models, hired, proposed_by, reason). 인격·역량은 에이전트별 마크다운(`agents/<이름>.md`)으로 두고 프롬프트에 넣는다 (2026-10-26 Zero 결정) |
| Q13 | 파이프라인 선언 형식: 단계, 담당 팀, 승인 게이트 위치, 상한. 지금 고정된 상태 머신을 어떻게 옮길지 | 파이프라인은 `pipelines/*.yaml`에 단계 순서·담당 에이전트·승인 게이트(`gate`)·상한만 선언한다. 단계 종류(agent, gate, test, review 등)의 동작은 엔진에 구현하고 단위 테스트로 고정한다. 엔진은 불러올 때 규칙(검증 에이전트 ≠ 작성 에이전트, merge·배포 앞 gate 필수)을 검사한다. 지금 개발 흐름도 이 형식으로 옮긴다 (2026-10-26 Zero 결정) |
| Q14 | 입력 계층: Discord·Slack·Jenkins·SSH 요청을 하나의 형식으로 정규화하는 방법, SSH 허용 명령 목록과 요청자 기록 | (2026-10-28 ADR-007로 축소: 입력은 Discord와 봇 일정만. 아래 Slack·Jenkins·SSH 부분은 쓰지 않는다) 모든 요청을 하나의 요청 형식(출처, 프로젝트, 파이프라인, 요청자, 내용, 접수 시각)으로 DB 대기열에 넣고 엔진이 꺼내 실행한다. Jenkins·SSH는 로컬 접수 명령(`python -m bot.intake`)을 호출한다. SSH는 `authorized_keys`의 강제 명령으로 접수 명령만 허용하고, 접수 명령은 파이프라인 YAML에서 허용한 출처·파이프라인만 받는다. Slack은 Socket Mode 어댑터(비공개 저장소)로 받아 외부에 여는 포트가 없다. Discord 포럼은 지금과 같다 (2026-10-26 Zero 결정) |
| Q15 | 일정 트리거(새벽 작업, 정기 점검)의 정의 방식과 실행 주체 | (Jenkins 부분은 ADR-007로 쓰지 않는다) 파이프라인 YAML의 `schedule`(cron 형식)에 적고 봇이 실행한다. 봇이 꺼져 있어 놓친 실행은 켜질 때 한 번만 따라잡는다. Jenkins 자체 일정 작업은 그대로 두고 결과만 접수 명령으로 보낸다 (2026-10-26 Zero 결정) |
| Q16 | Discord 표시 이름 형식. 지금 `[Beta · Claude]`처럼 엔진을 붙이는 형식을 직책 기준(예: `[Beta · 기획팀장]`)으로 바꿀지 | `[이름 · 직책]` (예: `[Beta · 기획팀장]`). 엔진·모델은 대시보드와 실행 기록에서만 보인다 (2026-10-26 Zero 결정) |
| Q17 | 회사 업무용 어댑터·파이프라인 정의·설정의 비공개 저장 위치 (비공개 저장소 / 커밋하지 않는 폴더) | 별도 비공개 저장소(예: `zerosync-company`, 로컬 `~/.zerosync/company`)에 `org.yaml`, `projects.yaml`, `agents/`, `pipelines/`, `adapters/`를 둔다. 공개 저장소에는 값을 비운 예시만 둔다 (2026-10-26 Zero 결정) |
| Q18 | 예산 상한의 단위(파이프라인, 에이전트, 일·주)와 넘었을 때 동작 | 호출 수·시간 기준. 에이전트별 일일 호출 수, 파이프라인 1회당 호출 수·실행 시간에 상한을 둔다. 넘으면 해당 작업을 NEEDS_HUMAN으로 멈추고 운영 채널에 알린다 (2026-10-26 Zero 결정) |
| Q19 | 팀장의 채용 제안 형식(JSON 필드)과 근거로 쓸 기록(대기 시간, 실패, 한도 초과, 역량 밖 작업 횟수), 보고에 붙이는 방법 | 팀장 보고 JSON에 선택 필드 `hiring_proposal`(kind, team, role, reason, cases, duties, mbti, competencies, if_not_hired)을 둔다. 숫자 근거(대기 시간, 실패 수, 한도 초과 수)는 봇이 DB에서 계산해 붙인다. Zero는 `/hire` 수락·거절로 답하고, 거절 기록은 다음 프롬프트에 넣어 반복 제안을 막는다. `org.yaml` 변경은 Zero가 한다 (2026-10-26 Zero 결정) |

### 11.2 회사 재정의 결정 (ADR-007, 2026-10-27~28)

Zero가 회사 재정의 1~5단계([docs/회사/](회사/README.md))에서 정한 것이다. 2026-10-28에 이 문서의 본문에 반영했다.

| 항목 | 결정 | 반영 |
|---|---|---|
| 회사 범위 | 교육·비서·제품 세 부서, 이 문서는 제품 부서. 직장 업무 지원 제외, 메일 보류 | 1장 |
| 입력 | Discord와 봇 일정만 | 2.1, 6.5 |
| 채널 | 제품마다 포럼(진행·보고)과 채팅(의견), `#대표실`, `#zerosync-ops` | 6.1 |
| 의견 단계 | 포럼 OPINIONS → 채팅 대화. `/task`가 `/decide`를 대신 | 3.1, 4장 |
| 흐름 | 기능 개발(화면 디자인 추가), 버그 수정, 출시, 정기 테스트, 아이디어 관리 | 3.1, 3.1-1, 3.5 |
| 조직 | Beta 기획(배포 겸임), Gamma 디자인(ISFP), Alpha 개발, Delta QA(ISTJ). 리뷰는 Delta | 3.1, 9.1-1 |
| 디자인 결과물 | Claude 디자인 + 명세(md). 안 되면 명세만 | 3.5, 5.4, V14 |
| 운영 | 동시 실행 1개, 대기열 우선순위, 예산 시작값, 아침·주간 보고, 사용량 한도 정지 | 3.3, 6.5, 6.6 |
| 대시보드 | 홈·작업판·지표·병목·아이디어·예산, 지표 정의 | 9.3-1 |
| 로드맵 | Phase 2 실제 사이클 후 PR, 이후 운영화 먼저 | 12.1 |

---

## 12. 구현 순서 (로드맵)

### 12.1 Phase

2026-10-28에 ADR-007(회사 재정의)에 맞춰 Phase 3 이후를 다시 짰다 (Zero 결정: 운영화 먼저). Phase 0~2는 ADR-005 기준으로 진행한 그대로 둔다.

| Phase | 내용 | 상태 |
|---|---|---|
| 0 | 문서 정비: 운영 규약, ADR-005, AGENTS.md, 스키마, SPEC.md | 완료 |
| 1 | MVP: 포럼 게시글 감지 → Beta·Alpha 의견 병렬 게시 | 완료 (2026-10-11 실제 서버에서 한 사이클 확인) |
| 2 | 워크플로 (ADR-005 기준): 상태 머신 전체, 승인 명령, 리뷰 루프, 테스트 실행, Issue·PR, /stop | 구현 (`feat/0-phase2-workflow`). 2026-10-28 가게씨 실제 사이클(작업 5, Issue #34)로 의견·결정·Issue·설계·승인·구현·worktree·push를 확인. 테스트 실행 이후(리뷰, draft PR, merge 감지)는 맥의 시뮬레이터 설치 뒤 `/resume`으로 확인한다. 테스트 환경 오류를 NEEDS_HUMAN으로 보내는 수정 포함 |
| 3 | 조직: 비공개 회사 저장소 경로, `org.yaml`(부서·팀·에이전트)·인격 파일 로더, Beta·Gamma·Alpha·Delta, 에이전트별 엔진·모델(모두 Claude), 표시 이름 `[이름 · 직책]`, 리뷰 담당을 Delta(새 세션·읽기 전용)로, `hiring_proposal`·`/hire` | 구현 (2026-10-28, `feat/0-company-system`) |
| 4 | 운영화: launchd, 요청 대기열(우선순위, 동시 실행 1개), 일정 실행기, `#대표실` 아침·주간 보고, 팀장별 일일 예산, 사용량 한도 정지(V15), 재멘션, 대시보드 확장(9.3-1) | 구현 (2026-10-28, `feat/0-company-system`). 대기열은 에이전트 실행 단위(AgentGate). 사용량 한도 판정 문구는 V15 확인 전 추정 |
| 5 | 채팅과 기능 개발 흐름: 제품 채팅 채널과 대화, `/task`·`/debate`·`/idea` 입력, 파이프라인 YAML 로더와 규칙 검사(R1~R5, `when`), 기능 개발을 `pipelines/feature.yaml`로 이전(PLANNING, 화면 디자인, Alpha 설계·구현, Delta 검증), Claude 디자인 확인(V14), 흐름별 예산, 볼트·DOC_SYNC | 구현 (2026-10-28, `feat/0-company-system`). 파이프라인은 YAML이 없으면 코드의 기본 선언을 쓴다. 볼트가 git이면 봇이 쓴 파일만 커밋(push 없음). V14는 화면 디자인 결과에 `design_url`을 받는 데까지 |
| 6 | 버그 수정·출시 흐름(진단, 수정 방향 승인, 출시 준비)과 가게씨 안정화·첫 출시 준비 | 흐름 구현 (2026-10-28, `feat/0-company-system`). 가게씨 적용은 시뮬레이터 설치(V9) 뒤 |
| 7 | 정기 테스트(제품별 일정, 실패 진단·보고), 아이디어 관리 화면, 제이데이·오롯 적용 | 정기 테스트·아이디어 화면 구현 (2026-10-28, `feat/0-company-system`). 제이데이·오롯 적용, 같은 실패 묶기는 남음 |
| 8 | 교육·비서 부서: 회사 재정의 1~5단계를 두 부서에 대해 다시 하고 구현 | 교육 아침 학습·문제·주간 보고 구현 (ADR-009), 비서 시작 예정 (ADR-010) |

- 2026-10-26판 계획의 Phase 6(입력·일정: 접수 명령, Jenkins·SSH, Slack 어댑터)은 ADR-007로 대부분 빠지고, 대기열과 일정만 Phase 4로 옮겼다
- 빌드 락(테스트 전역 락)과 `/ping`은 Phase 1~2에서 구현했다
- 순서 근거: 조직(3)이 있어야 에이전트별 예산·표시 이름이 생긴다. 운영화(4)를 흐름보다 먼저 해 매일 쓰는 바탕(상시 실행, 보고, 대시보드)을 갖춘다 (Zero 결정). 흐름은 3단계에서 정한 순서(대화 → 기능 개발 → 버그 수정 → 출시 → 정기 테스트 → 아이디어 관리)로 5~7에 놓는다. 이 순서 때문에 가게씨 버그 수정·출시 흐름은 Phase 6이다 (ADR-007 트레이드오프)
- Phase 3~7은 Zero 결정(2026-10-28)으로 `feat/0-company-system` 한 브랜치에 Phase별 커밋으로 모아 PR을 한 번 연다

### 12.2 Phase 1 스레드 분할 [제안]
각 스레드는 develop에서 브랜치를 만들고 draft PR 하나로 끝낸다. 시작 전 범위를 제안하고 Zero의 승인을 받는다.
(2026-10-11부터 PR 단위를 바꿨다. 목표 하나가 최종 완료될 때까지 한 브랜치에 모아 PR을 한 번만 연다. 아래 분할은 작업 순서로만 남긴다. AGENTS.md 참고)

6. `feat/0-phase1-run` — 실제 서버 첫 실행과 시험 사이클, 시작 실패 처리, 이름 통일, 로컬 대시보드(9.3-1) (2026-10-11 구현)
7. `feat/0-phase2-workflow` — Phase 2 전체: 스키마, GitHub·git 계층, 테스트 실행, 상태 머신 전체, 엔진 단계 작업, Discord 명령, merge 확인, GagaeSsi 실제 사이클 (2026-10-11~)

1. `feat/0-agent-runner` — `AgentRunner` 인터페이스, `FakeAgentRunner`, 자식 프로세스 실행(타임아웃·취소, 실행 로그 저장)
   - CLI 러너는 CLI 설치 후 별도 스레드로 분리했다 (2026-10-07 Zero 승인): 이 맥에 `claude`·`codex`가 없어 V1~V3을 확인할 수 없었다
1-2. `feat/0-cli-runners` — `ClaudeCliRunner`·`CodexCliRunner`. V1~V3 확인 후 명령 형식 결정. 실제 CLI 호출은 수동 스모크 스크립트(`scripts/smoke_cli.py`)로만 확인 (2026-10-10 구현)
2. `feat/0-store-state` — SQLite 스키마·저장소, 상태 enum과 OPINIONS 범위의 전이 규칙 (`/decide` → DESIGNING, 모든 상태의 `/stop` 포함). 효과는 타입만 정의하고 실행은 엔진 스레드에서
3. `feat/0-config` — `projects.yaml`, `.env` 로더, 단일 인스턴스 락. 진입점은 설정 로드와 락까지만 연결 (YAML은 PyYAML, `.env`는 직접 파싱: 2026-10-07 Zero 결정)
4. 두 스레드로 나눴다 (2026-10-10 Zero 승인)
   - 4-1. `feat/0-opinion-engine` — `ChatIO` 인터페이스, 의견·반박 프롬프트, render, 멘션 규칙, OPINIONS 단계 엔진(병렬 호출, 형식 오류 재요청, /stop 취소). `/decide`는 결정 기록과 안내까지만(Issue·볼트·설계는 Phase 2) (2026-10-10 구현)
   - 4-2. `feat/0-discord-io` — discord.py, 포럼 게시글 감지, Zero 외 입력 무시, 슬래시 명령, 웹훅 게시(V5), 운영 채널, 진입점 연결, 재시작 복구. 실제 `.env`·채널 ID 필요 (2026-10-10 구현, 실제 서버 확인 전)
5. `chore/0-launchd` — plist와 실행 문서 (Phase 3로 미뤄도 됨)

### 12.3 테스트 전략
- 단위: 전이 규칙, 멘션 판정, 브랜치 이름·push 보호, 커밋 메시지 정규식, 볼트 허용 경로, render 출력, 프롬프트 렌더링
- 통합: 모든 외부 의존(Agent, ChatIO, GitHub, Git, Vault, TestRunner, Clock)을 가짜로 바꾼 채 한 사이클 전체를 시뮬레이션하는 테스트 1개 이상
- 실제 CLI, Discord, GitHub는 CI에서 호출하지 않는다. 수동 스모크 스크립트(`scripts/`)로만 확인한다
- 완료 조건: `pytest`, `ruff check .`, `ruff format --check .` 통과 (AGENTS.md)

---

## 부록 A. 초안(2026-09-30) 대비 바뀐 점

| 항목 | 초안 | 현재 | 근거 |
|---|---|---|---|
| Alpha/Beta 매핑 | 미결 | Alpha = Codex, Beta = Claude, 봇은 인격 없음 | 운영 규약 1.2 |
| 테스트 재실행 | Codex | 봇이 실행, Codex는 결과로 판정 | 운영 규약 1.7 |
| 작업 브랜치 | `claude/<작업명>` | `<type>/<Issue번호>-<slug>`, develop 기준 | 운영 규약 4.2 |
| 작업 시작 | `/idea` | 포럼 게시글 작성 | 이 문서 3.1 |
| 승인 지점 | 3개 (설계 승인 생략 가능) | 설계 승인, 최종 merge, 문서 반영 승인 | 운영 규약 1.5, 3.9 |
| 문서 반영 | 없음 | DOC_SYNC 단계 추가 | 운영 규약 3.9 |
| 에이전트 출력 | 첫 줄 판정 문자열 | JSON 스키마 + pydantic 검증 | 운영 규약 2.1 |
| CLAUDE.md 규칙 | 수정 필요 | AGENTS.md 기준으로 재작성 완료 | PR #2 |
| v1 Xcode 프로젝트 | 처리 미정 | `menubar/`로 이동 | PR #3·#4 |

## 부록 B. 검토했으나 채택하지 않은 대안

| 대안 | 미채택 이유 |
|---|---|
| Slack | 무료 플랜 기록 90일 제한. 팀 도입 시 재검토 |
| 봇 2개가 서로의 메시지에 반응 | 흐름 제어 주체가 없어 무한 루프 위험 |
| 공식 Slack 연동(@Claude, @Codex) | 클라우드에서 실행되어 로컬 저장소·시뮬레이터에 접근 불가 |
| Claude Code의 Discord 채널 기능 | 사람 한 명과 세션 하나의 용도. 보조 지시 채널로만 활용 가능 |
| 서드파티 브릿지 | 상태 머신과 승인 지점을 직접 통제하기 위해 자체 구현 |
| HTTP Interactions Endpoint 방식 봇 | 맥은 외부에서 접속할 수 없음. Gateway 방식 사용 |
