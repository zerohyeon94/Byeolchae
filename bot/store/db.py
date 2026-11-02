"""SQLite 연결과 스키마 버전 관리 (SPEC 8).

스키마 버전은 `PRAGMA user_version`으로 관리한다. MIGRATIONS[i]는 버전 i에서 i+1로 올리는 SQL이다.
연결은 자동 커밋 모드로 열고, 트랜잭션은 저장소(repo)가 BEGIN/COMMIT으로 직접 연다.
"""

from pathlib import Path

import aiosqlite

_V1 = """
CREATE TABLE tasks (
  id               INTEGER PRIMARY KEY,
  project          TEXT NOT NULL,
  discord_thread   TEXT NOT NULL UNIQUE,
  title            TEXT NOT NULL,
  state            TEXT NOT NULL,
  prev_state       TEXT,
  review_round     INTEGER NOT NULL DEFAULT 0,
  test_retry       INTEGER NOT NULL DEFAULT 0,
  opinion_rounds   INTEGER NOT NULL DEFAULT 0,
  issue_number     INTEGER,
  pr_number        INTEGER,
  branch           TEXT,
  worktree         TEXT,
  claude_session   TEXT,
  waiting_since    TEXT,
  reminded_at      TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE events (
  id               INTEGER PRIMARY KEY,
  task_id          INTEGER NOT NULL REFERENCES tasks(id),
  actor            TEXT NOT NULL,
  kind             TEXT NOT NULL,
  payload          TEXT NOT NULL,
  discord_message  TEXT,
  created_at       TEXT NOT NULL
);
CREATE INDEX events_task ON events(task_id, id);

CREATE TABLE agent_runs (
  id               TEXT PRIMARY KEY,
  task_id          INTEGER NOT NULL REFERENCES tasks(id),
  agent            TEXT NOT NULL,
  stage            TEXT NOT NULL,
  exit_code        INTEGER,
  status           TEXT NOT NULL,
  log_path         TEXT,
  started_at       TEXT NOT NULL,
  finished_at      TEXT
);
CREATE INDEX agent_runs_task ON agent_runs(task_id);
"""

# v2: /resume으로 늘린 한도와 /fix 권장 반영 사용 여부 (SPEC 3.3, Q4)
_V2 = """
ALTER TABLE tasks ADD COLUMN review_limit_bonus INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN test_limit_bonus INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN recommended_fixed INTEGER NOT NULL DEFAULT 0;
"""

# v3: 에이전트 이름과 엔진 분리 (ADR-006 4절). agent_runs.agent는 에이전트 이름, engine은 실행 엔진
_V3 = """
ALTER TABLE agent_runs ADD COLUMN engine TEXT;
UPDATE agent_runs SET engine = agent WHERE agent IN ('claude', 'codex');
"""

# v4: 팀장의 채용 제안과 Zero의 답 (운영 규약 1.12, SPEC Q19)
_V4 = """
CREATE TABLE hiring_proposals (
  id               INTEGER PRIMARY KEY,
  task_id          INTEGER REFERENCES tasks(id),
  proposed_by      TEXT NOT NULL,
  payload          TEXT NOT NULL,
  evidence         TEXT NOT NULL,
  status           TEXT NOT NULL,
  decided_note     TEXT,
  created_at       TEXT NOT NULL,
  decided_at       TEXT
);
"""

# v5: 봇 전체 상태(사용량 한도 정지 등)와 일정 실행 기록 (ADR-007, SPEC 6.6)
_V5 = """
CREATE TABLE bot_state (
  key              TEXT PRIMARY KEY,
  value            TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE TABLE schedule_runs (
  job              TEXT PRIMARY KEY,
  last_run_at      TEXT NOT NULL
);
CREATE INDEX agent_runs_agent ON agent_runs(agent, started_at);
"""

