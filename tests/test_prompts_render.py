"""프롬프트 스냅샷, 렌더링, 멘션 규칙 테스트.

스냅샷을 의도적으로 바꿨다면 `UPDATE_SNAPSHOTS=1 pytest tests/test_prompts_render.py`로 갱신한다.
"""

import os
from pathlib import Path

import pytest

from bot.org import DEFAULT_ORG
from bot.prompts import (
    HistoryEntry,
    concept_image_prompt,
    concept_note_prompt,
    debate_prompt,
    lead_check_prompt,
    opinion_prompt,
    persona_for,
    secretary_daily_prompt,
    weekly_quiz_prompt,
    with_format_error,
)
from bot.render import MAX_POST_LENGTH, render_format_error, render_notice, render_opinion_post
from bot.schemas import Opinion
from bot.workflow.mentions import ACTION_REQUIRED, Moment, should_mention
from bot.workflow.state import NoticeCode

SNAPSHOTS = Path(__file__).parent / "snapshots"


def check_snapshot(name: str, text: str) -> None:
    path = SNAPSHOTS / f"{name}.txt"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not path.exists():
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def make_opinion(**overrides):
    data = {
        "stance": "conditional",
        "conclusion": "배너는 좋지만 끌 수 있어야 한다",
        "reasons": ["초과 사실을 바로 알 수 있다", "기존 알림 설정을 재사용할 수 있다"],
        "risks": ["배너가 반복되면 피로하다"],
        "proposal": "하루 한 번만 띄우고 설정에서 끌 수 있게 한다",
    }
    data.update(overrides)
    return Opinion.model_validate(data)


BETA = persona_for(DEFAULT_ORG, "beta")
ALPHA = persona_for(DEFAULT_ORG, "alpha")

HISTORY = [
    HistoryEntry("Beta · 기획팀장", "**찬성** — 배너로 충분하다"),
    HistoryEntry("Alpha · 개발팀장", "**반대** — 푸시 알림이 더 단순하다"),
    HistoryEntry("Zero", "@alpha 푸시 권한이 없으면?"),
]


# 프롬프트


def test_first_opinion_prompt_snapshot():
    prompt = opinion_prompt(BETA, title="예산 배너", post="예산을 넘으면 배너를 띄우자\n가능할까?")
    assert "지금까지의 흐름" not in prompt
    for name in ("persona", "perspective", "profile", "title", "post", "history", "request"):
        assert f"${name}" not in prompt
    check_snapshot("opinion_first_beta", prompt)


def test_follow_up_prompt_snapshot():
    prompt = opinion_prompt(
        ALPHA,
        title="예산 배너",
        post="예산을 넘으면 배너를 띄우자",
        question="푸시 권한이 없으면?",
        history=HISTORY,
    )
    assert "[Alpha · 개발팀장]" in prompt and "[Zero]" in prompt
    check_snapshot("opinion_follow_up_alpha", prompt)


def test_debate_prompt_snapshot():
    prompt = debate_prompt(
        BETA,
        title="예산 배너",
        post="예산을 넘으면 배너를 띄우자",
        own="**찬성** — 배너로 충분하다",
        other_name="Alpha · 개발팀장",
        other="**반대** — 푸시 알림이 더 단순하다",
        history=HISTORY,
    )
    check_snapshot("debate_beta", prompt)


def test_lead_check_prompt_snapshot():
    prompt = lead_check_prompt(
        ALPHA,
        member="Epsilon · 개발팀원",
        label="구현",
        title="예산 배너",
        decision="예산을 넘으면 배너를 띄운다",
        round_=2,
        limit=2,
        spec="설계서: `docs/설계/42-budget-banner.md`",
        subject="**구현** — 배너를 추가했다",
        previous_required="1. [필수] HomeView.swift:3 — 경계값",
    )
    check_snapshot("lead_check_alpha", prompt)


def test_personas_come_from_org():
    beta = opinion_prompt(BETA, title="t", post="p")
    alpha = opinion_prompt(ALPHA, title="t", post="p")
    assert "Beta · 기획팀장 (INFJ)" in beta and "사용 흐름" in beta
    assert "Alpha · 개발팀장 (INTJ)" in alpha and "엣지 케이스" in alpha
    # 엔진 이름은 프롬프트에 드러나지 않는다 (SPEC Q16)
    assert "Claude" not in beta and "Codex" not in alpha


def test_profile_file_text_goes_into_prompt():
    org = DEFAULT_ORG.model_copy(
        update={"profiles": {"delta": "역량: 기준 대조\n위험: 기준 밖 문제"}}
    )
    prompt = opinion_prompt(persona_for(org, "delta"), title="t", post="p")
    assert "## 인격과 역량" in prompt and "기준 밖 문제" in prompt
    assert "## 인격과 역량" not in opinion_prompt(BETA, title="t", post="p")


def test_post_text_with_dollar_sign_is_kept():
    assert "가격 $5 이상" in opinion_prompt(BETA, title="t", post="가격 $5 이상")


def test_format_error_prompt_appends_error():
    retried = with_format_error("원래 프롬프트\n", "스키마 검증 실패:\n- risks: 최소 1개")
    assert retried.startswith("원래 프롬프트\n\n## 이전 출력의 형식 오류")
    assert "- risks: 최소 1개" in retried


# 렌더링


