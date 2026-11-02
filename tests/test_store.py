import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from bot.agents.base import RunStatus, Stage
from bot.clock import FixedClock, from_iso, to_iso
from bot.store import (
    INVALID_OUTPUT,
    RUNNING,
    SCHEMA_VERSION,
    Actor,
    EventKind,
    NewEvent,
    NotFoundError,
    Repository,
    SchemaVersionError,
    StaleStateError,
    connect,
    schema_version,
)
from bot.workflow.state import TaskState

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


async def open_repo(path=":memory:", clock=None):
    conn = await connect(path)
    return conn, Repository(conn, clock or FixedClock(T0))


def zero(text, kind=EventKind.MESSAGE):
    return NewEvent(Actor.ZERO, kind, {"text": text})


async def new_task(repo, thread="thread-1"):
    return await repo.create_task(
        project="gagaessi",
        discord_thread=thread,
        title="예산 배너",
        state=TaskState.OPINIONS,
        event=zero("배너를 띄우자"),
        updates={"opinion_rounds": 0},
    )


# 시각


def test_clock_iso_round_trip_in_utc():
    kst = datetime(2026, 10, 7, 18, 0, tzinfo=UTC).astimezone()
    assert to_iso(kst) == "2026-10-07T18:00:00+00:00"
    assert from_iso(to_iso(kst)) == datetime(2026, 10, 7, 18, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 10, 7))
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 10, 7))


# 스키마


def test_connect_creates_schema_and_file(tmp_path):
    path = tmp_path / "nested" / "zerosync.db"

    async def scenario():
        conn = await connect(path)
        version = await schema_version(conn)
        async with conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
            tables = {row[0] for row in await cur.fetchall()}
        async with conn.execute("PRAGMA foreign_keys") as cur:
            fk = (await cur.fetchone())[0]
        await conn.close()
        return version, tables, fk

    version, tables, fk = run(scenario())
    assert path.exists()
    assert version == SCHEMA_VERSION == 8
    assert {"tasks", "events", "agent_runs"} <= tables
    assert fk == 1


def test_reconnect_keeps_data_and_does_not_migrate_twice(tmp_path):
    path = tmp_path / "zerosync.db"

    async def scenario():
        conn, repo = await open_repo(path)
        await new_task(repo)
        await conn.close()
        conn, repo = await open_repo(path)
        tasks = await repo.list_tasks()
        await conn.close()
        return tasks

    assert [t.title for t in run(scenario())] == ["예산 배너"]


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "zerosync.db"

    async def scenario():
        conn = await aiosqlite.connect(path)
        await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        await conn.commit()
        await conn.close()
        await connect(path)

    with pytest.raises(SchemaVersionError):
        run(scenario())


# tasks + events


def test_create_task_records_first_event():
    async def scenario():
        conn, repo = await open_repo()
        task = await new_task(repo)
        events = await repo.list_events(task.id)
        found = await repo.find_task_by_thread("thread-1")
        await conn.close()
        return task, events, found

    task, events, found = run(scenario())
    assert task.state is TaskState.OPINIONS
    assert task.opinion_rounds == 0
    assert task.created_at == task.updated_at == "2026-10-07T09:00:00+00:00"
    assert found == task
    assert [(e.actor, e.kind, e.payload) for e in events] == [
        (Actor.ZERO, EventKind.MESSAGE, {"text": "배너를 띄우자"})
    ]


def test_duplicate_thread_is_rejected_without_partial_event():
    async def scenario():
        conn, repo = await open_repo()
        task = await new_task(repo)
        with pytest.raises(aiosqlite.IntegrityError):
            await new_task(repo)
        events = await repo.list_events(task.id)
        tasks = await repo.list_tasks()
        await conn.close()
        return events, tasks

    events, tasks = run(scenario())
    assert len(tasks) == 1
    assert len(events) == 1


def test_apply_transition_saves_state_fields_and_event_together():
    clock = FixedClock(T0)

    async def scenario():
        conn, repo = await open_repo(clock=clock)
        task = await new_task(repo)
        clock.advance(timedelta(minutes=5))
        task = await repo.apply_transition(
            task.id,
            expected_state=task.state,
            new_state=TaskState.OPINIONS,
            event=zero("@codex 반례는?"),
            updates={"opinion_rounds": 1},
        )
        events = await repo.list_events(task.id, {EventKind.MESSAGE})
        await conn.close()
        return task, events

    task, events = run(scenario())
    assert task.state is TaskState.OPINIONS
    assert task.opinion_rounds == 1
    assert task.updated_at == "2026-10-07T09:05:00+00:00"
    assert task.created_at == "2026-10-07T09:00:00+00:00"
    assert [e.payload["text"] for e in events] == ["배너를 띄우자", "@codex 반례는?"]


