"""비서 부서 저녁 Daily와 아침 보고 테스트 (ADR-010).

토큰 기록, 볼트, DB는 tmp_path에 만들고, 비서 호출과 커밋은 가짜로 바꾼다.
"""

import asyncio
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from bot.agents.base import Stage
from bot.clock import FixedClock
from bot.schemas import DailySummary
from bot.secretary.daily import (
    DayFacts,
    MonthLine,
    Score,
    StudyFact,
    TaskEvent,
    curriculum_id,
    daily_path,
    render_daily,
    render_discord,
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
    tokens_text,
    weekly_limit,
)
from bot.store import Actor, EventKind, NewEvent
from bot.workflow.secretary import SecretaryDesk, day_bounds
from bot.workflow.state import TaskState

DAY = date(2026, 11, 2)  # 월요일


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute).astimezone()


def utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


# 토큰 기록


def claude_line(moment, message_id, tokens, entrypoint="claude-desktop"):
    usage = {
        "input_tokens": tokens[0],
        "cache_creation_input_tokens": tokens[1],
        "cache_read_input_tokens": tokens[2],
        "output_tokens": tokens[3],
    }
    return json.dumps(
        {
            "timestamp": utc(moment),
            "entrypoint": entrypoint,
            "type": "assistant",
            "message": {"id": message_id, "usage": usage, "content": "대화 내용"},
        }
    )


def write_claude(root, lines):
    path = root / "-Users-zero-Developer-Other" / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    nested = root / "-Users-zero-x" / "abc" / "subagents" / "agent.jsonl"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text(claude_line(at(DAY, 9), "m-sub", (1, 0, 0, 1)) + "\n", encoding="utf-8")


def codex_event(moment, total, used=None, resets=None):
    payload = {"type": "token_count", "info": {"total_token_usage": {"total_tokens": total}}}
    if used is not None:
        payload["rate_limits"] = {
            "primary": {"used_percent": 50.0, "window_minutes": 300, "resets_at": 0},
            "secondary": {
                "used_percent": used,
                "window_minutes": 10080,
                "resets_at": resets.timestamp(),
            },
        }
    return json.dumps({"timestamp": utc(moment), "type": "event_msg", "payload": payload})


def write_codex(root, name, lines):
    path = root / "2026" / "11" / "02" / f"rollout-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps({"type": "session_meta", "payload": {"originator": "Codex Desktop"}})
    path.write_text("\n".join([meta, *lines]) + "\n", encoding="utf-8")


def test_claude_counts_each_message_once_and_splits_bot(tmp_path):
    write_claude(
        tmp_path,
        [
            claude_line(at(DAY, 10), "m1", (2, 100, 1000, 50)),
            claude_line(at(DAY, 10), "m1", (2, 100, 1000, 50)),  # 같은 메시지가 두 줄
            claude_line(at(DAY, 11), "m2", (1, 0, 0, 9), entrypoint="sdk-cli"),
            claude_line(at(DAY - timedelta(days=1), 23), "m0", (5, 0, 0, 5)),
            "깨진 줄",
        ],
    )
    usages = claude_usage(tmp_path, at(DAY, 0))
    assert sorted((u.tokens, u.bot) for u in usages) == [(2, False), (10, True), (1152, False)]


def test_claude_skips_files_not_touched_since(tmp_path):
    write_claude(tmp_path, [claude_line(at(DAY, 10), "m1", (1, 0, 0, 1))])
    old = (at(DAY, 0) - timedelta(days=30)).timestamp()
    for path in tmp_path.rglob("*.jsonl"):
        os.utime(path, (old, old))
    assert claude_usage(tmp_path, at(DAY, 0)) == []


def test_codex_counts_increase_of_session_total_and_reads_weekly_limit(tmp_path):
    resets = at(DAY, 0) + timedelta(days=3)
    write_codex(
        tmp_path,
        "a",
        [
            codex_event(at(DAY, 9), 1000, used=10.0, resets=resets),
            codex_event(at(DAY, 9, 1), 1000),  # 같은 누적값이 다시 나와도 세지 않는다
            codex_event(at(DAY, 10), 1500, used=12.0, resets=resets),
        ],
    )
    usages, samples = codex_sessions(tmp_path, at(DAY, 0))
    assert [(u.tokens, u.bot) for u in usages] == [(1000, False), (500, False)]
    assert [s.used_percent for s in samples] == [10.0, 12.0]


