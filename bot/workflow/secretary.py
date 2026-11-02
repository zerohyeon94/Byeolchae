"""비서 부서: 매일 저녁 하루 정리(Daily)와 짧은 아침 보고 (ADR-010).

저녁에는 그날의 사실을 봇이 모은다.
- 학습: DB의 학습 기록과 문제 점수
- 제품 부서: 작업 단계 변화와 PR 열림·merge
- 결정할 것: 승인 대기 작업과 열린 채용 제안
- 토큰: Claude·Codex 로컬 기록 (봇과 Zero, 모든 저장소)과 봇 실행 로그의 구독 한도

비서는 그 사실로 요약 1~3줄만 쓴다. 볼트에 Daily 파일을 쓰고 커밋하는 주체는 봇이다.
요약을 받지 못해도 사실만으로 Daily를 쓴다. 같은 날 Daily가 이미 있으면 다시 쓰지 않는다.
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
from bot.org import Org
from bot.prompts import secretary_daily_prompt
from bot.reports import pending_decisions
from bot.schemas import DailySummary
from bot.secretary.daily import (
    DayFacts,
    Score,
    StudyFact,
    TaskEvent,
    daily_path,
    render_daily,
    render_discord,
    render_facts,
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
        display: str,
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
        self._display = display
        self._notify = notify
        self._repo = repo
        self._org = org
        self._claude_dir = claude_dir
        self._codex_dir = codex_dir
        self._workdir = workdir  # 비서 실행 폴더. 비서는 파일을 읽지 않으므로 빈 폴더다

    async def run_evening(self, day: date) -> str:
        """그날 Daily를 쓰고 #비서에 짧게 알린다. 알린 문구를 돌려준다."""
        path = daily_path(day)
        if (self._root / path).exists():
            text = f"{day.isoformat()} 하루 정리는 이미 있다: `{path}`"
            await self._notify(text, False)
            return text
        facts = await self.facts(day)
        summary = await self._summary(facts)
        # 비서 호출 자신의 한도 줄까지 넣는다. 그날 Zero만 Claude를 썼어도 값이 생긴다
        facts = await self._with_claude_limit(facts)
        target = self._root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_daily(facts, summary, display=self._display), encoding="utf-8")
        if self._vault is not None:
            await self._vault.commit(
                self._root, [path], f"docs(daily): {day.isoformat()} 하루 정리", COMMIT_TRAILER
            )
        text = render_discord(facts, summary, path)
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
        )

    async def _study(self, day: date) -> StudyFact | None:
        rows = await self._repo.study_days(day.isoformat(), day.isoformat())
        if not rows:
            return None
        row = rows[0]
        return StudyFact(
            target=row["target"],
            label=row["label"],
            carried=bool(row["carried"]),
            done=row["done_at"] is not None,
            note=await self._score(NOTE_KEY + row["target"]),
            review=await self._score(REVIEW_KEY + day.isoformat()),
        )

    async def _score(self, key: str) -> Score | None:
        quiz_id = await self._repo.quiz_by_key(key)
        if quiz_id is None:
            return None
        stored = await self._repo.get_quiz(quiz_id)
        total = len(json.loads(stored[1])) if stored is not None else 0
        answered, correct = await self._repo.quiz_score(quiz_id)
        return Score(total, answered, correct)

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

    async def _summary(self, facts: DayFacts) -> list[str] | None:
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
        return list(answer.result.summary)


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
