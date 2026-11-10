"""로컬 대시보드 테스트. 봇 저장소로 DB를 만들고 handle()로 각 주소를 확인한다."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from http import HTTPStatus

import pytest

from bot.agents.base import RunStatus, Stage
from bot.clock import FixedClock
from bot.dashboard import queries
from bot.dashboard.atlas import _SCENE
from bot.dashboard.server import DashboardSettings, handle
from bot.org import DEFAULT_ORG
from bot.store import INVALID_OUTPUT, Actor, EventKind, NewEvent, Repository, connect
from bot.workflow.state import TaskState

T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
OPINION = {
    "stance": "alternative",
    "conclusion": "tripId를 쓰자",
    "reasons": ["이미 있다"],
    "risks": ["노출 조건이 복잡해진다"],
    "proposal": "기간 기준으로만 노출",
}


@pytest.fixture
def settings(tmp_path):
    db = tmp_path / "zerosync.db"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    async def build():
        conn = await connect(db)
        repo = Repository(conn, FixedClock(T0))
        try:
            first = await repo.create_task(
                project="gagaessi",
                discord_thread="111",
                title="여행 <소비>",
                state=TaskState.OPINIONS,
                event=NewEvent(
                    Actor.ZERO, EventKind.MESSAGE, {"title": "여행", "text": "<script>x</script>"}
                ),
            )
            await repo.add_event(
                first.id, NewEvent(Actor.CLAUDE, EventKind.OPINION, {"opinion": OPINION}, "m1")
            )
            await repo.add_event(
                first.id,
                NewEvent(
                    Actor.CODEX,
                    EventKind.ERROR,
                    {"reason": INVALID_OUTPUT, "error": "형식", "raw": "엉망"},
                ),
            )
            await repo.start_agent_run(
                "r-ok", task_id=first.id, agent="claude", stage=Stage.OPINION
            )
            log = runs_dir / "r-ok.log"
            log.write_text("run_id: r-ok\n전체 출력", encoding="utf-8")
            await repo.finish_agent_run("r-ok", status=RunStatus.OK, exit_code=0, log_path=str(log))
            await repo.start_agent_run(
                "r-bad", task_id=first.id, agent="codex", stage=Stage.OPINION
            )
            await repo.finish_agent_run(
                "r-bad", status=INVALID_OUTPUT, exit_code=0, log_path="/etc/passwd"
            )

            second = await repo.create_task(
                project="jday",
                discord_thread="222",
                title="다른 프로젝트",
                state=TaskState.OPINIONS,
                event=NewEvent(Actor.ZERO, EventKind.MESSAGE, {"title": "t", "text": "x"}),
            )
            await repo.apply_transition(
                second.id,
                expected_state=TaskState.OPINIONS,
                new_state=TaskState.STOPPED,
                event=NewEvent(Actor.ZERO, EventKind.COMMAND, {"name": "stop", "arg": ""}),
            )
            await repo.start_agent_run(
                "r-cancel", task_id=second.id, agent="claude", stage=Stage.DEBATE
            )
            await repo.add_idea("gagaessi", "홈 위젯 <b>")
            await repo.add_idea("jday", "알림 묶기")
            await repo.finish_agent_run(
                "r-cancel", status=RunStatus.CANCELLED, exit_code=None, log_path=None
            )
        finally:
            await conn.close()

    asyncio.run(build())
    return DashboardSettings(db=db, runs_dir=runs_dir, guild_id="999")


def get(settings, path):
    response = handle(settings, path)
    return response.status, response.body


def test_atlas_story_requires_the_recorded_speakers_in_order(settings):
    # 작업 번호는 운영 DB와 달라도 된다. 예전 이름으로 쓴 원문도 현재 직원에게 연결한다.
    agents = dict(DEFAULT_ORG.agents)
    for key, display, former in (("shaula", "샤울라", "oldqa"), ("mizar", "미자르", "oldlead")):
        agents[key] = agents["zeta"].model_copy(update={"display": display, "former": (former,)})
    org = DEFAULT_ORG.model_copy(update={"agents": agents})
    settings = replace(settings, org=org)
    with sqlite3.connect(settings.db) as db:
        for label, speaker, kind, field, sentence in _SCENE:
            payload = json.dumps({field: {"summary": sentence}})
            event = db.execute(
                "INSERT INTO events(task_id, actor, kind, payload, created_at) VALUES(1,?,?,?,?)",
                (
                    "oldqa" if speaker == "shaula" else "oldlead",
                    kind,
                    payload,
                    "2026-10-29T18:13:38.995897+00:00" if label == "첫 진단" else T0.isoformat(),
                ),
            )
            if label == "첫 진단":
                source = event.lastrowid
    status, body = get(settings, "/org/stars/shaula")
    assert status == 200 and "기록 속 한마디" in body and f"/tasks/1#event-{source}" in body
    assert "다시 확인 · 미자르" in body
    with sqlite3.connect(settings.db) as db:
        db.execute("UPDATE events SET actor='zero' WHERE id=?", (source,))
    assert "기록 속 한마디" not in get(settings, "/org/stars/shaula")[1]


def test_atlas_without_a_database_still_serves_the_roster(settings):
    settings = replace(settings, db=settings.db.with_name("missing.db"))
    assert 'href="/org/teams/qa"' in get(settings, "/org")[1]
    assert 'href="/org/stars/zeta"' in get(settings, "/org/teams/qa")[1]
    assert "원인을 끝까지 좁히기" in get(settings, "/org/stars/zeta")[1]
    assert get(settings, "/org/assets/..%2Fserver.py")[0] == HTTPStatus.NOT_FOUND
    assert get(settings, "/org/teams/unknown")[0] == HTTPStatus.NOT_FOUND


def test_summary_counts_across_projects(settings):
    conn = queries.open_readonly(settings.db)
    try:
        summaries = {s.project: s for s in queries.project_summaries(conn)}
        assert summaries["gagaessi"].tasks == 1
        assert summaries["gagaessi"].by_status == {"ok": 1, "invalid_output": 1}
        assert summaries["jday"].by_state == {"STOPPED": 1}
        stats = {(s.agent, s.stage): s for s in queries.stage_stats(conn)}
        assert stats[("claude", "opinion")].runs == 1
        assert stats[("claude", "debate")].by_status == {"cancelled": 1}
        # 평균은 성공한 실행만
        assert stats[("claude", "debate")].avg_sec is None
        assert stats[("claude", "opinion")].avg_sec == 0.0
    finally:
        conn.close()

    status, body = get(settings, "/summary")
    assert status is HTTPStatus.OK
    assert "gagaessi" in body and "jday" in body
    assert "형식 오류 1" in body


def test_company_home_links_open_work_and_latest_event_per_task(settings):
    settings = replace(settings, now=lambda: T0)
    status, body = get(settings, "/")
    assert status == HTTPStatus.OK and "밤도란 · 별직원들이" in body
    assert "대표 노을" in body
    # 오늘 숫자 띠: T0에 시작한 실행 3회, 열린 작업 1건(의견 수렴은 대표 차례)
    assert "<small>오늘 실행</small><b>3회</b>" in body
    assert "<small>진행 중인 작업</small><b>1건</b>" in body
    assert "<small>노을 대표 차례</small><b>1건</b>" in body
    # 작업 흐름 띠: 의견 단계가 지금 단계
    assert '<li class="now"><i></i><span>의견</span></li>' in body
    assert "노을 대표 차례</span>" in body
    work, history = body.split("<h3>최근 작업 기록</h3>")
    assert 'href="/tasks/1"' in work and 'href="/tasks/2"' not in work
    assert "여행 &lt;소비&gt;" in work
    assert history.count("/tasks/1#event-") == history.count("/tasks/2#event-") == 1
    with sqlite3.connect(settings.db) as db:
        event_id = db.execute("SELECT MAX(id) FROM events WHERE task_id=1").fetchone()[0]
    assert f'href="/tasks/1#event-{event_id}"' in history
    assert 'href="/summary"' in body


def test_task_list_filters_and_links(settings):
    _, body = get(settings, "/tasks")
    assert "#1 여행 &lt;소비&gt;" in body
    assert "#2 다른 프로젝트" in body
    assert "https://discord.com/channels/999/111" in body

    _, body = get(settings, "/tasks?project=jday")
    assert "#2 다른 프로젝트" in body and "#1 여행" not in body
    _, body = get(settings, "/tasks?state=OPINIONS")
    assert "#1 여행" in body and "#2 다른" not in body


def test_task_detail_escapes_content(settings):
    status, body = get(settings, "/tasks/1")
    assert status is HTTPStatus.OK
    assert "<script>x</script>" not in body
    assert "&lt;script&gt;x&lt;/script&gt;" in body
    assert "대안 제시 — tripId를 쓰자" in body
    assert "원문:\n엉망" in body
    assert "에이전트 실행 2회" in body


def test_runs_list_filters(settings):
    _, body = get(settings, "/runs")
    assert body.count('/log">보기</a>') == 2  # 로그 경로가 없는 실행은 링크가 없다
    _, body = get(settings, "/runs?status=cancelled")
    assert "취소" in body and "/runs/r-ok/log" not in body
    _, body = get(settings, "/runs?project=gagaessi")
    assert "다른 프로젝트" not in body


def test_ideas_list_filters(settings):
    status, body = get(settings, "/ideas")
    assert status is HTTPStatus.OK
    assert "홈 위젯 &lt;b&gt;" in body and "알림 묶기" in body and "열림" in body
    _, body = get(settings, "/ideas?project=jday")
    assert "홈 위젯" not in body and "알림 묶기" in body


def test_log_is_served_only_from_runs_dir(settings):
    response = handle(settings, "/runs/r-ok/log")
    assert response.status is HTTPStatus.OK
    assert response.content_type.startswith("text/plain")
    assert response.body == "run_id: r-ok\n전체 출력"

    response = handle(settings, "/runs/r-bad/log")
    assert response.status is HTTPStatus.FORBIDDEN
    assert "root:" not in response.body

    assert handle(settings, "/runs/r-cancel/log").status is HTTPStatus.NOT_FOUND
    assert handle(settings, "/runs/nope/log").status is HTTPStatus.NOT_FOUND


def test_unknown_paths_are_404(settings):
    for path in ("/tasks/99", "/tasks/abc", "/nope", "/runs/r-ok"):
        assert handle(settings, path).status is HTTPStatus.NOT_FOUND


def test_database_is_opened_read_only(settings):
    conn = queries.open_readonly(settings.db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM tasks")
    finally:
        conn.close()


def test_missing_database_shows_message(tmp_path):
    settings = DashboardSettings(db=tmp_path / "none.db", runs_dir=tmp_path)
    status, body = get(settings, "/summary")
    assert status is HTTPStatus.OK
    assert "봇을 한 번 실행하면 만들어진다" in body
    status, home = get(settings, "/")
    assert status == HTTPStatus.OK and "제품과 목표" in home and "첫 업무 기록" in home
    assert not (tmp_path / "none.db").exists()


def test_phase2_events_are_rendered(settings):
    from bot.testrun import TestReport

    async def add():
        conn = await connect(settings.db)
        repo = Repository(conn, FixedClock(T0))
        try:
            report = TestReport(RunStatus.OK, 0, 431, 0, duration_sec=700.0)
            await repo.add_event(1, NewEvent(Actor.BOT, EventKind.TEST, report.to_payload()))
            await repo.add_event(1, NewEvent(Actor.BOT, EventKind.MERGE, {"pr": 43}))
        finally:
            await conn.close()

    asyncio.run(add())
    _, body = get(settings, "/tasks/1")
    assert "테스트 통과 — 431개" in body
    assert "밤도란 봇 · 테스트" in body and "노을 · 메시지" in body
    assert "PR #43 merge 감지" in body


def test_team_stage_events_are_rendered(settings):
    """기획·진단·팀장 확인 이벤트가 날 데이터가 아니라 Discord와 같은 문구로 보인다."""
    review = {"verdict": "APPROVED", "summary": "설계대로다", "findings": []}
    diagnosis = {
        "summary": "날짜 의존 테스트",
        "reproduction": ["토요일 다음 날 실행"],
        "cause": "test",
        "evidence": ["Tests.swift:10"],
        "fix_direction": "기준 날짜를 고정한다",
        "confidence": "high",
    }

    async def add():
        conn = await connect(settings.db)
        repo = Repository(conn, FixedClock(T0))
        try:
            await repo.add_event(1, NewEvent("zeta", EventKind.DIAGNOSIS, {"report": diagnosis}))
            payload = {"review": review, "round": 1, "stage": "diagnose", "lead": "delta"}
            await repo.add_event(1, NewEvent("delta", EventKind.LEAD_CHECK, payload))
        finally:
            await conn.close()

    asyncio.run(add())
    _, body = get(settings, "/tasks/1")
    assert "기준 날짜를 고정한다" in body and "{&#x27;" not in body
    assert "진단 팀장 확인 1회: 통과" in body
    assert "Zeta · QA팀원" in body and "팀장 확인" in body


def test_work_summary_puts_zero_todo_first(settings):
    from dataclasses import replace
    from datetime import timedelta

    later = replace(
        settings,
        daily_calls={"claude": 1, "beta": 30},
        now=lambda: T0 + timedelta(hours=5),
    )
    status, body = get(later, "/summary")
    assert status is HTTPStatus.OK
    # 승인 대기(OPINIONS) 1건이 맨 위 표에, 대기 시간과 함께
    todo = body.index("<h2>노을이 할 일</h2>")
    assert todo < body.index("<h2>누적</h2>")
    assert "#1 여행 &lt;소비&gt;" in body[todo:] and "5시간" in body
    # 오늘 예산: 한도에 닿은 에이전트는 표시한다
    assert '<span class="bad">2</span>' in body
    assert "<h2>제품별 작업판</h2>" in body and "<h2>지표 (7일)</h2>" in body


def test_overview_works_on_old_schema_without_new_tables(settings):
    # 봇이 새 스키마로 올리기 전의 DB에서도 화면이 뜬다
    raw = sqlite3.connect(settings.db)
    raw.executescript("DROP TABLE hiring_proposals; DROP TABLE bot_state;")
    raw.close()
    status, body = get(settings, "/summary")
    assert status is HTTPStatus.OK and "노을이 할 일" in body


def test_team_page_shows_status_jobs_and_persona(settings):
    """직원 화면: 팀별 팀장 먼저, 일하는 중·쉬는 중, 맡은 일, 인격 파일 (ADR-008)."""
    from dataclasses import replace

    from bot.pipeline import default_pipelines

    async def add():
        conn = await connect(settings.db)
        repo = Repository(conn, FixedClock(T0))
        try:
            await repo.start_agent_run("r-eps", task_id=1, agent="epsilon", stage=Stage.IMPLEMENT)
        finally:
            await conn.close()

    asyncio.run(add())
    org = DEFAULT_ORG.model_copy(update={"profiles": {"zeta": "# Zeta <성격>"}})
    team = replace(
        settings,
        org=org,
        owners={"orot": "mu"},
        pipelines=default_pipelines(),
        daily_calls={"epsilon": 30},
        now=lambda: T0,
    )
    status, body = get(team, "/team")
    assert status is HTTPStatus.OK
    assert body.index("Alpha · 개발팀장") < body.index("Epsilon · 개발팀원")
    epsilon = body[body.index('id="epsilon"') : body.index('id="delta"')]
    assert "일하는 중" in epsilon and "구현" in epsilon and "1 / 30" in epsilon
    assert "기능 개발 · 구현" in epsilon
    alpha = body[body.index('id="alpha"') : body.index('id="epsilon"')]
    assert "쉬는 중" in alpha and "기능 개발 · 구현 (확인)" in alpha
    assert "# Zeta &lt;성격&gt;" in body  # 인격 파일은 이스케이프해 보인다
    assert "12명 중 1명이 일하는 중" in body
    assert "orot 담당 · 기획팀장에게 진행·막힘 보고" in body


# 교육 커리큘럼 (ADR-009)

GOALS = """\
| 월 | 주제 | 층 | 자격증 |
|---|---|---|---|
| 2026-11 | 시스템 전체 한 바퀴 (플랫폼 지도) | 폭 | — |
| 2027-02 | 데이터 계층 (PostgreSQL) | 폭 | SQLD 준비 |
"""

QUEUE = """\
## 2026-11 · 시스템 전체 한 바퀴