def test_apply_transition_with_wrong_expected_state_changes_nothing():
    async def scenario():
        conn, repo = await open_repo()
        task = await new_task(repo)
        with pytest.raises(StaleStateError, match="OPINIONS"):
            await repo.apply_transition(
                task.id,
                expected_state=TaskState.DESIGNING,
                new_state=TaskState.AWAIT_DESIGN_APPROVAL,
                event=NewEvent(Actor.BOT, EventKind.DESIGN, {}),
            )
        after = await repo.get_task(task.id)
        events = await repo.list_events(task.id)
        await conn.close()
        return after, events

    after, events = run(scenario())
    assert after.state is TaskState.OPINIONS
    assert len(events) == 1


def test_apply_transition_rejects_unknown_fields_and_missing_task():
    async def scenario():
        conn, repo = await open_repo()
        task = await new_task(repo)
        with pytest.raises(ValueError, match="state"):
            await repo.apply_transition(
                task.id,
                expected_state=task.state,
                new_state=task.state,
                event=zero("x"),
                updates={"state": "DONE"},
            )
        with pytest.raises(NotFoundError):
            await repo.apply_transition(
                999, expected_state=task.state, new_state=task.state, event=zero("x")
            )
        with pytest.raises(NotFoundError):
            await repo.get_task(999)
        await conn.close()

    run(scenario())


def test_prev_state_round_trips_as_enum():
    async def scenario():
        conn, repo = await open_repo()
        task = await new_task(repo)
        task = await repo.apply_transition(
            task.id,
            expected_state=TaskState.OPINIONS,
            new_state=TaskState.NEEDS_HUMAN,
            event=NewEvent(Actor.BOT, EventKind.ERROR, {"reason": "test"}),
            updates={"prev_state": TaskState.OPINIONS},
        )
        waiting = await repo.list_tasks({TaskState.NEEDS_HUMAN})
        await conn.close()
        return task, waiting

    task, waiting = run(scenario())
    assert task.prev_state is TaskState.OPINIONS
    assert [t.id for t in waiting] == [task.id]


def test_concurrent_transitions_only_one_wins():
    async def scenario():
        conn, repo = await open_repo()
        task = await new_task(repo)

        async def decide(n):
            return await repo.apply_transition(
                task.id,
                expected_state=TaskState.OPINIONS,
                new_state=TaskState.DESIGNING,
                event=zero(f"/decide {n}", EventKind.DECISION),
            )

        results = await asyncio.gather(decide(1), decide(2), return_exceptions=True)
        decisions = await repo.list_events(task.id, {EventKind.DECISION})
        await conn.close()
        return results, decisions

    results, decisions = run(scenario())
    assert sum(isinstance(r, StaleStateError) for r in results) == 1
    assert len(decisions) == 1


# agent_runs


def test_agent_run_lifecycle():
    async def scenario():
        conn, repo = await open_repo()
        task = await new_task(repo)
        started = await repo.start_agent_run(
            "run-1", task_id=task.id, agent="claude", stage=Stage.OPINION
        )
        await repo.start_agent_run("run-2", task_id=task.id, agent="codex", stage=Stage.OPINION)
        running = await repo.list_running_agent_runs(task.id)
        ok = await repo.finish_agent_run(
            "run-1", status=RunStatus.OK, exit_code=0, log_path="/runs/run-1.log"
        )
        invalid = await repo.finish_agent_run(
            "run-2", status=INVALID_OUTPUT, exit_code=0, log_path=None
        )
        with pytest.raises(StaleStateError):
            await repo.finish_agent_run("run-1", status=RunStatus.ERROR, exit_code=1, log_path=None)
        with pytest.raises(ValueError):
            await repo.finish_agent_run("run-2", status="weird", exit_code=0, log_path=None)
        left = await repo.list_running_agent_runs()
        await conn.close()
        return started, running, ok, invalid, left

    started, running, ok, invalid, left = run(scenario())
    assert started.status == RUNNING and started.finished_at is None
    assert [r.id for r in running] == ["run-1", "run-2"]
    assert (ok.status, ok.exit_code, ok.log_path) == ("ok", 0, "/runs/run-1.log")
    assert ok.finished_at is not None
    assert invalid.status == "invalid_output"
    assert left == []


