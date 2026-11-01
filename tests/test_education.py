"""교육 아침 학습·주간 보고 규칙과 실행 테스트 (ADR-009, 볼트 AGENTS.md "아침").

볼트와 DB는 tmp_path에 만들고, 교육팀장 호출과 커밋은 가짜로 바꾼다.
"""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from bot.clock import FixedClock
from bot.education.daily import (
    DailyError,
    LastStudy,
    Month,
    NoteQuiz,
    QueueItem,
    note_target,
    optional_for,
    parse_month,
    parse_months,
    parse_queue,
    pick_review,
    plan_daily,
    update_queue,
)
from bot.education.note import check_note
from bot.education.quiz import answer_text, check_questions, render_question
from bot.education.weekly import QuizScore, render_weekly, week_facts, week_key
from bot.schemas import ConceptNote, QuizQuestion, WeeklyReview
from bot.workflow.education import EducationDesk

GOALS = """\
# 월별 목표

| 월 | 주제 | 층 | 자격증 |
|---|---|---|---|
| 2026-11 | 시스템 전체 한 바퀴 (플랫폼 지도) | 폭 | — |
| 2027-02 | 데이터 계층 (PostgreSQL) | 폭 | SQLD 준비 |
| 2027-03 | 실시간·영상 | 폭 | **SQLD 응시** |

## 2026-10 · 준비

- [x] 운영 방식을 정했다
- [ ] 대기열을 채웠다

**월말 확인:**

## 2026-11 · 시스템 전체 한 바퀴 (플랫폼 지도)

- [ ] 16개 구성 요소에 표시했다
"""

QUEUE = """\
# 학습 대기열

## 2026-11 · 시스템 전체 한 바퀴

### 0. 전체
- [ ] 엣지-중앙 서버 구조: 장비와 서버의 역할 나누기

### 1. 장비 안
- [ ] 소리 이벤트 감지 기초: dB, EWMA
- [ ] gRPC 스트리밍: 서버 스트림과 keepalive
"""

BODY = """\
## 한 문장으로

장비는 현장에서 판단하고 서버는 모아서 보여 준다.

## 비유

지점과 본사다. 다만 지점이 본사 없이도 일을 계속한다는 점은 다르다.

## 흐름

```text
장비 → 서버 → 화면
```

## 어디서 깨지나

연결이 끊기면 이벤트가 늦게 도착한다.

## 이런 시스템에서는

엣지 장비가 이벤트를 만들고 중앙 서버가 수집한다.

## 단어 정리

- **엣지:** 현장에 있는 장비

"""


def _question(answer="B", prompt="엣지와 중앙의 역할은?"):
    return QuizQuestion(
        prompt=prompt,
        options=["장비가 모은다", "장비가 판단한다", "웹이 판단한다", "아무도 안 한다"],
        answer=answer,
        explanation="장비가 현장에서 판단한다. (엣지-중앙 서버 구조)",
    )


THREE = [_question(prompt=f"노트 {n}번 질문") for n in (1, 2, 3)]


NOV = Month("2026-11", "시스템 전체 한 바퀴", "")


# 읽기


def test_parse_queue_keeps_sections_links_and_done():
    text = QUEUE.replace(
        "- [ ] 엣지-중앙 서버 구조: 장비와 서버의 역할 나누기",
        "- [x] 엣지-중앙 서버 구조: 장비와 서버의 역할 나누기 → [[Learning/개념/a 엣지|엣지]]",
    )
    items = parse_queue(text)
    assert [(i.title, i.done, i.target, i.section) for i in items] == [
        ("엣지-중앙 서버 구조", True, "Learning/개념/a 엣지", "전체"),
        ("소리 이벤트 감지 기초", False, None, "장비 안"),
        ("gRPC 스트리밍", False, None, "장비 안"),
    ]


def test_parse_month_strips_note_and_reads_certificate():
    nov = Month("2026-11", "시스템 전체 한 바퀴", "", "폭", "플랫폼 지도")
    assert parse_month(GOALS, "2026-11") == nov
    assert parse_month(GOALS, "2027-03").certificate == "SQLD 응시"
    assert parse_month(GOALS, "2026-10") == nov  # 계획 시작 전 달은 다음 달 주제
    assert [m.key for m in parse_months(GOALS)] == ["2026-11", "2027-02", "2027-03"]
    with pytest.raises(DailyError):
        parse_month(GOALS, "2030-01")