def test_codex_run_log_tokens():
    assert codex_run_tokens("codex\n{...}\ntokens used\n19,975\n") == 19975
    assert codex_run_tokens("다른 내용") is None


def limit_line(utilization, resets_at=1794240000):
    """실제 `claude -p --output-format stream-json`의 한도 줄 모양."""
    info = {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.5, "resetsAt": 1793676600},
            "seven_day": {"utilization": utilization, "resetsAt": resets_at},
        },
    }
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


def test_claude_run_log_weekly_limit():
    moment = datetime(2026, 11, 3, 7, 30, tzinfo=UTC)
    log = "\n".join(["run_id: r1", "=== stdout ===", limit_line(0.01), limit_line(0.02), "{}"])
    found = claude_run_limit(log, moment)
    assert found == LimitSample(moment, 2.0, datetime(2026, 11, 9, 16, 0, tzinfo=UTC))
    assert claude_run_limit("=== stdout ===\n{}", moment) is None
    # 모양이 바뀌면 값을 지어내지 않는다
    assert claude_run_limit('{"type": "rate_limit_event", "rate_limit_info": {}}', moment) is None


def sample(moment, used, resets):
    return LimitSample(moment, used, resets)


def test_weekly_limit_compares_with_value_before_the_day():
    start, end = day_bounds(DAY)
    resets = end + timedelta(days=2)
    samples = [
        sample(start - timedelta(hours=3), 10.0, resets),
        sample(start + timedelta(hours=9), 14.0, resets),
        sample(start + timedelta(hours=20), 17.5, resets),
    ]
    assert weekly_limit(samples, start, end) == Limit(17.5, 7.5)
    # 그날 전 값이 없으면 오늘 증가분은 모른다
    assert weekly_limit(samples[1:], start, end) == Limit(17.5, None)
    # 그날 사이에 주간 창이 바뀌면 마지막 값이 오늘 쓴 만큼이다
    moved = [sample(start - timedelta(hours=3), 80.0, start + timedelta(hours=1)), samples[2]]
    assert weekly_limit(moved, start, end) == Limit(17.5, 17.5)
    # 같은 창의 초기화 시각이 1초 흔들려도 창이 바뀐 것이 아니다 (실제 Codex 기록)
    jitter = [
        sample(start - timedelta(days=4), 4.0, resets),
        sample(start + timedelta(hours=8), 23.0, resets - timedelta(seconds=1)),
    ]
    assert weekly_limit(jitter, start, end) == Limit(23.0, 19.0)
    # 마지막 기록 뒤에 창이 이미 초기화됐으면 0%
    stale = [sample(start - timedelta(days=5), 40.0, start - timedelta(days=1))]
    assert weekly_limit(stale, start, end) == Limit(0.0, 0.0)
    assert weekly_limit([], start, end) is None


def test_engine_usage_splits_day_and_week():
    start, end = day_bounds(DAY)
    week = start - timedelta(days=start.weekday())
    usages = [
        Usage(start + timedelta(hours=1), 300, bot=True),
        Usage(start + timedelta(hours=2), 100, bot=False),
        Usage(start - timedelta(hours=1), 600, bot=False),  # 지난주 일요일: 이번 주가 아니다
    ]
    usage = engine_usage("codex", usages, start, end, week)
    assert (usage.bot, usage.zero, usage.total, usage.week) == (300, 100, 400, 400)
    assert usage.week_share == 100
    tuesday = engine_usage("codex", usages, start - timedelta(days=1), start, week - timedelta(7))
    assert tuesday.week_share == 100
    assert engine_usage("claude", [], start, end, week).week_share is None


def test_tokens_text():
    assert [tokens_text(n) for n in (999, 12_345, 4_560_000)] == ["999", "12.3K", "4.56M"]


# Daily 표시