def test_agent_run_requires_existing_task():
    async def scenario():
        conn, repo = await open_repo()
        with pytest.raises(aiosqlite.IntegrityError):
            await repo.start_agent_run("run-1", task_id=999, agent="claude", stage=Stage.OPINION)
        await conn.close()

    run(scenario())


def test_v1_database_is_upgraded_with_defaults(tmp_path):
    """Phase 1에서 만든 실제 DB(v1)가 v2로 올라가고 기존 작업은 기본값을 받는다."""
    import sqlite3

    from bot.store.db import MIGRATIONS

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
    raw.execute(
        "INSERT INTO tasks (project, discord_thread, title, state, created_at, updated_at) "
        "VALUES ('gagaessi', 't', '제목', 'DESIGNING', 'x', 'x')"
    )
    raw.commit()
    raw.close()

    async def scenario():
        conn, repo = await open_repo(path)
        try:
            return await schema_version(conn), await repo.get_task(1)
        finally:
            await conn.close()

    version, task = run(scenario())
    assert version == SCHEMA_VERSION
    assert (task.review_limit_bonus, task.test_limit_bonus, task.recommended_fixed) == (0, 0, 0)


def test_v2_runs_get_engine_from_legacy_agent_name(tmp_path):
    """ADR-005 기준 실행 기록(agent=claude/codex)은 v3에서 엔진 열을 그 이름으로 채운다."""
    import sqlite3

    from bot.store.db import MIGRATIONS

    path = tmp_path / "v2.db"
    raw = sqlite3.connect(path)
    raw.executescript(MIGRATIONS[0] + MIGRATIONS[1] + "\nPRAGMA user_version = 2;")
    raw.execute(
        "INSERT INTO tasks (project, discord_thread, title, state, created_at, updated_at) "
        "VALUES ('gagaessi', 't', '제목', 'DONE', 'x', 'x')"
    )
    raw.execute(
        "INSERT INTO agent_runs (id, task_id, agent, stage, status, started_at) "
        "VALUES ('r1', 1, 'codex', 'review', 'ok', 'x')"
    )
    raw.commit()
    raw.close()

    async def scenario():
        conn, repo = await open_repo(path)
        try:
            return await repo.get_agent_run("r1")
        finally:
            await conn.close()

    record = run(scenario())
    assert (record.agent, record.engine) == ("codex", "codex")


def test_bot_state_and_schedule_runs(tmp_path):
    async def scenario():
        conn, repo = await open_repo(tmp_path / "s.db")
        try:
            assert await repo.get_state("k") is None
            await repo.set_state("k", "1")
            await repo.set_state("k", "2")
            value = await repo.get_state("k")
            await repo.set_state("k", None)
            await repo.record_schedule_run("morning_report", "2026-10-28T00:00:00+00:00")
            await repo.record_schedule_run("morning_report", "2026-10-29T00:00:00+00:00")
            return value, await repo.get_state("k"), await repo.schedule_runs()
        finally:
            await conn.close()

    value, cleared, runs = run(scenario())
    assert (value, cleared) == ("2", None)
    assert runs == {"morning_report": "2026-10-29T00:00:00+00:00"}


# 교육 주간 문제


def test_quiz_keeps_first_answer_and_scores(tmp_path):
    async def scenario():
        conn, repo = await open_repo(tmp_path / "zerosync.db")
        quiz = await repo.add_quiz("weekly", "week:2026-W45", "[]")
        with pytest.raises(sqlite3.IntegrityError):
            await repo.add_quiz("weekly", "week:2026-W45", "[]")
        await repo.add_quiz("note", "note:Learning/개념/2026-11-02 가", "[1]")
        first = await repo.record_quiz_answer(quiz, 1, "B", True)
        again = await repo.record_quiz_answer(quiz, 1, "C", False)
        await repo.record_quiz_answer(quiz, 2, "A", False)
        found = (
            await repo.get_quiz(quiz),
            await repo.quiz_by_key("week:2026-W45"),
            await repo.quiz_by_key("week:2026-W44"),
            await repo.quiz_score(quiz),
            await repo.answered_numbers(quiz),
            await repo.list_quizzes("note"),
        )
        await conn.close()
        return first, again, found

    first, again, (stored, week, missing, score, numbers, notes) = run(scenario())
    assert (first, again) == ("B", "B")
    assert stored == ("week:2026-W45", "[]") and week is not None and missing is None
    assert score == (2, 1) and numbers == {1, 2}
    assert notes == [(2, "note:Learning/개념/2026-11-02 가", "[1]")]
