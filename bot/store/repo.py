"""tasks, events, agent_runs 접근 (SPEC 8).

상태 변경은 이벤트 기록과 함께 한 트랜잭션으로 저장한다 (SPEC 3.4).
하나의 연결을 여러 코루틴이 함께 쓰므로 트랜잭션은 락으로 순서를 지킨다.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import aiosqlite

from bot.agents.base import AgentName, EngineName, RunStatus, Stage
from bot.clock import Clock, to_iso
from bot.workflow.state import TaskState


class Actor(StrEnum):
    """사람과 봇. 에이전트는 회사 설정의 이름(beta, alpha …)을 문자열로 그대로 쓴다.

    CLAUDE·CODEX는 ADR-005 기준 기록(엔진 이름이 곧 에이전트 이름)을 읽기 위해 남긴다.
    """

    ZERO = "zero"
    CLAUDE = "claude"
    CODEX = "codex"
    BOT = "bot"


ActorName = Actor | str


class EventKind(StrEnum):
    MESSAGE = "message"
    OPINION = "opinion"
    DEBATE = "debate"
    DECISION = "decision"
    DESIGN = "design"
    IMPL = "impl"
    TEST = "test"
    REVIEW = "review"
    FIX = "fix"
    DOC_SYNC = "doc_sync"
    PLAN = "plan"
    UI_DESIGN = "ui_design"
    DIAGNOSIS = "diagnosis"
    LEAD_CHECK = "lead_check"
    RELEASE = "release"
    REPORT = "report"
    COMMAND = "command"
    MERGE = "merge"
    ERROR = "error"


# 상태 전이와 함께 바꿀 수 있는 tasks 필드
UPDATABLE_FIELDS = frozenset(
    {
        "prev_state",
        "review_round",
        "test_retry",
        "opinion_rounds",
        "issue_number",
        "pr_number",
        "branch",
        "worktree",
        "claude_session",
        "waiting_since",
        "reminded_at",
        "review_limit_bonus",
        "test_limit_bonus",
        "recommended_fixed",
        "flags",
        "discord_thread",
    }
)


class StaleStateError(RuntimeError):
    """작업 상태가 예상과 다르다. 다른 코루틴이 먼저 상태를 바꿨다."""


class NotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class NewEvent:
    actor: ActorName
    kind: EventKind
    payload: Any  # JSON으로 저장할 수 있는 값 (검증된 스키마 원본은 model_dump(mode="json"))
    discord_message: str | None = None


@dataclass(frozen=True)
class TaskRecord:
    id: int
    project: str
    discord_thread: str
    title: str
    state: TaskState
    prev_state: TaskState | None
    review_round: int
    test_retry: int
    opinion_rounds: int
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    worktree: str | None
    claude_session: str | None
    waiting_since: str | None
    reminded_at: str | None
    created_at: str
    updated_at: str
    review_limit_bonus: int = 0
    test_limit_bonus: int = 0
    recommended_fixed: int = 0
    pipeline: str = "legacy"  # pipelines/의 이름. v6 전 작업은 legacy (ADR-005 흐름)
    flags: str = ""  # 단계 when에 쓰는 사실 (쉼표 목록, 예: has_ui_change)

    @property
    def flag_set(self) -> frozenset[str]:
        return frozenset(flag for flag in self.flags.split(",") if flag)


@dataclass(frozen=True)
class EventRecord:
    id: int
    task_id: int
    actor: ActorName
    kind: EventKind
    payload: Any
    discord_message: str | None
    created_at: str


@dataclass(frozen=True)
class AgentRunRecord:
    id: str
    task_id: int | None  # 채팅 답은 작업이 없다
    agent: AgentName
    stage: Stage
    exit_code: int | None
    status: str  # running | ok | invalid_output | timeout | cancelled | error
    log_path: str | None
    started_at: str
    finished_at: str | None
    engine: str | None = None  # 실행한 엔진 (v3부터 기록)
    project: str | None = None  # 채팅 답의 제품 (v6부터)


RUNNING = "running"
INVALID_OUTPUT = "invalid_output"


class HiringStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class HiringRecord:
    id: int
    task_id: int | None
    proposed_by: str
    payload: Any  # HiringProposal JSON
    evidence: Any  # 봇이 계산한 숫자 근거
    status: HiringStatus
    decided_note: str | None
    created_at: str
    decided_at: str | None


class Repository:
    def __init__(self, conn: aiosqlite.Connection, clock: Clock):
        self._conn = conn
        self._clock = clock
        self._lock = asyncio.Lock()

    def _now(self) -> str:
        return to_iso(self._clock.now())

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
            await self._conn.execute("COMMIT")

    # tasks

    async def create_task(
        self,
        *,
        project: str,
        discord_thread: str,
        title: str,
        state: TaskState,
        event: NewEvent,
        updates: Mapping[str, object] | None = None,
        pipeline: str | None = None,
    ) -> TaskRecord:
        fields = _checked_updates(updates or {})
        if pipeline is not None:
            fields["pipeline"] = pipeline
        now = self._now()
        if state.waits_for_zero:
            fields.setdefault("waiting_since", now)
        columns = ["project", "discord_thread", "title", "state", "created_at", "updated_at"]
        values: list[object] = [project, discord_thread, title, state.value, now, now]
        columns += list(fields)
        values += list(fields.values())
        async with self._transaction():
            cursor = await self._conn.execute(
                f"INSERT INTO tasks ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            task_id = cursor.lastrowid
            assert task_id is not None
            await self._insert_event(task_id, event, now)
        return await self.get_task(task_id)

    async def get_task(self, task_id: int) -> TaskRecord:
        row = await self._fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise NotFoundError(f"작업 {task_id}가 없다")
        return _task(row)

    async def find_task_by_thread(self, discord_thread: str) -> TaskRecord | None:
        row = await self._fetchone(
            "SELECT * FROM tasks WHERE discord_thread = ?", (discord_thread,)
        )
        return None if row is None else _task(row)

    async def list_tasks(self, states: set[TaskState] | None = None) -> list[TaskRecord]:
        if states:
            marks = ", ".join("?" for _ in states)
            rows = await self._fetchall(
                f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY id",
                [s.value for s in states],
            )
        else:
            rows = await self._fetchall("SELECT * FROM tasks ORDER BY id", ())
        return [_task(row) for row in rows]

    async def apply_transition(
        self,
        task_id: int,
        *,
        expected_state: TaskState,
        new_state: TaskState,
        event: NewEvent,
        updates: Mapping[str, object] | None = None,
    ) -> TaskRecord:
        """상태와 필드를 바꾸고 이벤트를 기록한다. 현재 상태가 expected_state가 아니면 실패한다.

        Zero 대기 상태에 들어가거나 그 상태에서 Zero가 입력하면 대기 시작 시각을 새로 적고,
        대기 상태를 벗어나면 지운다. 재멘션(운영 규약 2.5)의 기준이다.
        """
        fields = _checked_updates(updates or {})
        if "waiting_since" not in fields:
            if new_state.waits_for_zero:
                if new_state is not expected_state or event.actor == Actor.ZERO:
                    fields.update(waiting_since=self._now(), reminded_at=None)
            else:
                fields.update(waiting_since=None, reminded_at=None)
        now = self._now()
        assignments = ["state = ?", "updated_at = ?"] + [f"{name} = ?" for name in fields]
        values: list[object] = [new_state.value, now, *fields.values(), task_id]
        values.append(expected_state.value)
        async with self._transaction():
            cursor = await self._conn.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ? AND state = ?",
                values,
            )
            if cursor.rowcount != 1:
                current = await self._fetchone("SELECT state FROM tasks WHERE id = ?", (task_id,))
                if current is None:
                    raise NotFoundError(f"작업 {task_id}가 없다")
                raise StaleStateError(
                    f"작업 {task_id}의 상태가 {expected_state}가 아니라 {current['state']}이다"
                )
            await self._insert_event(task_id, event, now)
        return await self.get_task(task_id)

    async def update_fields(self, task_id: int, updates: Mapping[str, object]) -> TaskRecord:
        """상태는 그대로 두고 필드만 바꾼다 (Issue·PR 번호, 브랜치, worktree, 세션 등)."""
        fields = _checked_updates(updates)
        if not fields:
            return await self.get_task(task_id)
        assignments = ["updated_at = ?"] + [f"{name} = ?" for name in fields]
        async with self._transaction():
            cursor = await self._conn.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
                [self._now(), *fields.values(), task_id],
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"작업 {task_id}가 없다")
        return await self.get_task(task_id)

    # events

    async def add_event(self, task_id: int, event: NewEvent) -> EventRecord:
        now = self._now()
        async with self._transaction():
            event_id = await self._insert_event(task_id, event, now)
        row = await self._fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
        assert row is not None
        return _event(row)

    async def list_events(
        self, task_id: int, kinds: set[EventKind] | None = None
    ) -> list[EventRecord]:
        sql = "SELECT * FROM events WHERE task_id = ?"
        params: list[object] = [task_id]
        if kinds:
            sql += f" AND kind IN ({', '.join('?' for _ in kinds)})"
            params += [k.value for k in kinds]
        rows = await self._fetchall(sql + " ORDER BY id", params)
        return [_event(row) for row in rows]

    async def _insert_event(self, task_id: int, event: NewEvent, now: str) -> int:
        cursor = await self._conn.execute(
            "INSERT INTO events (task_id, actor, kind, payload, discord_message, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                str(event.actor),
                event.kind.value,
                json.dumps(event.payload, ensure_ascii=False),
                event.discord_message,
                now,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    # agent_runs

    async def start_agent_run(
        self,
        run_id: str,
        *,
        task_id: int | None,
        agent: AgentName,
        stage: Stage,
        engine: EngineName | None = None,
        project: str | None = None,
    ) -> AgentRunRecord:
        async with self._transaction():
            await self._conn.execute(
                "INSERT INTO agent_runs "
                "(id, task_id, agent, stage, status, started_at, engine, project) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, task_id, agent, stage.value, RUNNING, self._now(), engine, project),
            )
        return await self.get_agent_run(run_id)

    async def finish_agent_run(
        self,
        run_id: str,
        *,
        status: RunStatus | str,
        exit_code: int | None,
        log_path: str | None,
    ) -> AgentRunRecord:
        """status는 러너 결과(RunStatus) 또는 출력 검증 실패 시 INVALID_OUTPUT."""
        status_value = str(status)
        if status_value not in {s.value for s in RunStatus} | {INVALID_OUTPUT}:
            raise ValueError(f"알 수 없는 실행 상태: {status_value}")
        async with self._transaction():
            cursor = await self._conn.execute(
                "UPDATE agent_runs SET status = ?, exit_code = ?, log_path = ?, finished_at = ? "
                "WHERE id = ? AND status = ?",
                (status_value, exit_code, log_path, self._now(), run_id, RUNNING),
            )
            if cursor.rowcount != 1:
                raise StaleStateError(f"실행 {run_id}가 없거나 이미 끝났다")
        return await self.get_agent_run(run_id)

    async def get_agent_run(self, run_id: str) -> AgentRunRecord:
        row = await self._fetchone("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFoundError(f"실행 {run_id}가 없다")
        return AgentRunRecord(**{key: row[key] for key in row.keys()})

    async def list_running_agent_runs(self, task_id: int | None = None) -> list[AgentRunRecord]:
        sql, params = "SELECT * FROM agent_runs WHERE status = ?", [RUNNING]
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        rows = await self._fetchall(sql + " ORDER BY started_at", params)
        return [AgentRunRecord(**{key: row[key] for key in row.keys()}) for row in rows]

    # hiring_proposals (운영 규약 1.12)

    async def add_hiring_proposal(
        self, *, task_id: int | None, proposed_by: str, payload: Any, evidence: Any
    ) -> HiringRecord:
        async with self._transaction():
            cursor = await self._conn.execute(
                "INSERT INTO hiring_proposals "
                "(task_id, proposed_by, payload, evidence, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    proposed_by,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    HiringStatus.OPEN.value,
                    self._now(),
                ),
            )
            proposal_id = cursor.lastrowid
        assert proposal_id is not None
        return await self.get_hiring_proposal(proposal_id)

    async def get_hiring_proposal(self, proposal_id: int) -> HiringRecord:
        row = await self._fetchone("SELECT * FROM hiring_proposals WHERE id = ?", (proposal_id,))
        if row is None:
            raise NotFoundError(f"채용 제안 {proposal_id}가 없다")
        return _hiring(row)

    async def list_hiring_proposals(
        self, *, proposed_by: str | None = None, status: HiringStatus | None = None
    ) -> list[HiringRecord]:
        sql, params = "SELECT * FROM hiring_proposals WHERE 1 = 1", []
        if proposed_by is not None:
            sql += " AND proposed_by = ?"
            params.append(proposed_by)
        if status is not None:
            sql += " AND status = ?"
            params.append(status.value)
        rows = await self._fetchall(sql + " ORDER BY id", params)
        return [_hiring(row) for row in rows]

    async def decide_hiring_proposal(
        self, proposal_id: int, *, accept: bool, note: str
    ) -> HiringRecord:
        """열려 있는 제안에만 답할 수 있다. 이미 답한 제안이면 StaleStateError."""
        status = HiringStatus.ACCEPTED if accept else HiringStatus.REJECTED
        async with self._transaction():
            cursor = await self._conn.execute(
                "UPDATE hiring_proposals SET status = ?, decided_note = ?, decided_at = ? "
                "WHERE id = ? AND status = ?",
                (status.value, note or None, self._now(), proposal_id, HiringStatus.OPEN.value),
            )
            if cursor.rowcount != 1:
                current = await self._fetchone(
                    "SELECT status FROM hiring_proposals WHERE id = ?", (proposal_id,)
                )
                if current is None:
                    raise NotFoundError(f"채용 제안 {proposal_id}가 없다")
                raise StaleStateError(f"채용 제안 {proposal_id}는 이미 {current['status']}이다")
        return await self.get_hiring_proposal(proposal_id)

    async def count_agent_runs(self, agent: str) -> dict[str, int]:
        """에이전트의 실행 결과별 횟수. 채용 제안의 숫자 근거로 쓴다."""
        rows = await self._fetchall(
            "SELECT status, COUNT(*) AS n FROM agent_runs WHERE agent = ? GROUP BY status",
            (agent,),
        )
        return {row["status"]: row["n"] for row in rows}

    async def count_agent_runs_since(self, agent: str, since: str) -> int:
        """since(UTC ISO) 이후 시작한 이 에이전트의 실행 수. 하루 예산 검사에 쓴다 (SPEC Q18)."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM agent_runs WHERE agent = ? AND started_at >= ?",
            (agent, since),
        )
        return int(row["n"]) if row is not None else 0

    async def agent_run_stats_since(self, since: str) -> dict[str, dict[str, int]]:
        """since 이후 에이전트별·결과별 실행 수 (보고, 예산 추이)."""
        rows = await self._fetchall(
            "SELECT agent, status, COUNT(*) AS n FROM agent_runs WHERE started_at >= ? "
            "GROUP BY agent, status",
            (since,),
        )
        stats: dict[str, dict[str, int]] = {}
        for row in rows:
            stats.setdefault(row["agent"], {})[row["status"]] = row["n"]
        return stats

    async def count_events_since(self, kind: EventKind, since: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM events WHERE kind = ? AND created_at >= ?",
            (kind.value, since),
        )
        return int(row["n"]) if row is not None else 0

    # 작업별 사용량 (파이프라인 예산, SPEC Q18)

    async def task_usage(self, task_id: int) -> tuple[int, float]:
        """이 작업의 에이전트 실행 수와 실행 시간 합(초)."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM((julianday(COALESCE(finished_at, started_at))"
            " - julianday(started_at)) * 86400), 0) AS sec FROM agent_runs WHERE task_id = ?",
            (task_id,),
        )
        assert row is not None
        return int(row["n"]), float(row["sec"])

    # chat_turns (제품 채팅 채널의 대화, ADR-007)

    async def add_chat_turn(
        self,
        project: str,
        actor: ActorName,
        kind: str,
        payload: Any,
        discord_message: str | None = None,
    ) -> int:
        async with self._transaction():
            cursor = await self._conn.execute(
                "INSERT INTO chat_turns "
                "(project, actor, kind, payload, discord_message, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project,
                    str(actor),
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    discord_message,
                    self._now(),
                ),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def recent_chat_turns(self, project: str, limit: int = 20) -> list[dict[str, Any]]:
        """작업으로 옮기지 않은 최근 대화. 오래된 것부터."""
        rows = await self._fetchall(
            "SELECT * FROM chat_turns WHERE project = ? AND task_id IS NULL "
            "ORDER BY id DESC LIMIT ?",
            (project, limit),
        )
        return [
            {**{k: row[k] for k in row.keys()}, "payload": json.loads(row["payload"])}
            for row in reversed(rows)
        ]

    async def attach_chat_turns(self, project: str, task_id: int) -> None:
        """/task로 작업을 열면 그 전 대화를 작업에 붙인다. 다음 대화는 새로 시작한다."""
        async with self._transaction():
            await self._conn.execute(
                "UPDATE chat_turns SET task_id = ? WHERE project = ? AND task_id IS NULL",
                (task_id, project),
            )

    # ideas (/idea, ADR-007)

    async def add_idea(self, project: str, body: str) -> int:
        async with self._transaction():
            cursor = await self._conn.execute(
                "INSERT INTO ideas (project, body, status, created_at) VALUES (?, ?, 'open', ?)",
                (project, body, self._now()),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def list_ideas(self, project: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM ideas", []
        if project is not None:
            sql += " WHERE project = ?"
            params.append(project)
        rows = await self._fetchall(sql + " ORDER BY id", params)
        return [{k: row[k] for k in row.keys()} for row in rows]

    # 교육 학습 기록 (ADR-009). 하루에 개념 1개, 완료는 Zero가 Discord 버튼으로 표시한다

    async def add_study_day(self, day: str, target: str, label: str, carried: bool) -> None:
        async with self._transaction():
            await self._conn.execute(
                "INSERT INTO study_days (day, target, label, carried, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (day, target, label, int(carried), self._now()),
            )

    async def study_days(self, start: str = "", end: str = "9999") -> list[dict[str, Any]]:
        """start <= day <= end, 날짜 순. 날짜는 YYYY-MM-DD 문자열."""
        rows = await self._fetchall(
            "SELECT * FROM study_days WHERE day >= ? AND day <= ? ORDER BY day", (start, end)
        )
        return [{k: row[k] for k in row.keys()} for row in rows]

    async def mark_study_done(self, day: str) -> tuple[str, bool] | None:
        """그날 개념을 완료로 표시한다. 같은 개념을 이어 한 날도 함께 표시한다.

        (개념 이름, 이번에 새로 표시했는가). 그날 기록이 없으면 None.
        """
        row = await self._fetchone(
            "SELECT target, label, done_at FROM study_days WHERE day = ?", (day,)
        )
        if row is None:
            return None
        if row["done_at"] is not None:
            return str(row["label"]), False
        async with self._transaction():
            await self._conn.execute(
                "UPDATE study_days SET done_at = ? WHERE target = ? AND done_at IS NULL",
                (self._now(), row["target"]),
            )
        return str(row["label"]), True

    # 교육 문제 (ADR-009)

    async def add_quiz(self, kind: str, key: str, payload: str) -> int:
        """kind: note | review | weekly. key가 이미 있으면 IntegrityError."""
        async with self._transaction():
            cursor = await self._conn.execute(
                "INSERT INTO quizzes (kind, key, payload, created_at) VALUES (?, ?, ?, ?)",
                (kind, key, payload, self._now()),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def get_quiz(self, quiz_id: int) -> tuple[str, str] | None:
        """(key, payload)."""
        row = await self._fetchone("SELECT key, payload FROM quizzes WHERE id = ?", (quiz_id,))
        return None if row is None else (str(row["key"]), str(row["payload"]))

    async def quiz_by_key(self, key: str) -> int | None:
        row = await self._fetchone("SELECT id FROM quizzes WHERE key = ?", (key,))
        return None if row is None else int(row["id"])

    async def list_quizzes(self, kind: str) -> list[tuple[int, str, str]]:
        """(id, key, payload), key 순."""
        rows = await self._fetchall(
            "SELECT id, key, payload FROM quizzes WHERE kind = ? ORDER BY key", (kind,)
        )
        return [(int(r["id"]), str(r["key"]), str(r["payload"])) for r in rows]

    async def answered_numbers(self, quiz_id: int) -> set[int]:
        rows = await self._fetchall("SELECT number FROM quiz_answers WHERE quiz_id = ?", (quiz_id,))
        return {int(row["number"]) for row in rows}

    async def record_quiz_answer(
        self, quiz_id: int, number: int, choice: str, correct: bool
    ) -> str:
        """처음 고른 답만 남긴다. 기록된 답(이번 것이거나 이전 것)을 돌려준다."""
        async with self._transaction():
            await self._conn.execute(
                "INSERT OR IGNORE INTO quiz_answers "
                "(quiz_id, number, choice, correct, answered_at) VALUES (?, ?, ?, ?, ?)",
                (quiz_id, number, choice, int(correct), self._now()),
            )
        row = await self._fetchone(
            "SELECT choice FROM quiz_answers WHERE quiz_id = ? AND number = ?",
            (quiz_id, number),
        )
        assert row is not None
        return str(row["choice"])

    async def quiz_score(self, quiz_id: int) -> tuple[int, int]:
        """(답한 문제 수, 맞힌 문제 수)."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(correct), 0) AS ok "
            "FROM quiz_answers WHERE quiz_id = ?",
            (quiz_id,),
        )
        assert row is not None
        return int(row["n"]), int(row["ok"])

    # bot_state, schedule_runs

    async def get_state(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM bot_state WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    async def set_state(self, key: str, value: str | None) -> None:
        """value가 None이면 지운다."""
        async with self._transaction():
            if value is None:
                await self._conn.execute("DELETE FROM bot_state WHERE key = ?", (key,))
            else:
                await self._conn.execute(
                    "INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (key, value, self._now()),
                )

    async def schedule_runs(self) -> dict[str, str]:
        rows = await self._fetchall("SELECT job, last_run_at FROM schedule_runs", ())
        return {row["job"]: row["last_run_at"] for row in rows}

    async def record_schedule_run(self, job: str, at: str) -> None:
        async with self._transaction():
            await self._conn.execute(
                "INSERT INTO schedule_runs (job, last_run_at) VALUES (?, ?) "
                "ON CONFLICT(job) DO UPDATE SET last_run_at = excluded.last_run_at",
                (job, at),
            )

    async def count_errors(self) -> int:
        """기록된 오류 이벤트 수 (단계 실패, 형식 오류, 재시작 중단 등)."""
        row = await self._fetchone("SELECT COUNT(*) AS n FROM events WHERE kind = 'error'", ())
        return int(row["n"]) if row is not None else 0

    # helpers

    async def _fetchone(self, sql: str, params: Any) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: Any) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())


