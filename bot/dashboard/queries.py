"""대시보드용 읽기 전용 조회 (SPEC 8의 tasks·events·agent_runs).

봇과 같은 DB를 읽기 전용(`mode=ro`)으로 연다. 쓰기는 하지 않는다.
요청마다 연결을 새로 열어 스레드 간에 연결을 나눠 쓰지 않는다.
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DURATION = "(julianday(r.finished_at) - julianday(r.started_at)) * 86400"


class DatabaseMissing(FileNotFoundError):
    """봇이 아직 한 번도 실행되지 않아 DB가 없다."""


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise DatabaseMissing(f"DB가 없다: {path}. 봇을 한 번 실행하면 만들어진다")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class ProjectSummary:
    project: str
    tasks: int
    by_state: dict[str, int]
    runs: int
    by_status: dict[str, int]


@dataclass(frozen=True)
class StageStat:
    agent: str
    stage: str
    runs: int
    avg_sec: float | None
    by_status: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskRow:
    id: int
    project: str
    discord_thread: str
    title: str
    state: str
    prev_state: str | None
    opinion_rounds: int
    review_round: int
    test_retry: int
    created_at: str
    updated_at: str
    runs: int


@dataclass(frozen=True)
class EventRow:
    id: int
    actor: str
    kind: str
    payload: Any
    discord_message: str | None
    created_at: str


@dataclass(frozen=True)
class RunRow:
    id: str
    task_id: int | None  # 채팅 의견·교육·비서 실행은 작업이 없다
    project: str | None
    title: str | None
    agent: str
    stage: str
    status: str
    exit_code: int | None
    log_path: str | None
    started_at: str
    finished_at: str | None
    duration_sec: float | None


def project_summaries(conn: sqlite3.Connection) -> list[ProjectSummary]:
    states: dict[str, Counter[str]] = {}
    for row in conn.execute("SELECT project, state, COUNT(*) AS n FROM tasks GROUP BY 1, 2"):
        states.setdefault(row["project"], Counter())[row["state"]] = row["n"]
    statuses: dict[str, Counter[str]] = {}
    for row in conn.execute(
        "SELECT t.project, r.status, COUNT(*) AS n FROM agent_runs r "
        "JOIN tasks t ON t.id = r.task_id GROUP BY 1, 2"
    ):
        statuses.setdefault(row["project"], Counter())[row["status"]] = row["n"]
    return [
        ProjectSummary(
            project=project,
            tasks=sum(states.get(project, Counter()).values()),
            by_state=dict(states.get(project, Counter())),
            runs=sum(statuses.get(project, Counter()).values()),
            by_status=dict(statuses.get(project, Counter())),
        )
        for project in sorted(set(states) | set(statuses))
    ]


def stage_stats(conn: sqlite3.Connection) -> list[StageStat]:
    """에이전트·단계별 실행 수와 평균 소요 시간.

    평균은 성공한 실행만 넣는다. 취소·시간 초과가 섞이면 실제 응답 시간을 알 수 없다.
    """
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(
        f"SELECT r.agent, r.stage, r.status, COUNT(*) AS n, AVG({_DURATION}) AS avg_sec "
        "FROM agent_runs r GROUP BY 1, 2, 3"
    ):
        entry = stats.setdefault(
            (row["agent"], row["stage"]), {"runs": 0, "weighted": 0.0, "timed": 0, "by": {}}
        )
        entry["runs"] += row["n"]
        entry["by"][row["status"]] = row["n"]
        if row["status"] == "ok" and row["avg_sec"] is not None:
            entry["weighted"] += row["avg_sec"] * row["n"]
            entry["timed"] += row["n"]
    return [
        StageStat(
            agent,
            stage,
            entry["runs"],
            entry["weighted"] / entry["timed"] if entry["timed"] else None,
            entry["by"],
        )
        for (agent, stage), entry in sorted(stats.items())
    ]


_TASK_SELECT = (
    "SELECT t.*, (SELECT COUNT(*) FROM agent_runs r WHERE r.task_id = t.id) AS runs FROM tasks t"
)


def _task(row: sqlite3.Row) -> TaskRow:
    return TaskRow(**{name: row[name] for name in TaskRow.__dataclass_fields__})


def list_tasks(
    conn: sqlite3.Connection, *, project: str | None = None, state: str | None = None
) -> list[TaskRow]:
    where, params = [], []
    if project:
        where.append("t.project = ?")
        params.append(project)
    if state:
        where.append("t.state = ?")
        params.append(state)
    sql = _TASK_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    return [_task(row) for row in conn.execute(sql + " ORDER BY t.id DESC", params)]


def get_task(conn: sqlite3.Connection, task_id: int) -> TaskRow | None:
    row = conn.execute(_TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone()
    return None if row is None else _task(row)


def task_events(conn: sqlite3.Connection, task_id: int) -> list[EventRow]:
    rows = conn.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,))
    return [
        EventRow(
            row["id"],
            row["actor"],
            row["kind"],
            json.loads(row["payload"]),
            row["discord_message"],
            row["created_at"],
        )
        for row in rows
    ]


_RUN_SELECT = (
    f"SELECT r.*, t.project, t.title, {_DURATION} AS duration_sec "
    "FROM agent_runs r LEFT JOIN tasks t ON t.id = r.task_id"
)


def _run(row: sqlite3.Row) -> RunRow:
    return RunRow(**{name: row[name] for name in RunRow.__dataclass_fields__})


def list_runs(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    status: str | None = None,
    task_id: int | None = None,
    agent: str | None = None,
    limit: int = 200,
) -> list[RunRow]:
    where, params = [], []
    if agent:
        where.append("r.agent = ?")
        params.append(agent)
    if project:
        where.append("t.project = ?")
        params.append(project)
    if status:
        where.append("r.status = ?")
        params.append(status)
    if task_id is not None:
        where.append("r.task_id = ?")
        params.append(task_id)
    sql = _RUN_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY r.started_at DESC, r.id LIMIT ?"
    return [_run(row) for row in conn.execute(sql, [*params, limit])]


@dataclass(frozen=True)
class AgentTask:
    task_id: int
    project: str
    title: str
    state: str
    runs: int
    last_at: str


@dataclass(frozen=True)
class AgentWork:
    """직원 한 명이 한 일 전체: 단계·결과별 실행 수와 맡았던 작업."""

    by_stage: dict[str, dict[str, int]]
    tasks: list[AgentTask]
    taskless: int  # 작업 없이 한 실행 (채팅 의견, 교육, 비서)

    @property
    def total(self) -> int:
        return sum(sum(by.values()) for by in self.by_stage.values())


def agent_work(conn: sqlite3.Connection, agent: str) -> AgentWork:
    by_stage: dict[str, dict[str, int]] = {}
    taskless = 0
    for row in conn.execute(
        "SELECT stage, status, task_id IS NULL AS taskless, COUNT(*) AS n FROM agent_runs "
        "WHERE agent = ? GROUP BY 1, 2, 3",
        (agent,),
    ):
        stage = by_stage.setdefault(row["stage"], {})
        stage[row["status"]] = stage.get(row["status"], 0) + row["n"]
        taskless += row["n"] if row["taskless"] else 0
    tasks = [
        AgentTask(*tuple(row))
        for row in conn.execute(
            "SELECT t.id, t.project, t.title, t.state, COUNT(*), MAX(r.started_at) "
            "FROM agent_runs r JOIN tasks t ON t.id = r.task_id WHERE r.agent = ? "
            "GROUP BY t.id ORDER BY MAX(r.started_at) DESC",
            (agent,),
        )
    ]
    return AgentWork(by_stage, tasks, taskless)


def get_run(conn: sqlite3.Connection, run_id: str) -> RunRow | None:
    row = conn.execute(_RUN_SELECT + " WHERE r.id = ?", (run_id,)).fetchone()
    return None if row is None else _run(row)


@dataclass(frozen=True)
class AgentActivity:
    """직원 한 명의 지금 일과 최근 기록 (직원 화면)."""

    running: list[RunRow]
    today: int
    week: dict[str, int]  # 최근 7일 결과별 실행 수
    recent: list[RunRow]


def agent_activity(
    conn: sqlite3.Connection, agents: Sequence[str], *, today_start: str, week_start: str
) -> dict[str, AgentActivity]:
    activity = {}
    for agent in agents:
        today = conn.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE agent = ? AND started_at >= ?",
            (agent, today_start),
        ).fetchone()[0]
        week = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM agent_runs "
                "WHERE agent = ? AND started_at >= ? GROUP BY status",
                (agent, week_start),
            )
        }
        activity[agent] = AgentActivity(
            running=list_runs(conn, agent=agent, status="running", limit=10),
            today=today,
            week=week,
            recent=list_runs(conn, agent=agent, limit=5),
        )
    return activity


@dataclass(frozen=True)
class IdeaRow:
    id: int
    project: str
    body: str
    status: str
    task_id: int | None
    created_at: str


def list_ideas(conn: sqlite3.Connection, *, project: str | None = None) -> list[IdeaRow]:
    """/idea로 남긴 아이디어. 새것부터 (ADR-007)."""
    sql = "SELECT id, project, body, status, task_id, created_at FROM ideas"
    params: tuple[Any, ...] = ()
    if project:
        sql, params = sql + " WHERE project = ?", (project,)
    rows = _optional(conn, sql + " ORDER BY id DESC", params)
    return [IdeaRow(*tuple(row)) for row in rows]


@dataclass(frozen=True)
class StudyRow:
    day: str
    target: str
    label: str
    carried: bool
    done_at: str | None


def schedule_runs(conn: sqlite3.Connection) -> dict[str, str]:
    """일정별 마지막 실행 시각 (SPEC 6.6). 테이블이 없으면 빈 사전."""
    return {
        row["job"]: row["last_run_at"]
        for row in _optional(conn, "SELECT job, last_run_at FROM schedule_runs")
    }


def study_days(conn: sqlite3.Connection, *, limit: int = 30) -> list[StudyRow]:
    """교육 학습 기록 (ADR-009). 최근 날짜부터. 스키마 v8 전이면 빈 목록."""
    rows = _optional(
        conn,
        "SELECT day, target, label, carried, done_at FROM study_days ORDER BY day DESC LIMIT ?",
        (limit,),
    )
    return [StudyRow(r[0], r[1], r[2], bool(r[3]), r[4]) for r in rows]


def projects(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT DISTINCT project FROM tasks ORDER BY 1")]


# 홈 개요 (ADR-007: 승인 대기, 실행 중, 예산, 작업판, 지표, 병목)

WAITING_STATES = (
    "OPINIONS",
    "AWAIT_DESIGN_APPROVAL",
    "AWAIT_MERGE",
    "AWAIT_DOC_APPROVAL",
    "NEEDS_HUMAN",
)


@dataclass(frozen=True)
class WaitingRow:
    id: int
    project: str
    title: str
    state: str
    waiting_since: str | None


@dataclass(frozen=True)
class RunningRow:
    task_id: int | None
    title: str | None
    agent: str
    stage: str
    started_at: str


@dataclass(frozen=True)
class ProductMetrics:
    project: str
    started: int  # 도달
    merged: int  # 채택
    dropped: int  # 자동 제외
    needs_human: int


@dataclass(frozen=True)
class Overview:
    waiting: list[WaitingRow]
    running: list[RunningRow]
    paused_until: str | None
    today: dict[str, int]  # 에이전트 → 오늘 실행 수
    board: dict[str, dict[str, list[tuple[int, str]]]]  # 제품 → 상태 → (번호, 제목)
    week: list[ProductMetrics]
    failures: int  # 7일 시스템 실패 (오류 이벤트)
    hiring: list[tuple[int, str, str, str]]  # 번호, 제안한 팀장, 팀, 역할


def _optional(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    """봇이 아직 새 스키마로 올리지 않은 DB에서도 화면이 뜨게 없는 테이블은 빈 결과로."""
    try:
        return list(conn.execute(sql, params))
    except sqlite3.OperationalError:
        return []


def overview(conn: sqlite3.Connection, *, today_start: str, week_start: str) -> Overview:
    marks = ", ".join("?" for _ in WAITING_STATES)
    waiting = [
        WaitingRow(r["id"], r["project"], r["title"], r["state"], r["waiting_since"])
        for r in conn.execute(
            f"SELECT id, project, title, state, waiting_since FROM tasks WHERE state IN ({marks}) "
            "ORDER BY waiting_since",
            WAITING_STATES,
        )
    ]
    running = [
        RunningRow(r["task_id"], r["title"], r["agent"], r["stage"], r["started_at"])
        for r in conn.execute(
            "SELECT r.task_id, t.title, r.agent, r.stage, r.started_at FROM agent_runs r "
            "LEFT JOIN tasks t ON t.id = r.task_id WHERE r.status = 'running' "
            "ORDER BY r.started_at"
        )
    ]
    paused = _optional(conn, "SELECT value FROM bot_state WHERE key = 'queue_paused_until'")
    today = {
        r["agent"]: r["n"]
        for r in conn.execute(
            "SELECT agent, COUNT(*) AS n FROM agent_runs WHERE started_at >= ? GROUP BY agent",
            (today_start,),
        )
    }
    board: dict[str, dict[str, list[tuple[int, str]]]] = {}
    for r in conn.execute(
        "SELECT id, project, title, state FROM tasks WHERE state NOT IN ('DONE', 'STOPPED') "
        "ORDER BY project, id"
    ):
        board.setdefault(r["project"], {}).setdefault(r["state"], []).append((r["id"], r["title"]))
    week = [
        ProductMetrics(r["project"], r["started"], r["merged"], r["dropped"], r["needs_human"])
        for r in conn.execute(
            "SELECT project, "
            "SUM(created_at >= ?) AS started, "
            "SUM(state = 'DONE' AND updated_at >= ?) AS merged, "
            "SUM(state = 'STOPPED' AND updated_at >= ?) AS dropped, "
            "SUM(state = 'NEEDS_HUMAN') AS needs_human "
            "FROM tasks GROUP BY project ORDER BY project",
            (week_start, week_start, week_start),
        )
    ]
    failures = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'error' AND created_at >= ?", (week_start,)
    ).fetchone()[0]
    hiring = []
    for r in _optional(
        conn, "SELECT id, proposed_by, payload FROM hiring_proposals WHERE status = 'open'"
    ):
        payload = json.loads(r["payload"])
        hiring.append((r["id"], r["proposed_by"], payload.get("team", ""), payload.get("role", "")))
    return Overview(
        waiting=waiting,
        running=running,
        paused_until=paused[0]["value"] if paused else None,
        today=today,
        board=board,
        week=week,
        failures=failures,
        hiring=hiring,
    )
