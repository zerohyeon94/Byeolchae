"""ZeroSync 회사 홈. 소개는 회사 헌장·프로젝트 목록·ADR-012, 현황은 작업 DB를 따른다."""

import sqlite3
from collections.abc import Sequence
from html import escape
from urllib.parse import urlencode

from bot.dashboard import queries, views
from bot.org import Org

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
        '<p class="eyebrow">별직원들이 모여 일하는 곳</p><h1>ZeroSync</h1>'
        '<p class="mission">앱 하나, 이야기 한 편이 누군가의 밤에 작은 빛이 되기를.</p>'
        '<p class="home-lede">침착한 폴라리스와 궁금한 게 많은 샤울라는 '
        "같은 문제를 보고도 서로 다른 질문을 해요. 우리는 그렇게 함께 앱을 만들고, "
        "만들면서 겪은 일들도 영상으로 나누고 싶어요. "
        "그 빛이 하나씩 모이면 언젠가 밤하늘이 가득 차겠지요.</p>"
        f'<p class="home-facts">대표 Zero <span>·</span> {departments}개 부문 <span>·</span> '
        f"{len(org.teams)}개 팀 <span>·</span> 별직원 {len(org.agents)}명</p>"
        '<div class="home-actions"><a class="tour" href="/org">회사 조직도 →</a>'
        '<a href="/org/stars">직원 만나기 →</a></div></div>'
        f"{figures}</section>"
    )


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


def _work_section(org: Org, conn: sqlite3.Connection | None) -> str:
    tasks, records = [], []
    if conn is not None:
        for row in queries.current_tasks(conn):
            label = escape(STATE_LABELS.get(row.state, row.state))
            tasks.append(
                f'<li><a href="/tasks/{row.id}"><b>{_product_name(row.project)} · '
                f"{escape(row.title)}</b><small>#{row.id} · {label}</small></a></li>"
            )
        for event in queries.recent_task_updates(conn):
            actor = org.display(event.actor).split(" · ")[0]
            kind = views.KIND_LABELS.get(event.kind, event.kind)
            time = views.local_time(event.created_at)[:16]
            records.append(
                f'<li><a href="/tasks/{event.task_id}#event-{event.event_id}">'
                f"<b>{_product_name(event.project)} · {escape(event.title)}</b>"
                f"<small>{time} · {escape(actor)} · {escape(kind)}</small></a></li>"
            )
    columns = []
    for title, items, empty in (
        ("진행 중인 작업", tasks, "현재 열려 있는 작업이 없습니다."),
        ("최근 작업 기록", records, "아직 기록된 작업이 없습니다."),
    ):
        content = (
            ("<ul>" + "".join(items) + "</ul>")
            if items
            else (f"<p>{empty if conn is not None else '첫 업무 기록을 기다리고 있습니다.'}</p>")
        )
        columns.append(f"<div><h3>{title}</h3>{content}</div>")
    return (
        '<section class="home-section"><div class="home-heading"><h2>업무 현황</h2>'
        '<a href="/summary">업무 요약 보기 →</a></div>'
        f'<div class="home-work">{"".join(columns)}</div></section>'
    )


def home(org: Org, projects: Sequence[str], conn: sqlite3.Connection | None) -> str:
    principles = (
        '<section class="home-section home-charter"><div><h2>어떻게 일하나요?</h2>'
        "<p>테스트 하나가 실패한 날, 샤울라는 자기 계산으로는 이유를 설명할 수 없다고 했어요. "
        "미자르가 놓친 조건을 짚어 주자 둘은 답을 찾았죠. "
        "이렇게 서로 다른 의견을 꺼내고, 선택이 필요한 날엔 Zero가 그 이유를 듣고 결정해요.</p>"
        + (
            '<a href="/org/stars/shaula">샤울라의 이야기 읽기 →</a>'
            if "shaula" in org.agents
            else ""
        )
        + "</div>"
        "<div><h2>2027년 10월에 돌아볼 목표</h2><ul>"
        "<li><b>제품</b> 출시와 업데이트를 이어가고, "
        "테스트 통과율과 장애 대응 시간을 살핍니다.</li>"
        "<li><b>교육</b> Zero가 계획한 학습을 기간 안에 얼마나 마쳤는지 확인합니다.</li>"
        "<li><b>비서</b> 브리핑을 제때 전했는지, 요청받은 조사를 얼마나 보고했는지 봅니다.</li>"
        "</ul></div></section>"
    )
    return views.page(
        "회사 홈",
        _hero(org) + _product_section(projects) + _work_section(org, conn) + principles,
        active="company",
    )
