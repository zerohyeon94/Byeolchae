"""별자리 조직도. 회사 명단과 실제 작업 기록을 읽어 캐릭터 도감을 그린다."""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from bot.dashboard import queries, views
from bot.dashboard.characters import CHARACTERS, Character
from bot.dashboard.identity import FOUNDER_NAME, staff_description
from bot.org import Org

ASSETS = Path(__file__).with_name("assets")
DEPARTMENTS = {
    "executive": "대표 직속",
    "management": "경영지원",
    "product": "제품",
    "market": "시장",
    "content": "콘텐츠",
    "education": "교육",
    "secretary": "비서",
}
TEAM_WORK = {
    "secretary": "오늘 결정할 일",
    "education": "배움과 복습",
    "research": "시장과 기술 조사",
    "management": "전사 우선순위",
    "hr": "채용과 역할",
    "finance": "가격과 비용",
    "planning": "제품 기획",
    "design": "화면과 그래픽",
    "dev": "앱과 웹 개발",
    "qa": "테스트와 원인 진단",
    "marketing": "제품 홍보",
    "sales": "판매와 제휴",
    "cs": "사용자 문의",
    "channel": "채널과 커뮤니티",
    "video": "대본과 편집",
}


def _team_name(org: Org, key: str) -> str:
    name = org.teams[key].name
    return name if name.endswith("팀") else name + "팀"


def _portrait(key: str, org: Org, *, eager: bool = False) -> str:
    if (ASSETS / "portraits" / f"{key}.webp").is_file():
        loading = "eager" if eager else "lazy"
        return f'<img src="/org/assets/{key}.webp" alt="" loading="{loading}" decoding="async">'
    return f'<span class="monogram" aria-hidden="true">{escape(org.agents[key].display[:1])}</span>'


def _crumb(team: tuple[str, str] | None = None, member: str | None = None) -> str:
    parts = ['<a href="/org">별자리 조직도</a>']
    if team:
        key, name = team
        parts.append(
            f'<a href="/org/teams/{quote(key, safe="")}">{escape(name)}</a>'
            if member
            else escape(name)
        )
    if member:
        parts.append(escape(member))
    return '<nav class="crumb" aria-label="현재 위치">' + " <span>›</span> ".join(parts) + "</nav>"


def _team_members(org: Org, key: str) -> list[str]:
    members = [name for name, spec in org.agents.items() if spec.team == key]
    return sorted(members, key=lambda name: name != org.teams[key].lead)


def _character(org: Org, key: str) -> Character:
    if key in CHARACTERS:
        return CHARACTERS[key]
    spec = org.agents[key]
    return Character(spec.title, staff_description(spec.perspective), "")


def _profile_link(org: Org, key: str) -> str:
    return (
        f'<a class="portrait-link" href="/org/stars/{key}">'
        f'<span class="thumb">{_portrait(key, org)}</span>'
        f"<span><b>{escape(org.agents[key].display)}</b>"
        f"<small>{escape(_character(org, key).role)}</small></span></a>"
    )


ENGINES = {"claude": "Claude", "codex": "Codex"}

# 부서 버튼과 검색으로 카드를 거른다. 스크립트가 없으면 도구를 숨기고 모든 카드를 보인다
_FILTER_SCRIPT = """<script>
(() => {
  const grid = document.querySelector('.workers');
  const search = document.querySelector('#worker-search');
  const empty = document.querySelector('.workers-empty');
  const chips = document.querySelectorAll('.dept-chips button');
  let dept = '';
  const apply = () => {
    const word = search.value.trim().toLowerCase();
    let shown = 0;
    for (const card of grid.children) {
      const ok = (!dept || card.dataset.dept === dept) &&
        (!word || card.dataset.search.includes(word));
      card.hidden = !ok;
      shown += ok;
    }
    empty.hidden = shown > 0;
  };
  for (const chip of chips) {
    chip.addEventListener('click', () => {
      dept = chip.dataset.dept;
      for (const other of chips) other.setAttribute('aria-pressed', String(other === chip));
      apply();
    });
  }
  search.addEventListener('input', apply);
  document.querySelector('.worker-tools').hidden = false;
})();
</script>"""