# 계획


def test_unfinished_study_is_carried_without_new_note():
    latest = LastStudy("Learning/개념/2026-11-01 엣지", "엣지", False)
    plan = plan_daily(date(2026, 11, 2), latest, parse_queue(QUEUE), NOV, 0)
    assert plan.carried and plan.item is None
    assert plan.target == "Learning/개념/2026-11-01 엣지"


def test_finished_or_missing_study_takes_top_of_queue():
    queue = parse_queue(QUEUE)
    first = plan_daily(date(2026, 11, 1), None, queue, NOV, 0)
    assert first.label == "엣지-중앙 서버 구조" and first.finished is None
    assert first.target == "Learning/개념/2026-11-01 엣지-중앙 서버 구조"

    linked = parse_queue(update_queue(QUEUE, first))
    latest = LastStudy(first.target, first.label, True)
    second = plan_daily(date(2026, 11, 2), latest, linked, NOV, 1)
    assert second.label == "소리 이벤트 감지 기초"
    assert second.finished == first.target


def test_empty_queue_is_an_error():
    done = QUEUE.replace("- [ ]", "- [x]")
    with pytest.raises(DailyError, match="남은 항목"):
        plan_daily(date(2026, 11, 1), None, parse_queue(done), NOV, 0)


def test_queue_item_with_link_reuses_its_note():
    item = QueueItem(3, "엣지: 설명", False, "Learning/개념/2026-10-31 엣지", "전체")
    plan = plan_daily(date(2026, 11, 1), None, [item], NOV, 0)
    assert plan.target == "Learning/개념/2026-10-31 엣지"


def test_queue_title_splits_on_colon_space_only():
    item = parse_queue("- [ ] go:embed: 웹 화면을 바이너리에 넣기\n- [ ] 프로세스 감시\n")
    assert (item[0].title, item[0].detail) == ("go:embed", "웹 화면을 바이너리에 넣기")
    assert (item[1].title, item[1].detail) == ("프로세스 감시", "")
    assert note_target(date(2026, 11, 1), item[0].title) == "Learning/개념/2026-11-01 go embed"


def test_note_names_drop_characters_obsidian_cannot_link():
    assert note_target(date(2026, 11, 1), "SSE와 pull: [방식]") == (
        "Learning/개념/2026-11-01 SSE와 pull 방식"
    )


def test_optional_item_follows_certificate_month_or_section():
    assert optional_for(NOV, "장비 안") == "플랫폼 지도: '장비 안' 구성 요소 칸 하나 채우기 (5분)"
    sqld = Month("2027-02", "데이터 계층", "SQLD 준비")
    assert optional_for(sqld, "서버") == "SQLD 기출·요약 10~15분"


def _note(day, name):
    return NoteQuiz(f"Learning/개념/{day.isoformat()} {name}", THREE)


def test_review_takes_latest_note_at_least_three_or_seven_days_old():
    notes = [
        _note(date(2026, 11, 1), "가"),
        _note(date(2026, 11, 4), "나"),
        _note(date(2026, 11, 6), "다"),
        _note(date(2026, 11, 7), "라"),
    ]
    day = date(2026, 11, 9)  # 3일 전 이하 최근: 11-06 다, 7일 전 이하 최근: 11-01 가
    review = pick_review(day, notes, "")
    assert review.label in ("다", "가")
    assert review.days_ago == (day - date(2026, 11, 6 if review.label == "다" else 1)).days
    labels = {pick_review(date(2026, 11, 9) + timedelta(days=d), notes, "").label for d in (0, 1)}
    assert len(labels) == 2  # 날마다 돌려 가며 고른다


def test_review_skips_todays_note_and_young_notes():
    notes = [_note(date(2026, 11, 1), "가"), _note(date(2026, 11, 3), "나")]
    assert pick_review(date(2026, 11, 3), notes, "") is None  # 3일 지난 노트가 없다
    only = pick_review(date(2026, 11, 4), notes, "Learning/개념/2026-11-01 가")
    assert only is None