def facts(**changes):
    base = dict(
        day=DAY,
        study=StudyFact(
            "Learning/개념/2026-11-02 gRPC 스트리밍",
            "gRPC 스트리밍",
            carried=False,
            done=True,
            note=Score(3, 3, 2),
            review=Score(1, 0, 0),
            concept_id="SYS-05",
            optional="플랫폼 지도: '장비 안' 구성 요소 칸 하나 채우기 (5분)",
        ),
        transitions=[
            TaskEvent(
                at(DAY, 14, 2), "gagaessi", 19, "카테고리 추가", "PLANNING → AWAIT_PLAN_APPROVAL"
            )
        ],
        prs=[TaskEvent(at(DAY, 16), "gagaessi", 18, "통계 화면", "PR #12 merge")],
        decisions=["[gagaessi] #19 카테고리 추가 — AWAIT_PLAN_APPROVAL (대기 9시간)"],
        tokens=[
            EngineUsage("claude", bot=1_200_000, zero=30_500_000, week=170_000_000),
            EngineUsage("codex", bot=20_000, zero=1_400_000, week=11_000_000, limit=Limit(4, 1)),
        ],
        month=MonthLine("Schedule/월별 목표 (2026-11 ~ 2027-10)", "시스템 전체 한 바퀴", 1, 28),
    )
    base.update(changes)
    return DayFacts(**base)


def test_daily_path_keeps_vault_layout():
    assert daily_path(DAY) == "Daily/2026-11/2026-11-02.md"


def test_render_daily_follows_vault_template():
    text = render_daily(facts(), "#19 기획 승인을 기다린다.")
    assert text.startswith(
        "# 2026-11-02 (월)\n\n"
        "> [[Schedule/월별 목표 (2026-11 ~ 2027-10)|이번 달]]: 시스템 전체 한 바퀴 · "
        "이번 달 완료 1개 · 대기열 남은 28개\n\n## 오늘 학습\n"
    )
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == ["## 오늘 학습", "## 오늘 한 일", "## 토큰", "## 결정할 것", "## 한 줄 요약"]
    assert (
        "- **개념:** [[Learning/개념/2026-11-02 gRPC 스트리밍|gRPC 스트리밍]] (SYS-05) — 완료"
        in text
    )
    assert "- **문제:** 오늘 3개 중 2개 맞힘 · 아직 안 푼 문제 0개" in text
    assert "- **복습:** 안 풂" in text
    assert "- **선택:** 플랫폼 지도: '장비 안' 구성 요소 칸 하나 채우기 (5분) — 기록 없음" in text
    # 오늘 한 일은 단계 변화와 PR을 시각 순으로
    assert (
        "- 14:02 [gagaessi] #19 카테고리 추가: PLANNING → AWAIT_PLAN_APPROVAL\n"
        "- 16:00 [gagaessi] #18 통계 화면: PR #12 merge"
    ) in text
    assert "- **봇:** Claude 1.20M · Codex 20.0K" in text
    assert "- **나:** Claude 30.50M · Codex 1.40M" in text
    assert "- **이번 주 중 오늘:** Claude 19% · Codex 13%" in text
    assert "- **구독 주간 한도:** Claude 기록 없음 · Codex 4% (오늘 +1%p)" in text
    assert text.endswith("## 한 줄 요약\n\n#19 기획 승인을 기다린다.\n")
    assert "|" not in text.replace("|이번 달]]", "").replace("|gRPC 스트리밍]]", "")  # 표 없음


def test_render_daily_without_summary_study_or_month():
    text = render_daily(facts(study=None, decisions=[], month=None, transitions=[], prs=[]), None)
    assert text.startswith("# 2026-11-02 (월)\n\n## 오늘 학습\n")
    assert "- 오늘 학습 기록 없음" in text
    assert "- 제품 부서 작업 변화 없음" in text
    assert "## 결정할 것\n\n## 한 줄 요약\n\n(요약을 만들지 못했다)\n" in text


def test_render_daily_review_and_unknown_scores():
    study = facts().study
    wrong = replace(study, review=Score(1, 1, 0), note=None, concept_id="", optional="")
    text = render_daily(facts(study=wrong), "요약")
    assert "gRPC 스트리밍]] — 완료" in text
    assert "- **문제:** 기록 없음" in text and "- **복습:** 틀림" in text
    assert "**선택:**" not in text


def test_curriculum_id_from_heading():
    curriculum = "## 2026-11\n\n### SYS-01 엣지-중앙 서버 구조\n- **먼저:** 없음\n### SYS-02 SSE\n"
    assert curriculum_id(curriculum, "엣지-중앙 서버 구조") == "SYS-01"
    assert curriculum_id(curriculum, "없는 개념") == ""