def test_render_opinion_post():
    post = render_opinion_post(make_opinion())
    assert post.attachments == ()
    assert post.text == (
        "**조건부 찬성** — 배너는 좋지만 끌 수 있어야 한다\n\n"
        "근거\n- 초과 사실을 바로 알 수 있다\n- 기존 알림 설정을 재사용할 수 있다\n\n"
        "위험\n- 배너가 반복되면 피로하다\n\n"
        "제안\n하루 한 번만 띄우고 설정에서 끌 수 있게 한다"
    )
    assert render_opinion_post(make_opinion(), heading="반박").text.startswith(
        "반박 · **조건부 찬성**"
    )


def test_long_opinion_is_attached():
    long = make_opinion(reasons=["가" * 200] * 4, risks=["나" * 200] * 3, proposal="다" * 300)
    post = render_opinion_post(long)
    assert len(post.text) <= MAX_POST_LENGTH
    assert "첨부 파일" in post.text
    (attachment,) = post.attachments
    assert attachment.filename == "opinion.md"
    assert "다" * 300 in attachment.content


def test_render_format_error():
    short = render_format_error("그냥 문장", "```json 코드 블록을 찾지 못했다\n추가")
    assert short.text.startswith("**형식 오류** — 출력이 의견 형식에 맞지 않았다 (```json")
    assert "```\n그냥 문장\n```" in short.text
    long = render_format_error("x" * 1000, "오류")
    assert long.attachments[0].content == "x" * 1000
    fenced = render_format_error("```json\n{}\n```", "오류")
    assert fenced.attachments
    assert "비어 있었다" in render_format_error("  ", "오류").text


def test_every_notice_code_has_text():
    for code in NoticeCode:
        assert render_notice(code)
    assert render_notice(NoticeCode.REJECTED, "현재 상태: X") == "현재 상태: X"
    assert render_notice(NoticeCode.AGENT_FAILED, "[Alpha · Codex] timeout").endswith(
        "\n[Alpha · Codex] timeout"
    )


# 멘션 규칙 (운영 규약 2.5)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (Moment.OPINIONS_READY, True),
        (Moment.DESIGN_READY, True),
        (Moment.FINAL_APPROVAL, True),
        (Moment.DOC_SYNC_READY, True),
        (Moment.NEEDS_HUMAN, True),
        (Moment.BOT_ERROR, True),
        (Moment.HIRING_PROPOSAL, True),
        (Moment.REMINDER, True),
        (Moment.IMPLEMENT_STARTED, False),
        (Moment.TEST_RUNNING, False),
        (Moment.REVIEW_RUNNING, False),
        (Moment.TEST_RESULT, False),
        (Moment.REVIEW_ROUND_RESULT, False),
        (Moment.STAGE_DONE, False),
        (Moment.NOTICE, False),
    ],
)
def test_mention_rules(moment, expected):
    assert should_mention(moment) is expected


def test_action_required_covers_only_listed_moments():
    assert len(ACTION_REQUIRED) == 8
    assert set(Moment) - ACTION_REQUIRED == {
        Moment.IMPLEMENT_STARTED,
        Moment.TEST_RUNNING,
        Moment.REVIEW_RUNNING,
        Moment.TEST_RESULT,
        Moment.REVIEW_ROUND_RESULT,
        Moment.STAGE_DONE,
        Moment.NOTICE,
    }


def test_concept_note_prompt_snapshot():
    prompt = concept_note_prompt(
        persona_for(DEFAULT_ORG, "eta"),
        day="2026-11-01",
        label="엣지-중앙 서버 구조",
        item="엣지-중앙 서버 구조: 장비 안의 AI 엔진·에이전트와 중앙 서버의 역할 나누기",
        section="전체",
        month="시스템 전체 한 바퀴",
    )
    assert "회사/" in prompt  # 금지 안내
    assert "figure" in prompt and "그림 안에는 글자를 넣지 않는다" in prompt
    check_snapshot("concept_note_eta", prompt)


def test_concept_image_prompt_snapshot():
    prompt = concept_image_prompt(
        persona_for(DEFAULT_ORG, "iota"),
        day="2026-11-01",
        label="엣지-중앙 서버 구조",
        brief="위에 중앙 서버(랙) 하나, 아래에 장비 상자 셋. 서버에서 장비로 화살표가 내려간다",
    )
    assert "Iota · 교육팀원" in prompt and "image_path" in prompt
    assert "글자, 숫자, 로고를 넣지 않는다" in prompt
    check_snapshot("concept_image_iota", prompt)


def test_weekly_quiz_prompt_snapshot():
    prompt = weekly_quiz_prompt(
        persona_for(DEFAULT_ORG, "eta"),
        facts="**주간 학습 보고** (11-02 ~ 11-08)\n- Daily: 7일 중 5일 · 끝낸 개념 3개",
        notes=["Learning/개념/2026-11-02 엣지-중앙 서버 구조", "Learning/개념/2026-11-04 gRPC"],
    )
    assert "5~8문제" in prompt and "`Learning/개념/2026-11-04 gRPC.md`" in prompt
    check_snapshot("weekly_quiz_eta", prompt)


def test_secretary_daily_prompt_snapshot():
    prompt = secretary_daily_prompt(
        persona_for(DEFAULT_ORG, "theta"),
        day="2026-11-02",
        facts="## 결정할 것\n\n- [gagaessi] #19 카테고리 추가 — AWAIT_PLAN_APPROVAL (대기 9시간)",
    )
    assert "Theta" in prompt and "#19 카테고리 추가" in prompt and "한 줄 요약" in prompt
    check_snapshot("secretary_daily_theta", prompt)