def test_review_question_rotates_by_day():
    notes = [_note(date(2026, 11, 1), "가")]
    prompts = {pick_review(date(2026, 11, d), notes, "").question.prompt for d in (4, 5, 6)}
    assert len(prompts) == 3


# 쓰기


def test_update_queue_marks_done_and_links_today_only():
    first = plan_daily(date(2026, 11, 1), None, parse_queue(QUEUE), NOV, 0)
    once = update_queue(QUEUE, first)
    assert "- [ ] 엣지-중앙 서버 구조: 장비와 서버의 역할 나누기 → [[" in once
    second = plan_daily(
        date(2026, 11, 2), LastStudy(first.target, first.label, True), parse_queue(once), NOV, 1
    )
    twice = update_queue(once, second)
    lines = twice.splitlines()
    assert lines[5].startswith("- [x] 엣지-중앙 서버 구조")
    assert lines[8].startswith("- [ ] 소리 이벤트 감지 기초: dB, EWMA → [[Learning/개념/2026-11-02")
    assert lines[9] == "- [ ] gRPC 스트리밍: 서버 스트림과 keepalive"
    assert twice.endswith("\n")


# 개념 노트 검사


def test_valid_note_has_no_problems():
    assert check_note(BODY) == []


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("# 제목\n\n" + BODY, "제목"),
        (BODY.replace("## 비유", "## 예시"), "빠진 섹션: ## 비유"),
        (BODY + "\n## 확인 문제\n\n### 1. 개념\n", "템플릿에 없는 섹션: ## 확인 문제"),
        (BODY + "\n`회사/플랫폼 지도.md` 참고\n", "회사 폴더"),
        (BODY + "\n## 내 말로 한 줄 (선택)\n", "템플릿에 없는 섹션"),
    ],
)
def test_note_problems_are_reported(body, problem):
    assert any(problem in found for found in check_note(body))


# 실행


@dataclass
class FakeAnswer:
    result: object
    error: str = ""
    status: str = "ok"


class FakeEngine:
    def __init__(self, note=None, review=None):
        self.note = note
        self.review = review
        self.calls = []

    async def ask_outside_task(self, scope, agent, stage, build_prompt, *, model, cwd, verify):
        prompt = build_prompt(_persona())
        self.calls.append((scope, agent, stage.value, prompt))
        answer = self.review if stage.value == "weekly_quiz" else self.note
        if answer is None:
            return FakeAnswer(None, "형식 오류")
        assert isinstance(answer, model)
        assert await verify(answer) == []
        return FakeAnswer(answer)


class FakeVault:
    def __init__(self):
        self.commits = []

    async def commit(self, root, paths, title, trailer):
        self.commits.append((list(paths), title))
        return True


def _persona():
    from bot.org import DEFAULT_ORG
    from bot.prompts import persona_for

    return persona_for(DEFAULT_ORG, "eta")


def make_vault(tmp_path):
    (tmp_path / "Schedule").mkdir(parents=True)
    (tmp_path / "Schedule" / "월별 목표 (2026-11 ~ 2027-10).md").write_text(GOALS, "utf-8")
    (tmp_path / "Schedule" / "학습 대기열.md").write_text(QUEUE, "utf-8")
    return tmp_path


NOTE = ConceptNote(body=BODY, questions=THREE, sources=["https://grpc.io/docs/"])


class Desk:
    """볼트와 DB를 유지하며 날짜를 바꿔 가며 교육 데스크를 돌린다."""

    def __init__(self, tmp_path):
        self.root = make_vault(tmp_path / "vault")
        self.db = tmp_path / "z.db"
        self.notes = []  # (text, mention)
        self.posts = []  # (quiz_id, title, numbers, total)
        self.dones = []  # (day, label)

    def call(self, day, work, engine=None, vault=None, hour=7):
        from bot.store import Repository, connect

        async def notify(text, mention):
            self.notes.append((text, mention))

        async def post_quiz(quiz_id, title, items, total):
            self.posts.append((quiz_id, title, [n for n, _ in items], total))

        async def post_done(day, label):
            self.dones.append((day, label))

        async def main():
            clock = FixedClock(datetime(day.year, day.month, day.day, hour, 30).astimezone())
            conn = await connect(self.db)
            try:
                desk = EducationDesk(
                    engine or FakeEngine(NOTE),
                    vault,
                    self.root,
                    "eta",
                    "Eta",
                    notify,
                    clock,
                    repo=Repository(conn, clock),
                    post_quiz=post_quiz,
                    post_done=post_done,
                )
                return await work(desk)
            finally:
                await conn.close()

        return asyncio.run(main())

    def daily(self, day, engine=None, vault=None):
        return self.call(day, lambda desk: desk.run_daily(), engine, vault)

    def done(self, day, pressed=None):
        return self.call(pressed or day, lambda desk: desk.mark_done(day.isoformat()), hour=21)

    def weekly(self, day, engine):
        return self.call(day, lambda desk: desk.run_weekly(), engine, hour=20)

    def answer(self, day, quiz_id, number, choice):
        return self.call(day, lambda desk: desk.answer(quiz_id, number, choice), hour=21)