def test_render_discord_is_short():
    text = render_discord(facts(), "요약 한 줄", "Daily/2026-11/2026-11-02.md")
    assert text.splitlines()[0] == "**하루 정리** (11-02)"
    assert "> 요약 한 줄" in text
    assert "- 결정할 것: 1건" in text
    assert "- 학습: gRPC 스트리밍 · 완료 · 오늘 3개 중 2개 맞힘 · 아직 안 푼 문제 0개" in text
    assert "- 제품: 단계 변화 1건, PR 1건" in text
    assert "- Codex: 1.42M (이번 주 중 13%, 주간 한도 4% (오늘 +1%p))" in text
    assert text.endswith("Daily: `Daily/2026-11/2026-11-02.md`")


# 실행


@dataclass
class FakeAnswer:
    result: object
    error: str = ""
    status: str = "ok"


class FakeEngine:
    def __init__(self, summary=None):
        self.summary = summary
        self.calls = []

    async def ask_outside_task(self, scope, agent, stage, build_prompt, *, model, cwd):
        from bot.org import DEFAULT_ORG
        from bot.prompts import persona_for

        self.calls.append((scope, agent, stage, build_prompt(persona_for(DEFAULT_ORG, agent))))
        if self.summary is None:
            return FakeAnswer(None, "형식 오류")
        assert model is DailySummary
        return FakeAnswer(self.summary)


class FakeVault:
    def __init__(self):
        self.commits = []

    async def commit(self, root, paths, title, trailer):
        self.commits.append((list(paths), title, trailer))
        return True


def run_desk(tmp_path, work, engine, vault=None, now=None):
    from bot.org import DEFAULT_ORG
    from bot.store import Repository, connect

    notes = []

    async def notify(text, mention):
        notes.append((text, mention))

    async def main():
        clock = FixedClock(now or at(DAY, 23, 30))
        conn = await connect(tmp_path / "z.db")
        try:
            repo = Repository(conn, clock)
            desk = SecretaryDesk(
                engine,
                vault,
                tmp_path / "vault",
                "theta",
                notify,
                repo=repo,
                org=DEFAULT_ORG,
                claude_dir=tmp_path / "claude",
                codex_dir=tmp_path / "codex",
                workdir=tmp_path / "secretary",
            )
            return await work(desk, repo, clock)
        finally:
            await conn.close()

    return asyncio.run(main()), notes


async def seed(repo, clock):
    """학습 하루, 단계 변화 하나, PR 열림, 봇 Codex 실행 하나."""
    await repo.add_study_day(DAY.isoformat(), "Learning/개념/2026-11-02 SSE", "SSE", False)
    await repo.add_quiz(
        "note",
        "note:Learning/개념/2026-11-02 SSE",
        json.dumps([{"prompt": "문제"}] * 3),
    )
    task = await repo.create_task(
        project="gagaessi",
        discord_thread="t1",
        title="카테고리 추가",
        state=TaskState.PLANNING,
        event=NewEvent(Actor.ZERO, EventKind.COMMAND, {}),
    )
    await repo.apply_transition(
        task.id,
        expected_state=TaskState.PLANNING,
        new_state=TaskState.AWAIT_PLAN_APPROVAL,
        event=NewEvent(Actor.BOT, EventKind.PLAN, {}),
    )
    await repo.add_event(task.id, NewEvent(Actor.BOT, EventKind.PR_OPENED, {"pr": 7}))
    return task


def write_vault(root):
    """머리말·커리큘럼 ID·선택 항목을 읽는 볼트 파일."""
    (root / "Schedule").mkdir(parents=True)
    (root / "Schedule" / "월별 목표 (2026-11 ~ 2027-10).md").write_text(
        "| 달 | 주제 | 층 | 자격증 |\n|---|---|---|---|\n"
        "| 2026-11 | 시스템 전체 한 바퀴 | 폭 | — |\n",
        encoding="utf-8",
    )
    (root / "Schedule" / "학습 대기열.md").write_text(
        "## 2026-11 · 시스템 전체 한 바퀴\n\n### 2. 장비 ↔ 서버\n"
        "- [x] 엣지: 설명 → [[Learning/개념/2026-11-01 엣지|엣지]]\n"
        "- [ ] SSE: 설명 → [[Learning/개념/2026-11-02 SSE|SSE]]\n"
        "- [ ] gRPC: 설명\n\n## 2026-12 · 다음 달\n- [ ] 다음: 설명\n",
        encoding="utf-8",
    )
    (root / "Schedule" / "교육 커리큘럼.md").write_text("### SYS-07 SSE\n", encoding="utf-8")


