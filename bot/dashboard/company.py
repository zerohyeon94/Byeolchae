"""회사 홈. 소개는 회사 헌장·프로젝트 목록·ADR-012, 현황은 작업 DB와 봇 일정을 따른다."""

import sqlite3
from collections.abc import Sequence
from html import escape
from urllib.parse import urlencode

from bot.dashboard import queries, views
from bot.dashboard.features import ScheduledJob, cron_label
from bot.dashboard.identity import (
    COMPANY_NAME,
    COMPANY_NAME_EN,
    FOUNDER_NAME,
    FOUNDER_NAME_EN,
    FOUNDER_SUBJECT,
    actor_name,
)
from bot.org import Org
from bot.scheduler import MORNING_REPORT, WEEKLY_REPORT, parse_cron

# docs/회사/02-프로젝트.md의 우선순위와 다음 목표.
PRODUCTS = {
    "gagaessi": (
        "가게씨",
        "오늘 얼마를 쓸 수 있는지 알려 주는 개인 예산 앱.",
        "일일 예산 계산과 소비 기록을 안정화해 첫 버전을 출시합니다.",
    ),
    "jday": (
        "제이데이",
        "할 일과 일정, 업무 중 생긴 이슈를 한곳에 모으는 앱.",
        "운영 중 발견한 불편을 고치고 필요한 기능을 업데이트합니다.",
    ),
    "orot": (
        "오롯",
        "학습 계획과 타이머로 실제 집중한 시간을 돌아보는 앱.",
        "사용 흐름을 살펴 다음에 개선할 아이디어와 그 근거를 모읍니다.",
    ),
}
STATE_LABELS = {
    "OPINIONS": "의견 수렴",
    "PLANNING": "기획 중",
    "AWAIT_PLAN_APPROVAL": "기획 승인 대기",
    "UI_DESIGNING": "화면 디자인 중",
    "AWAIT_UI_APPROVAL": "디자인 승인 대기",
    "DESIGNING": "설계 중",
    "AWAIT_DESIGN_APPROVAL": "설계 승인 대기",
    "DIAGNOSING": "원인 진단 중",
    "AWAIT_FIX_APPROVAL": "수정 승인 대기",
    "IMPLEMENTING": "구현 중",
    "TESTING": "테스트 중",
    "REVIEWING": "검토 중",
    "FIXING": "수정 중",
    "AWAIT_MERGE": "병합 대기",
    "DOC_DRAFTING": "문서 작성 중",
    "AWAIT_DOC_APPROVAL": "문서 승인 대기",
    "RELEASE_PREPARING": "출시 준비 중",
    "AWAIT_RELEASE_APPROVAL": "출시 승인 대기",
    "REPORTING": "보고 중",
    "NEEDS_HUMAN": "대표 확인 필요",
}
# 작업 흐름 띠. 기능 개발·버그 수정·출시 상태를 같은 단계 이름으로 묶는다
FLOW_STEPS: tuple[tuple[str, frozenset[str]], ...] = (
    ("의견", frozenset({"OPINIONS"})),
    ("기획", frozenset({"PLANNING", "AWAIT_PLAN_APPROVAL"})),
    ("디자인", frozenset({"UI_DESIGNING", "AWAIT_UI_APPROVAL"})),
    (
        "설계",
        frozenset({"DESIGNING", "AWAIT_DESIGN_APPROVAL", "DIAGNOSING", "AWAIT_FIX_APPROVAL"}),
    ),
    (
        "구현",
        frozenset({"IMPLEMENTING", "FIXING", "RELEASE_PREPARING", "AWAIT_RELEASE_APPROVAL"}),
    ),
    ("테스트", frozenset({"TESTING"})),
    ("검토", frozenset({"REVIEWING"})),
    ("병합", frozenset({"AWAIT_MERGE"})),
    ("문서", frozenset({"DOC_DRAFTING", "AWAIT_DOC_APPROVAL", "REPORTING"})),
)
# 대표가 승인·결정해야 다음으로 넘어가는 상태 (업무 요약의 대표 할 일과 모든 승인 대기)
FOUNDER_TURN = tuple(
    state for state in STATE_LABELS if state in queries.WAITING_STATES or state.startswith("AWAIT_")
)


