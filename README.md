# ZeroSync

Discord 기반 멀티 에이전트 오케스트레이터 (v2).

Zero가 Discord 포럼 게시글에 아이디어를 올리면, Beta · Claude와 Alpha · Codex가 의견을 내고, Zero가 결정한다. 이후 설계, 구현, 리뷰, merge, 문서 반영까지 봇이 정해진 규칙대로 에이전트를 호출하고 GitHub과 Obsidian 볼트에 기록한다.

| 주체 | 역할 |
|------|------|
| Zero | 질문, 결정, 승인, merge. 유일한 명령 권한자 |
| Beta · Claude (ISFJ) | 의견(설계·유지보수), 설계, 구현, 리뷰 반영, 문서 반영안 |
| Alpha · Codex (INTJ) | 의견(실행·위험), 리뷰, 판정 |
| 봇 `[ZeroSync]` | 호출, 테스트 실행, Issue·push·PR, 볼트 쓰기, 기록, 알림 |

봇은 LLM이 아닌 결정론적 프로그램이며, 판단은 Zero만 한다.

## 현재 상태

Phase 1 (MVP) 진행 중. 봇 코드는 골격, 에이전트 출력 스키마, 에이전트 호출 인터페이스가 있다. v1 메뉴바 앱(Swift)은 `menubar/`에 있다.

## 문서

- [운영 규약](docs/운영-규약.md) — 역할·권한, Discord 대화 형태, 기록 위치, GitHub 규칙
- [ADR-005](docs/아키텍처%20결정/ADR-005-Discord-오케스트레이터-전환.md) — v2 전환 결정
- [문서 인덱스](docs/README.md) — 개발 일지, ADR, 트러블슈팅 등
- [AGENTS.md](AGENTS.md) — 이 저장소에서 작업하는 에이전트용 개발 규칙
- [SPEC](docs/SPEC.md) — 봇 설계 (상태 머신, 명령어, 모듈, 데이터 모델, 로드맵)

## 별채 웹 대시보드

웹 화면·자산·테스트는 별도 프로젝트 `Byeolchae-Web`에 있다. 해당 폴더의 `README.md`에 설치 방법이 있으며, `.venv/bin/python -m byeolchae_web`으로 실행한다. 회사 봇은 이 저장소에서 기존 명령인 `python -m bot`으로 실행한다.
