"""단계별 프롬프트 (SPEC 5.3).

템플릿은 같은 폴더의 `<단계>.md`에 두고 `string.Template`(`$이름`)으로 채운다.
렌더링은 순수 함수이고, 결과는 스냅샷 테스트로 고정한다.
구성: 인격과 관점(운영 규약 1.2, 1.4) → 작업 맥락 → 단계별 지시 → 출력 스키마 → 금지 사항
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from string import Template

from pydantic import BaseModel

from bot.org import Org
from bot.schemas import (
    ChangeSummary,
    ConceptImage,
    ConceptNote,
    DailySummary,
    DesignSummary,
    DiagnosisReport,
    DocSyncProposal,
    ImplementationReport,
    Opinion,
    PlanSummary,
    ReleasePrep,
    Review,
    UiDesignSummary,
    WeeklyReview,
)
from bot.schemas.education import QUIZ_MAX, QUIZ_MIN

PROMPTS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Persona:
    """프롬프트에 넣는 인격. 회사 설정(org.yaml, agents/<이름>.md)에서 만든다 (SPEC 5.3)."""

    name: str  # 예: Beta · 기획팀장 (INFJ)
    perspective: str
    basic_question: str
    profile: str = ""  # 인격 파일 전문 (역량, 위험과 보완)
    rejected: tuple[str, ...] = ()  # Zero가 거절한 이 팀장의 채용 제안 (운영 규약 1.12)


def persona_for(org: Org, agent: str, rejected: Sequence[str] = ()) -> Persona:
    spec = org.agents[agent]
    name = org.display(agent) + (f" ({spec.mbti})" if spec.mbti else "")
    return Persona(name, spec.perspective, spec.basic_question, org.profile(agent), tuple(rejected))


HIRING_GUIDE = (
    "자기 팀 역량 밖의 일이 반복되거나, 다른 관점이나 실제로 일을 맡을 담당이 더 필요하면 "
    "출력의 hiring_proposal에 근거와 함께 새 팀·팀원을 제안한다. 근거가 없으면 null로 둔다. "
    "팀이나 역할을 직접 만들지 않는다. 채용은 Zero가 정한다."
)


@dataclass(frozen=True)
class HistoryEntry:
    """게시글 흐름의 한 줄. speaker는 표시 이름(예: Zero, Beta · 기획팀장)이다."""

    speaker: str
    text: str


@cache
def _template(name: str) -> Template:
    return Template((PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8"))


def _schema(model: type[BaseModel] = Opinion) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False)


def _history(entries: Sequence[HistoryEntry]) -> str:
    if not entries:
        return ""
    lines = ["", "## 지금까지의 흐름", ""]
    for entry in entries:
        lines.append(f"[{entry.speaker}]")
        lines.append(entry.text.strip())
        lines.append("")
    return "\n".join(lines)


def _profile(persona: Persona, *, hiring_guide: bool = True) -> str:
    parts = []
    if persona.profile.strip():
        parts.append(f"## 인격과 역량\n\n{persona.profile.strip()}")
    if not hiring_guide:
        return "\n" + "\n\n".join(parts) + "\n\n" if parts else "\n"
    hiring = HIRING_GUIDE
    if persona.rejected:
        hiring += "\n\nZero가 거절한 제안 (새 근거 없이 다시 제안하지 않는다):\n" + "\n".join(
            f"- {line}" for line in persona.rejected
        )
    parts.append(f"## 채용 제안\n\n{hiring}")
    return "\n" + "\n\n".join(parts) + "\n\n"


def _common(persona: Persona, title: str, post: str) -> dict[str, str]:
    return {
        "persona": persona.name,
        "perspective": persona.perspective,
        "basic_question": persona.basic_question,
        "profile": _profile(persona),
        "title": title.strip(),
        "post": post.strip(),
        "schema": _schema(),
    }


def opinion_prompt(
    persona: Persona,
    *,
    title: str,
    post: str,
    question: str = "",
    history: Sequence[HistoryEntry] = (),
) -> str:
    """의견 프롬프트.

    history가 비면 첫 의견: 상대 의견을 보지 않고 독립 작성한다 (운영 규약 1.6).
    """
    if history:
        request = (
            f"지금까지의 흐름을 참고해 Zero의 질문에 답한다.\n\nZero의 질문:\n{question.strip()}"
        )
    else:
        request = (
            "게시글의 아이디어에 대한 너의 의견을 독립적으로 작성한다. "
            "다른 에이전트의 의견은 아직 없으며 참고하지 않는다."
        )
    return _template("opinion").substitute(
        _common(persona, title, post), history=_history(history), request=request
    )


def debate_prompt(
    persona: Persona,
    *,
    title: str,
    post: str,
    own: str,
    other_name: str,
    other: str,
    history: Sequence[HistoryEntry] = (),
) -> str:
    """반박 프롬프트. own·other는 렌더링된 직전 의견 전문이다."""
    return _template("debate").substitute(
        _common(persona, title, post),
        history=_history(history),
        own=own.strip(),
        other=other.strip(),
        other_name=other_name,
    )


def with_format_error(prompt: str, error: str) -> str:
    """형식 오류 재요청 프롬프트 (운영 규약 2.1). 원래 프롬프트에 오류 내용을 붙인다."""
    return (
        f"{prompt.rstrip()}\n\n"
        "## 이전 출력의 형식 오류\n\n"
        f"{error.strip()}\n\n"
        "위 오류를 고쳐 스키마에 맞는 JSON 객체 하나만 다시 출력한다.\n"
    )


# 흐름의 단계: 기획, 화면 디자인, 설계, 진단, 구현, 수정, 팀장 확인, 리뷰, 출시 준비, 문서 반영


def _persona(persona: Persona, *, hiring_guide: bool = True) -> dict[str, str]:
    return {
        "persona": persona.name,
        "perspective": persona.perspective,
        "profile": _profile(persona, hiring_guide=hiring_guide),
    }


def _section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body.strip()}\n" if body.strip() else ""


def _lines(label: str, items: Sequence[str]) -> list[str]:
    return [f"- {label}:", *(f"  - {item}" for item in items)] if items else []


def change_text(change: ChangeSummary) -> str:
    return "\n".join(
        [
            f"- 변경 유형: {change.change_type.label} ({change.change_type.value})",
            f"- 대상 기능: {change.target_feature}",
            f"- slug: {change.slug}",
            f"- 제목: {change.conventional_title}",
        ]
    )


def plan_text(plan: PlanSummary) -> str:
    lines = [
        f"기획서: `{plan.plan_doc}`",
        change_text(plan),
        f"- 문제: {plan.problem}",
        f"- 요약: {plan.summary}",
        *_lines("할 것", plan.in_scope),
        *_lines("하지 않을 것", plan.out_of_scope),
        *_lines("완료 기준", plan.acceptance),
        *_lines("예외 상황", plan.edge_cases),
    ]
    return "\n".join(lines)


def ui_text(ui: UiDesignSummary) -> str:
    lines = [f"화면 디자인 명세: `{ui.design_doc}`", f"- 요약: {ui.summary}", "- 화면:"]
    lines += [f"  - {screen.name}: {', '.join(screen.states)}" for screen in ui.screens]
    lines += _lines("컴포넌트", ui.components)
    if ui.design_url:
        lines.append(f"- Claude 디자인: {ui.design_url}")
    return "\n".join(lines)


def design_text(summary: DesignSummary) -> str:
    """프롬프트에 넣는 설계 요약."""
    lines = [
        f"설계 문서: `{summary.design_doc}`",
        change_text(summary),
        f"- 요약: {summary.summary}",
        *_lines("바꿀 것", summary.changes),
        *_lines("테스트 계획", summary.test_plan),
        *_lines("위험", summary.risks),
    ]
    return "\n".join(lines)


def diagnosis_text(report: DiagnosisReport) -> str:
    lines = [
        f"진단 ({report.cause.label}, 확신도 {report.confidence.label})",
        f"- 요약: {report.summary}",
        *_lines("재현", report.reproduction),
        *_lines("근거", report.evidence),
        f"- 수정 방향: {report.fix_direction}",
    ]
    if report.action is not None:
        lines.append(f"- 정기 테스트 판정: {report.action.label} ({report.action.value})")
    return "\n".join(lines)


def _features(features: Sequence[str] | None) -> str:
    if features is None:
        return "(볼트 기능 폴더를 읽지 못했다. 기존 기능이면 그 이름을 그대로 쓴다)"
    return "\n".join(f"- {name}" for name in sorted(features)) or "(아직 없다)"


_CHANGE_KEYS = {"change_type", "target_feature", "slug", "title", "scope"}


def _change_rule(change: ChangeSummary | None, features: Sequence[str] | None) -> str:
    """변경 유형을 이미 정했으면(기획·진단 승인) 그대로 쓰게 하고, 아니면 목록에서 고르게 한다."""
    if change is not None:
        # 표시용 제목(feat(영역): 요약)을 title에 옮겨 적지 않게 필드 값을 그대로 준다
        values = json.dumps(
            change.model_dump(mode="json", include=_CHANGE_KEYS), ensure_ascii=False
        )
        return (
            "Zero가 이미 승인한 값이다. 출력의 아래 필드에 글자 하나 바꾸지 않고 그대로 쓴다:\n"
            f"{values}\n\n" + change_text(change)
        )
    return (
        "기능 정의서(볼트 `기능/` 폴더) 목록이다. target_feature는 이 목록에서 고르고, "
        "change_type이 feature_add일 때만 목록에 없는 새 이름을 쓴다.\n" + _features(features)
    )


def _revision(revision: str, previous: str, doc: str) -> str:
    if not revision:
        return ""
    body = f"Zero의 수정 요청:\n{revision.strip()}\n"
    if previous:
        body += (
            f"\n이전 산출물:\n{previous}\n\n같은 파일(`{doc}`)을 고쳐 새 커밋으로 추가한다. "
            "slug나 변경 유형이 바뀌면 파일 이름도 새 slug로 옮긴다"
        )
    return _section("수정 요청", body)


def plan_prompt(
    persona: Persona,
    *,
    title: str,
    decision: str,
    issue: int,
    features: Sequence[str] | None,
    history: Sequence[HistoryEntry] = (),
    revision: str = "",
    previous: PlanSummary | None = None,
) -> str:
    return _template("plan").substitute(
        _persona(persona),
        title=title.strip(),
        issue=issue,
        decision=decision.strip(),
        history=_history(history),
        features=_features(features),
        revision=_revision(
            revision, plan_text(previous) if previous else "", previous.plan_doc if previous else ""
        ),
        schema=_schema(PlanSummary),
    )


def ui_design_prompt(
    persona: Persona,
    *,
    title: str,
    issue: int,
    slug: str,
    spec: str,
    design_system: str,
    revision: str = "",
    previous: UiDesignSummary | None = None,
) -> str:
    return _template("ui_design").substitute(
        _persona(persona),
        title=title.strip(),
        issue=issue,
        slug=slug,
        spec=spec.strip() + "\n",
        design_system=design_system.strip(),
        revision=_revision(
            revision, ui_text(previous) if previous else "", previous.design_doc if previous else ""
        ),
        schema=_schema(UiDesignSummary),
    )


def design_prompt(
    persona: Persona,
    *,
    title: str,
    decision: str,
    issue: int,
    features: Sequence[str] | None,
    history: Sequence[HistoryEntry] = (),
    revision: str = "",
    previous: DesignSummary | None = None,
    spec: str = "",
    change: ChangeSummary | None = None,
) -> str:
    return _template("design").substitute(
        _persona(persona),
        title=title.strip(),
        issue=issue,
        decision=decision.strip(),
        history=_history(history),
        spec=_section("승인된 기획과 화면 디자인", spec),
        change_rule=_change_rule(change, features),
        revision=_revision(
            revision,
            design_text(previous) if previous else "",
            previous.design_doc if previous else "",
        ),
        schema=_schema(DesignSummary),
    )


def diagnose_prompt(
    persona: Persona,
    *,
    title: str,
    decision: str,
    tests: str = "",
    history: Sequence[HistoryEntry] = (),
    features: Sequence[str] | None = None,
    needs_change: bool = True,
) -> str:
    if needs_change:
        rule = (
            "3. 수정 방향이 승인되면 바로 구현하므로 변경 유형(change_type), "
            "대상 기능(target_feature), slug, title, scope를 함께 정한다. "
            "target_feature는 아래 목록에서 고르고, feature_add일 때만 새 이름을 쓴다"
        )
        features_text = _section("기능 정의서 목록", _features(features))
    else:
        rule = (
            "3. 정기 테스트 실패 보고다. 봇이 한 번 더 돌려도 실패했다. 변경 유형 관련 필드는 "
            "null로 둔다. 테스트를 하나도 돌리지 못했으면 전체 로그를 읽고 실행 환경"
            "(Xcode, 시뮬레이터, 서명 등)의 어디가 어긋났는지 밝힌다\n"
            "4. 개선이 필요한지 action으로 정한다. report: 코드·테스트·실행 환경 중 무엇을 고쳐야 "
            "한다. none: 고칠 것이 없다(근거를 evidence에 적는다). summary에는 무엇이 문제인지, "
            "fix_direction에는 무엇을 개선해야 하는지를 처음 보는 사람도 알게 쓴다"
        )
        features_text = ""
    return _template("diagnose").substitute(
        _persona(persona),
        title=title.strip(),
        decision=decision.strip(),
        tests=_section("봇 테스트 결과", tests),
        history=_history(history),
        features=features_text,
        change_rule=rule,
        schema=_schema(DiagnosisReport),
    )


def implement_prompt(
    persona: Persona, *, title: str, decision: str, issue: int, branch: str, spec: str
) -> str:
    return _template("implement").substitute(
        _persona(persona),
        title=title.strip(),
        issue=issue,
        branch=branch,
        decision=decision.strip(),
        spec=spec.strip() + "\n",
        schema=_schema(ImplementationReport),
    )


def fix_prompt(
    persona: Persona, *, title: str, issue: int, branch: str, spec_docs: str, problems: str
) -> str:
    return _template("fix").substitute(
        _persona(persona),
        title=title.strip(),
        issue=issue,
        branch=branch,
        spec_docs=spec_docs.strip(),
        problems=problems.strip(),
        schema=_schema(ImplementationReport),
    )


def review_prompt(
    persona: Persona,
    *,
    author: str,
    title: str,
    decision: str,
    issue: int,
    round_: int,
    spec: str,
    tests: str,
    diff: str,
    previous_required: str = "",
) -> str:
    previous = ""
    if previous_required:
        previous = _section(
            "이전 라운드 [필수] 지적",
            previous_required + "\n\n각 항목이 해결됐는지 확인한다. 등급을 낮춰 넘기지 않는다",
        )
    return _template("review").substitute(
        _persona(persona),
        author=author,
        title=title.strip(),
        issue=issue,
        round=round_,
        decision=decision.strip(),
        spec=spec.strip() + "\n",
        tests=tests.strip(),
        previous=previous,
        diff=diff,
        schema=_schema(Review),
    )


def lead_check_prompt(
    persona: Persona,
    *,
    member: str,
    label: str,
    title: str,
    decision: str,
    round_: int,
    limit: int,
    spec: str,
    subject: str,
    previous_required: str = "",
) -> str:
    previous = ""
    if previous_required:
        previous = _section(
            "지난 확인의 [필수] 지적",
            previous_required + "\n\n각 항목이 해결됐는지 확인한다. 등급을 낮춰 넘기지 않는다",
        )
    return _template("lead_check").substitute(
        _persona(persona),
        member=member,
        label=label,
        title=title.strip(),
        round=round_,
        limit=limit,
        decision=decision.strip(),
        spec=_section("승인된 기획과 설계", spec),
        previous=previous,
        subject=subject.strip(),
        schema=_schema(Review),
    )


def release_prep_prompt(
    persona: Persona, *, title: str, version: str, decision: str, tests: str
) -> str:
    return _template("release_prep").substitute(
        _persona(persona),
        title=title.strip(),
        version=version,
        decision=decision.strip(),
        tests=tests.strip() or "결과 없음",
        schema=_schema(ReleasePrep),
    )


def doc_sync_prompt(
    persona: Persona,
    *,
    title: str,
    pr: int | None,
    change: ChangeSummary,
    spec: str,
    docs: str,
    hub: str,
    today: str,
) -> str:
    return _template("doc_sync").substitute(
        _persona(persona),
        title=title.strip(),
        pr=pr or "-",
        change_type=change.change_type.label,
        target_feature=change.target_feature,
        spec=spec.strip() + "\n",
        docs=docs.strip() or "(없음)",
        hub=hub,
        today=today,
        schema=_schema(DocSyncProposal),
    )


def concept_note_prompt(
    persona: Persona,
    *,
    day: str,
    label: str,
    item: str,
    section: str,
    month: str,
) -> str:
    """교육팀장의 개념 노트 본문 (ADR-009)."""
    # 교육 출력 스키마에는 hiring_proposal이 없다
    return _template("concept_note").substitute(
        _persona(persona, hiring_guide=False),
        day=day,
        label=label.strip(),
        item=item.strip(),
        section=section.strip() or "-",
        month=month.strip(),
        schema=_schema(ConceptNote),
    )


def concept_image_prompt(
    persona: Persona, *, day: str, label: str, brief: str, labels: Sequence[str]
) -> str:
    """교육팀원의 개념 노트 그림 (ADR-009). brief와 labels는 교육팀장이 쓴 그림 설명과 이름표."""
    return _template("concept_image").substitute(
        _persona(persona, hiring_guide=False),
        day=day,
        label=label.strip(),
        brief=brief.strip(),
        labels="\n".join(f"- {name}" for name in labels),
        schema=_schema(ConceptImage),
    )


def secretary_daily_prompt(persona: Persona, *, day: str, facts: str) -> str:
    """비서의 저녁 하루 정리 요약 (ADR-010). facts는 봇이 센 Daily 본문."""
    return _template("secretary_daily").substitute(
        _persona(persona, hiring_guide=False),
        day=day,
        facts=facts.strip(),
        schema=_schema(DailySummary),
    )


def weekly_quiz_prompt(persona: Persona, *, facts: str, notes: list[str]) -> str:
    """교육팀장의 주간 문제와 한마디 (ADR-009). notes는 이번 주 개념 노트 경로."""
    return _template("weekly_quiz").substitute(
        _persona(persona, hiring_guide=False),
        facts=facts.strip(),
        notes="\n".join(f"- `{note}.md`" for note in notes),
        count_min=QUIZ_MIN,
        count_max=QUIZ_MAX,
        schema=_schema(WeeklyReview),
    )