def _worker_card(org: Org, key: str) -> str:
    spec = org.agents[key]
    team = org.teams[spec.team]
    character = _character(org, key)
    team_name = _team_name(org, spec.team)
    lead = '<span class="lead-badge">팀장</span>' if team.lead == key else ""
    chips = [TEAM_WORK.get(spec.team, team_name), ENGINES.get(spec.engine, spec.engine)]
    if spec.mbti:
        chips.append(spec.mbti)
    search = " ".join((spec.display, key, character.role, team_name)).lower()
    return (
        f'<a class="worker" href="/org/stars/{key}" data-dept="{escape(team.department)}" '
        f'data-search="{escape(search)}">'
        f'<span class="worker-face">{_portrait(key, org)}</span>'
        f'<span class="worker-team">{escape(team_name)}{lead}</span>'
        f"<b>{escape(spec.display)}</b><small>{escape(character.role)}</small>"
        f"<p>{escape(character.personality)}</p>"
        f'<span class="worker-question"><em>늘 던지는 질문</em>'
        f"{escape(staff_description(spec.basic_question))}</span>"
        f'<span class="chips">{"".join(f"<i>{escape(c)}</i>" for c in chips)}</span>'
        '<span class="text-link">자세히 보기 →</span></a>'
    )


def directory(org: Org) -> str:
    departments = list(dict.fromkeys(team.department for team in org.teams.values()))
    chips = ['<button type="button" data-dept="" aria-pressed="true">전체</button>']
    for dept in departments:
        count = sum(1 for spec in org.agents.values() if org.teams[spec.team].department == dept)
        chips.append(
            f'<button type="button" data-dept="{escape(dept)}" aria-pressed="false">'
            f"{escape(DEPARTMENTS.get(dept, dept))} <small>{count}</small></button>"
        )
    cards = [_worker_card(org, member) for key in org.teams for member in _team_members(org, key)]
    body = (
        '<section class="workers-head"><p class="eyebrow">'
        f"{len(departments)}개 부문 · {len(org.teams)}개 팀 · 별직원 {len(org.agents)}명</p>"
        "<h1>팀원 소개</h1><p>별직원마다 맡은 일과 말투, 늘 던지는 질문이 달라요. "
        "부문을 골라 필요한 동료를 찾아보세요.</p>"
        '<a class="text-link" href="/org">조직도에서 보기 →</a></section>'
        '<div class="worker-tools" hidden>'
        f'<div class="dept-chips" role="group" aria-label="부문">{"".join(chips)}</div>'
        '<input id="worker-search" type="search" placeholder="이름·역할·팀 찾기" '
        'aria-label="별직원 찾기"></div>'
        f'<div class="workers">{"".join(cards)}</div>'
        '<p class="workers-empty empty" hidden>찾는 별직원이 없어요.</p>'
        f"{_FILTER_SCRIPT}"
    )
    return views.page("팀원 소개", body, active="stars")