### 0. 전체
- [x] 엣지-중앙 서버 구조: 역할 나누기 → [[Learning/개념/2026-11-01 엣지|엣지]]

### 1. 장비 안
- [ ] 소리 이벤트 <감지>: dB, EWMA → [[Learning/개념/2026-11-02 소리|소리]]
- [ ] gRPC 스트리밍: keepalive
- [ ] TFLite: 온디바이스 추론
"""


def test_education_page_shows_curriculum_queue_and_study(settings, tmp_path):
    from dataclasses import replace

    vault = tmp_path / "vault"
    (vault / "Schedule").mkdir(parents=True)
    (vault / "Schedule" / "월별 목표 (2026-11 ~ 2027-10).md").write_text(GOALS, "utf-8")
    (vault / "Schedule" / "학습 대기열.md").write_text(QUEUE, "utf-8")

    async def study():
        conn = await connect(settings.db)
        try:
            repo = Repository(conn, FixedClock(T0))
            await repo.add_study_day("2026-11-01", "Learning/개념/2026-11-01 엣지", "엣지", False)
            await repo.mark_study_done("2026-11-01")
            await repo.add_study_day("2026-11-02", "Learning/개념/2026-11-02 소리", "소리", False)
        finally:
            await conn.close()

    asyncio.run(study())
    education = replace(
        settings,
        education_agent="eta",
        education_vault=vault,
        now=lambda: datetime(2026, 11, 2, 3, 0, tzinfo=UTC),
    )
    status, body = get(education, "/education")
    assert status == HTTPStatus.OK
    assert "교육 커리큘럼" in body and "담당: Eta · 교육팀장" in body
    assert "시스템 전체 한 바퀴" in body and "(플랫폼 지도)" in body and "이번 달" in body
    assert "SQLD 준비" in body
    assert "1 / 4" in body  # 이번 달 대기열
    assert "소리 이벤트 &lt;감지&gt;" in body  # 볼트 내용도 이스케이프
    assert body.count(">완료</span>") == 1 and "진행 중" in body
    assert ">다음</td>" in body  # 다음에 꺼낼 항목
    assert '소리 <span class="muted">(읽는 중)</span>' in body
    assert 'href="/education" class="on"' in body


def test_education_page_without_config_says_so(settings):
    status, body = get(settings, "/education")
    assert status == HTTPStatus.OK and "교육 설정" in body


# 기능 화면 (/features)


def test_features_page_shows_config_schedules_and_flows(settings, tmp_path):
    """기능 화면: 설정에서 만든 파이프라인·일정, DB의 최근 실행, 봇 실행 여부, 흐름도."""
    import os
    from dataclasses import replace

    from bot.dashboard.features import scheduled_jobs
    from bot.pipeline import default_pipelines

    async def add():
        conn = await connect(settings.db)
        repo = Repository(conn, FixedClock(T0))
        try:
            await repo.record_schedule_run("morning_report", T0.isoformat())
        finally:
            await conn.close()

    asyncio.run(add())
    lock = tmp_path / "bot.lock"
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    pipelines = default_pipelines()
    features = replace(
        settings,
        pipelines=pipelines,
        projects=("gagaessi", "jday"),
        schedules=scheduled_jobs(
            pipelines, ["gagaessi", "jday"], education=("30 7 * * *", "0 20 * * 0")
        ),
        education_agent="eta",
        lock=lock,
    )
    status, body = get(features, "/features")
    assert status is HTTPStatus.OK
    assert f"PID {os.getpid()}" in body
    assert "기능 개발" in body and "/task feature" in body
    assert "정기 테스트 · jday" in body and "매일 03:00" in body
    assert "교육 주간 보고" in body and "일요일 20:00" in body
    assert "비서 하루 정리" not in body  # 비서 설정이 없으면 일정도 없다
    assert "아직 실행 기록 없음" in body  # 아침 보고 말고는 기록이 없다
    assert "화면 변경이 있을 때만" in body  # 화면 디자인 단계의 조건
    assert "확인: Alpha · 개발팀장" in body  # 구현 팀장 확인
    assert "<svg" in body and 'href="/features" class="on"' in body


def test_features_page_says_bot_is_off_without_live_pid(settings, tmp_path):
    from dataclasses import replace

    lock = tmp_path / "bot.lock"
    lock.write_text("999999999\n", encoding="utf-8")
    _, body = get(replace(settings, lock=lock), "/features")
    assert "꺼져 있음" in body and "봇이 꺼져 있음" in body


def test_cron_label():
    from bot.dashboard.features import cron_label

    assert cron_label("0 8 * * *") == "매일 08:00"
    assert cron_label("0 8 * * 1") == "월요일 08:00"
    assert cron_label("30 7,19 * * 0,6") == "일요일, 토요일 07:30, 19:30"


def test_company_home_night_shift_runs_from_evening_to_morning(settings):
    from bot.dashboard.features import scheduled_jobs
    from bot.pipeline import default_pipelines

    jobs = scheduled_jobs(
        default_pipelines(),
        ["gagaessi"],
        education=("30 7 * * *", "0 20 * * 0"),
        secretary="30 23 * * *",
    )
    _, body = get(replace(settings, schedules=jobs), "/")
    shift = body.split('<ol class="shift">')[1].split("</ol>")[0]
    order = ["비서 하루 정리", "정기 테스트 · gagaessi", "교육 아침 학습", "아침 보고"]
    assert [shift.index(name) for name in order] == sorted(shift.index(name) for name in order)
    assert "QA팀" in shift and "교육팀" in shift


def test_staff_directory_cards_carry_department_and_filters(settings):
    _, body = get(settings, "/org/stars")
    cards = body.count('<a class="worker" ')
    assert cards == len(DEFAULT_ORG.agents)
    # 부문 버튼 수 = 전체 + 부문, 카드마다 거를 부문과 검색어가 있다
    departments = {team.department for team in DEFAULT_ORG.teams.values()}
    assert body.count('<button type="button" data-dept=') == len(departments) + 1
    assert body.count("data-search=") == cards
    assert 'href="/org/stars" class="on">팀원 소개' in body
    assert '<div class="worker-tools" hidden>' in body  # 스크립트가 없으면 도구를 숨긴다


def test_org_chart_is_one_tree_from_founder_to_teams(settings):
    _, body = get(settings, "/org")
    assert '<div class="ot-node chief"><b>노을</b>' in body
    departments = {team.department for team in DEFAULT_ORG.teams.values()}
    assert body.count('class="ot-dept"') == len(departments)
    assert body.count('class="ot-team-link"') == len(DEFAULT_ORG.teams)
    for key in DEFAULT_ORG.agents:
        assert f'href="/org/stars/{key}"' in body
    assert 'href="/org" class="on">조직도' in body


def test_flow_step_groups_every_state():
    from bot.dashboard.company import FLOW_STEPS, STATE_LABELS, flow_step

    assert flow_step("OPINIONS") == 0 and flow_step("AWAIT_MERGE") == 7
    assert flow_step("NEEDS_HUMAN") == -1
    grouped = set().union(*(states for _, states in FLOW_STEPS))
    assert grouped | {"NEEDS_HUMAN"} == set(STATE_LABELS)
    assert {state.name for state in TaskState} - {"DONE", "STOPPED"} <= set(STATE_LABELS)


def test_feature_commands_match_discord_client():
    """기능 화면의 명령 목록은 client.py에 등록된 슬래시 명령과 같아야 한다."""
    import re
    from pathlib import Path

    from bot.dashboard.features import COMMANDS

    source = Path("bot/discord_io/client.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'tree\.command\(\s*name="([a-z]+)"', source))
    assert registered and {name.lstrip("/") for name, _ in COMMANDS} == registered


# 직원별 한 일 (/team/<이름>)


def _add_member_runs(settings):
    async def add():
        conn = await connect(settings.db)
        repo = Repository(conn, FixedClock(T0))
        try:
            await repo.start_agent_run("r-impl", task_id=1, agent="epsilon", stage=Stage.IMPLEMENT)
            await repo.finish_agent_run("r-impl", status=RunStatus.OK, exit_code=0, log_path=None)
            await repo.start_agent_run("r-fix", task_id=1, agent="epsilon", stage=Stage.FIX)
            await repo.finish_agent_run("r-fix", status=INVALID_OUTPUT, exit_code=0, log_path=None)
            # 작업 없이 한 실행 (교육 개념 노트)
            await repo.start_agent_run(
                "r-note", task_id=None, agent="eta", stage=Stage.CONCEPT_NOTE
            )
            await repo.finish_agent_run("r-note", status=RunStatus.OK, exit_code=0, log_path=None)
        finally:
            await conn.close()

    asyncio.run(add())


def test_team_page_links_to_member_page_without_run_table(settings):
    from dataclasses import replace

    _add_member_runs(settings)
    _, body = get(replace(settings, now=lambda: T0), "/team")
    epsilon = body[body.index('id="epsilon"') : body.index('id="delta"')]
    assert 'href="/team/epsilon"' in epsilon and "한 일 보기" in epsilon
    assert "<table" not in epsilon  # 실행 기록은 직원 화면으로 옮겼다


def test_member_page_shows_stages_tasks_and_runs(settings):
    from dataclasses import replace

    _add_member_runs(settings)
    member = replace(settings, now=lambda: T0)
    status, body = get(member, "/team/epsilon")
    assert status is HTTPStatus.OK
    assert "Epsilon · 개발팀원" in body and 'href="/team"' in body
    assert "2회" in body and "1회 · 50%" in body  # 전체 실행, 성공률
    assert "구현" in body and "수정" in body and "형식 오류 1" in body
    assert "#1 여행 &lt;소비&gt;" in body  # 맡았던 작업 (이스케이프)
    _, filtered = get(member, "/team/epsilon?status=ok")
    assert filtered.count("형식 오류</span>") == 0  # 실행 기록 표는 성공만


def test_member_page_includes_runs_without_task(settings):
    from dataclasses import replace

    _add_member_runs(settings)
    _, body = get(replace(settings, now=lambda: T0), "/team/eta")
    assert "작업 없음 (채팅·교육·비서)" in body and "개념 노트" in body
    assert "작업 밖 실행" in body
    _, runs = get(settings, "/runs")
    assert "작업 없음 (채팅·교육·비서)" in runs  # 실행 기록 화면에도 보인다


def test_unknown_member_is_404(settings):
    status, _ = get(settings, "/team/nobody")
    assert status is HTTPStatus.NOT_FOUND