def test_new_day_writes_note_and_queue_but_no_daily(tmp_path):
    desk, engine, vault = Desk(tmp_path), FakeEngine(NOTE), FakeVault()
    result = desk.daily(date(2026, 11, 2), engine, vault)

    assert result.created and result.label == "엣지-중앙 서버 구조"
    scope, agent, stage, prompt = engine.calls[0]
    assert (scope, agent, stage) == ("education", "eta", "concept_note")
    assert "주제: 엣지-중앙 서버 구조" in prompt and "구성 요소: 전체" in prompt
    text = (desk.root / "Learning/개념/2026-11-02 엣지-중앙 서버 구조.md").read_text("utf-8")
    assert text.startswith("# 엣지-중앙 서버 구조\n\n> 2026-11-02 · 이번 달 주제: ")
    assert text.rstrip().endswith("- **엣지:** 현장에 있는 장비")  # 학습 내용만
    assert "확인 문제" not in text
    assert not (desk.root / "Daily").exists()  # Daily는 비서가 저녁에 만든다
    assert "→ [[Learning/개념/2026-11-02 엣지-중앙 서버 구조|" in (
        desk.root / "Schedule/학습 대기열.md"
    ).read_text("utf-8")
    assert vault.commits == [
        (
            ["Learning/개념/2026-11-02 엣지-중앙 서버 구조.md", "Schedule/학습 대기열.md"],
            "docs(learning): 2026-11-02 엣지-중앙 서버 구조",
        )
    ]
    text, mention = desk.notes[0]
    assert mention is False and "**오늘 개념** (11-02, 새 개념)" in text
    assert "<https://grpc.io/docs/>" in text and "완료 버튼" in text
    assert desk.dones == [("2026-11-02", "엣지-중앙 서버 구조")]
    assert desk.posts == [(1, "오늘 문제", [1, 2, 3], 3)]


def test_not_done_is_carried_with_unanswered_questions_only(tmp_path):
    desk = Desk(tmp_path)
    desk.daily(date(2026, 11, 2))
    desk.answer(date(2026, 11, 2), 1, 2, "B")
    desk.posts.clear()
    engine = FakeEngine(NOTE)
    result = desk.daily(date(2026, 11, 3), engine)
    assert result.created and engine.calls == []
    assert "어제 것 이어서" in desk.notes[-1][0]
    assert desk.posts == [(1, "이어 하는 문제", [1, 3], 3)]
    assert desk.dones[-1] == ("2026-11-03", "엣지-중앙 서버 구조")


def test_done_button_moves_on_and_counts(tmp_path):
    desk = Desk(tmp_path)
    desk.daily(date(2026, 11, 2))
    desk.daily(date(2026, 11, 3))  # 이어 하기
    assert desk.done(date(2026, 11, 2), pressed=date(2026, 11, 3)).startswith(
        "'엣지-중앙 서버 구조'을 완료로 기록했다"
    )
    assert "이미 완료" in desk.done(date(2026, 11, 3))  # 같은 개념의 이어 한 날도 완료
    assert desk.done(date(2026, 11, 9)) == "그날 학습 기록이 없다."
    desk.daily(date(2026, 11, 4))
    assert "소리 이벤트 감지 기초" in desk.notes[-1][0] and "완료 1개" in desk.notes[-1][0]
    queue = (desk.root / "Schedule/학습 대기열.md").read_text("utf-8")
    assert "- [x] 엣지-중앙 서버 구조" in queue