def chart(org: Org, conn: sqlite3.Connection | None) -> str:
    departments: dict[str, list[str]] = defaultdict(list)
    for key, team in org.teams.items():
        departments[team.department].append(key)
    active = set()
    if conn is not None:
        actors = conn.execute(
            "SELECT agent FROM agent_runs UNION SELECT actor FROM events "
            "UNION SELECT actor FROM chat_turns"
        )
        active = {org.current(actor) for (actor,) in actors}

    branches = []
    for dept, team_keys in departments.items():
        teams = []
        for key in team_keys:
            team = org.teams[key]
            members = _team_members(org, key)
            lit = any(name in active for name in members)
            state = "활동 기록 있음" if lit else "활동 기록 없음"
            leader = org.agents[team.lead].display
            faces = "".join(
                f'<a href="/org/stars/{member}" title="{escape(org.agents[member].display)}">'
                f"{_portrait(member, org, eager=True)}</a>"
                for member in members
            )
            teams.append(
                f'<li><div class="ot-team"><a class="ot-team-link" '
                f'href="/org/teams/{quote(key, safe="")}">'
                f'<span class="node-face">{_portrait(team.lead, org, eager=True)}</span>'
                f'<span class="node-copy"><b>{escape(_team_name(org, key))}</b>'
                f"<small>{escape(TEAM_WORK.get(key, leader))}</small>"
                f"<small>팀장 {escape(leader)} · {len(members)}명</small></span>"
                f'<i class="signal {"lit" if lit else ""}" role="img" '
                f'aria-label="{state}" title="{state}"></i></a>'
                f'<span class="ot-faces" aria-label="팀원">{faces}</span></div></li>'
            )
        count = sum(len(_team_members(org, key)) for key in team_keys)
        branches.append(
            f'<li class="ot-dept"><button type="button" class="ot-node dept" aria-expanded="true">'
            f"<b>{escape(DEPARTMENTS.get(dept, dept))}</b>"
            f"<small>{len(team_keys)}개 팀 · {count}명</small></button>"
            f"<ul>{''.join(teams)}</ul></li>"
        )
    body = (
        '<section class="ot-head"><div><p class="eyebrow">'
        f"{len(departments)}개 부문 · {len(org.teams)}개 팀 · 별직원 {len(org.agents)}명</p>"
        "<h1>조직도</h1><p>끌어서 옮기고, 휠이나 버튼으로 확대해 보세요. "
        "부문을 누르면 접히고, 팀을 누르면 그 팀으로 갑니다.</p></div>"
        '<small class="legend"><i class="signal lit"></i> 활동 기록 있음</small></section>'
        '<div class="ot-tools" hidden>'
        '<button type="button" data-zoom="out" aria-label="축소">−</button>'
        '<output class="ot-scale">100%</output>'
        '<button type="button" data-zoom="in" aria-label="확대">+</button>'
        '<button type="button" data-zoom="fit">화면에 맞춤</button>'
        '<button type="button" data-fold="open">모두 펼치기</button>'
        '<button type="button" data-fold="close">모두 접기</button></div>'
        '<div class="ot-view"><div class="ot-canvas"><ul class="ot">'
        f'<li><div class="ot-node chief"><b>{FOUNDER_NAME}</b><small>대표</small></div>'
        f"<ul>{''.join(branches)}</ul></li></ul></div></div>"
        f"{_CHART_SCRIPT}"
    )
    return views.page("조직도", body, active="org")


# 끌어서 이동, 휠·버튼 확대, 부문 접기. 스크립트가 없으면 상자 안에서 스크롤로 본다
_CHART_SCRIPT = """<script>
(() => {
  const view = document.querySelector('.ot-view');
  const canvas = document.querySelector('.ot-canvas');
  const label = document.querySelector('.ot-scale');
  const tools = document.querySelector('.ot-tools');
  let x = 0, y = 0, s = 1;
  const draw = () => {
    canvas.style.transform = `translate(${x}px, ${y}px) scale(${s})`;
    label.textContent = Math.round(s * 100) + '%';
  };
  const zoomAt = (next, cx, cy) => {
    next = Math.min(2, Math.max(0.3, next));
    x = cx - (cx - x) * next / s;
    y = cy - (cy - y) * next / s;
    s = next;
    draw();
  };
  const fit = () => {
    const w = canvas.scrollWidth, h = canvas.scrollHeight;
    s = Math.min(1, Math.max(0.3, Math.min(view.clientWidth / w, view.clientHeight / h) * 0.95));
    x = Math.max(12, (view.clientWidth - w * s) / 2);
    y = Math.max(12, (view.clientHeight - h * s) / 2);
    draw();
  };
  view.classList.add('live');
  tools.hidden = false;
  let drag = null;
  view.addEventListener('pointerdown', (e) => {
    if (e.target.closest('a, button')) return;
    drag = { px: e.clientX - x, py: e.clientY - y };
    view.setPointerCapture(e.pointerId);
    view.classList.add('grabbing');
  });
  view.addEventListener('pointermove', (e) => {
    if (!drag) return;
    x = e.clientX - drag.px;
    y = e.clientY - drag.py;
    draw();
  });
  const stop = () => { drag = null; view.classList.remove('grabbing'); };
  view.addEventListener('pointerup', stop);
  view.addEventListener('pointercancel', stop);
  view.addEventListener('wheel', (e) => {
    e.preventDefault();
    const box = view.getBoundingClientRect();
    zoomAt(s * Math.exp(-e.deltaY * 0.0015), e.clientX - box.left, e.clientY - box.top);
  }, { passive: false });
  tools.addEventListener('click', (e) => {
    const button = e.target.closest('button');
    if (!button) return;
    const mid = [view.clientWidth / 2, view.clientHeight / 2];
    if (button.dataset.zoom === 'in') zoomAt(s * 1.2, ...mid);
    if (button.dataset.zoom === 'out') zoomAt(s / 1.2, ...mid);
    if (button.dataset.zoom === 'fit') fit();
    if (button.dataset.fold) {
      const open = button.dataset.fold === 'open';
      for (const node of document.querySelectorAll('.ot-dept')) {
        node.classList.toggle('folded', !open);
        node.querySelector('.dept').setAttribute('aria-expanded', String(open));
      }
      fit();
    }
  });
  for (const dept of document.querySelectorAll('.ot-node.dept')) {
    dept.addEventListener('click', () => {
      const folded = dept.parentElement.classList.toggle('folded');
      dept.setAttribute('aria-expanded', String(!folded));
    });
  }
  fit();
  addEventListener('resize', fit);
})();
</script>"""


