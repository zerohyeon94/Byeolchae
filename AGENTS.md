# 저장소 작업 지침 (AGENTS.md)

이 저장소에서 작업하는 모든 코딩 에이전트(Claude Code, Codex 등)가 따르는 규칙이다.
v2 봇의 동작 규칙 전체는 [docs/운영-규약.md](docs/운영-규약.md)에, 봇 설계(상태 머신, 모듈, 데이터 모델, 로드맵)는 [docs/SPEC.md](docs/SPEC.md)에 있다. 이 문서와 운영 규약이 다르면 운영 규약을 따른다.
SPEC의 [제안]·[미결] 항목은 임의로 확정하지 않고, 구현에 필요할 때 Zero에게 묻는다.

## 공통 규칙

- 응답과 문서는 한국어로 작성한다
- 이모지를 쓰지 않는다
- Zero의 확인 없이 push하지 않는다

## 프로젝트 개요

ZeroSync는 Discord 기반 멀티 에이전트 오케스트레이터(v2)이자 Zero 한 사람을 위한 회사다. 회사는 교육·비서·제품 세 부서이고, 지금은 제품 부서(가게씨, 제이데이, 오롯)를 만든다. Zero가 대표 겸 제품 부서장이고 팀장(기획팀 Beta, 디자인팀 Gamma, 개발팀 Alpha, QA팀 Delta)이 기획·디자인·설계·구현·검증을 맡으며, 봇이 파이프라인 단계를 규칙대로 진행한다. 전환 배경은 [ADR-005](docs/아키텍처%20결정/ADR-005-Discord-오케스트레이터-전환.md), 회사 구조는 [ADR-006](docs/아키텍처%20결정/ADR-006-회사형-구조.md)과 [ADR-007](docs/아키텍처%20결정/ADR-007-회사-재정의.md), 회사 재정의 문서는 [docs/회사/](docs/회사/README.md)에 있다.

- 봇 안의 에이전트(15팀 41명, 별 이름. 예: 아르크투루스 기획팀장, 리겔 개발팀장, ADR-012)와 이 저장소를 개발하는 코딩 에이전트(Claude Code, Codex)는 별개다. 봇 에이전트의 이름·엔진은 회사 설정(`org.yaml`)이 정하고, 코딩 에이전트의 규칙은 이 문서와 `CLAUDE.md`가 정한다

- 봇은 LLM이 아니라 결정론적 프로그램이다. 에이전트 출력의 JSON 블록을 검증해 옮길 뿐 해석하지 않는다
- 판단(방향, 설계 승인, merge, 문서 반영)은 Zero만 한다
- 외부(GitHub, Obsidian 볼트)에 쓰는 주체는 봇 하나다

## 구조