def test_same_day_is_not_made_twice(tmp_path):
    desk = Desk(tmp_path)
    desk.daily(date(2026, 11, 2))
    engine = FakeEngine(NOTE)
    result = desk.daily(date(2026, 11, 2), engine)
    assert not result.created and engine.calls == []
    assert "이미 냈다" in desk.notes[-1][0] and desk.notes[-1][1] is True
    assert len(desk.dones) == 1


def test_agent_failure_writes_nothing_and_mentions(tmp_path):
    desk, vault = Desk(tmp_path), FakeVault()
    result = desk.daily(date(2026, 11, 2), FakeEngine(None), vault)
    assert not result.created and vault.commits == [] and desk.dones == []
    assert not (desk.root / "Learning").exists()
    assert desk.notes == [("Eta가 개념 노트를 쓰지 못했다.", True)]
    assert desk.daily(date(2026, 11, 2)).created  # 기록이 없으니 다시 낼 수 있다


def test_first_day_of_month_reports_last_month_facts(tmp_path):
    desk = Desk(tmp_path)
    desk.daily(date(2026, 10, 31))
    desk.done(date(2026, 10, 31))
    desk.daily(date(2026, 11, 1))
    text, mention = desk.notes[-1]
    assert mention is True
    assert "**2026-10 월말 확인**" in text and "완료한 개념: 1개" in text
    assert "완료 기준: 1 / 2" in text and "- [x] 운영 방식을 정했다" in text
    assert "오늘 개념" in text


def test_review_question_is_posted_from_note_three_days_old(tmp_path):
    desk = Desk(tmp_path)
    desk.daily(date(2026, 11, 2))
    for day in (2, 3):  # 11-04 노트는 끝내지 않아 11-05에 이어 한다
        desk.done(date(2026, 11, day))
        desk.daily(date(2026, 11, day + 1))
    desk.posts.clear()
    desk.daily(date(2026, 11, 5))
    review = [post for post in desk.posts if post[1].startswith("복습")]
    assert review == [(4, "복습 (3일 전 · 엣지-중앙 서버 구조)", [1], 1)]
    assert "복습 1개" in desk.notes[-1][0]


# 주간 보고와 주간 문제


REVIEW = WeeklyReview(
    comment=["gRPC는 이어 하는 중이다. 다음 주 월요일에 마무리한다."],
    questions=[_question(prompt=f"{n}번째 질문입니다") for n in range(1, 6)],
)


def _study(day, target, done):
    return {
        "day": date(2026, 11, day).isoformat(),
        "target": f"Learning/개념/{target}",
        "label": target,
        "done_at": "t" if done else None,
    }


def test_week_facts_counts_this_week_only():
    d = _study
    studies = [
        d(1, "지난주", True),  # 일요일: 지난주
        d(2, "엣지", False),
        d(3, "엣지", True),
        d(4, "소리", True),
        d(5, "gRPC", False),
    ]
    queue = parse_queue(QUEUE.replace("- [ ] 엣지-중앙", "- [x] 엣지-중앙"))
    facts = week_facts(date(2026, 11, 8), studies, queue)
    assert facts.start == date(2026, 11, 2) and facts.study_days == 4
    assert [(c.label, c.done) for c in facts.concepts] == [
        ("엣지", True),
        ("소리", True),
        ("gRPC", False),
    ]
    assert (facts.done, facts.month_done, facts.month_total) == (2, 1, 3)
    assert week_key(date(2026, 11, 8)) == "2026-W45"


def test_render_weekly_lists_facts_score_and_comment():
    facts = week_facts(
        date(2026, 11, 8),
        [_study(2, "엣지", True)],
        parse_queue(QUEUE),
    )
    text = render_weekly(facts, QuizScore(6, 5, 4), ["다음 주에 이어 간다"], "Eta · 교육팀장")
    assert text.splitlines()[:4] == [
        "**주간 학습 보고** (11-02 ~ 11-08)",
        "- 학습: 7일 중 1일 · 끝낸 개념 1개",
        "- 2026-11 대기열: 3개 중 0개 완료",
        "- 지난주 문제: 6문제 중 5개 풂, 4개 맞힘",
    ]
    assert "- [x] 엣지" in text and "**Eta · 교육팀장의 한마디**\n- 다음 주에 이어 간다" in text
    unanswered = render_weekly(facts, QuizScore(6, 0, 0), [], "Eta")
    assert "6문제, 풀지 않음" in unanswered and "한마디" not in unanswered