def team_page(org: Org, key: str) -> str:
    team = org.teams[key]
    name = _team_name(org, key)
    members = _team_members(org, key)
    cards = []
    for member in members:
        spec = org.agents[member]
        character = _character(org, member)
        cards.append(
            f'<a class="person" href="/org/stars/{member}">'
            f'<span class="paper">{_portrait(member, org, eager=True)}</span>'
            f'<div class="person-copy"><small>{escape(character.role)}</small>'
            f"<h2>{escape(spec.display)}</h2><p>{escape(character.personality)}</p>"
            '<span class="text-link">직원 만나기 →</span></div></a>'
        )
    sub = (
        "어디서 문제가 생겼는지 찾아내고, 같은 실수가 반복되지 않도록 확인하는 팀이에요."
        if key == "qa"
        else staff_description(org.agents[team.lead].basic_question)
    )
    department = DEPARTMENTS.get(team.department, team.department)
    body = (
        _crumb((key, name)) + '<section class="team-head">'
        f'<p class="eyebrow">{escape(department)} · {len(members)}명</p>'
        f"<h1>{escape(name)}</h1><p>{escape(sub)}</p></section>"
        f'<div class="people">{"".join(cards)}</div>'
    )
    return views.page(name, body, active="org")


@dataclass(frozen=True)
class Excerpt:
    label: str
    text: str
    event: queries.EventRow


# 10월 30일 가게씨 정기 테스트에서 고른 연속 장면. 각 문장이 해당 화자의 원문에 있을 때만 싣는다.
_SCENE = (
    (
        "첫 진단",
        "shaula",
        "diagnosis",
        "report",
        "다만 실행일 10/30 기준으로는 31일 기간이라 통과해야 하므로 "
        "이 가설만으로 이번 실패가 설명되지는 않습니다.",
    ),
    (
        "다시 확인",
        "mizar",
        "lead_check",
        "review",
        "근거, 재현 조건, 확신도를 바로잡아 다시 내야 한다.",
    ),
    (
        "수정 진단",
        "shaula",
        "diagnosis",
        "report",
        "급여일 주말 보정으로 급여 기간이 29일로 줄면 기본 예산이 103,448원이 되고 "
        "임계값은 10,344원이다.",
    ),
    ("팀장 승인", "mizar", "lead_check", "review", "지난 [필수] 3건이 모두 해결됐다."),
)


