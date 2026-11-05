"""Phase 2 렌더링: Issue, 설계, 테스트, 리뷰, PR 본문 (운영 규약 1.8, 1.9, 2.3).

Discord·GitHub 모두 마크다운을 쓰므로 같은 함수를 쓴다. Discord에 의존하지 않는다.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from bot.schemas import (
    CauseClass,
    DesignSummary,
    DiagnosisReport,
    DocSyncProposal,
    Finding,
    ImplementationReport,
    Opinion,
    PlanSummary,
    ReleasePrep,
    Review,
    UiDesignSummary,
)
from bot.testrun import TestReport


@dataclass(frozen=True)
class SpecPart:
    """승인된 산출물 하나 (기획, 화면 디자인, 설계, 진단). 구현·리뷰·PR이 함께 참조한다."""

    label: str
    doc: str | None  # 앱 저장소 경로. 진단은 없다
    summary: str
    text: str  # 프롬프트에 넣는 전문 요약


@dataclass(frozen=True)
class ReviewRound:
    round: int
    review: Review


def _bullets(items: Sequence[str]) -> list[str]:
    return [f"- {item}" for item in items]


def render_issue_body(
    *,
    decision: str,
    post: str,
    opinions: Sequence[tuple[str, Opinion]],
    discord_url: str | None,
) -> str:
    """Issue 본문은 봇이 기존 텍스트로 조립한다 (운영 규약 1.8)."""
    lines = ["## 결정", "", decision.strip(), "", "## 배경", "", post.strip(), ""]
    if opinions:
        lines += ["## 의견 요약", ""]
        for name, opinion in opinions:
            lines.append(f"**{name}** — {opinion.stance.label}: {opinion.conclusion}")
            lines += [f"- 위험: {risk}" for risk in opinion.risks]
            lines.append("")
    lines += ["## 링크", "", f"- Discord 게시글: {discord_url or '-'}", ""]
    lines.append("_ZeroSync 봇이 만든 Issue_")
    return "\n".join(lines)


def render_design(summary: DesignSummary, *, doc_url: str | None = None) -> str:
    lines = [
        f"**설계** — {summary.conventional_title}",
        "",
        f"변경 유형: {summary.change_type.label} · 대상 기능: {summary.target_feature}",
        "",
        summary.summary,
        "",
        "바꿀 것",
        *_bullets(summary.changes),
        "",
        "테스트 계획",
        *_bullets(summary.test_plan),
    ]
    if summary.risks:
        lines += ["", "위험", *_bullets(summary.risks)]
    lines += ["", f"설계 전문: {doc_url or summary.design_doc}"]
    return "\n".join(lines)


def render_plan(plan: PlanSummary, *, doc_url: str | None = None) -> str:
    lines = [
        f"**기획** — {plan.conventional_title}",
        "",
        f"변경 유형: {plan.change_type.label} · 대상 기능: {plan.target_feature}"
        f" · 화면 변경: {'있음' if plan.has_ui_change else '없음'}",
        "",
        f"문제: {plan.problem}",
        "",
        plan.summary,
        "",
        "이번에 할 것",
        *_bullets(plan.in_scope),
        "",
        "하지 않을 것",
        *_bullets(plan.out_of_scope),
        "",
        "완료 기준",
        *_bullets(plan.acceptance),
    ]
    if plan.edge_cases:
        lines += ["", "예외 상황", *_bullets(plan.edge_cases)]
    lines += ["", f"기획서: {doc_url or plan.plan_doc}"]
    return "\n".join(lines)


def render_ui_design(ui: UiDesignSummary, *, doc_url: str | None = None) -> str:
    lines = [f"**화면 디자인** — {ui.summary}", "", "화면"]
    lines += [f"- {screen.name}: {', '.join(screen.states)}" for screen in ui.screens]
    if ui.components:
        lines += ["", "컴포넌트", *_bullets(ui.components)]
    if ui.design_system_changes:
        lines += ["", "디자인 시스템 변경", *_bullets(ui.design_system_changes)]
    if ui.out_of_plan:
        lines += ["", "기획 밖 제안", *_bullets(ui.out_of_plan)]
    lines += ["", f"명세: {doc_url or ui.design_doc}"]
    if ui.design_url:
        lines.append(f"Claude 디자인: {ui.design_url}")
    return "\n".join(lines)


def render_diagnosis(report: DiagnosisReport) -> str:
    lines = [
        f"**진단** — {report.cause.label} (확신도 {report.confidence.label})",
        "",
        report.summary,
        "",
        "재현",
        *_bullets(report.reproduction),
        "",
        "근거",
        *_bullets(report.evidence),
        "",
        f"수정 방향: {report.fix_direction}",
    ]
    change = report.change()
    if change is not None:
        lines += [
            "",
            f"변경 유형: {change.change_type.label} · 대상 기능: {change.target_feature}"
            f" · 제목: {change.conventional_title}",
        ]
    return "\n".join(lines)


def render_nightly_report(diagnosis: DiagnosisReport | None, tests: TestReport | None) -> str:
    """정기 테스트 보고 포스트: 무엇이 문제인지, 무엇을 개선해야 하는지, 다음 행동 (2026-11-05)."""
    lines: list[str] = []
    environment = False
    if diagnosis is not None:
        environment = diagnosis.cause is CauseClass.ENVIRONMENT
        lines += [
            f"**무엇이 문제인가** — {diagnosis.cause.label} (확신도 {diagnosis.confidence.label})",
            diagnosis.summary,
            "",
            "**무엇을 개선해야 하나**",
            diagnosis.fix_direction,
            "",
            "근거",
            *_bullets(diagnosis.evidence),
            "",
        ]
    if environment:
        step = (
            "코드가 아니라 이 맥의 실행 환경 문제다. 환경을 고치면 다음 정기 테스트에서 확인된다."
        )
    else:
        step = "수정하려면 제품 채팅 채널에서 `/task bug <제목>`으로 시작한다."
    lines += [f"**다음 행동**: {step}", "같은 문제가 이어지면 이 포스트에 날짜만 덧붙인다.", ""]
    lines.append(render_test_report(tests) if tests else "테스트 결과 없음")
    if tests is not None and tests.log_path:
        lines.append(f"전체 로그: `{tests.log_path}`")
    return "\n".join(lines)


def render_release_prep(prep: ReleasePrep) -> str:
    lines = [f"**출시 준비** — 버전 {prep.version}", "", "점검 목록"]
    lines += [f"- [{item.state.label}] {item.item}" for item in prep.checklist]
    if prep.blockers:
        lines += ["", "막힌 것", *_bullets(prep.blockers)]
    if prep.certificate_expiry:
        lines += ["", "인증서·프로비저닝 만료"]
        lines += [f"- {c.name}: {c.expires}" for c in prep.certificate_expiry]
    lines += ["", "릴리스 노트 (초안)", "", prep.release_notes]
    if prep.store_description:
        lines += ["", "스토어 설명 (초안)", "", prep.store_description]
    return "\n".join(lines)


def render_doc_sync(proposal: DocSyncProposal) -> str:
    lines = [f"**문서 반영안** — {proposal.summary}", ""]
    lines += [f"- {file.action} `{file.path}` ({len(file.content)}자)" for file in proposal.files]
    return "\n".join(lines)


def render_implementation(report: ImplementationReport, *, heading: str = "구현") -> str:
    lines = [f"**{heading}** — {report.changes_summary}", "", f"설계 의도: {report.design_intent}"]
    if report.check_points:
        lines += ["", "확인할 점", *_bullets(report.check_points)]
    if report.responses:
        lines += ["", "지적별 대응"]
        lines += [f"- {r.index}. {r.action.label}: {r.note}" for r in report.responses]
    return "\n".join(lines)


def render_test_report(report: TestReport) -> str:
    if report.passed:
        head = f"**테스트 통과** — {report.total if report.total is not None else '?'}개"
    else:
        head = "**테스트 실패**"
        if report.failures:
            head += f" — 실패 {report.failures}개 / 전체 {report.total}개"
    lines = [f"{head} ({report.duration_sec}초)"]
    if report.failed_tests:
        lines += ["", "실패한 테스트", *_bullets(report.failed_tests)]
    if report.errors:
        lines += ["", "오류", *(f"- `{error}`" for error in report.errors)]
    return "\n".join(lines)


def render_findings(findings: Sequence[Finding]) -> str:
    """번호를 붙인 지적 목록. FIX 단계의 responses.index가 이 번호를 가리킨다."""
    lines = []
    for index, finding in enumerate(findings, start=1):
        where = ""
        if finding.file:
            where = f" `{finding.file}{f':{finding.line}' if finding.line else ''}`"
        lines.append(f"{index}. {finding.severity.label}{where} {finding.problem}")
        lines.append(f"   - 이유: {finding.reason}")
        lines.append(f"   - 제안: {finding.suggestion}")
    return "\n".join(lines)


def render_review(review: Review, *, round_: int) -> str:
    required = len(review.required_findings)
    recommended = len(review.recommended_findings)
    lines = [
        f"**리뷰 {round_}라운드: {review.verdict.value}** — {review.summary}",
        f"[필수] {required}개 · [권장] {recommended}개",
    ]
    if review.findings:
        lines += ["", render_findings(review.findings)]
    return "\n".join(lines)


def render_lead_check(review: Review, *, label: str, round_: int) -> str:
    """팀장 확인 결과 (ADR-008). 통과는 짧게, 수정 요청은 지적을 함께 보인다."""
    if review.verdict.value == "APPROVED":
        head = f"**{label} 팀장 확인 {round_}회: 통과** — {review.summary}"
    else:
        head = f"**{label} 팀장 확인 {round_}회: 수정 요청** — {review.summary}"
    lines = [head]
    if review.findings:
        lines += ["", render_findings(review.findings)]
    return "\n".join(lines)


def render_pr_body(
    *,
    decision: str,
    specs: Sequence[SpecPart],
    report: ImplementationReport | None,
    tests: TestReport | None,
    reviews: Sequence[ReviewRound],
    discord_url: str | None,
    branch: str,
    issue: int,
) -> str:
    """PR 본문 (운영 규약 1.9). 변경 요약·설계 의도·확인할 점은 구현 담당의 보고에서 온다."""
    lines = ["## 결정", "", decision.strip(), ""]
    if report is not None:
        lines += ["## 변경 요약", "", report.changes_summary, ""]
        lines += ["## 설계 의도", "", report.design_intent, ""]
        if report.check_points:
            lines += ["## 확인할 점", "", *_bullets(report.check_points), ""]
    lines += ["## 기획과 설계", ""]
    for part in specs:
        where = f"`{part.doc}` — " if part.doc else ""
        lines.append(f"- {part.label}: {where}{part.summary}")
    lines.append("")
    lines += ["## 테스트", "", render_test_report(tests) if tests else "아직 실행 전", ""]
    lines += ["## 리뷰 이력", ""]
    if reviews:
        lines += [
            f"- {r.round}라운드: {r.review.verdict.value} — {r.review.summary}" for r in reviews
        ]
    else:
        lines.append("- 아직 없음")
    lines += [
        "",
        "## 링크",
        "",
        f"- Discord 게시글: {discord_url or '-'}",
        f"- 브랜치: `{branch}`",
        "",
        f"Refs #{issue}",
    ]
    return "\n".join(lines)