def test_evening_writes_daily_commits_and_posts(tmp_path):
    engine, vault = FakeEngine(DailySummary(summary="#1 기획 승인이 남았다.")), FakeVault()
    log = tmp_path / "codex-run.log"
    log.write_text("codex\ntokens used\n2,500\n", encoding="utf-8")

    write_vault(tmp_path / "vault")

    async def work(desk, repo, clock):
        await seed(repo, clock)
        await repo.start_agent_run(
            "r1", task_id=None, agent="delta", stage=Stage.REVIEW, engine="codex"
        )
        await repo.finish_agent_run("r1", status="ok", exit_code=0, log_path=str(log))
        return await desk.run_evening(DAY)

    text, notes = run_desk(tmp_path, work, engine, vault)
    path = tmp_path / "vault" / "Daily" / "2026-11" / "2026-11-02.md"
    daily = path.read_text(encoding="utf-8")
    assert daily.endswith("## 한 줄 요약\n\n#1 기획 승인이 남았다.\n")
    assert (
        "> [[Schedule/월별 목표 (2026-11 ~ 2027-10)|이번 달]]: 시스템 전체 한 바퀴 · "
        "이번 달 완료 1개 · 대기열 남은 2개"
    ) in daily
    assert "[[Learning/개념/2026-11-02 SSE|SSE]] (SYS-07) — 내일 이어 하기" in daily
    assert "- **문제:** 오늘 3개 중 0개 맞힘 · 아직 안 푼 문제 3개" in daily
    assert (
        "- **선택:** 플랫폼 지도: '장비 ↔ 서버' 구성 요소 칸 하나 채우기 (5분) — 기록 없음" in daily
    )
    assert "[gagaessi] #1 카테고리 추가: PLANNING → AWAIT_PLAN_APPROVAL" in daily
    assert "[gagaessi] #1 카테고리 추가: PR #7 열림" in daily
    assert "AWAIT_PLAN_APPROVAL (대기" in daily  # 결정할 것
    assert "- **봇:** Claude 0 · Codex 2,500" in daily
    assert vault.commits == [
        (
            ["Daily/2026-11/2026-11-02.md"],
            "docs(daily): 2026-11-02 하루 정리",
            "ZeroSync-Department: secretary",
        )
    ]
    assert notes == [(text, False)] and text.startswith("**하루 정리** (11-02)")
    scope, agent, stage, prompt = engine.calls[0]
    assert (scope, agent, stage) == ("secretary", "theta", Stage.SECRETARY_DAILY)
    assert "PLANNING → AWAIT_PLAN_APPROVAL" in prompt and "Theta" in prompt


class RecordingEngine(FakeEngine):
    """비서 호출도 봇 Claude 실행처럼 한도 줄이 담긴 실행 로그를 남긴다."""

    def __init__(self, summary, repo_box, log):
        super().__init__(summary)
        self.repo_box, self.log = repo_box, log

    async def ask_outside_task(self, scope, agent, stage, build_prompt, *, model, cwd):
        repo = self.repo_box[0]
        await repo.start_agent_run(
            "theta-1", task_id=None, agent=agent, stage=stage, engine="claude"
        )
        await repo.finish_agent_run("theta-1", status="ok", exit_code=0, log_path=str(self.log))
        return await super().ask_outside_task(
            scope, agent, stage, build_prompt, model=model, cwd=cwd
        )


def test_evening_reads_claude_weekly_limit_from_bot_run_logs(tmp_path):
    before, theta = tmp_path / "before.log", tmp_path / "theta.log"
    before.write_text(limit_line(0.03) + "\n", encoding="utf-8")
    theta.write_text(limit_line(0.05) + "\n", encoding="utf-8")
    repo_box = []
    engine = RecordingEngine(DailySummary(summary="요약"), repo_box, theta)

    async def work(desk, repo, clock):
        repo_box.append(repo)
        clock.advance(timedelta(hours=-38))  # 전날 09:30 (오늘 증가분을 셀 기준 표본)
        await repo.start_agent_run(
            "r0", task_id=None, agent="beta", stage=Stage.OPINION, engine="claude"
        )
        await repo.finish_agent_run("r0", status="ok", exit_code=0, log_path=str(before))
        clock.advance(timedelta(hours=38))
        return await desk.run_evening(DAY)

    text, _ = run_desk(tmp_path, work, engine)
    daily = (tmp_path / "vault" / daily_path(DAY)).read_text(encoding="utf-8")
    # 비서 호출(23:30)의 한도 줄까지 넣어 전날 3%에서 오늘 5%
    assert "- **구독 주간 한도:** Claude 5% (오늘 +2%p) · Codex 기록 없음" in daily
    assert "- Claude: " in text and "주간 한도 5% (오늘 +2%p)" in text