| 경로 | 내용 | 상태 |
|------|------|------|
| `bot/` | 봇 본체 (워크플로, 상태, 에이전트 호출, GitHub·볼트 쓰기). `python -m bot`이 진입점 | 골격 |
| `bot/schemas/` | 에이전트 출력 스키마(의견, 리뷰 판정, 설계 요약 일부)와 JSON 추출·검증 | 구현 |
| `bot/agents/` | 에이전트 호출 인터페이스, 자식 프로세스 실행(타임아웃·취소·실행 로그), Claude·Codex CLI 러너, 테스트용 가짜 러너 | 구현 |
| `bot/store/` | SQLite 연결·스키마 버전 관리, tasks·events·agent_runs·chat_turns·ideas·채용 제안·교육 학습 기록과 문제·작업 단계 변화 저장소 | 구현 |
| `bot/workflow/` | 상태 enum과 전이 규칙(순수 함수), `ChatIO` 인터페이스, 멘션 규칙, 엔진(`engine.py`, 채팅 대화·예산·사용량 한도), 에이전트 실행 대기열(`gate.py`), 단계별 작업(`work.py`), 교육 아침 학습·주간 보고 실행(`education.py`), 비서 아침판 Daily·저녁 하루 정리 실행(`secretary.py`). 상태 머신은 파이프라인 선언을 입력으로 받는다. discord.py를 import하지 않는다 | 구현 |
| `bot/education/` | 교육 아침 학습 규칙(순수 함수: 이어 하기·다음 개념, 대기열, 월별 목표, 복습 문제 고르기, 대기열 쓰기), 개념 노트 형식 검사, 개념 노트 그림(새 PNG 확인, 줄이기, 노트에 넣기), 문제 형식과 Discord 표시 문구, 주간 보고 숫자 (ADR-009) | 구현 |
| `bot/secretary/` | 비서 하루 정리 규칙: Claude·Codex 토큰 집계(봇·Zero, 주간 비율, 구독 주간 한도), Daily 파일(아침판·저녁판)과 `#비서` 문구 (ADR-010) | 구현 |
| `bot/github/` | push 보호·커밋 메시지 규칙, git worktree 작업(`GitOps`), `gh` 연동(`GitHubClient`), 볼트 커밋(`VaultGit`), 테스트용 가짜 | 구현 |
| `bot/testrun.py` | 앱 테스트 명령 실행(전역 락)과 xcodebuild 결과 요약 | 구현 |
| `bot/prompts/` | 단계별 프롬프트 템플릿(`.md`)과 렌더링 함수 | 의견·반박·기획·화면 디자인·설계·구현·수정·리뷰·진단·출시 준비·문서 반영 구현 |
| `bot/render/` | 스키마 → 사람이 읽는 텍스트 (Discord·Issue·PR·볼트 공용) | 구현 |
| `bot/clock.py` | 현재 시각 인터페이스 (테스트에서 고정) | 구현 |
| `bot/org.py` | 회사 조직 설정(`org.yaml`, 인격 파일) 로더. 에이전트 이름과 엔진 분리, 예전 이름(`former`) 연결, 단계별 담당, R1 검사. 없으면 ADR-007~011 기본 조직(제품 팀장 넷, 개발·QA 팀원 Epsilon·Zeta, 프로젝트 담당 Kappa·Lambda·Mu, 교육팀장 Eta와 팀원 Iota, 비서팀장 Theta). 예시는 `examples/company/` | 구현 |
| `bot/pipeline.py` | 파이프라인 선언(기능 개발·버그 수정·출시·정기 테스트) 로더와 규칙 검사(R1~R6, 팀장 확인 `check`). 회사 설정에 `pipelines/`가 없으면 기본 선언. 예시는 `examples/company/pipelines/` | 구현 |
| `bot/scheduler.py`, `bot/reports.py` | 일정 실행기(아침·주간 보고, 교육·비서 일정, 파이프라인 schedule)와 `#대표실`·`#비서` 보고 만들기 | 구현 |
| `bot/config.py` | `bot/projects.yaml` + `.env` 로더 (pydantic 검증). 예시는 `bot/projects.example.yaml`, `.env.example` | 구현 |
| `bot/locks.py` | 단일 인스턴스 락 (`flock`) | 구현 (빌드 락은 Phase 2) |
| `../Byeolchae-Web/` | 별도 로컬 웹 프로젝트. 그 폴더에서 `.venv/bin/python -m byeolchae_web`으로 실행하며, 봇의 설정과 작업 기록을 읽어 회사 홈·팀원·조직도·업무 화면을 제공한다 | 분리 |
| `bot/discord_io/` | Discord 입출력 계층(입력 판정 규칙, 웹훅 게시, 슬래시 명령, 교육 문제 버튼, 클라이언트). 워크플로 로직은 이 계층 밖에 둔다 | 구현 (실제 서버 확인 전) |
| `tests/` | pytest 테스트. 실제 CLI·Discord·GitHub는 호출하지 않는다. 프롬프트 스냅샷은 `tests/snapshots/` (`UPDATE_SNAPSHOTS=1`로 갱신) | 사용 중 |
| `scripts/` | 수동 확인 스크립트 (`smoke_cli.py`: 실제 CLI로 러너 확인, 구독 사용량 소모) | 사용 중 |
| `deploy/` | launchd 상시 실행 템플릿 (`com.zerosync.orchestrator.plist`) | 사용 중 |
| `menubar/` | v1 Swift 메뉴바 앱 (`ZeroSync/`, `ZeroSync.xcodeproj`, 테스트 타깃) | 보류 |
| `docs/` | 봇 자체 개발 문서 | 사용 중 |