def test_quiz_question_and_answer_text():
    question = _question()
    assert render_question(question, "복습", 1, 1).splitlines()[0] == "**복습**"
    assert render_question(question, "주간 문제", 2, 6).splitlines() == [
        "**주간 문제 2/6**",
        "엣지와 중앙의 역할은?",
        "",
        "A. 장비가 모은다",
        "B. 장비가 판단한다",
        "C. 웹이 판단한다",
        "D. 아무도 안 한다",
    ]
    assert answer_text(question, "B", "B").startswith("정답이다 (B).")
    assert answer_text(question, "A", "A").startswith("오답이다. 고른 답 A, 정답 B.")
    assert answer_text(question, "B", "A").startswith("이미 A를 골랐다 (정답 B).")


def test_check_questions_reports_marked_or_repeated_options_and_private_folder():
    bad = QuizQuestion(
        prompt="질문입니다", options=["A. 하나", "둘", "둘", "넷"], answer="A", explanation="해설"
    )
    problems = check_questions([bad, *THREE], ["회사/ 폴더 참고"])
    assert any("겹친다" in p for p in problems)
    assert any("머리표" in p for p in problems)
    assert any("회사 폴더" in p for p in problems)
    assert check_questions(REVIEW.questions, REVIEW.comment) == []


def test_weekly_asks_eta_posts_quiz_and_scores_next_week(tmp_path):
    desk = Desk(tmp_path)
    desk.daily(date(2026, 11, 2))  # 오늘 문제 quiz 1
    desk.answer(date(2026, 11, 2), 1, 1, "B")
    desk.posts.clear()
    engine = FakeEngine(review=REVIEW)
    text = desk.weekly(date(2026, 11, 8), engine)

    scope, agent, stage, prompt = engine.calls[0]
    assert (scope, stage) == ("education", "weekly_quiz")
    assert "`Learning/개념/2026-11-02 엣지-중앙 서버 구조.md`" in prompt
    assert "끝낸 개념 0개" in prompt
    assert desk.posts == [(2, "주간 문제", [1, 2, 3, 4, 5], 5)]
    assert desk.notes[-1] == (text, False) and "Eta의 한마디" in text
    assert "이번 주 매일 문제(복습 포함): 3문제 중 1개 풂, 1개 맞힘" in text

    day = date(2026, 11, 8)
    first = desk.answer(day, 2, 1, "B")
    assert first.startswith("정답이다 (B).") and first.endswith("5문제 중 1개 풂, 1개 맞힘")
    assert desk.answer(day, 2, 1, "A").startswith("이미 B를 골랐다")
    wrong = desk.answer(day, 2, 2, "C")
    assert wrong.startswith("오답이다") and wrong.endswith("2개 풂, 1개 맞힘")
    assert desk.answer(day, 2, 9, "C") == "없는 문제다."

    desk.posts.clear()
    again = desk.weekly(day, FakeEngine(review=REVIEW))
    assert desk.posts == [] and again.endswith("이번 주 문제는 이미 냈다.")
    next_week = desk.weekly(date(2026, 11, 15), FakeEngine(review=REVIEW))
    assert "지난주 문제: 5문제 중 2개 풂, 1개 맞힘" in next_week


def test_weekly_without_concepts_skips_eta_and_failure_mentions(tmp_path):
    desk = Desk(tmp_path)
    engine = FakeEngine(review=REVIEW)
    text = desk.weekly(date(2026, 11, 8), engine)
    assert engine.calls == [] and "학습: 7일 중 0일" in text and desk.notes[-1][1] is False

    desk.daily(date(2026, 11, 2))
    desk.posts.clear()
    text = desk.weekly(date(2026, 11, 8), FakeEngine())
    assert desk.posts == [] and text.endswith("Eta가 주간 문제를 만들지 못했다.")
    assert desk.notes[-1][1] is True