def _shaula_scene(org: Org, conn: sqlite3.Connection) -> str:
    origin = conn.execute(
        "SELECT e.task_id FROM events e JOIN tasks t ON t.id=e.task_id "
        "WHERE t.project='gagaessi' AND e.kind='diagnosis' AND e.created_at=?",
        ("2026-10-29T18:13:38.995897+00:00",),
    ).fetchone()
    if origin is None:
        return ""
    task_id = origin["task_id"]
    events = iter(queries.task_events(conn, task_id))
    lines = []
    for label, speaker, kind, field, excerpt in _SCENE:
        for event in events:
            if event.kind != kind or org.current(event.actor) != speaker:
                continue
            data = event.payload.get(field) if isinstance(event.payload, dict) else None
            summary = data.get("summary") if isinstance(data, dict) else None
            if isinstance(summary, str) and excerpt in summary:
                lines.append(Excerpt(label, excerpt, event))
                break
        else:
            return ""  # 원문과 경위를 함께 확인할 수 있는 장면만 소개한다
    first, *rest = lines
    stamp = datetime.fromisoformat(first.event.created_at).astimezone(ZoneInfo("Asia/Seoul"))
    moments = []
    for line in rest:
        member = org.agents.get(org.current(line.event.actor))
        person = member.display if member else line.event.actor
        moments.append(
            f"<li><small>{line.label} · {escape(person)}</small><q>{escape(line.text)}</q></li>"
        )
    source = f"/tasks/{task_id}#event-{first.event.id}"
    return (
        '<article class="scene"><p class="eyebrow">기록 속 한마디</p>'
        "<h2>테스트 하나가 실패한 새벽</h2>"
        f'<p class="byline">{stamp:%Y.%m.%d} · 가게씨 정기 테스트 · 작업 #{task_id}</p>'
        "<p>431개 중 딱 하나가 실패했다. 샤울라는 마지막 1만 원을 처리하는 기준이 "
        "날짜에 따라 달라진다는 점을 찾아냈다. "
        "그런데 첫 계산으로는 그날의 실패를 설명할 수 없었다.</p>"
        f'<blockquote><a href="{source}">“{escape(first.text)}”</a>'
        "<cite>샤울라의 첫 진단에서</cite></blockquote>"
        "<h3>그 뒤에 일어난 일</h3><p>미자르가 급여일이 주말이면 앞당겨진다는 조건을 짚었다. "
        "샤울라가 기간을 31일에서 29일로 바로잡자, 예상했던 금액과 실제 결과의 차이가 설명됐다.</p>"
        f'<ol class="moments">{"".join(moments)}</ol>'
        f'<a class="text-link" href="{source}">작업 원문 보기 →</a></article>'
    )


def star_page(org: Org, key: str, conn: sqlite3.Connection | None) -> str:
    spec = org.agents[key]
    team = _team_name(org, spec.team)
    character = _character(org, key)
    caption = f"<figcaption>{escape(character.caption)}</figcaption>" if character.caption else ""
    peers = "".join(
        _profile_link(org, member) for member in _team_members(org, spec.team) if member != key
    )
    story = _shaula_scene(org, conn) if key == "shaula" and conn is not None else ""
    if key == "shaula" and not story:
        story = '<p class="empty">이 대시보드에는 소개할 진단 장면의 원문이 아직 없습니다.</p>'
    body = (
        _crumb((spec.team, team), spec.display)
        + '<section class="profile"><figure class="star-art"><div class="profile-paper">'
        f'{_portrait(key, org, eager=True)}</div>{caption}</figure><div class="profile-copy">'
        f'<p class="eyebrow">{escape(character.role)}</p><h1>{escape(spec.display)}</h1>'
        f'<p>{escape(character.personality)}</p><div class="star-question"><h2>늘 던지는 질문</h2>'
        f"<p>{escape(staff_description(spec.basic_question))}</p></div></div></section>{story}"
    )
    if peers:
        body += (
            '<section class="teammates"><h2>같은 팀의 별직원</h2>'
            f'<div class="portrait-links">{peers}</div></section>'
        )
    body += (
        f'<p class="back"><a href="/org/teams/{quote(spec.team, safe="")}">← {escape(team)}으로</a>'
        '<a href="/org/stars">별직원 전체 보기</a>'
        f'<a href="/team/{key}">업무 기록 보기 →</a></p>'
    )
    return views.page(spec.display, body, active="stars")
