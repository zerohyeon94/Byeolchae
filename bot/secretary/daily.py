"""비서 저녁 Daily: 하루 사실과 볼트·Discord 표시 (ADR-010).

숫자와 사실(이번 달 진도, 학습, 제품 부서의 단계 변화와 PR, 토큰, 결정할 것)은 봇이 DB와
로컬 기록, 볼트의 대기열·월별 목표·커리큘럼에서 센다. 비서는 그 사실을 읽고 한 줄 요약만 쓴다.
모양은 볼트의 `Templates/Daily.md`를 따른다: 머리말 → 오늘 학습 → 오늘 한 일 → 토큰 →
이번 주 → 결정할 것 → 한 줄 요약. 이번 주는 월요일부터 그날까지의 누적이다 (2026-11-04 Zero 결정).
iPad에서 위아래로 읽히게 표를 쓰지 않는다.
아침에는 교육 노트가 나온 뒤 같은 모양의 아침판(오늘 학습만, 나머지는 저녁에 채움)을 먼저 쓴다.
Obsidian에서 오늘의 Daily를 열면 개념 노트로 바로 갈 수 있게 하려는 것이다 (2026-11-03 Zero 결정).
표시는 순수 함수로 두고 단위 테스트로 고정한다.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from bot.secretary.tokens import EngineUsage, tokens_text

DAILY_DIR = "Daily"
CURRICULUM_FILE = "Schedule/교육 커리큘럼.md"
WEEKDAYS = "월화수목금토일"
# 아침판에서 저녁에 채울 자리. 저녁에는 이 문구가 있는 Daily만 다시 쓴다
EVENING_PENDING = "(저녁 23:30에 비서가 채운다)"
_CURRICULUM_HEADING = re.compile(r"^###\s+([A-Z]+-\d+)\s+(.+?)\s*$")


def daily_path(day: date) -> str:
    """`Daily/YYYY-MM/YYYY-MM-DD.md` (볼트의 기존 Daily 위치)."""
    return f"{DAILY_DIR}/{day:%Y-%m}/{day.isoformat()}.md"


def curriculum_id(curriculum: str, label: str) -> str:
    """커리큘럼의 `### SYS-01 주제` 제목에서 주제가 label인 ID. 없으면 ""."""
    for line in curriculum.splitlines():
        match = _CURRICULUM_HEADING.match(line.strip())
        if match and match.group(2) == label:
            return match.group(1)
    return ""


@dataclass(frozen=True)
class Score:
    total: int  # 문제 수
    answered: int
    correct: int


@dataclass(frozen=True)
class MonthLine:
    """머리말: 이번 달 주제와 대기열 진도."""

    goals: str  # 월별 목표 링크 대상 (Schedule/월별 목표 (2026-11 ~ 2027-10))
    topic: str
    done: int  # 이번 달 대기열의 완료 항목
    left: int  # 이번 달 대기열의 남은 항목


@dataclass(frozen=True)
class StudyFact:
    target: str  # 개념 노트 링크 대상
    label: str
    carried: bool
    done: bool
    note: Score | None  # 오늘 개념의 문제
    review: Score | None  # 오늘 복습 1문제
    concept_id: str = ""  # 커리큘럼 ID (SYS-01). 못 찾으면 ""
    optional: str = ""  # 그날 선택 항목
    optional_done: bool | None = None  # 선택 했음 버튼. 기록이 없으면(v10 전) None


@dataclass(frozen=True)
class TaskEvent:
    """제품 부서 작업의 단계 변화(from→to) 또는 PR 열림·merge."""

    at: datetime
    project: str
    task_id: int
    title: str
    change: str  # "PLANNING → AWAIT_PLAN_APPROVAL", "PR #12 열림", "PR #12 merge"


@dataclass(frozen=True)
class WeekLine:
    """이번 주(월요일~그날) 누적. 토큰 합계는 DayFacts.tokens의 week를 쓴다."""

    start: date  # 이번 주 월요일
    end: date  # 그날
    study_days: int  # 아침에 개념을 낸 날 수
    done: int  # 이번 주에 끝낸 개념 수
    quiz: Score | None  # 이번 주에 올린 개념 노트 문제와 복습 문제의 합. 없으면 None
    optional_days: int = 0  # 선택 항목이 있던 날 (v10 뒤 기록만)
    optional_done: int = 0  # 그중 '선택 했음'을 누른 날


@dataclass(frozen=True)
class DayFacts:
    day: date
    study: StudyFact | None
    transitions: list[TaskEvent]
    prs: list[TaskEvent]
    decisions: list[str]  # "- ..." 없이 한 줄씩
    tokens: list[EngineUsage]
    month: MonthLine | None = None  # 볼트 파일을 못 읽으면 None
    week: WeekLine | None = None


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}%"


def _limit(usage: EngineUsage) -> str:
    if usage.limit is None:
        return "기록 없음"
    today = usage.limit.today
    added = "" if today is None else f" (오늘 +{today:.0f}%p)"
    return f"{usage.limit.weekly:.0f}%{added}"


def _today_score(score: Score | None) -> str:
    if score is None:
        return "기록 없음"
    left = score.total - score.answered
    return f"오늘 {score.total}개 중 {score.correct}개 맞힘 · 아직 안 푼 문제 {left}개"


def _review_score(score: Score | None) -> str:
    if score is None:
        return "없음"
    if not score.answered:
        return "안 풂"
    return "맞힘" if score.correct else "틀림"


def _month_line(month: MonthLine | None) -> list[str]:
    if month is None:
        return []
    return [
        f"> [[{month.goals}|이번 달]]: {month.topic} · 이번 달 완료 {month.done}개 · "
        f"대기열 남은 {month.left}개",
        "",
    ]


def _study_lines(study: StudyFact | None) -> list[str]:
    if study is None:
        return ["- 오늘 학습 기록 없음"]
    concept = f"[[{study.target}|{study.label}]]"
    if study.concept_id:
        concept += f" ({study.concept_id})"
    lines = [
        f"- **개념:** {concept} — {'완료' if study.done else '내일 이어 하기'}",
        f"- **문제:** {_today_score(study.note)}",
        f"- **복습:** {_review_score(study.review)}",
    ]
    if study.optional:
        state = {True: "했음", False: "안 함", None: "기록 없음"}[study.optional_done]
        lines.append(f"- **선택:** {study.optional} — {state}")
    return lines


def _work_lines(facts: DayFacts) -> list[str]:
    events = sorted([*facts.transitions, *facts.prs], key=lambda event: event.at)
    if not events:
        return ["- 제품 부서 작업 변화 없음"]
    return [
        f"- {e.at.astimezone():%H:%M} [{e.project}] #{e.task_id} {e.title}: {e.change}"
        for e in events
    ]


def _engines(tokens: list[EngineUsage], value) -> str:
    return " · ".join(f"{usage.engine.capitalize()} {value(usage)}" for usage in tokens)


def _token_lines(tokens: list[EngineUsage]) -> list[str]:
    return [
        f"- **봇:** {_engines(tokens, lambda u: tokens_text(u.bot))}",
        f"- **나:** {_engines(tokens, lambda u: tokens_text(u.zero))}",
        f"- **이번 주 중 오늘:** {_engines(tokens, lambda u: _percent(u.week_share))}",
        f"- **구독 주간 한도:** {_engines(tokens, _limit)}",
    ]


def _day(day: date) -> str:
    return f"{day:%m-%d} ({WEEKDAYS[day.weekday()]})"


def _week_lines(week: WeekLine | None, tokens: list[EngineUsage]) -> list[str]:
    if week is None:
        return ["- 이번 주 기록 없음"]
    days = (week.end - week.start).days + 1
    quiz = week.quiz
    if quiz is None:
        score = "기록 없음"
    else:
        left = quiz.total - quiz.answered
        score = f"{quiz.total}개 중 {quiz.correct}개 맞힘 · 안 푼 문제 {left}개"
    lines = [
        f"- **기간:** {_day(week.start)} ~ {_day(week.end)}",
        f"- **학습:** {days}일 중 {week.study_days}일 · 끝낸 개념 {week.done}개",
        f"- **문제 (복습 포함):** {score}",
    ]
    if week.optional_days:
        lines.append(f"- **선택:** {week.optional_days}일 중 {week.optional_done}일 했음")
    if tokens:
        lines.append(f"- **토큰:** {_engines(tokens, lambda u: tokens_text(u.week))}")
    return lines


def render_facts(facts: DayFacts) -> str:
    """제목·요약을 뺀 Daily 본문 (머리말 → 오늘 학습 → 오늘 한 일 → 토큰 → 이번 주 → 결정할 것).

    비서 프롬프트에도 그대로 넣는다. 결정할 것이 없으면 그 절은 비운다.
    """
    lines = _month_line(facts.month)
    lines += ["## 오늘 학습", ""]
    lines += _study_lines(facts.study)
    lines += ["", "## 오늘 한 일", ""]
    lines += _work_lines(facts)
    lines += ["", "## 토큰", ""]
    lines += _token_lines(facts.tokens)
    lines += ["", "## 이번 주", ""]
    lines += _week_lines(facts.week, facts.tokens)
    lines += ["", "## 결정할 것", ""]
    lines += [f"- {line}" for line in facts.decisions]
    return "\n".join(lines).rstrip()


def _morning_study_lines(study: StudyFact) -> list[str]:
    concept = f"[[{study.target}|{study.label}]]"
    if study.concept_id:
        concept += f" ({study.concept_id})"
    kind = "어제 것 이어서" if study.carried else "새 개념"
    problems = f"오늘 {study.note.total}개" if study.note is not None else "없음"
    lines = [
        f"- **개념:** {concept} — 오늘 읽기 ({kind})",
        f"- **문제:** {problems} · #교육에서 푼다",
        f"- **복습:** {'1개' if study.review is not None else '없음'}",
    ]
    if study.optional:
        lines.append(f"- **선택:** {study.optional}")
    return lines


def render_morning(facts: DayFacts) -> str:
    """아침판 Daily. 오늘 학습과 개념 노트 링크만 쓰고, 나머지 절은 저녁에 채운다."""
    day = facts.day
    lines = [f"# {day.isoformat()} ({WEEKDAYS[day.weekday()]})", ""]
    lines += _month_line(facts.month)
    lines += ["## 오늘 학습", ""]
    lines += _morning_study_lines(facts.study) if facts.study else ["- 오늘 학습 기록 없음"]
    for section in ("오늘 한 일", "토큰", "이번 주", "결정할 것", "한 줄 요약"):
        lines += ["", f"## {section}", "", EVENING_PENDING]
    return "\n".join(lines) + "\n"


def is_morning(text: str) -> bool:
    """저녁에 다시 써도 되는 Daily인가: 아침판이거나, Obsidian이 먼저 만든 빈 파일."""
    return not text.strip() or EVENING_PENDING in text


def render_daily(facts: DayFacts, summary: str | None) -> str:
    day = facts.day
    title = f"# {day.isoformat()} ({WEEKDAYS[day.weekday()]})\n\n"
    tail = summary or "(요약을 만들지 못했다)"
    return title + render_facts(facts) + f"\n\n## 한 줄 요약\n\n{tail}\n"


def render_discord(facts: DayFacts, summary: str | None, path: str) -> str:
    """#비서에 올리는 짧은 저녁 보고."""
    lines = [f"**하루 정리** ({facts.day:%m-%d})"]
    if summary:
        lines.append(f"> {summary}")
    lines.append(f"- 결정할 것: {len(facts.decisions)}건")
    study = facts.study
    if study is None:
        lines.append("- 학습: 기록 없음")
    else:
        state = "완료" if study.done else "내일 이어 하기"
        lines.append(f"- 학습: {study.label} · {state} · {_today_score(study.note)}")
    lines.append(f"- 제품: 단계 변화 {len(facts.transitions)}건, PR {len(facts.prs)}건")
    for usage in facts.tokens:
        lines.append(
            f"- {usage.engine.capitalize()}: {tokens_text(usage.total)} "
            f"(이번 주 중 {_percent(usage.week_share)}, 주간 한도 {_limit(usage)})"
        )
    lines.append(f"Daily: `{path}`")
    return "\n".join(lines)
