"""비서 부서: 매일 저녁 하루 정리(Daily)와 짧은 아침 보고 (ADR-010).

저녁에는 그날의 사실을 봇이 모은다.
- 머리말: 볼트의 월별 목표(이번 달 주제)와 학습 대기열(이번 달 완료·남은 개수)
- 학습: DB의 학습 기록과 문제 점수, 커리큘럼 ID, 그날 선택 항목
- 제품 부서: 작업 단계 변화와 PR 열림·merge
- 결정할 것: 승인 대기 작업과 열린 채용 제안
- 토큰: Claude·Codex 로컬 기록 (봇과 Zero, 모든 저장소)과 봇 실행 로그의 구독 한도
- 이번 주: 월요일~그날의 학습한 날·끝낸 개념(교육 주간 보고와 같은 셈), 문제 점수, 선택 했음

비서는 그 사실로 한 줄 요약만 쓴다. 볼트에 Daily 파일을 쓰고 커밋하는 주체는 봇이다.
요약을 받지 못해도 사실만으로 Daily를 쓴다.
아침에는 교육 노트가 나온 뒤 아침판 Daily(오늘 학습 `· 0/m`,
어제까지의 이번 주)를 먼저 쓴다. 에이전트는 부르지 않는다.
낮에는 교육 버튼을 누를 때마다 가장 최근 아침판 Daily의
오늘 학습과 이번 주 줄만 DB 기준으로 다시 쓴다 (refresh_daily, 커밋하지 않는다).
저녁에는 아침판(또는 Obsidian이 만든 빈 파일)을 하루 정리로
다시 쓴다. 저녁판이 이미 있으면 다시 쓰지 않는다.
저녁 Daily를 커밋한 뒤 볼트를 GitHub에 한 번 올린다.
"""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from bot.agents.base import AgentName, Stage
from bot.clock import to_iso
from bot.education.daily import (
    QUEUE_FILE,
    DailyError,
    goals_file,
    optional_for,
    parse_month,
    parse_queue,
    weekly_goal,
)
from bot.education.weekly import week_facts, week_start
from bot.org import Org
from bot.prompts import secretary_daily_prompt
from bot.reports import pending_decisions
from bot.schemas import DailySummary
from bot.secretary.daily import (
    CURRICULUM_FILE,
    DayFacts,
    MonthLine,
    Score,
    StudyFact,
    TaskEvent,
    WeekLine,
    curriculum_id,
    daily_path,
    day_score,
    is_morning,
    render_daily,
    render_discord,
    render_facts,
    render_morning,
    study_lines,
    update_daily,
    week_lines,
)
from bot.secretary.tokens import (
    EngineUsage,
    Limit,
    LimitSample,
    Usage,
    claude_run_limit,
    claude_usage,
    codex_run_tokens,
    codex_sessions,
    engine_usage,
    weekly_limit,
)
from bot.store import EventKind
from bot.workflow.education import daily_quiz_score, studied_since

log = logging.getLogger(__name__)

SCOPE = "secretary"  # 실행 기록의 project 값
COMMIT_TRAILER = "ZeroSync-Department: secretary"
NOTE_KEY, REVIEW_KEY = "note:", "review:"  # 교육 문제 키 (bot.workflow.education)

Notify = Callable[[str, bool], Awaitable[None]]


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """이 맥의 시간대로 그날 0시와 다음 날 0시."""
    start = datetime.combine(day, time()).astimezone()
    return start, datetime.combine(day + timedelta(days=1), time()).astimezone()


