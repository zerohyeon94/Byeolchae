# 아키텍처 결정 (ADR)

Architecture Decision Records — 중요한 기술 선택의 근거를 남긴다.
나중에 "왜 이걸 썼지?"라는 의문이 생겼을 때 읽는 문서.

---

## 작성 형식

```
파일명: ADR-[번호]-[결정 주제].md
예: ADR-001-Electron-선택.md
```

각 파일 구성:
1. **결정** — 무엇을 선택했는가
2. **맥락** — 왜 이 선택이 필요했는가
3. **선택지** — 어떤 대안들이 있었는가
4. **근거** — 왜 이것을 골랐는가
5. **트레이드오프** — 이 선택의 단점은?

---

## ADR 목록

- [ADR-001](ADR-001-Electron-선택.md) — macOS 앱 프레임워크로 Electron 선택 *(폐기 — ADR-004로 대체)*
- [ADR-002](ADR-002-Claude-API-sonnet-4-6.md) — AI 엔진으로 Claude API 선택 *(v1 앱 한정)*
- [ADR-003](ADR-003-페르소나-분리-설계.md) — Alpha/Beta 페르소나를 별도 시스템 프롬프트로 분리 *(v1 앱 한정)*
- [ADR-004](ADR-004-Swift-SwiftUI-전환.md) — Electron에서 Swift + SwiftUI로 전환
- [샌드박스 해제](2026-07-12-샌드박스-해제.md) — Accessibility 창 제목 감지를 위해 App Sandbox 해제 *(v1 앱 한정, 번호 없음)*
- [ADR-005](ADR-005-Discord-오케스트레이터-전환.md) — ZeroSync를 Discord 멀티 에이전트 오케스트레이터(v2)로 전환
- [ADR-006](ADR-006-회사형-구조.md) — ZeroSync를 팀 단위 회사 구조로 재설계 *(일부 ADR-007로 대체)*
- [ADR-007](ADR-007-회사-재정의.md) — 회사 재정의: 부서 구조와 제품 부서 운영
- [ADR-008](ADR-008-팀장-팀원-구조.md) — 팀장·팀원 구조: 개발·QA 팀원과 팀장 확인
- [ADR-009](ADR-009-교육-Daily.md) — 교육 부서 시작: 교육팀장 Eta와 Daily 자동 생성
- [ADR-010](ADR-010-비서-Daily.md) — 비서 부서 시작: 비서팀장 Theta, 저녁 하루 정리 Daily와 짧은 아침 보고
- [ADR-011](ADR-011-프로젝트-담당자.md) — 제품별 기획팀원과 기획팀장 보고
