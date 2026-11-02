"""교육 주간 보고의 결정론적 규칙 (ADR-009).

이번 주(월~오늘) 학습 기록(DB)과 학습 대기열에서 사실만 센다. 다음 주를 위한 한마디와
주간 문제는 교육팀장이 쓰고, 숫자와 목록은 여기서 만든다.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from bot.education.daily import QueueItem


@dataclass(frozen=True)
class WeekConcept:
    target: str
    label: str
    done: bool


@dataclass(frozen=True)
class WeekFacts:
    start: date  # 이번 주 월요일
    end: date  # 보고하는 날
    study_days: int  # 학습한 날 수 (아침에 개념을 낸 날)
    concepts: list[WeekConcept]  # 이번 주에 다룬 개념, 처음 나온 순서
    month: str
    month_done: int  # 이번 달 대기열에서 끝낸 항목
    month_total: int

    @property
    def done(self) -> int:
        return sum(1 for concept in self.concepts if concept.done)


@dataclass(frozen=True)
class QuizScore:
    total: int
    answered: int
    correct: int


def week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_facts(day: date, studies: list[dict[str, Any]], queue: list[QueueItem]) -> WeekFacts:
    """studies는 study_days 행(day, target, label, done_at). 이번 주 월요일~day만 센다."""
    start = week_start(day)
    concepts: dict[str, WeekConcept] = {}
    days = 0
    for row in sorted(studies, key=lambda r: r["day"]):
        if not start <= date.fromisoformat(row["day"]) <= day:
            continue
        days += 1
        seen = concepts.get(row["target"])
        done = row["done_at"] is not None or (seen is not None and seen.done)
        concepts[row["target"]] = WeekConcept(row["target"], row["label"], done)
    month = f"{day:%Y-%m}"
    items = [q for q in queue if q.month == month]
    return WeekFacts(
        start=start,
        end=day,
        study_days=days,
        concepts=list(concepts.values()),
        month=month,
        month_done=sum(1 for q in items if q.done),
        month_total=len(items),
    )


def render_weekly(
    facts: WeekFacts,
    last_quiz: QuizScore | None,
    comment: list[str],
    display: str,
    *,
    daily: QuizScore | None = None,
) -> str:
    total_days = (facts.end - facts.start).days + 1
    lines = [
        f"**주간 학습 보고** ({facts.start:%m-%d} ~ {facts.end:%m-%d})",
        f"- 학습: {total_days}일 중 {facts.study_days}일 · 끝낸 개념 {facts.done}개",
    ]
    if facts.month_total:
        lines.append(f"- {facts.month} 대기열: {facts.month_total}개 중 {facts.month_done}개 완료")
    if daily is not None:
        lines.append(
            f"- 이번 주 매일 문제(복습 포함): {daily.total}문제 중 {daily.answered}개 풂, "
            f"{daily.correct}개 맞힘"
        )
    if last_quiz is not None:
        if last_quiz.answered:
            lines.append(
                f"- 지난주 문제: {last_quiz.total}문제 중 {last_quiz.answered}개 풂, "
                f"{last_quiz.correct}개 맞힘"
            )
        else:
            lines.append(f"- 지난주 문제: {last_quiz.total}문제, 풀지 않음")
    if facts.concepts:
        lines += ["", "**이번 주 개념**"]
        for concept in facts.concepts:
            state = "x" if concept.done else " "
            rest = "" if concept.done else " (이어 하는 중)"
            lines.append(f"- [{state}] {concept.label}{rest}")
    if comment:
        lines += ["", f"**{display}의 한마디**", *(f"- {line}" for line in comment)]
    return "\n".join(lines)
