"""교육 부서: 매일 아침 개념 노트와 문제, 일요일 주간 보고와 주간 문제 (ADR-009).

오늘 개념을 정하는 규칙은 bot.education.daily가, 개념 노트 본문과 문제는 교육팀장이 맡는다.
볼트에 파일을 쓰고 커밋하는 주체는 봇이다. 아침에는 개념 노트(학습 내용만)와 대기열만 쓰고,
하루 정리(Daily 파일)는 비서 부서가 저녁에 만든다.
학습 기록(오늘 개념, 완료)은 DB에 둔다. 완료는 Zero가 #교육의 완료 버튼으로 표시한다.
문제(개념 노트 3문제, 복습 1문제, 주간 문제)도 DB에 두고 Discord에 버튼으로 올린다.
처음 고른 답만 남겨 주간 보고에 넣는다.
매달 1일에는 지난달 사실(완료 개수, 완료 기준)을 함께 알린다. 다음 달 주제는 Zero가 정한다.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from bot.agents.base import AgentName, Stage
from bot.clock import Clock
from bot.education.daily import (
    QUEUE_FILE,
    DailyError,
    DailyPlan,
    LastStudy,
    NoteQuiz,
    goals_file,
    month_criteria,
    note_header,
    optional_for,
    parse_month,
    parse_queue,
    pick_review,
    plan_daily,
    section_of,
    update_queue,
)
from bot.education.note import check_note, complete_note
from bot.education.quiz import answer_text, check_questions
from bot.education.weekly import (
    QuizScore,
    WeekFacts,
    render_weekly,
    week_facts,
    week_key,
    week_start,
)
from bot.prompts import concept_note_prompt, weekly_quiz_prompt
from bot.schemas import ConceptNote, QuizQuestion, WeeklyReview

log = logging.getLogger(__name__)

SCOPE = "education"  # 실행 기록의 project 값
COMMIT_TRAILER = "ZeroSync-Department: education"

Notify = Callable[[str, bool], Awaitable[None]]
# 문제를 버튼과 함께 게시한다 (quiz_id, 제목, [(번호, 문제)], 전체 문제 수). Discord 계층이 맡는다
PostQuiz = Callable[[int, str, list[tuple[int, QuizQuestion]], int], Awaitable[None]]
# 오늘 개념의 완료 버튼을 게시한다 (날짜, 개념 이름)
PostDone = Callable[[str, str], Awaitable[None]]
NOTE_KEY, REVIEW_KEY, WEEK_KEY = "note:", "review:", "week:"


@dataclass(frozen=True)
class QuizPost:
    quiz_id: int
    title: str
    items: list[tuple[int, QuizQuestion]]
    total: int


@dataclass(frozen=True)
class DailyResult:
    created: bool
    text: str  # #교육에 올린 안내
    posts: tuple[QuizPost, ...] = ()
    label: str = ""  # 오늘 개념 (완료 버튼에 쓴다)


class EducationDesk:
    def __init__(
        self,
        engine: Any,
        vault: Any,
        root: Path,
        agent: AgentName,
        display: str,
        notify: Notify,
        clock: Clock,
        *,
        repo: Any,
        post_quiz: PostQuiz | None = None,
        post_done: PostDone | None = None,
    ):
        self._engine = engine
        self._vault = vault
        self._root = root
        self._agent = agent
        self._display = display
        self._notify = notify
        self._clock = clock
        self._repo = repo
        self._post_quiz = post_quiz
        self._post_done = post_done

    async def run_daily(self) -> DailyResult:
        today = self._clock.now().astimezone().date()
        notes = []
        if today.day == 1:
            notes.append(await self.month_review(today - timedelta(days=1)))
        try:
            result = await self._daily(today)
        except DailyError as error:
            result = DailyResult(False, f"오늘 학습을 만들지 못했다: {error}")
        text = "\n\n".join([*notes, result.text])
        await self._notify(text, not result.created or bool(notes))
        if result.created and self._post_done is not None:
            await self._post_done(today.isoformat(), result.label)
        await self._post(result.posts)
        return DailyResult(result.created, text, result.posts, result.label)

    async def mark_done(self, day: str) -> str:
        """완료 버튼. 다음 날 아침에 대기열의 다음 개념으로 넘어간다."""
        marked = await self._repo.mark_study_done(day)
        if marked is None:
            return "그날 학습 기록이 없다."
        label, new = marked
        if not new:
            return f"'{label}'은 이미 완료로 기록했다."
        return f"'{label}'을 완료로 기록했다. 내일 아침에는 대기열의 다음 개념이 나온다."

    async def _daily(self, today: date) -> DailyResult:
        root = self._root
        studies = await self._repo.study_days(end=today.isoformat())
        if studies and studies[-1]["day"] == today.isoformat():
            return DailyResult(False, f"오늘 학습({today.isoformat()})은 이미 냈다.")
        last = studies[-1] if studies else None
        latest = (
            LastStudy(last["target"], last["label"], last["done_at"] is not None) if last else None
        )
        goals = goals_file(root)
        month = parse_month(_read(goals), f"{today:%Y-%m}")
        queue_text = _read(root / QUEUE_FILE)
        queue = parse_queue(queue_text)
        done_this_month = len(
            {
                row["target"]
                for row in studies
                if row["day"].startswith(f"{today:%Y-%m}") and row["done_at"] is not None
            }
        )
        plan = plan_daily(today, latest, queue, month, done_this_month)

        written = []
        note = root / plan.note_path
        sources: list[str] = []
        unverified: list[str] = []
        new_questions: list[QuizQuestion] = []
        if not note.exists():
            answer = await self._write_note(plan)
            if answer is None:
                return DailyResult(False, f"{self._display}가 개념 노트를 쓰지 못했다.")
            _write(note, complete_note(note_header(plan), answer.body))
            written.append(plan.note_path)
            sources, unverified = answer.sources, answer.unverified
            new_questions = list(answer.questions)
        optional = optional_for(month, section_of(queue, plan))
        updated = update_queue(queue_text, plan)
        if updated != queue_text:
            _write(root / QUEUE_FILE, updated)
            written.append(QUEUE_FILE)
        if self._vault is not None and written:
            await self._vault.commit(
                root, written, f"docs(learning): {today.isoformat()} {plan.label}", COMMIT_TRAILER
            )
        await self._repo.add_study_day(today.isoformat(), plan.target, plan.label, plan.carried)
        posts = await self._daily_quizzes(today, plan, new_questions)
        text = _announce(plan, optional, posts, sources, unverified)
        return DailyResult(True, text, posts, plan.label)

    async def _daily_quizzes(
        self, today: date, plan: DailyPlan, new_questions: list[QuizQuestion]
    ) -> tuple[QuizPost, ...]:
        """오늘 노트의 문제(새 노트면 저장, 이어 하면 아직 안 푼 것)와 복습 1문제."""
        repo = self._repo
        posts = []
        key = NOTE_KEY + plan.target
        if new_questions and await repo.quiz_by_key(key) is None:
            await repo.add_quiz("note", key, _payload(new_questions))
        quiz_id = await repo.quiz_by_key(key)
        if quiz_id is not None:
            questions = await self._load(quiz_id)
            done = await repo.answered_numbers(quiz_id)
            items = [(n, q) for n, q in enumerate(questions, start=1) if n not in done]
            title = "오늘 문제" if new_questions else "이어 하는 문제"
            if items:
                posts.append(QuizPost(quiz_id, title, items, len(questions)))
        notes = [
            NoteQuiz(stored_key[len(NOTE_KEY) :], _questions(payload))
            for _, stored_key, payload in await repo.list_quizzes("note")
        ]
        review = pick_review(today, notes, plan.target)
        review_key = REVIEW_KEY + today.isoformat()
        if review is not None and await repo.quiz_by_key(review_key) is None:
            review_id = await repo.add_quiz("review", review_key, _payload([review.question]))
            title = f"복습 ({review.days_ago}일 전 · {review.label})"
            posts.append(QuizPost(review_id, title, [(1, review.question)], 1))
        return tuple(posts)

    async def _post(self, posts: tuple[QuizPost, ...]) -> None:
        if self._post_quiz is None:
            return
        for post in posts:
            await self._post_quiz(post.quiz_id, post.title, post.items, post.total)

    async def _load(self, quiz_id: int) -> list[QuizQuestion]:
        stored = await self._repo.get_quiz(quiz_id)
        return _questions(stored[1]) if stored is not None else []

    async def _write_note(self, plan: DailyPlan) -> ConceptNote | None:
        item = plan.item
        assert item is not None or plan.carried

        async def verify(note: ConceptNote) -> list[str]:
            return check_note(note.body) + check_questions(note.questions)

        answer = await self._engine.ask_outside_task(
            SCOPE,
            self._agent,
            Stage.CONCEPT_NOTE,
            lambda persona: concept_note_prompt(
                persona,
                day=plan.day.isoformat(),
                label=plan.label,
                item=item.text if item is not None else plan.label,
                section=item.section if item is not None else "",
                month=plan.month.topic,
            ),
            model=ConceptNote,
            cwd=self._root,
            verify=verify,
        )
        if answer.result is None:
            log.warning("개념 노트 실패: %s", answer.error or answer.status)
        return answer.result

    async def run_weekly(self) -> str:
        """이번 주 사실을 세고, 개념이 있으면 교육팀장에게 주간 문제를 받아 함께 올린다."""
        today = self._clock.now().astimezone().date()
        root = self._root
        start = week_start(today)
        studies = await self._repo.study_days(start.isoformat(), today.isoformat())
        queue_path = root / QUEUE_FILE
        queue = parse_queue(_read(queue_path)) if queue_path.exists() else []
        facts = week_facts(today, studies, queue)
        last = await self._score(WEEK_KEY + week_key(today - timedelta(days=7)))
        daily = await self._daily_score(start, today)
        week = WEEK_KEY + week_key(today)
        review: WeeklyReview | None = None
        tail = ""
        if await self._repo.quiz_by_key(week) is not None:
            tail = "이번 주 문제는 이미 냈다."
        elif facts.concepts:
            review = await self._write_review(facts, last, daily)
            if review is None:
                tail = f"{self._display}가 주간 문제를 만들지 못했다."
        comment = review.comment if review else []
        text = render_weekly(facts, last, comment, self._display, daily=daily)
        if tail:
            text += f"\n\n{tail}"
        await self._notify(text, tail.endswith("못했다."))
        if review is not None:
            questions = list(review.questions)
            quiz_id = await self._repo.add_quiz("weekly", week, _payload(questions))
            items = list(enumerate(questions, start=1))
            await self._post((QuizPost(quiz_id, "주간 문제", items, len(questions)),))
        return text

    async def answer(self, quiz_id: int, number: int, choice: str) -> str:
        """Zero가 누른 버튼. 결과와 해설을 돌려준다 (Zero에게만 보인다)."""
        questions = await self._load(quiz_id)
        if not 1 <= number <= len(questions):
            return "없는 문제다."
        question = questions[number - 1]
        first = await self._repo.record_quiz_answer(
            quiz_id, number, choice, choice == question.answer
        )
        text = answer_text(question, choice, first)
        if len(questions) > 1:
            answered, correct = await self._repo.quiz_score(quiz_id)
            text += f"\n-# {len(questions)}문제 중 {answered}개 풂, {correct}개 맞힘"
        return text

    async def _score(self, key: str) -> QuizScore | None:
        quiz_id = await self._repo.quiz_by_key(key)
        if quiz_id is None:
            return None
        answered, correct = await self._repo.quiz_score(quiz_id)
        return QuizScore(len(await self._load(quiz_id)), answered, correct)

    async def _daily_score(self, start: date, end: date) -> QuizScore | None:
        """이번 주에 올린 개념 노트 문제와 복습 문제를 합친 점수."""
        total = answered = correct = 0
        for kind, prefix in (("note", NOTE_KEY), ("review", REVIEW_KEY)):
            for quiz_id, key, payload in await self._repo.list_quizzes(kind):
                made = _key_day(key[len(prefix) :])
                if made is None or not start <= made <= end:
                    continue
                total += len(_questions(payload))
                done, ok = await self._repo.quiz_score(quiz_id)
                answered, correct = answered + done, correct + ok
        return QuizScore(total, answered, correct) if total else None

    async def _write_review(
        self, facts: WeekFacts, last: QuizScore | None, daily: QuizScore | None
    ) -> WeeklyReview | None:
        summary = render_weekly(facts, last, [], self._display, daily=daily)

        async def verify(review: WeeklyReview) -> list[str]:
            return check_questions(review.questions, review.comment)

        answer = await self._engine.ask_outside_task(
            SCOPE,
            self._agent,
            Stage.WEEKLY_QUIZ,
            lambda persona: weekly_quiz_prompt(
                persona, facts=summary, notes=[c.target for c in facts.concepts]
            ),
            model=WeeklyReview,
            cwd=self._root,
            verify=verify,
        )
        if answer.result is None:
            log.warning("주간 문제 실패: %s", answer.error or answer.status)
        return answer.result

    async def month_review(self, last_day: date) -> str:
        """지난달 사실만 정리한다 (볼트 AGENTS.md "월말" 1·2). 다음 달 주제는 Zero가 정한다."""
        key = f"{last_day:%Y-%m}"
        studies = await self._repo.study_days(f"{key}-01", last_day.isoformat())
        done = len({row["target"] for row in studies if row["done_at"] is not None})
        try:
            criteria = month_criteria(_read(goals_file(self._root)), key)
        except DailyError:
            criteria = []
        lines = [f"**{key} 월말 확인**", f"- 완료한 개념: {done}개"]
        if criteria:
            met = [text for text, checked in criteria if checked]
            lines.append(f"- 완료 기준: {len(met)} / {len(criteria)}")
            lines += [f"  - [{'x' if checked else ' '}] {text}" for text, checked in criteria]
        lines.append(
            "다음 달 주제를 유지할지 바꿀지 정해 주세요. "
            "월별 목표의 월말 확인과 다음 달 대기열은 답을 받은 뒤에 채운다."
        )
        return "\n".join(lines)


def _announce(
    plan: DailyPlan,
    optional: str,
    posts: tuple[QuizPost, ...],
    sources: list[str],
    unverified: list[str],
) -> str:
    kind = "어제 것 이어서" if plan.carried else "새 개념"
    lines = [
        f"**오늘 개념** ({plan.day:%m-%d}, {kind}): {plan.label} — 20~30분",
        f"- 이번 달 {plan.month.topic} · 완료 {plan.done_this_month}개",
        f"- 여유 있으면: {optional}",
        f"- 노트: `{plan.note_path}`",
    ]
    if posts:
        parts = [f"{post.title.split(' (')[0]} {len(post.items)}개" for post in posts]
        lines.append(f"- 문제: {', '.join(parts)}. 노트를 읽고 아래 버튼으로 푼다")
    lines.append("- 다 읽었으면 아래 완료 버튼을 누른다. 누르지 않으면 내일 이어 한다")
    if sources:
        lines.append("- 확인한 자료: " + ", ".join(f"<{url}>" for url in sources))
    if unverified:
        lines.append("- 확인하지 못함: " + "; ".join(unverified))
    return "\n".join(lines)


def _payload(questions: list[QuizQuestion]) -> str:
    return json.dumps([q.model_dump() for q in questions], ensure_ascii=False)


def _key_day(text: str) -> date | None:
    """`2026-11-02` 또는 `Learning/개념/2026-11-02 주제`의 날짜."""
    name = text.rsplit("/", 1)[-1][:10]
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def _questions(payload: str) -> list[QuizQuestion]:
    return [QuizQuestion.model_validate(item) for item in json.loads(payload)]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