class SecretaryDesk:
    def __init__(
        self,
        engine: Any,
        vault: Any,
        root: Path,
        agent: AgentName,
        notify: Notify,
        *,
        repo: Any,
        org: Org,
        claude_dir: Path,
        codex_dir: Path,
        workdir: Path,
    ):
        self._engine = engine
        self._vault = vault
        self._root = root
        self._agent = agent
        self._notify = notify
        self._repo = repo
        self._org = org
        self._claude_dir = claude_dir
        self._codex_dir = codex_dir
        self._workdir = workdir  # 비서 실행 폴더. 비서는 파일을 읽지 않으므로 빈 폴더다

    async def run_morning(self, day: date) -> str:
        """교육 노트가 나온 뒤 아침판 Daily를 쓴다. 저녁판이 이미 있으면 두고 경로를 돌려준다."""
        path = daily_path(day)
        target = self._root / path
        if target.exists() and not is_morning(target.read_text(encoding="utf-8")):
            return path
        facts = DayFacts(
            day=day,
            study=await self._study(day),
            transitions=[],
            prs=[],
            decisions=[],
            tokens=[],
            month=self._month(day),
            week=await self._week(day, day - timedelta(days=1)),  # 어제까지
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_morning(facts), encoding="utf-8")
        if self._vault is not None:
            await self._vault.commit(
                self._root, [path], f"docs(daily): {day.isoformat()} 아침", COMMIT_TRAILER
            )
        return path

    async def run_evening(self, day: date) -> str:
        """그날 Daily를 쓰고 #비서에 짧게 알린다. 알린 문구를 돌려준다.

        아침판이나 빈 파일은 하루 정리로 다시 쓴다. 저녁판이 이미 있으면 다시 쓰지 않는다.
        """
        path = daily_path(day)
        target = self._root / path
        if target.exists() and not is_morning(target.read_text(encoding="utf-8")):
            text = f"{day.isoformat()} 하루 정리는 이미 있다: `{path}`"
            await self._notify(text, False)
            return text
        facts = await self.facts(day)
        summary = await self._summary(facts)
        # 비서 호출 자신의 한도 줄까지 넣는다. 그날 Zero만 Claude를 썼어도 값이 생긴다
        facts = await self._with_claude_limit(facts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_daily(facts, summary), encoding="utf-8")
        text = render_discord(facts, summary, path)
        if self._vault is not None:
            await self._vault.commit(
                self._root, [path], f"docs(daily): {day.isoformat()} 하루 정리", COMMIT_TRAILER
            )
            # 하루의 마지막 볼트 쓰기 뒤에 한 번 GitHub에 올린다 (그날 노트·그림·Daily)
            text += "\n" + _push_line(await self._vault.push(self._root))
        await self._notify(text, False)
        return text

    async def facts(self, day: date) -> DayFacts:
        start, end = day_bounds(day)
        span = (to_iso(start), to_iso(end))
        return DayFacts(
            day=day,
            study=await self._study(day),
            transitions=[
                _event(row, f"{row['from_state']} → {row['to_state']}")
                for row in await self._repo.transitions_between(*span)
            ],
            prs=[
                _event(row, _pr_change(row))
                for row in await self._repo.events_between(
                    {EventKind.PR_OPENED, EventKind.MERGE}, *span
                )
            ],
            decisions=await pending_decisions(self._repo, self._org, end, lambda _: None),
            tokens=await self._tokens(start, end),
            month=self._month(day),
            week=await self._week(day, day),
        )

    async def refresh_daily(self) -> bool:
        """낮 갱신: 가장 최근 아침판 Daily의 오늘 학습과 이번 주 줄을 DB 기준으로 다시 쓴다.

        교육 버튼("다 읽었다", 문제 A~D, "선택 했음")을 누른 뒤 부른다. 저녁판 뒤에 눌렀으면
        저녁판의 말투로 그 줄만 고친다. 커밋하지 않는다 (다음 저녁판·아침판 커밋에 담긴다).
        파일이나 줄이 없으면 아무것도 하지 않는다. 바꿨으면 True.
        """
        rows = await self._repo.study_days()
        if not rows:
            return False
        day = date.fromisoformat(rows[-1]["day"])
        path = self._root / daily_path(day)
        try:
            evening = not is_morning(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        study = study_lines(await self._study(day), evening=evening)
        # 토큰 줄은 저녁에만 채우므로 건드리지 않는다
        week = week_lines(await self._week(day, day), [])
        changed = update_daily(path, "오늘 학습", study)
        return update_daily(path, "이번 주", week) or changed

    async def _week(self, day: date, through: date) -> WeekLine:
        """day가 속한 주의 월요일~through 누적. 셈은 교육 주간 보고와 같다.

        아침판은 through가 어제다 (월요일 아침은 모두 0).
        """
        monday = week_start(day)
        rows = await self._repo.study_days(monday.isoformat(), through.isoformat())
        counted = week_facts(through, rows, [], studied=await studied_since(self._repo, monday))
        quiz = await daily_quiz_score(self._repo, monday, through) if rows else None
        optional = [row for row in rows if row.get("optional")]
        return WeekLine(
            start=monday,
            end=through,
            study_days=counted.study_days,
            done=counted.done,
            quiz=None if quiz is None else Score(quiz.total, quiz.answered, quiz.correct),
            optional_days=len(optional),
            optional_done=sum(1 for row in optional if row.get("optional_done_at")),
            goal=self._goal(day),
        )

    def _goal(self, day: date) -> int | None:
        """월별 목표 그달 절의 `**주간 목표:** 학습한 날 N일`. 못 읽으면 None."""
        try:
            goals = goals_file(self._root).read_text(encoding="utf-8")
        except (DailyError, OSError):
            return None
        return weekly_goal(goals, f"{day:%Y-%m}")

    def _month(self, day: date) -> MonthLine | None:
        """머리말. 볼트의 월별 목표나 대기열을 못 읽으면 None (머리말을 빼고 쓴다)."""
        key = f"{day:%Y-%m}"
        try:
            goals = goals_file(self._root)
            month = parse_month(goals.read_text(encoding="utf-8"), key)
            queue = parse_queue((self._root / QUEUE_FILE).read_text(encoding="utf-8"))
        except (DailyError, OSError):
            return None
        items = [item for item in queue if item.month == key]
        done = sum(1 for item in items if item.done)
        return MonthLine(
            goals.relative_to(self._root).with_suffix("").as_posix(),
            month.topic,
            done,
            len(items) - done,
        )

    def _vault_text(self, path: str) -> str:
        try:
            return (self._root / path).read_text(encoding="utf-8")
        except OSError:
            return ""

    def _optional(self, day: date, target: str) -> str:
        """선택 항목이 저장되지 않은 날(v10 전)은 아침 규칙(optional_for)으로 다시 정한다."""
        section = next(
            (
                item.section
                for item in parse_queue(self._vault_text(QUEUE_FILE))
                if item.target == target
            ),
            "",
        )
        try:
            goals = goals_file(self._root).read_text(encoding="utf-8")
            return optional_for(parse_month(goals, f"{day:%Y-%m}"), section)
        except (DailyError, OSError):
            return ""

    async def _study(self, day: date) -> StudyFact | None:
        """그 학습일(그날 아침부터 다음 아침 전까지)의 학습 기록."""
        rows = (await self._repo.study_days(day.isoformat()))[:2]
        if not rows or rows[0]["day"] != day.isoformat():
            return None
        row = rows[0]
        start = row["created_at"]
        end = rows[1]["created_at"] if len(rows) > 1 else None
        done_at = row["done_at"]
        return StudyFact(
            target=row["target"],
            label=row["label"],
            carried=bool(row["carried"]),
            done=done_at is not None and (end is None or done_at < end),
            note=await self._score(NOTE_KEY + row["target"], start, end),
            review=await self._score(REVIEW_KEY + day.isoformat(), start, end),
            concept_id=curriculum_id(self._vault_text(CURRICULUM_FILE), row["label"]),
            # 아침에 정해 둔 선택 항목. v10 전 기록은 아침 규칙으로 다시 정하고 했는지는 모른다
            optional=row["optional"] or self._optional(day, row["target"]),
            optional_done=(row["optional_done_at"] is not None) if row["optional"] else None,
        )

    async def _score(self, key: str, start: str, end: str | None) -> Score | None:
        """학습일 [start, end)의 점수. 이어 하는 날은 남은 문제만 센다 (day_score)."""
        quiz_id = await self._repo.quiz_by_key(key)
        if quiz_id is None:
            return None
        stored = await self._repo.get_quiz(quiz_id)
        total = len(json.loads(stored[1])) if stored is not None else 0
        return day_score(total, await self._repo.quiz_answers(quiz_id), start, end)

    async def _tokens(self, start: datetime, end: datetime) -> list[EngineUsage]:
        day = start.date()
        week_start = start - timedelta(days=start.weekday())
        claude = claude_usage(self._claude_dir, week_start)
        # 주간 한도는 그날 시작 전 마지막 값과 비교하므로 일주일 더 앞에서부터 읽는다
        codex, samples = codex_sessions(self._codex_dir, week_start - timedelta(days=7))
        codex += await self._codex_bot(week_start, end)
        return [
            engine_usage("claude", claude, start, end, week_start, await self._claude_limit(day)),
            engine_usage("codex", codex, start, end, week_start, weekly_limit(samples, start, end)),
        ]

    async def _with_claude_limit(self, facts: DayFacts) -> DayFacts:
        limit = await self._claude_limit(facts.day)
        tokens = [
            replace(usage, limit=limit) if usage.engine == "claude" else usage
            for usage in facts.tokens
        ]
        return replace(facts, tokens=tokens)

    async def _claude_limit(self, day: date) -> Limit | None:
        start, end = day_bounds(day)
        samples: list[LimitSample] = []
        # 그날 증가분은 그날 시작 전 마지막 값과 비교하므로 일주일 앞에서부터 읽는다
        async for at, text in self._run_logs("claude", start - timedelta(days=7), end):
            if (sample := claude_run_limit(text, at)) is not None:
                samples.append(sample)
        return weekly_limit(samples, start, end)

    async def _codex_bot(self, start: datetime, end: datetime) -> list[Usage]:
        found = []
        async for at, text in self._run_logs("codex", start, end):
            if tokens := codex_run_tokens(text):
                found.append(Usage(at, tokens, bot=True))
        return found

    async def _run_logs(
        self, engine: str, start: datetime, end: datetime
    ) -> AsyncIterator[tuple[datetime, str]]:
        """[start, end) 사이에 시작한 봇 실행의 시작 시각과 실행 로그."""
        for row in await self._repo.runs_between(engine, to_iso(start), to_iso(end)):
            path = row.get("log_path")
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
            except OSError:
                continue
            yield datetime.fromisoformat(row["started_at"]), text

    async def _summary(self, facts: DayFacts) -> str | None:
        self._workdir.mkdir(parents=True, exist_ok=True)
        answer = await self._engine.ask_outside_task(
            SCOPE,
            self._agent,
            Stage.SECRETARY_DAILY,
            lambda persona: secretary_daily_prompt(
                persona, day=facts.day.isoformat(), facts=render_facts(facts)
            ),
            model=DailySummary,
            cwd=self._workdir,
        )
        if answer.result is None:
            log.warning("하루 정리 요약 실패: %s", answer.error or answer.status)
            return None
        return answer.result.summary


def _push_line(result: Any) -> str:
    if result.problem:
        return f"볼트 GitHub 백업 못 함: {result.problem}. 다음 저녁에 함께 올린다"
    if result.pushed:
        return f"볼트 GitHub 백업: 커밋 {result.pushed}개"
    return "볼트 GitHub 백업: 올릴 커밋 없음"


def _event(row: dict[str, Any], change: str) -> TaskEvent:
    return TaskEvent(
        at=datetime.fromisoformat(row["created_at"]),
        project=row["project"],
        task_id=int(row["task_id"]),
        title=row["title"],
        change=change,
    )


def _pr_change(row: dict[str, Any]) -> str:
    payload = json.loads(row["payload"]) if row.get("payload") else {}
    number = payload.get("pr") if isinstance(payload, dict) else None
    name = f"PR #{number}" if number else "PR"
    return f"{name} 열림" if row["kind"] == EventKind.PR_OPENED else f"{name} merge"
