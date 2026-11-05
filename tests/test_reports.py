"""아침·주간 보고 테스트 (ADR-007, SPEC 6.6)."""

import asyncio
from datetime import UTC, datetime, timedelta

from bot.agents import Stage
from bot.clock import FixedClock
from bot.org import DEFAULT_ORG
from bot.reports import morning_report, nightly_line, weekly_report
from bot.store import Actor, EventKind, NewEvent, Repository, connect
from bot.workflow.state import TaskState

NOW = datetime(2026, 10, 28, 23, 0, tzinfo=UTC)  # 한국 10-29 08:00


def url(thread):
    return f"https://discord.com/channels/1/{thread}"


async def seed(repo, clock):
    """대기 1, 진행 1, 어제 완료 1, 중지 1."""
    created = []
    for thread, state in [
        ("t1", TaskState.AWAIT_DESIGN_APPROVAL),
        ("t2", TaskState.IMPLEMENTING),
        ("t3", TaskState.DONE),
        ("t4", TaskState.STOPPED),
    ]:
        task = await repo.create_task(
            project="gagaessi",
            discord_thread=thread,
            title=f"작업 {thread}",
            state=state,
            event=NewEvent(Actor.ZERO, EventKind.MESSAGE, {"text": "x"}),
        )
        created.append(task)
    await repo.start_agent_run("r1", task_id=1, agent="alpha", stage=Stage.DESIGN, engine="claude")
    await repo.finish_agent_run("r1", status="ok", exit_code=0, log_path=None)
    await repo.start_agent_run("r2", task_id=1, agent="delta", stage=Stage.REVIEW, engine="claude")
    await repo.finish_agent_run("r2", status="invalid_output", exit_code=0, log_path=None)
    return created


def run(body):
    async def main():
        conn = await connect(":memory:")
        try:
            clock = FixedClock(NOW - timedelta(hours=12))
            repo = Repository(conn, clock)
            await seed(repo, clock)
            clock.advance(timedelta(hours=12))
            return await body(repo)
        finally:
            await conn.close()

    return asyncio.run(main())


def test_morning_report_lists_what_zero_must_do():
    async def body(repo):
        return await morning_report(repo, DEFAULT_ORG, NOW, url, paused="")

    report = run(body)
    text = report.text
    assert report.mention is True
    assert (
        "**Zero가 할 일** (1)" in text and "#1 [작업 t1](https://discord.com/channels/1/t1)" in text
    )
    assert "(대기 12시간)" in text
    assert "**진행 중** (1)" in text and "#2" in text
    assert "Alpha · 개발팀장 1회" in text and "Delta · QA팀장 1회 (실패 1)" in text


def test_morning_report_without_waiting_work_is_silent():
    async def body(repo):
        await repo.apply_transition(
            1,
            expected_state=TaskState.AWAIT_DESIGN_APPROVAL,
            new_state=TaskState.STOPPED,
            event=NewEvent(Actor.ZERO, EventKind.COMMAND, {"name": "stop"}),
        )
        return await morning_report(repo, DEFAULT_ORG, NOW, url, paused="10-29 09:00")

    report = run(body)
    assert report.mention is False
    assert "**Zero가 할 일** (0)\n- 없음" in report.text
    assert "사용량 한도 정지**: 10-29 09:00까지" in report.text


def test_weekly_report_counts_per_product_and_metrics():
    async def body(repo):
        await repo.add_hiring_proposal(
            task_id=1,
            proposed_by="beta",
            payload={"team": "운영팀", "role": "운영팀장"},
            evidence={},
        )
        return await weekly_report(repo, DEFAULT_ORG, NOW, ["gagaessi", "jday"])

    report = run(body)
    text = report.text
    assert "- gagaessi: 4 / 1 / 1 / 0" in text
    assert "- jday: 0 / 0 / 0 / 0" in text
    assert "도달 4 / 채택 1 / 자동 제외 1" in text
    assert "#1 Beta · 기획팀장: 운영팀 운영팀장" in text
    assert report.mention is True


def test_nightly_line_shows_unreported_results():
    """정기 테스트는 보고하지 않은 결과(통과, 일시적 실패, 개선 불필요)도 아침에 한 줄로 보인다."""

    async def body(repo):
        for project, outcome in [("gagaessi", "flaky"), ("jday", "posted")]:
            task = await repo.create_task(
                project=project,
                discord_thread=f"sched:{project}",
                title="정기 테스트",
                state=TaskState.DONE,
                event=NewEvent(Actor.ZERO, EventKind.DECISION, {"text": "정기 테스트"}),
                pipeline="nightly-test",
            )
            payload = {"posted": outcome == "posted", "outcome": outcome}
            await repo.add_event(task.id, NewEvent(Actor.BOT, EventKind.REPORT, payload))
        line = await nightly_line(repo, NOW)
        report = await morning_report(repo, DEFAULT_ORG, NOW, url)
        return line, report.text

    line, text = run(body)
    assert line == "정기 테스트: gagaessi 일시적 실패(다시 돌려 통과) · jday 문제 보고"
    assert line in text
