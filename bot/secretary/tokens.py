"""Claude·Codex·Gemini 토큰 사용량 집계 (ADR-010).

봇이 부른 것과 Zero가 직접 쓴 것(데스크톱 앱, 터미널, 다른 저장소 포함)을 모두 센다.
읽는 곳은 각 CLI가 남기는 로컬 기록이고, 파일 내용은 숫자만 꺼낸다(대화 내용은 읽지 않는다).

- Claude: `~/.claude/projects/**/*.jsonl`의 assistant 메시지 `usage`. 같은 메시지가 여러 줄에
  나오므로 메시지 id로 한 번만 센다. `entrypoint`가 `sdk-cli`(`claude -p`)면 봇, 그 밖은 Zero
- Codex (Zero): `~/.codex/sessions/YYYY/MM/DD/*.jsonl`의 `token_count` 이벤트. 세션 누적값
  (`total_token_usage`)의 증가분을 그 이벤트 시각에 센다. 구독 한도 비율(`rate_limits`)도
  여기서 읽는다
- Codex (봇): 봇은 세션 파일을 남기지 않는다(`--ephemeral`). 실행 로그의 `tokens used` 숫자를 센다
- Claude 구독 한도 비율: 로컬 기록에는 없다. 봇 Claude 실행 로그(`stream-json`)의
  `rate_limit_event` 줄에서 읽는다. 계정 전체(봇과 Zero) 기준 값이다
- Gemini (봇): 실행 로그(`stream-json`)의 마지막 `result` 이벤트 `usage`. 그 실행의
  `conversation_id`는 Zero 몫에서 뺀다 (봇 대화도 같은 폴더에 남는다)
- Gemini (Zero): Antigravity CLI·데스크톱 앱의 `conversations/<id>.db`(SQLite). 공식 형식이 아니다.
  `gen_metadata.data`(protobuf)의 필드 1.4.2(입력)·1.4.3(출력, thinking 포함)·1.4.5(캐시 읽기)를
  숫자로만 읽는다. 시각은 같은 순서의 모델 응답 단계(`steps.step_type` 15) metadata 필드 1.1(초)다.
  agy 1.5.14에서 봇 실행의 결과 이벤트 숫자와 맞는 것을 확인했다 (2026-11-22). 형식이 바뀌면
  말없이 0이 되므로, Daily에서 Gemini "나"가 계속 0이면 이 형식부터 확인한다. 구독 한도 정보는 없다

토큰 수는 입력(캐시 읽기·쓰기 포함)과 출력의 합이다.
"""

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BOT_ENTRYPOINT = "sdk-cli"
WEEKLY_WINDOW_MINUTES = 10080
# 같은 주간 창이라도 기록마다 초기화 시각이 1초쯤 흔들린다. 이보다 많이 다르면 창이 바뀐 것이다
SAME_WINDOW = timedelta(minutes=5)
GEMINI_RESPONSE_STEP = 15  # steps.step_type: 모델 응답 (gen_metadata 행과 1:1)
_GEMINI_USAGE = (1, 4)  # gen_metadata.data 안의 사용량 메시지 경로
_GEMINI_TOKEN_FIELDS = (2, 3, 5)  # 입력, 출력, 캐시 읽기
_CODEX_TOKENS = re.compile(r"^tokens used\s*\n\s*([\d,]+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Usage:
    at: datetime
    tokens: int
    bot: bool


@dataclass(frozen=True)
class LimitSample:
    at: datetime
    used_percent: float
    resets_at: datetime


@dataclass(frozen=True)
class Limit:
    """구독 주간 한도 사용 비율."""

    weekly: float  # 지금 주간 창에서 쓴 비율 (%)
    today: float | None  # 오늘 늘어난 비율 (%). 비교할 어제 값이 없으면 None


@dataclass(frozen=True)
class EngineUsage:
    engine: str  # claude | codex | gemini
    bot: int
    zero: int
    week: int  # 이번 주(월요일 0시부터 그날 끝까지) 합계
    limit: Limit | None = None

    @property
    def total(self) -> int:
        return self.bot + self.zero

    @property
    def week_share(self) -> float | None:
        """그날 사용량이 이번 주 합계에서 차지하는 비율 (%)."""
        return self.total * 100 / self.week if self.week else None


# 읽기


def _time(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _lines(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as file:
            for line in file:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    yield data
    except OSError:
        return


def _recent(paths: Iterable[Path], since: datetime) -> list[Path]:
    """since 이후에 바뀐 파일만. 오래된 세션은 열지 않는다."""
    stamp = since.timestamp()
    found = []
    for path in paths:
        try:
            if path.stat().st_mtime >= stamp:
                found.append(path)
        except OSError:
            continue
    return sorted(found)


def claude_usage(projects: Path, since: datetime) -> list[Usage]:
    seen: set[str] = set()
    found = []
    for path in _recent(projects.rglob("*.jsonl"), since):
        for data in _lines(path):
            message = data.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            at = _time(data.get("timestamp"))
            if at is None or at < since:
                continue
            key = str(message.get("id") or data.get("requestId") or data.get("uuid") or "")
            if key:
                if key in seen:
                    continue
                seen.add(key)
            usage = message["usage"]
            tokens = sum(
                int(usage.get(name) or 0)
                for name in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "output_tokens",
                )
            )
            found.append(Usage(at, tokens, data.get("entrypoint") == BOT_ENTRYPOINT))
    return found


def codex_sessions(sessions: Path, since: datetime) -> tuple[list[Usage], list[LimitSample]]:
    """Zero의 Codex 세션 사용량과 주간 한도 비율 표본."""
    usages: list[Usage] = []
    limits: list[LimitSample] = []
    for path in _recent(sessions.rglob("*.jsonl"), since):
        previous = 0
        for data in _lines(path):
            payload = data.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            at = _time(data.get("timestamp"))
            if at is None:
                continue
            info = payload.get("info")
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(total, dict):
                current = int(total.get("total_tokens") or 0)
                added = current - previous if current >= previous else current
                previous = current
                if added > 0 and at >= since:
                    usages.append(Usage(at, added, bot=False))
            sample = _weekly_sample(at, payload.get("rate_limits"))
            if sample is not None:
                limits.append(sample)
    return usages, sorted(limits, key=lambda s: s.at)


def _weekly_sample(at: datetime, limits: Any) -> LimitSample | None:
    if not isinstance(limits, dict):
        return None
    for window in (limits.get("secondary"), limits.get("primary")):
        if isinstance(window, dict) and window.get("window_minutes") == WEEKLY_WINDOW_MINUTES:
            try:
                resets = datetime.fromtimestamp(float(window["resets_at"]), UTC)
                return LimitSample(at, float(window["used_percent"]), resets)
            except (KeyError, TypeError, ValueError):
                return None
    return None


def claude_run_limit(log_text: str, at: datetime) -> LimitSample | None:
    """봇 Claude 실행 로그의 마지막 `rate_limit_event`에서 주간 한도 비율. 시각은 그 실행의 시각.

    공식으로 약속된 형식이 아니므로 모양이 다르면 None으로 둔다.
    """
    found = None
    for line in log_text.splitlines():
        if '"rate_limit_event"' not in line:
            continue
        try:
            data = json.loads(line)
            window = data["rate_limit_info"]["unifiedWindows"]["seven_day"]
            resets = datetime.fromtimestamp(float(window["resetsAt"]), UTC)
            found = LimitSample(at, float(window["utilization"]) * 100, resets)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return found


def codex_run_tokens(log_text: str) -> int | None:
    """봇 Codex 실행 로그의 `tokens used` 숫자. 없으면 None."""
    found = _CODEX_TOKENS.findall(log_text)
    return int(found[-1].replace(",", "")) if found else None


def gemini_run_usage(log_text: str) -> tuple[int, str | None] | None:
    """봇 Gemini 실행 로그의 마지막 `result` 이벤트에서 (토큰 수, conversation_id). 없으면 None."""
    found = None
    for line in log_text.splitlines():
        if '"event"' not in line or '"result"' not in line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = data.get("result") if isinstance(data, dict) else None
        if data.get("event") != "result" or not isinstance(result, dict):
            continue
        usage = result.get("usage")
        if not isinstance(usage, dict):
            continue
        tokens = sum(
            int(usage.get(name) or 0)
            for name in ("input_tokens", "cache_read_tokens", "output_tokens")
        )
        sid = result.get("conversation_id")
        found = (tokens, sid if isinstance(sid, str) and sid else None)
    return found


def gemini_conversations(dirs: Iterable[Path], since: datetime, exclude: set[str]) -> list[Usage]:
    """Zero의 Antigravity 대화 사용량. exclude는 봇 실행의 conversation_id."""
    found: list[Usage] = []
    for directory in dirs:
        for path in _recent(directory.glob("*.db"), since):
            if path.stem in exclude:
                continue
            found += [u for u in _gemini_db(path) if u.at >= since]
    return found


def _gemini_db(path: Path) -> list[Usage]:
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        gens = [row[0] for row in con.execute("SELECT data FROM gen_metadata ORDER BY idx")]
        steps = con.execute(
            "SELECT metadata FROM steps WHERE step_type = ? ORDER BY idx", (GEMINI_RESPONSE_STEP,)
        )
        times = [_proto_value(row[0], (1, 1)) for row in steps]
        mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except (sqlite3.Error, OSError):
        return []
    finally:
        con.close()
    found = []
    for i, data in enumerate(gens):
        usage = _proto_message(data, _GEMINI_USAGE)
        if usage is None:
            continue
        tokens = sum(_proto_value(usage, (field,)) or 0 for field in _GEMINI_TOKEN_FIELDS)
        # shortcut: 응답 단계가 모자라면 파일 수정 시각으로 둔다. 자주 어긋나면 짝짓기를 다시 본다
        seconds = times[i] if i < len(times) else None
        at = datetime.fromtimestamp(seconds, UTC) if seconds else mtime
        if tokens > 0:
            found.append(Usage(at, tokens, bot=False))
    return found


def _proto_fields(data: Any) -> Iterator[tuple[int, int, Any]]:
    """protobuf 한 단계의 (필드 번호, wire type, 값). 깨진 데이터면 거기서 멈춘다.

    값은 varint면 int, 길이 구분이면 bytes다. 다른 형식은 건너뛴다.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        return
    buf, i = bytes(data), 0
    try:
        while i < len(buf):
            key, i = _varint(buf, i)
            field, wire = key >> 3, key & 7
            if wire == 0:
                value, i = _varint(buf, i)
                yield field, wire, value
            elif wire == 2:
                size, i = _varint(buf, i)
                if i + size > len(buf):
                    return
                yield field, wire, buf[i : i + size]
                i += size
            elif wire == 1:
                i += 8
            elif wire == 5:
                i += 4
            else:
                return
    except IndexError:
        return


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, i


def _proto_message(data: Any, path: tuple[int, ...]) -> bytes | None:
    for number in path:
        data = next((v for f, w, v in _proto_fields(data) if f == number and w == 2), None)
        if data is None:
            return None
    return data


def _proto_value(data: Any, path: tuple[int, ...]) -> int | None:
    parent = _proto_message(data, path[:-1]) if len(path) > 1 else data
    if parent is None:
        return None
    return next((v for f, w, v in _proto_fields(parent) if f == path[-1] and w == 0), None)


# 계산


def weekly_limit(samples: list[LimitSample], start: datetime, end: datetime) -> Limit | None:
    """end 직전의 주간 한도 비율과, start 직전 값과 비교한 그날 증가분.

    마지막 표본 뒤에 주간 창이 이미 초기화됐으면 0%로 본다.
    그날 사이에 창이 초기화됐으면 그날 증가분은 마지막 값 그대로다.
    """
    before_end = [s for s in samples if s.at < end]
    if not before_end:
        return None
    last = before_end[-1]
    if last.resets_at <= end:
        return Limit(0.0, 0.0)
    before_start = [s for s in samples if s.at < start]
    if not before_start:
        return Limit(last.used_percent, None)
    first = before_start[-1]
    if abs(first.resets_at - last.resets_at) > SAME_WINDOW:
        return Limit(last.used_percent, last.used_percent)
    return Limit(last.used_percent, max(0.0, last.used_percent - first.used_percent))


def engine_usage(
    engine: str,
    usages: list[Usage],
    start: datetime,
    end: datetime,
    week_start: datetime,
    limit: Limit | None = None,
) -> EngineUsage:
    day = [u for u in usages if start <= u.at < end]
    week = [u for u in usages if week_start <= u.at < end]
    return EngineUsage(
        engine,
        bot=sum(u.tokens for u in day if u.bot),
        zero=sum(u.tokens for u in day if not u.bot),
        week=sum(u.tokens for u in week),
        limit=limit,
    )


def tokens_text(count: int) -> str:
    """사람이 읽는 토큰 수 (1,234 / 12.3K / 4.56M)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 10_000:
        return f"{count / 1_000:.1f}K"
    return f"{count:,}"