def _hero(org: Org) -> str:
    portraits = []
    for key in ("polaris", "arcturus", "shaula"):
        if key in org.agents:
            name = escape(org.agents[key].display)
            portraits.append(
                f'<a href="/org/stars/{key}"><img src="/org/assets/{key}.webp" alt="">'
                f"<span>{name}</span></a>"
            )
    figures = f'<div class="home-stars">{"".join(portraits)}</div>' if portraits else ""
    departments = len({team.department for team in org.teams.values()})
    return (
        '<section class="home-intro"><div class="sky" aria-hidden="true"></div><div>'
        f'<p class="eyebrow">{COMPANY_NAME}({COMPANY_NAME_EN}) · '
        "별직원들이 도란도란 모여 일하는 곳</p>"
        f"<h1>{FOUNDER_SUBJECT} 잠든 사이에도<br>별들은 일해요</h1>"
        '<p class="mission">앱 하나, 이야기 한 편이 누군가의 밤에 작은 빛이 되기를.</p>'
        '<p class="home-lede">새벽에는 QA팀이 앱을 시험하고, 아침이 오기 전에 교육팀이 '
        "오늘 배울 노트를 펼쳐 둬요. 침착한 폴라리스와 궁금한 게 많은 샤울라는 "
        "같은 문제를 보고도 서로 다른 질문을 하고, 선택이 필요한 날엔 대표가 결정해요.</p>"
        f'<p class="home-facts">대표 {FOUNDER_NAME}({FOUNDER_NAME_EN}) <span>·</span> '
        f"{departments}개 부문 <span>·</span> "
        f"{len(org.teams)}개 팀 <span>·</span> 별직원 {len(org.agents)}명</p>"
        '<div class="home-actions"><a class="tour" href="/org/stars">팀원 만나기 →</a>'
        '<a href="/org">조직도 보기 →</a></div></div>'
        f"{figures}</section>"
    )


def _pulse(org: Org, conn: sqlite3.Connection | None, today_start: str | None) -> str:
    """오늘 숫자 띠. DB가 없으면 그리지 않는다."""
    if conn is None or today_start is None:
        return ""
    actors = [
        agent
        for (agent,) in conn.execute(
            "SELECT agent FROM agent_runs WHERE started_at >= ?", (today_start,)
        )
    ]
    staff = {org.current(agent) for agent in actors} & set(org.agents)
    marks = ", ".join("?" for _ in FOUNDER_TURN)
    open_tasks, waiting = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(state IN ({marks})), 0) FROM tasks "
        "WHERE state NOT IN ('DONE','STOPPED')",
        FOUNDER_TURN,
    ).fetchone()
    items = (
        ("오늘 실행", f"{len(actors)}회", "/runs"),
        ("오늘 일한 별직원", f"{len(staff)}명", "/team"),
        ("진행 중인 작업", f"{open_tasks}건", "/tasks"),
        (f"{FOUNDER_NAME} 대표 차례", f"{waiting}건", "/summary"),
    )
    cells = "".join(
        f'<a href="{href}"><small>{label}</small><b>{value}</b></a>' for label, value, href in items
    )
    return f'<nav class="home-pulse" aria-label="오늘 숫자">{cells}</nav>'


def _product_section(projects: Sequence[str]) -> str:
    cards = []
    for key, (name, purpose, goal) in PRODUCTS.items():
        work = (
            f'<a href="/tasks?{urlencode({"project": key})}">작업 보기 →</a>'
            if key in projects
            else ""
        )
        cards.append(
            f"<article><h3>{name}</h3><p>{purpose}</p><div><b>지금 목표</b><p>{goal}</p></div>"
            f"{work}</article>"
        )
    return (
        '<section class="home-section"><h2>제품과 목표</h2>'
        f'<div class="home-products">{"".join(cards)}</div></section>'
    )