def _checked_updates(updates: Mapping[str, object]) -> dict[str, object]:
    unknown = set(updates) - UPDATABLE_FIELDS
    if unknown:
        raise ValueError(f"바꿀 수 없는 필드: {', '.join(sorted(unknown))}")
    return {
        name: value.value if isinstance(value, StrEnum) else value
        for name, value in updates.items()
    }


def _task(row: aiosqlite.Row) -> TaskRecord:
    data = {key: row[key] for key in row.keys()}
    data["state"] = TaskState(data["state"])
    data["prev_state"] = TaskState(data["prev_state"]) if data["prev_state"] else None
    return TaskRecord(**data)


def _actor(value: str) -> ActorName:
    try:
        return Actor(value)
    except ValueError:
        return value  # 에이전트 이름


def _hiring(row: aiosqlite.Row) -> HiringRecord:
    data = {key: row[key] for key in row.keys()}
    data["payload"] = json.loads(data["payload"])
    data["evidence"] = json.loads(data["evidence"])
    data["status"] = HiringStatus(data["status"])
    return HiringRecord(**data)


def _event(row: aiosqlite.Row) -> EventRecord:
    return EventRecord(
        id=row["id"],
        task_id=row["task_id"],
        actor=_actor(row["actor"]),
        kind=EventKind(row["kind"]),
        payload=json.loads(row["payload"]),
        discord_message=row["discord_message"],
        created_at=row["created_at"],
    )
