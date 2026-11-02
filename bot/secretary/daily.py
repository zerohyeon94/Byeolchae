"""비서 저녁 Daily: 하루 사실과 볼트·Discord 표시 (ADR-010).

숫자와 사실(학습, 제품 부서의 단계 변화와 PR, 결정할 것, 토큰)은 봇이 DB와 로컬 기록에서 센다.
비서는 그 사실을 읽고 요약 1~3줄만 쓴다. 표시는 순수 함수로 두고 단위 테스트로 고정한다.
"""

from dataclasses import dataclass
from datetime import date, datetime

from bot.secretary.tokens import EngineUsage, tokens_text

DAILY_DIR = "Daily"
WEEKDAYS = "월화수목금토일"


def daily_path(day: date) -> str:
    """`Daily/YYYY-MM/YYYY-MM-DD.md` (볼트의 기존 Daily 위치)."""
    return f"{DAILY_DIR}/{day:%Y-%m}/{day.isoformat()}.md"


@dataclass(frozen=True)
class Score:
    total: int  # 문제 수
    answered: int
    correct: int

    def text(self) -> str:
        if not self.answered:
            return f"{self.total}문제 중 안 풂"
        return f"{self.total}문제 중 {self.answered}문제 풂, {self.correct}개 정답"


@dataclass(frozen=True)
class StudyFact:
    target: str  # 개념 노트 링크 대상
    label: str
    carried: bool
    done: bool
    note: Score | None  # 오늘 개념의 문제
    review: Score | None  # 오늘 복습 1문제


@dataclass(frozen=True)
class TaskEvent:
    """제품 부서 작업의 단계 변화(from→to) 또는 PR 열림·merge."""

    at: datetime
    project: str
    task_id: int
    title: str
    change: str  # "PLANNING → AWAIT_PLAN_APPROVAL", "PR #12 열림", "PR #12 merge"


@dataclass(frozen=True)
class DayFacts:
    day: date
    study: StudyFact | None
    transitions: list[TaskEvent]
    prs: list[TaskEvent]
    decisions: list[str]  # "- ..." 없이 한 줄씩
    tokens: list[EngineUsage]


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}%"


def _limit(usage: EngineUsage) -> str:
    if usage.limit is None:
        return "기록 없음"
    today = usage.limit.today
    added = "" if today is None else f" (오늘 +{today:.0f}%p)"
    return f"{usage.limit.weekly:.0f}%{added}"


def _study_lines(study: StudyFact | None) -> list[str]:
    if study is None:
        return ["- 오늘 학습 기록 없음"]
    kind = "어제 것 이어서" if study.carried else "새 개념"
    lines = [
        f"- 개념: [[{study.target}|{study.label}]] ({kind}) · {'완료' if study.done else '아직'}"
    ]
    if study.note is not None:
        lines.append(f"- 오늘 문제: {study.note.text()}")
    if study.review is not None:
        lines.append(f"- 복습: {study.review.text()}")
    return lines


def _event_line(event: TaskEvent) -> str:
    return (
        f"- {event.at.astimezone():%H:%M} [{event.project}] #{event.task_id} {event.title}: "
        f"{event.change}"
    )


def _token_lines(tokens: list[EngineUsage]) -> list[str]:
    lines = [
        "| 엔진 | 봇 | Zero | 합계 | 이번 주 중 오늘 | 구독 주간 한도 |",
        "|---|---|---|---|---|---|",
    ]
    for usage in tokens:
        lines.append(
            f"| {usage.engine.capitalize()} | {tokens_text(usage.bot)} | "
            f"{tokens_text(usage.zero)} | {tokens_text(usage.total)} | "
            f"{_percent(usage.week_share)} | {_limit(usage)} |"
        )
    return lines


def render_facts(facts: DayFacts) -> str:
    """요약을 뺀 Daily 본문. 비서 프롬프트에도 그대로 넣는다."""
    lines = ["## 결정할 것", ""]
    lines += [f"- {line}" for line in facts.decisions] or ["- 없음"]
    lines += ["", "## 학습", ""]
    lines += _study_lines(facts.study)
    lines += ["", "## 제품 부서", ""]
    lines.append(f"단계 변화 {len(facts.transitions)}건")
    lines += [_event_line(event) for event in facts.transitions]
    lines += ["", f"PR {len(facts.prs)}건"]
    lines += [_event_line(event) for event in facts.prs]
    lines += ["", "## 토큰", ""]
    lines += _token_lines(facts.tokens)
    lines += [
        "",
        "- 봇: ZeroSync 봇이 부른 것 (Claude `claude -p`, Codex 실행 로그)",
        "- Zero: 데스크톱 앱·터미널 등 직접 쓴 것 (모든 저장소)",
        "- 이번 주 중 오늘: 월요일부터 오늘까지 합계에서 오늘이 차지하는 비율",
        "- 구독 주간 한도: 계정의 주간 한도 사용 비율 (Claude는 봇 실행 로그, Codex는 세션 기록)",
    ]
    return "\n".join(lines)


def render_daily(facts: DayFacts, summary: list[str] | None, *, display: str) -> str:
    day = facts.day
    head = [
        f"# {day.isoformat()} ({WEEKDAYS[day.weekday()]}) 하루 정리",
        "",
        f"> 비서 {display}가 정리했다. 숫자는 봇이 센 것이다.",
        "",
        "## 요약",
        "",
    ]
    head += [f"- {line}" for line in summary] if summary else ["- (요약을 만들지 못했다)"]
    return "\n".join(head) + "\n\n" + render_facts(facts) + "\n"


def render_discord(facts: DayFacts, summary: list[str] | None, path: str) -> str:
    """#비서에 올리는 짧은 저녁 보고."""
    lines = [f"**하루 정리** ({facts.day:%m-%d})"]
    lines += [f"> {line}" for line in summary or []]
    lines.append(f"- 결정할 것: {len(facts.decisions)}건")
    study = facts.study
    if study is None:
        lines.append("- 학습: 기록 없음")
    else:
        score = f", {study.note.text()}" if study.note is not None else ""
        lines.append(f"- 학습: {study.label} · {'완료' if study.done else '아직'}{score}")
    lines.append(f"- 제품: 단계 변화 {len(facts.transitions)}건, PR {len(facts.prs)}건")
    for usage in facts.tokens:
        lines.append(
            f"- {usage.engine.capitalize()}: {tokens_text(usage.total)} "
            f"(이번 주 중 {_percent(usage.week_share)}, 주간 한도 {_limit(usage)})"
        )
    lines.append(f"Daily: `{path}`")
    return "\n".join(lines)