def _product_name(project: str) -> str:
    return PRODUCTS[project][0] if project in PRODUCTS else escape(project)


def flow_step(state: str) -> int:
    """상태가 흐름 띠의 몇 번째 단계인지. 어느 단계도 아니면 -1 (예: 대표 확인 필요)."""
    for index, (_, states) in enumerate(FLOW_STEPS):
        if state in states:
            return index
    return -1


def _flow_card(org: Org, row: queries.TaskRow, last: queries.TaskUpdate | None) -> str:
    current = flow_step(row.state)
    here = "now"
    if current == -1 and row.prev_state:
        # 대표 확인 필요: 멈추기 직전 단계에 멈춤 표시를 한다
        current, here = flow_step(row.prev_state), "stop"
    steps = []
    for index, (label, _) in enumerate(FLOW_STEPS):
        css = "done" if index < current else here if index == current else ""
        steps.append(f'<li class="{css}"><i></i><span>{label}</span></li>')
    state = escape(STATE_LABELS.get(row.state, row.state))
    turn = (
        f'<span class="turn founder">{FOUNDER_NAME} 대표 차례</span>'
        if row.state in FOUNDER_TURN
        else '<span class="turn">별직원이 일하는 중</span>'
    )
    trace = ""
    if last is not None:
        actor = actor_name(org, last.actor).split(" · ")[0]
        kind = views.KIND_LABELS.get(last.kind, last.kind)
        time = views.local_time(last.created_at)[5:16]
        trace = f"<small>마지막 기록 {time} · {escape(actor)} · {escape(kind)}</small>"
    return (
        f'<a class="flow" href="/tasks/{row.id}"><div class="flow-head">'
        f"<b>{_product_name(row.project)} · {escape(row.title)}</b>{turn}</div>"
        f'<ol class="rail" aria-label="{state}">{"".join(steps)}</ol>'
        f"<small>#{row.id} · {state}</small>{trace}</a>"
    )


def _work_section(org: Org, conn: sqlite3.Connection | None) -> str:
    flows, records = [], []
    if conn is not None:
        updates = queries.recent_task_updates(conn, limit=20)
        latest = {update.task_id: update for update in updates}
        for row in queries.current_tasks(conn):
            flows.append(_flow_card(org, row, latest.get(row.id)))
        for event in updates[:3]:
            actor = actor_name(org, event.actor).split(" · ")[0]
            kind = views.KIND_LABELS.get(event.kind, event.kind)
            time = views.local_time(event.created_at)[:16]
            records.append(
                f'<li><a href="/tasks/{event.task_id}#event-{event.event_id}">'
                f"<b>{_product_name(event.project)} · {escape(event.title)}</b>"
                f"<small>{time} · {escape(actor)} · {escape(kind)}</small></a></li>"
            )
    waiting = "첫 업무 기록을 기다리고 있습니다."
    flow_html = "".join(flows) or (
        f"<p>{'현재 열려 있는 작업이 없습니다.' if conn is not None else waiting}</p>"
    )
    record_html = (
        ("<ul>" + "".join(records) + "</ul>")
        if records
        else f"<p>{'아직 기록된 작업이 없습니다.' if conn is not None else waiting}</p>"
    )
    return (
        '<section class="home-section"><div class="home-heading"><div>'
        '<p class="eyebrow">지금</p><h2>일이 흘러가는 모습</h2></div>'
        '<a href="/summary">업무 요약 보기 →</a></div>'
        '<p class="home-sub">작업 하나가 의견에서 문서까지 단계를 지나요. '
        "별직원이 만들고 검토하면, 승인과 병합은 대표가 합니다.</p>"
        f'<div class="home-work"><div class="flows"><h3>진행 중인 작업</h3>{flow_html}</div>'
        f"<div><h3>최근 작업 기록</h3>{record_html}</div></div></section>"
    )