def test_evening_writes_facts_when_summary_fails_and_never_twice(tmp_path):
    engine = FakeEngine(None)

    async def work(desk, repo, clock):
        await desk.run_evening(DAY)
        return await desk.run_evening(DAY)

    second, notes = run_desk(tmp_path, work, engine)
    daily = (tmp_path / "vault" / daily_path(DAY)).read_text(encoding="utf-8")
    assert daily.endswith("## 한 줄 요약\n\n(요약을 만들지 못했다)\n")
    assert second == "2026-11-02 하루 정리는 이미 있다: `Daily/2026-11/2026-11-02.md`"
    assert len(engine.calls) == 1 and len(notes) == 2


def test_missed_evening_next_morning_still_reports_that_day(tmp_path):
    # 11-03 아침에 따라잡아도 11-02 하루만 센다
    engine = FakeEngine(DailySummary(summary="요약"))

    async def work(desk, repo, clock):
        await seed(repo, clock)
        clock.advance(timedelta(hours=8))  # 다음 날 07:30에 다른 단계 변화
        task = await repo.get_task(1)
        await repo.apply_transition(
            task.id,
            expected_state=TaskState.AWAIT_PLAN_APPROVAL,
            new_state=TaskState.STOPPED,
            event=NewEvent(Actor.ZERO, EventKind.COMMAND, {}),
        )
        return await desk.facts(DAY)

    found, _ = run_desk(tmp_path, work, engine, now=at(DAY, 23, 30))
    assert [e.change for e in found.transitions] == ["PLANNING → AWAIT_PLAN_APPROVAL"]


def test_store_records_only_real_state_changes(tmp_path):
    async def work(desk, repo, clock):
        task = await seed(repo, clock)
        # 같은 상태 안에서 Zero가 입력해도 단계 변화가 아니다
        await repo.apply_transition(
            task.id,
            expected_state=TaskState.AWAIT_PLAN_APPROVAL,
            new_state=TaskState.AWAIT_PLAN_APPROVAL,
            event=NewEvent(Actor.ZERO, EventKind.MESSAGE, {}),
        )
        start, end = day_bounds(DAY)
        from bot.clock import to_iso

        span = (to_iso(start), to_iso(end))
        return (
            await repo.transitions_between(*span),
            await repo.events_between({EventKind.PR_OPENED}, *span),
            await repo.transitions_between(to_iso(end), to_iso(end + timedelta(days=1))),
        )

    (moves, prs, tomorrow), _ = run_desk(tmp_path, work, FakeEngine())
    assert [(m["from_state"], m["to_state"], m["project"]) for m in moves] == [
        ("PLANNING", "AWAIT_PLAN_APPROVAL", "gagaessi")
    ]
    assert [(p["kind"], json.loads(p["payload"])) for p in prs] == [("pr_opened", {"pr": 7})]
    assert tomorrow == []


# 아침 보고


def test_morning_brief_puts_decisions_first(tmp_path):
    from bot.reports import morning_brief

    async def work(desk, repo, clock):
        await seed(repo, clock)
        return await morning_brief(
            repo,
            desk._org,
            clock.now(),
            lambda thread: f"https://x/{thread}",
            daily="Daily/2026-11/2026-11-02.md",
        )

    report, _ = run_desk(tmp_path, work, FakeEngine(), now=at(DAY + timedelta(days=1), 8))
    lines = report.text.splitlines()
    assert lines[0] == "**아침 보고** (11-03)"
    assert lines[2] == "**오늘 결정할 것** (1)"
    assert lines[3].startswith(
        "- [gagaessi] #1 [카테고리 추가](https://x/t1) — AWAIT_PLAN_APPROVAL"
    )
    assert "진행 중 0건 · 어제 끝난 일 0건" in report.text
    assert "어제 하루 정리: `Daily/2026-11/2026-11-02.md`" in report.text
    assert report.mention is True