# v6: 파이프라인 선언으로 진행 (SPEC 3.5, ADR-007). 이전 작업은 legacy(ADR-005 흐름)
_V6 = """
ALTER TABLE tasks ADD COLUMN pipeline TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE tasks ADD COLUMN flags TEXT NOT NULL DEFAULT '';
CREATE TABLE chat_turns (
  id               INTEGER PRIMARY KEY,
  project          TEXT NOT NULL,
  actor            TEXT NOT NULL,
  kind             TEXT NOT NULL,
  payload          TEXT NOT NULL,
  discord_message  TEXT,
  task_id          INTEGER REFERENCES tasks(id),
  created_at       TEXT NOT NULL
);
CREATE INDEX chat_turns_project ON chat_turns(project, id);
-- 채팅 답은 작업이 없으므로 agent_runs.task_id가 비어도 되게 테이블을 다시 만든다
CREATE TABLE agent_runs_v6 (
  id               TEXT PRIMARY KEY,
  task_id          INTEGER REFERENCES tasks(id),
  agent            TEXT NOT NULL,
  stage            TEXT NOT NULL,
  exit_code        INTEGER,
  status           TEXT NOT NULL,
  log_path         TEXT,
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  engine           TEXT,
  project          TEXT
);
INSERT INTO agent_runs_v6
  (id, task_id, agent, stage, exit_code, status, log_path, started_at, finished_at, engine)
  SELECT id, task_id, agent, stage, exit_code, status, log_path, started_at, finished_at, engine
  FROM agent_runs;
DROP TABLE agent_runs;
ALTER TABLE agent_runs_v6 RENAME TO agent_runs;
CREATE INDEX agent_runs_task ON agent_runs(task_id);
CREATE INDEX agent_runs_agent ON agent_runs(agent, started_at);
CREATE TABLE ideas (
  id               INTEGER PRIMARY KEY,
  project          TEXT NOT NULL,
  body             TEXT NOT NULL,
  status           TEXT NOT NULL,
  task_id          INTEGER REFERENCES tasks(id),
  created_at       TEXT NOT NULL
);
"""

# v7: 교육 문제(개념 노트·복습·주간)와 Zero의 답 (ADR-009). 처음 고른 답만 기록한다
_V7 = """
CREATE TABLE quizzes (
  id               INTEGER PRIMARY KEY,
  kind             TEXT NOT NULL,
  key              TEXT NOT NULL UNIQUE,
  payload          TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
CREATE INDEX quizzes_kind ON quizzes(kind, key);
CREATE TABLE quiz_answers (
  quiz_id          INTEGER NOT NULL REFERENCES quizzes(id),
  number           INTEGER NOT NULL,
  choice           TEXT NOT NULL,
  correct          INTEGER NOT NULL,
  answered_at      TEXT NOT NULL,
  PRIMARY KEY (quiz_id, number)
);
"""

# v8: 교육 학습 기록 (ADR-009). 오늘 개념과 Zero의 완료 버튼
_V8 = """
CREATE TABLE study_days (
  day              TEXT PRIMARY KEY,
  target           TEXT NOT NULL,
  label            TEXT NOT NULL,
  carried          INTEGER NOT NULL,
  done_at          TEXT,
  created_at       TEXT NOT NULL
);
"""

MIGRATIONS: tuple[str, ...] = (_V1, _V2, _V3, _V4, _V5, _V6, _V7, _V8)
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaVersionError(RuntimeError):
    """DB 스키마가 이 코드보다 새 버전이다."""


async def connect(path: Path | str) -> aiosqlite.Connection:
    """DB를 열고 최신 스키마로 올린다. path가 ":memory:"이면 메모리 DB."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        await migrate(conn)
    except BaseException:
        await conn.close()
        raise
    return conn


async def schema_version(conn: aiosqlite.Connection) -> int:
    async with conn.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def migrate(conn: aiosqlite.Connection) -> int:
    current = await schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"DB 스키마 버전 {current}이 코드가 아는 최신 버전 {SCHEMA_VERSION}보다 높다"
        )
    for version in range(current, SCHEMA_VERSION):
        # 마이그레이션 하나와 버전 기록을 한 트랜잭션으로 묶는다
        await conn.executescript(
            f"BEGIN;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
        )
    return SCHEMA_VERSION