def _shift_order(job: ScheduledJob) -> tuple[int, int]:
    cron = parse_cron(job.cron)
    # 저녁 정리부터 다음 날 아침 보고까지 밤 근무 순서로 놓는다
    return ((min(cron.hours) - 18) % 24, min(cron.minutes))


def _shift_team(job: str) -> str | None:
    if job.startswith("education"):
        return "education"
    if job.startswith("secretary") or job in (MORNING_REPORT, WEEKLY_REPORT):
        return "secretary"
    if ":" in job:
        return "qa"  # 파이프라인 정기 실행은 정기 테스트다
    return None


def _night_shift(org: Org, schedules: Sequence[ScheduledJob]) -> str:
    if not schedules:
        return ""
    rows = []
    for job in sorted(schedules, key=_shift_order):
        team = _shift_team(job.job)
        face, who = "", f"{COMPANY_NAME} 봇"
        if team in org.teams:
            lead = org.teams[team].lead
            face = f'<img src="/org/assets/{lead}.webp" alt="" loading="lazy">'
            name = org.teams[team].name
            who = name if name.endswith("팀") else name + "팀"
        rows.append(
            f"<li><time>{escape(cron_label(job.cron))}</time>"
            f'<span class="shift-face">{face}</span>'
            f"<span><b>{escape(job.name)}</b><small>{escape(who)}</small></span></li>"
        )
    return (
        '<section class="home-section home-shift"><div class="home-heading"><div>'
        '<p class="eyebrow">밤 근무</p><h2>오늘 밤 근무표</h2></div>'
        '<a href="/features">일정 전체 보기 →</a></div>'
        '<p class="home-sub">정해진 시각이 되면 봇이 별직원을 깨워요. '
        "대표가 자는 동안 끝난 일은 아침 보고로 모입니다.</p>"
        f'<ol class="shift">{"".join(rows)}</ol></section>'
    )


def home(
    org: Org,
    projects: Sequence[str],
    conn: sqlite3.Connection | None,
    *,
    schedules: Sequence[ScheduledJob] = (),
    today_start: str | None = None,
) -> str:
    principles = (
        '<section class="home-section home-charter"><div><h2>어떻게 일하나요?</h2>'
        "<p>테스트 하나가 실패한 날, 샤울라는 자기 계산으로는 이유를 설명할 수 없다고 했어요. "
        "미자르가 놓친 조건을 짚어 주자 둘은 답을 찾았죠. "
        "이렇게 서로 다른 의견을 꺼내고, 선택이 필요한 날엔 "
        f"{FOUNDER_SUBJECT} 그 이유를 듣고 결정해요.</p>"
        + (
            '<a href="/org/stars/shaula">샤울라의 이야기 읽기 →</a>'
            if "shaula" in org.agents
            else ""
        )
        + "</div>"
        "<div><h2>2027년 10월에 돌아볼 목표</h2><ul>"
        "<li><b>제품</b> 출시와 업데이트를 이어가고, "
        "테스트 통과율과 장애 대응 시간을 살핍니다.</li>"
        f"<li><b>교육</b> {FOUNDER_SUBJECT} 계획한 학습을 기간 안에 "
        "얼마나 마쳤는지 확인합니다.</li>"
        "<li><b>비서</b> 브리핑을 제때 전했는지, 요청받은 조사를 얼마나 보고했는지 봅니다.</li>"
        "</ul></div></section>"
    )
    return views.page(
        "회사 홈",
        _hero(org)
        + _pulse(org, conn, today_start)
        + _work_section(org, conn)
        + _night_shift(org, schedules)
        + _product_section(projects)
        + principles,
        active="company",
    )