## 개발 환경과 명령

- Python 3.11 이상 (`.python-version`). 의존성과 도구 설정은 `pyproject.toml`에 있다
- 환경 준비: `python3.11 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
- 테스트: `pytest`
- 린트·포맷: `ruff check .`, `ruff format --check .`
- PR 전에 위 명령을 모두 통과시킨다

v1 메뉴바 앱을 수정할 때만 Xcode 명령을 쓴다. 저장소 루트에서 실행한다.

- 빌드: `xcodebuild -scheme ZeroSync -project menubar/ZeroSync.xcodeproj build`
- 테스트: `xcodebuild -scheme ZeroSync -project menubar/ZeroSync.xcodeproj test`

## 코딩 원칙

- 워크플로 로직은 Discord에 묶지 않는다. Discord 관련 코드는 `bot/discord_io/`에만 둔다
- 에이전트 출력은 pydantic 스키마로 검증한다. 저장, Discord 표시, Issue·PR·볼트 옮겨 적기는 모두 같은 JSON에서 만든다
- 스키마의 enum 값은 영문 키(`agree`, `required` 등)로 두고, 한국어 표시는 `label` 속성에서 만든다
- 멘션 여부, 브랜치 이름 검사, 볼트 쓰기 허용 경로처럼 규칙으로 정해진 동작은 단위 테스트로 고정한다

## 브랜치와 PR

- 기준 브랜치는 `develop`이다. 작업 브랜치는 develop에서 만들고, PR 대상도 develop이다
- `main`은 릴리스 기준이다. develop → main 반영은 Zero가 직접 한다
- 브랜치 이름: `<type>/<Issue번호>-<slug>` (예: `feat/12-vote-schema`). Issue가 없으면 번호 자리에 `0`을 쓴다. `claude/` 접두어는 쓰지 않는다
  - type: `feat`, `fix`, `refactor`, `docs`, `chore`
  - slug: 영문 소문자·숫자와 `-`, 40자 이내
- 목표 하나가 최종 완료될 때까지 한 작업 브랜치에 커밋을 모으고, 완료되면 draft PR을 한 번만 연다. 중간 단계마다 PR을 나누지 않는다 (2026-10-11 Zero 결정). merge는 Zero가 GitHub에서 Merge commit 방식으로 한다
- develop·main에 직접 push하거나 merge하지 않는다. 작업 브랜치에서 push한 뒤에는 force push하지 않는다

## 커밋 단위와 메시지

- 한 브랜치나 PR에 여러 작업이 있으면 독립적으로 검토하고 되돌릴 수 있는 작업마다 별도로 커밋한다. 예를 들어 대시보드의 회사명 변경과 조직도 기능 추가는 각각 커밋한다. 각 작업을 검증하는 테스트와 설명 문서는 해당 작업 커밋에 포함한다.

- 형식: `<type>(<영역>): <요약>` + 빈 줄 + 본문(선택). 영역이 애매하면 생략한다 (`docs: ...`)
- type: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`
- `wip`, `수정` 같은 의미 없는 메시지는 쓰지 않는다

## 보안

- 봇 토큰, 웹훅 URL, API 키, `.env`는 커밋하지 않고 문서에도 적지 않는다
- 테스트·CLI 전체 로그(`.zerosync/`)는 커밋하지 않는다
- Xcode 사용자 상태 파일(`xcuserdata/`, `*.xcuserstate`)은 커밋하지 않는다

## 문서

- `docs/README.md`가 문서 인덱스다
- 중요한 기술 결정은 `docs/아키텍처 결정/`에 ADR로 남긴다 (다음 번호: ADR-013)
- 작업한 날에는 `docs/개발 일지/YYYY-MM-DD.md`에 한 것 / 막힌 것·발견한 것 / 다음 할 것을 기록한다
- 개발 일지는 작업 PR 안에서 함께 갱신한다. 일지만 바꾸는 PR은 따로 만들지 않는다. 이전 PR의 merge 결과는 다음 작업 PR의 일지에 적는다
- 문제 해결 기록은 `docs/트러블슈팅/`에 남긴다
