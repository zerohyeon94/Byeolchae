"""대시보드 HTML.

모든 데이터 값은 이스케이프한다 (에이전트 출력과 Zero 메시지가 그대로 들어온다).
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from html import escape
from typing import Any
from urllib.parse import quote, urlencode

from bot.dashboard.features import COMMANDS, PAGES, SAFEGUARDS, ScheduledJob, cron_label
from bot.dashboard.queries import (
    AgentActivity,
    EventRow,
    IdeaRow,
    Overview,
    ProjectSummary,
    RunRow,
    StageStat,
    StudyRow,
    TaskRow,
)
from bot.education.daily import Month, QueueItem
from bot.org import DEFAULT_ORG, Org
from bot.pipeline import Pipeline
from bot.render import render_opinion
from bot.scheduler import parse_cron
from bot.schemas import Opinion

IDEA_LABELS = {"open": "열림"}

STATUS_LABELS = {
    "running": "실행 중",
    "ok": "성공",
    "invalid_output": "형식 오류",
    "timeout": "시간 초과",
    "cancelled": "취소",
    "error": "오류",
}
STAGE_LABELS = {
    "opinion": "의견",
    "debate": "반박",
    "plan": "기획",
    "ui_design": "화면 디자인",
    "design": "설계",
    "diagnose": "진단",
    "implement": "구현",
    "fix": "수정",
    "lead_check": "팀장 확인",
    "review": "리뷰",
    "release_prep": "출시 준비",
    "doc_sync": "문서 반영",
    "concept_note": "개념 노트",
    "weekly_quiz": "주간 문제",
    "secretary_daily": "하루 정리",
}
KIND_LABELS = {
    "message": "메시지",
    "opinion": "의견",
    "debate": "반박",
    "decision": "결정",
    "command": "명령",
    "plan": "기획",
    "ui_design": "화면 디자인",
    "design": "설계",
    "diagnosis": "진단",
    "impl": "구현",
    "fix": "수정",
    "lead_check": "팀장 확인",
    "test": "테스트",
    "review": "리뷰",
    "merge": "merge",
    "doc_sync": "문서 반영",
    "release": "출시 준비",
    "report": "보고",
    "error": "오류",
}
_BAD_STATUSES = {"invalid_output", "timeout", "error"}

_CSS = """
:root { --bg:#fafafa; --fg:#1d1d1f; --muted:#6e6e73; --line:#e3e3e8; --card:#fff;
  --accent:#3a5bd9; --bad:#c0392b; --ok:#2e7d4f; --warn:#b26a00; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --fg:#ececf0; --muted:#9a9aa2;
  --line:#2c2c31; --card:#1f1f23; --accent:#8aa4ff; --bad:#ff7b6b; --ok:#6fcf97; --warn:#f2b45c; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.5 -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", sans-serif; }
header { display:flex; gap:20px; align-items:baseline; padding:14px 24px;
  border-bottom:1px solid var(--line); background:var(--card); }
header strong { font-size:16px; }
header a { color:var(--muted); text-decoration:none; }
header a.on { color:var(--fg); font-weight:600; }
main { padding:20px 24px; max-width:1200px; }
h1 { font-size:20px; margin:0 0 16px; } h2 { font-size:16px; margin:28px 0 10px; }
a { color:var(--accent); }
table { border-collapse:collapse; width:100%; background:var(--card);
  border:1px solid var(--line); }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line);
  vertical-align:top; }
th { color:var(--muted); font-weight:500; font-size:12px; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.muted { color:var(--muted); } .bad { color:var(--bad); } .ok { color:var(--ok); }
.warn { color:var(--warn); }
.cards { display:flex; flex-wrap:wrap; gap:12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 16px;
  min-width:220px; }
.card .big { font-size:24px; font-weight:600; }
.member { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:14px 18px; margin:0 0 14px; }
.member h3 { margin:0 0 4px; font-size:16px; }
.member dl { display:grid; grid-template-columns:max-content 1fr; gap:4px 14px; margin:10px 0; }
.member dt { color:var(--muted); } .member dd { margin:0; }
.badge { display:inline-block; border-radius:10px; padding:1px 8px; font-size:12px;
  border:1px solid var(--line); margin-left:6px; }
.badge.working { color:var(--ok); border-color:var(--ok); }
details pre { white-space:pre-wrap; }
form.filters { display:flex; gap:8px; align-items:center; margin-bottom:12px; }
select, button { font:inherit; padding:3px 6px; }
.event { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:10px 14px; margin-bottom:10px; }
.event .meta { color:var(--muted); font-size:12px; margin-bottom:6px; }
pre { white-space:pre-wrap; word-break:break-word; margin:0;
  font:13px/1.5 ui-monospace, Menlo, monospace; }
h3 { font-size:14px; margin:18px 0 6px; }
table.grouped td:first-child, table.grouped td:last-child { white-space:nowrap; }
.diagram { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px;
  overflow-x:auto; }
.diagram svg { width:100%; min-width:640px; height:auto; display:block; }
.diagram text { fill:var(--fg); font-size:13px; }
.diagram text.sub { fill:var(--muted); font-size:11px; }
.diagram .box { fill:var(--bg); stroke:var(--line); }
.diagram .core { fill:none; stroke:var(--accent); stroke-width:1.5; }
.diagram .edge { fill:none; stroke:var(--muted); stroke-width:1.4; }
.diagram .head { fill:var(--muted); }
ol.flow { list-style:none; display:flex; flex-wrap:wrap; align-items:stretch; gap:8px 0;
  padding:0; margin:6px 0 10px; }
ol.flow li { display:flex; align-items:center; }
ol.flow li + li::before { content:"→"; color:var(--muted); padding:0 8px; }
.step { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:6px 10px; min-width:96px; }
.step.zero { border:1px dashed var(--accent); }
.step.bot { background:transparent; }
.step .who { color:var(--muted); font-size:12px; }
.step .cond { color:var(--warn); font-size:12px; }
"""


def _e(value: object) -> str:
    return escape("" if value is None else str(value))


def local_time(iso: str | None) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}초"


def _status(status: str) -> str:
    css = "bad" if status in _BAD_STATUSES else "ok" if status == "ok" else "warn"
    return f'<span class="{css}">{_e(STATUS_LABELS.get(status, status))}</span>'


def _counts(counts: Mapping[str, int], labels: Mapping[str, str] | None = None) -> str:
    if not counts:
        return '<span class="muted">-</span>'
    return ", ".join(
        f"{_e((labels or {}).get(key, key))} {count}" for key, count in sorted(counts.items())
    )


def _name(agent: str) -> str:
    # 대시보드는 DB만 읽으므로 표시 이름은 기본 조직(ADR-007) 기준이다
    return _e(DEFAULT_ORG.display(agent))


def _speaker(actor: str) -> str:
    return "Zero" if actor == "zero" else _name(actor)


def page(title: str, body: str, *, active: str = "") -> str:
    nav = "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
        for key, href, label in (
            ("summary", "/", "요약"),
            ("tasks", "/tasks", "작업"),
            ("team", "/team", "직원"),
            ("education", "/education", "교육"),
            ("runs", "/runs", "실행 기록"),
            ("ideas", "/ideas", "아이디어"),
            ("features", "/features", "기능"),
        )
    )
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)} · ZeroSync</title><style>{_CSS}</style></head>"
        f"<body><header><strong>ZeroSync</strong>{nav}</header><main>{body}</main></body></html>"
    )


def _table(
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    numeric: frozenset[int] | set[int] = frozenset(),
) -> str:
    head = "".join(
        f'<th class="{"num" if i in numeric else ""}">{_e(h)}</th>' for i, h in enumerate(headers)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="{"num" if i in numeric else ""}">{cell}</td>' for i, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="muted">기록이 없다</td></tr>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _filters(
    action: str, selects: Sequence[tuple[str, str, Sequence[tuple[str, str]], str]]
) -> str:
    parts = []
    for name, label, options, current in selects:
        opts = '<option value="">전체</option>' + "".join(
            f'<option value="{_e(v)}"{" selected" if v == current else ""}>{_e(t)}</option>'
            for v, t in options
        )
        parts.append(f'<label>{_e(label)} <select name="{_e(name)}">{opts}</select></label>')
    return f'<form class="filters" action="{action}">{"".join(parts)}<button>보기</button></form>'


def _task_link(task_id: int, title: str) -> str:
    return f'<a href="/tasks/{task_id}">#{task_id} {_e(title)}</a>'


# 화면


def _age(since: str | None, now: datetime) -> str:
    if not since:
        return "-"
    hours = (now - datetime.fromisoformat(since)).total_seconds() / 3600
    return f"{hours:.0f}시간" if hours < 48 else f"{hours / 24:.0f}일"


def overview_section(
    data: Overview,
    *,
    now: datetime,
    daily_calls: Mapping[str, int],
) -> str:
    """홈 개요: 승인 대기를 맨 위에 둔다 (ADR-007, SPEC 9.3-1)."""

    link = _task_link
    todo = len(data.waiting) + len(data.hiring)
    waits = [(now - datetime.fromisoformat(w.waiting_since)).total_seconds() / 3600
             for w in data.waiting if w.waiting_since]  # fmt: skip
    avg_wait = f"{sum(waits) / len(waits):.0f}시간" if waits else "-"
    paused = (
        f'<div class="card"><div class="muted">사용량 한도 정지</div>'
        f'<div class="big warn">{_e(local_time(data.paused_until))}까지</div></div>'
        if data.paused_until
        else ""
    )
    cards = (
        f'<div class="card"><div class="muted">Zero가 할 일</div>'
        f'<div class="big {"warn" if todo else ""}">{todo}</div></div>'
        f'<div class="card"><div class="muted">실행 중</div>'
        f'<div class="big">{len(data.running)}</div></div>'
        f'<div class="card"><div class="muted">Zero 승인 평균 대기</div>'
        f'<div class="big">{avg_wait}</div></div>'
        f'<div class="card"><div class="muted">7일 시스템 실패</div>'
        f'<div class="big {"bad" if data.failures else ""}">{data.failures}</div></div>' + paused
    )
    waiting = _table(
        ["작업", "제품", "상태", "대기"],
        [
            (link(w.id, w.title), _e(w.project), _e(w.state), _e(_age(w.waiting_since, now)))
            for w in data.waiting
        ]
        + [
            (f"채용 제안 #{n}", _name(agent), "/hire 대기", _e(f"{team} {role}"))
            for n, agent, team, role in data.hiring
        ],
    )
    running = _table(
        ["작업", "에이전트", "단계", "시작"],
        (
            (
                link(r.task_id, r.title),
                _name(r.agent),
                _e(STAGE_LABELS.get(r.stage, r.stage)),
                _e(local_time(r.started_at)),
            )
            for r in data.running
        ),
    )

    def used(agent: str) -> str:
        count = data.today.get(agent, 0)
        over = agent in daily_calls and count >= daily_calls[agent]
        return f'<span class="{"bad" if over else ""}">{count}</span>'

    agents = sorted(set(daily_calls) | set(data.today))
    budget = _table(
        ["에이전트", "오늘 실행", "하루 한도"],
        ((_name(a), used(a), str(daily_calls.get(a, "-"))) for a in agents),
        numeric={1, 2},
    )
    board = _table(
        ["제품", "상태별 작업"],
        (
            (
                _e(project),
                "<br>".join(
                    f"{_e(state)}: " + ", ".join(link(i, t) for i, t in items)
                    for state, items in states.items()
                ),
            )
            for project, states in data.board.items()
        ),
    )
    metrics = _table(
        ["제품", "도달", "채택", "자동 제외", "확인 필요"],
        (
            (_e(m.project), str(m.started), str(m.merged), str(m.dropped), str(m.needs_human))
            for m in data.week
        ),
        numeric={1, 2, 3, 4},
    )
    return (
        f'<div class="cards">{cards}</div>'
        f"<h2>Zero가 할 일</h2>{waiting}<h2>실행 중</h2>{running}"
        f"<h2>오늘 예산</h2>{budget}<h2>제품별 작업판</h2>{board}"
        f"<h2>지표 (7일)</h2>{metrics}"
    )


def summary_page(
    summaries: Sequence[ProjectSummary], stats: Sequence[StageStat], overview: str = ""
) -> str:
    total_tasks = sum(s.tasks for s in summaries)
    total_runs = sum(s.runs for s in summaries)
    bad_runs = sum(n for s in summaries for k, n in s.by_status.items() if k in _BAD_STATUSES)
    cards = (
        f'<div class="card"><div class="muted">작업</div><div class="big">{total_tasks}</div></div>'
        '<div class="card"><div class="muted">에이전트 실행</div>'
        f'<div class="big">{total_runs}</div></div>'
        f'<div class="card"><div class="muted">실패 (형식 오류·시간 초과·오류)</div>'
        f'<div class="big {"bad" if bad_runs else ""}">{bad_runs}</div></div>'
    )
    projects = _table(
        ["프로젝트", "작업", "상태별", "실행", "결과별"],
        (
            (
                f'<a href="/tasks?{urlencode({"project": s.project})}">{_e(s.project)}</a>',
                str(s.tasks),
                _counts(s.by_state),
                f'<a href="/runs?{urlencode({"project": s.project})}">{s.runs}</a>',
                _counts(s.by_status, STATUS_LABELS),
            )
            for s in summaries
        ),
        numeric={1, 3},
    )
    stages = _table(
        ["에이전트", "단계", "실행", "평균 소요 (성공)", "결과별"],
        (
            (
                _name(s.agent),
                _e(STAGE_LABELS.get(s.stage, s.stage)),
                str(s.runs),
                seconds(s.avg_sec),
                _counts(s.by_status, STATUS_LABELS),
            )
            for s in stats
        ),
        numeric={2, 3},
    )
    body = (
        f"<h1>요약</h1>{overview}"
        f'<h2>누적</h2><div class="cards">{cards}</div>'
        f"<h2>프로젝트별</h2>{projects}<h2>에이전트·단계별 (병목: 평균 소요)</h2>{stages}"
    )
    return page("요약", body, active="summary")


def tasks_page(
    tasks: Sequence[TaskRow],
    *,
    projects: Sequence[str],
    states: Sequence[str],
    project: str,
    state: str,
    discord_url: Any,
) -> str:
    filters = _filters(
        "/tasks",
        [
            ("project", "프로젝트", [(p, p) for p in projects], project),
            ("state", "상태", [(s, s) for s in states], state),
        ],
    )
    rows = (
        (
            _task_link(t.id, t.title),
            _e(t.project),
            _e(t.state),
            str(t.opinion_rounds),
            str(t.runs),
            local_time(t.created_at),
            local_time(t.updated_at),
            _discord_link(discord_url(t.discord_thread)),
        )
        for t in tasks
    )
    table = _table(
        ["작업", "프로젝트", "상태", "질문 수", "실행", "생성", "마지막 변경", "Discord"],
        rows,
        numeric={3, 4},
    )
    return page("작업", f"<h1>작업 {len(tasks)}개</h1>{filters}{table}", active="tasks")


def _discord_link(url: str | None) -> str:
    return f'<a href="{_e(url)}">게시글</a>' if url else '<span class="muted">-</span>'


def _event_body(event: EventRow) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.kind in ("opinion", "debate") and "opinion" in payload:
        heading = "반박" if event.kind == "debate" else ""
        text = render_opinion(Opinion.model_validate(payload["opinion"]), heading=heading)
        return f"<pre>{_e(text.replace('**', ''))}</pre>"
    if event.kind == "message":
        title = payload.get("title")
        head = f"<strong>{_e(title)}</strong>\n" if title else ""
        return f"<pre>{head}{_e(payload.get('text', ''))}</pre>"
    if event.kind == "decision":
        # ADR-007부터 작업은 채팅의 /task로 시작한다. 시작 명령은 기록에 없으므로 원문만 보인다
        text = payload.get("text", "")
        context = str(payload.get("context", "")).strip()
        extra = f"\n\n채팅 의견 요약:\n{context}" if context else ""
        return f"<pre>{_e(text)}{_e(extra)}</pre>"
    if event.kind == "command":
        return f"<pre>/{_e(payload.get('name', ''))} {_e(payload.get('arg', ''))}</pre>"
    rendered = _work_event(event.kind, payload)
    if rendered is not None:
        return f"<pre>{_e(rendered.replace('**', ''))}</pre>"
    if event.kind == "error":
        parts = [f"{k}: {v}" for k, v in payload.items() if k not in ("raw",)]
        raw = payload.get("raw")
        extra = f"\n\n원문:\n{raw}" if raw else ""
        return f'<pre class="bad">{_e(chr(10).join(parts) + extra)}</pre>'
    return f"<pre>{_e(payload)}</pre>"


def _work_event(kind: str, payload: dict[str, Any]) -> str | None:
    """흐름 단계 이벤트를 Discord·PR과 같은 문구로 보여 준다."""
    from bot.render.work import (
        render_design,
        render_diagnosis,
        render_doc_sync,
        render_implementation,
        render_lead_check,
        render_plan,
        render_release_prep,
        render_review,
        render_test_report,
        render_ui_design,
    )
    from bot.schemas import (
        DesignSummary,
        DiagnosisReport,
        DocSyncProposal,
        ImplementationReport,
        PlanSummary,
        ReleasePrep,
        Review,
        UiDesignSummary,
    )
    from bot.testrun import TestReport

    try:
        if kind == "plan":
            return render_plan(PlanSummary.model_validate(payload["summary"]))
        if kind == "ui_design":
            return render_ui_design(UiDesignSummary.model_validate(payload["summary"]))
        if kind == "design":
            return render_design(DesignSummary.model_validate(payload["summary"]))
        if kind == "diagnosis":
            return render_diagnosis(DiagnosisReport.model_validate(payload["report"]))
        if kind == "lead_check":
            review = Review.model_validate(payload["review"])
            label = "진단" if payload.get("stage") == "diagnose" else "구현"
            return render_lead_check(review, label=label, round_=payload.get("round", 0))
        if kind == "doc_sync":
            return render_doc_sync(DocSyncProposal.model_validate(payload["proposal"]))
        if kind == "release":
            return render_release_prep(ReleasePrep.model_validate(payload["prep"]))
        if kind == "report":
            return "보고 포스트를 열었다" if payload.get("posted") else "통과. 보고하지 않았다"
        if kind in ("impl", "fix"):
            report = ImplementationReport.model_validate(payload["report"])
            return render_implementation(report, heading="수정" if kind == "fix" else "구현")
        if kind == "test":
            return render_test_report(TestReport.from_payload(payload))
        if kind == "review":
            review = Review.model_validate(payload["review"])
            return render_review(review, round_=payload.get("round", 0))
        if kind == "merge":
            return f"PR #{payload.get('pr')} merge 감지"
    except (KeyError, ValueError):
        return None
    return None


def task_page(
    task: TaskRow, events: Sequence[EventRow], runs: Sequence[RunRow], *, discord_url: str | None
) -> str:
    facts = _table(
        [
            "프로젝트",
            "상태",
            "직전 상태",
            "질문 수",
            "리뷰 라운드",
            "테스트 재시도",
            "생성",
            "마지막 변경",
            "Discord",
        ],
        [
            (
                _e(task.project),
                _e(task.state),
                _e(task.prev_state or "-"),
                str(task.opinion_rounds),
                str(task.review_round),
                str(task.test_retry),
                local_time(task.created_at),
                local_time(task.updated_at),
                _discord_link(discord_url),
            )
        ],
    )
    timeline = (
        "".join(
            f'<div class="event"><div class="meta">{local_time(e.created_at)} · '
            f"{_speaker(e.actor)} · {_e(KIND_LABELS.get(e.kind, e.kind))}"
            f"</div>{_event_body(e)}</div>"
            for e in events
        )
        or '<p class="muted">기록이 없다</p>'
    )
    body = (
        f"<h1>#{task.id} {_e(task.title)}</h1>{facts}"
        f"<h2>흐름</h2>{timeline}"
        f"<h2>에이전트 실행 {len(runs)}회</h2>{_runs_table(runs, with_task=False)}"
    )
    return page(f"#{task.id}", body, active="tasks")


def _runs_table(runs: Sequence[RunRow], *, with_task: bool = True) -> str:
    headers = ["시작", "에이전트", "단계", "결과", "소요", "종료 코드", "로그"]
    if with_task:
        headers.insert(1, "작업")
        headers.insert(2, "프로젝트")
    rows = []
    for r in runs:
        log = (
            f'<a href="/runs/{quote(r.id, safe="")}/log">보기</a>'
            if r.log_path
            else '<span class="muted">-</span>'
        )
        row = [
            local_time(r.started_at),
            _name(r.agent),
            _e(STAGE_LABELS.get(r.stage, r.stage)),
            _status(r.status),
            seconds(r.duration_sec),
            _e("-" if r.exit_code is None else r.exit_code),
            log,
        ]
        if with_task:
            row.insert(1, _task_link(r.task_id, r.title))
            row.insert(2, _e(r.project))
        rows.append(row)
    numeric = {6, 7} if with_task else {4, 5}
    return _table(headers, rows, numeric=numeric)


def _elapsed(since: str, now: datetime) -> str:
    minutes = (now - datetime.fromisoformat(since)).total_seconds() / 60
    return f"{minutes:.0f}분" if minutes < 60 else _age(since, now)


def assignments(
    pipelines: Mapping[str, Pipeline], education: str | None = None
) -> dict[str, list[str]]:
    """에이전트별로 맡은 흐름 단계. 팀장 확인은 `(확인)`을 붙인다."""
    jobs: dict[str, list[str]] = {}
    if education is not None:
        jobs[education] = ["교육 · 매일 Daily 개념 노트"]
    for pipeline in pipelines.values():
        for stage in pipeline.stages:
            label = STAGE_LABELS.get(stage.kind.value, stage.kind.value)
            if stage.agent:
                jobs.setdefault(stage.agent, []).append(f"{pipeline.name} · {label}")
            if stage.check:
                jobs.setdefault(stage.check, []).append(f"{pipeline.name} · {label} (확인)")
    return jobs


def _member(
    org: Org,
    agent: str,
    activity: AgentActivity | None,
    jobs: Sequence[str],
    daily_calls: Mapping[str, int],
    now: datetime,
) -> str:
    spec = org.agents[agent]
    running = activity.running if activity else []
    if running:
        run = running[0]
        badge = '<span class="badge working">일하는 중</span>'
        doing = (
            f"{_task_link(run.task_id, run.title)} · "
            f"{_e(STAGE_LABELS.get(run.stage, run.stage))} · {_elapsed(run.started_at, now)}째"
        )
    else:
        badge = '<span class="badge">쉬는 중</span>'
        last = activity.recent[0] if activity and activity.recent else None
        doing = (
            f'<span class="muted">마지막 일 {local_time(last.started_at)} · '
            f"{_e(STAGE_LABELS.get(last.stage, last.stage))}</span>"
            if last
            else '<span class="muted">아직 한 일이 없다</span>'
        )
    limit = daily_calls.get(agent)
    today = activity.today if activity else 0
    models = ", ".join(
        f"{STAGE_LABELS.get(stage.value, stage.value)} {model}"
        for stage, model in spec.models.items()
    )
    facts = [
        ("지금", doing),
        ("오늘 실행", f"{today} / {limit}" if limit else str(today)),
        ("최근 7일", _counts(activity.week if activity else {}, STATUS_LABELS)),
        ("맡은 일", _e(", ".join(jobs)) or '<span class="muted">흐름에 배정 없음</span>'),
        ("관점", _e(spec.perspective)),
        ("기본 질문", _e(spec.basic_question)),
        ("엔진·모델", _e(spec.engine + (f" ({models})" if models else ""))),
        ("채용", _e(" · ".join(x for x in (spec.hired, spec.reason) if x)) or "-"),
    ]
    rows = "".join(f"<dt>{_e(k)}</dt><dd>{v}</dd>" for k, v in facts)
    profile = org.profile(agent)
    persona = (
        f"<details><summary>인격·역량 (agents/{_e(agent)}.md)</summary>"
        f"<pre>{_e(profile)}</pre></details>"
        if profile
        else '<p class="muted">인격 파일 없음 (기본 조직)</p>'
    )
    recent = _runs_table(activity.recent, with_task=True) if activity and activity.recent else ""
    mbti = f" ({_e(spec.mbti)})" if spec.mbti else ""
    return (
        f'<section class="member" id="{_e(agent)}"><h3>{_name(agent)}{mbti}{badge}</h3>'
        f"<dl>{rows}</dl>{persona}{recent}</section>"
    )


def team_page(
    org: Org,
    activity: Mapping[str, AgentActivity],
    jobs: Mapping[str, Sequence[str]],
    *,
    daily_calls: Mapping[str, int],
    now: datetime,
) -> str:
    """직원 화면: 팀별로 팀장 먼저, 지금 하는 일, 오늘·7일 기록, 맡은 일, 인격 (ADR-008)."""
    working = sum(1 for a in activity.values() if a.running)
    parts = [
        "<h1>직원</h1>",
        f'<p class="muted">{len(org.agents)}명 중 {working}명이 일하는 중. '
        "에이전트는 한 번에 한 명씩 일한다 (ADR-007)</p>",
    ]
    for team in org.teams.values():
        members = [a for a, s in org.agents.items() if org.teams[s.team] is team]
        members.sort(key=lambda a: a != team.lead)
        parts.append(f"<h2>{_e(team.name)}팀</h2>")
        parts += [
            _member(org, a, activity.get(a), jobs.get(a, []), daily_calls, now) for a in members
        ]
    return page("직원", "".join(parts), active="team")


def runs_page(
    runs: Sequence[RunRow], *, projects: Sequence[str], project: str, status: str, limit: int
) -> str:
    filters = _filters(
        "/runs",
        [
            ("project", "프로젝트", [(p, p) for p in projects], project),
            ("status", "결과", list(STATUS_LABELS.items()), status),
        ],
    )
    note = f'<p class="muted">최근 {limit}건까지 보여 준다</p>' if len(runs) >= limit else ""
    body = f"<h1>실행 기록</h1>{filters}{note}{_runs_table(runs)}"
    return page("실행 기록", body, active="runs")


def ideas_page(ideas: Sequence[IdeaRow], *, projects: Sequence[str], project: str) -> str:
    filters = _filters("/ideas", [("project", "프로젝트", [(p, p) for p in projects], project)])
    rows = [
        (
            str(idea.id),
            _e(idea.project),
            _e(idea.body),
            _e(IDEA_LABELS.get(idea.status, idea.status)),
            _task_link(idea.task_id, "") if idea.task_id else "",
            local_time(idea.created_at),
        )
        for idea in ideas
    ]
    table = _table(["번호", "프로젝트", "내용", "상태", "작업", "남긴 때"], rows, {0})
    return page("아이디어", f"<h1>아이디어</h1>{filters}{table}", active="ideas")


def _queue_state(item: QueueItem, next_line: int | None) -> str:
    if item.done:
        return '<span class="ok">완료</span>'
    if item.target:
        return '<span class="warn">진행 중</span>'
    if item.line == next_line:
        return "다음"
    return '<span class="muted">대기</span>'


def education_page(
    months: Sequence[Month],
    queue: Sequence[QueueItem],
    studies: Sequence[StudyRow],
    *,
    today: date,
    display: str,
) -> str:
    """교육 커리큘럼 (ADR-009): 월별 목표, 학습 대기열 진행, 최근 학습 기록.

    월별 목표와 대기열은 볼트 파일을, 학습 기록은 DB를 읽는다. 쓰지 않는다.
    """
    key = f"{today:%Y-%m}"
    current = next((m for m in months if m.key >= key), None)
    by_month: dict[str, list[QueueItem]] = {}
    for item in queue:
        by_month.setdefault(item.month, []).append(item)
    pending = [q for q in queue if not q.done and not q.target]
    next_line = pending[0].line if pending else None

    this_month = by_month.get(current.key, []) if current else []
    done_month = len({s.target for s in studies if s.day.startswith(key) and s.done_at is not None})
    latest = studies[0] if studies else None
    today_text = "-"
    if latest is not None and latest.day == today.isoformat():
        state = "완료" if latest.done_at else ("이어 하는 중" if latest.carried else "읽는 중")
        today_text = f'{_e(latest.label)} <span class="muted">({state})</span>'
    cards = [
        ("이번 달 주제", _e(current.topic) if current else "-"),
        (
            "이번 달 대기열",
            f"{sum(1 for q in this_month if q.done)} / {len(this_month)}" if this_month else "-",
        ),
        ("이번 달 끝낸 개념", str(done_month)),
        ("오늘 개념", today_text),
    ]
    body = [f'<h1>교육 커리큘럼</h1><p class="muted">담당: {_e(display)}</p>']
    body.append(
        '<div class="cards">'
        + "".join(
            f'<div class="card"><div class="muted">{_e(label)}</div><div class="big">{value}</div>'
            "</div>"
            for label, value in cards
        )
        + "</div>"
    )

    rows = []
    for month in months:
        items = by_month.get(month.key, [])
        progress = f"{sum(1 for q in items if q.done)} / {len(items)}" if items else "-"
        topic = _e(month.topic) + (
            f' <span class="muted">({_e(month.note)})</span>' if month.note else ""
        )
        if current is not None and month.key == current.key:
            topic += '<span class="badge working">이번 달</span>'
        rows.append((_e(month.key), topic, _e(month.layer), _e(month.certificate), progress))
    table = _table(["월", "주제", "층", "자격증", "대기열"], rows, {4})
    body.append(f"<h2>월별 목표</h2>{table}")

    body.append("<h2>학습 대기열</h2>")
    if not queue:
        body.append('<p class="muted">대기열이 비어 있다</p>')
    topics = {m.key: m.topic for m in months}
    for month_key, items in by_month.items():
        title = (
            f"{month_key} · {topics[month_key]}" if month_key in topics else (month_key or "기타")
        )
        rows = [
            (
                _e(item.section),
                _e(item.title),
                _e(item.detail),
                _queue_state(item, next_line),
            )
            for item in items
        ]
        body.append(f"<h3>{_e(title)}</h3>" + _table(["구성 요소", "개념", "내용", "상태"], rows))

    rows = [
        (
            _e(study.day),
            _e(study.label),
            "이어 하기" if study.carried else "새 개념",
            local_time(study.done_at) if study.done_at else '<span class="muted">아직</span>',
        )
        for study in studies
    ]
    body.append("<h2>최근 학습</h2>" + _table(["날짜", "개념", "종류", "완료"], rows))
    return page("교육", "".join(body), active="education")


def message_page(title: str, message: str, status_class: str = "muted") -> str:
    return page(title, f'<h1>{_e(title)}</h1><p class="{status_class}">{_e(message)}</p>')


# 기능 화면 (/features)

_STEP_LABELS = {
    "gate": "Zero 승인",
    "merge_gate": "Zero merge",
    "test": "테스트",
    "report": "보고",
}
_CONDITIONS = {"has_ui_change": "화면 변경이 있을 때", "test_failed": "테스트가 실패했을 때"}


def _on(flag: bool, on: str = "켜짐", off: str = "꺼짐") -> str:
    return f'<span class="ok">{on}</span>' if flag else f'<span class="muted">{off}</span>'


def _stage_label(kind: str) -> str:
    return _STEP_LABELS.get(kind, STAGE_LABELS.get(kind, kind))


def _trigger(pipeline: Pipeline) -> str:
    parts = []
    if "discord_task" in pipeline.triggers:
        parts.append(pipeline.command or "Discord 명령")
    if "schedule" in pipeline.triggers and pipeline.schedule:
        parts.append(cron_label(pipeline.schedule))
    return " · ".join(parts)


def _step(title: str, who: str, css: str = "", notes: Sequence[str] = ()) -> str:
    extra = "".join(f'<div class="cond">{_e(note)}</div>' for note in notes)
    return (
        f'<li><div class="step {css}"><div>{_e(title)}</div>'
        f'<div class="who">{_e(who)}</div>{extra}</div></li>'
    )


def _pipeline_flow(org: Org, pipeline: Pipeline) -> str:
    steps = []
    for stage in pipeline.stages:
        kind = stage.kind.value
        notes = []
        if stage.when:
            notes.append(_CONDITIONS.get(stage.when, stage.when) + "만")
        if stage.check:
            notes.append(f"확인: {org.display(stage.check)}")
        if stage.retries is not None:
            notes.append(f"실패 시 재시도 {stage.retries}회")
        if stage.rounds is not None:
            notes.append(f"최대 {stage.rounds}라운드")
        if stage.agent:
            steps.append(_step(_stage_label(kind), org.display(stage.agent), notes=notes))
        elif kind in ("gate", "merge_gate"):
            steps.append(_step(_stage_label(kind), "Zero", "zero", notes))
        else:
            steps.append(_step(_stage_label(kind), "봇", "bot", notes))
    steps.append(_step("완료", "DONE", "bot"))
    return f'<ol class="flow">{"".join(steps)}</ol>'


_COMMON_FLOW = (
    ("제품 채팅에 글", "Zero", "zero"),
    ("팀장 의견 (/debate로 반박 1회)", "팀장", ""),
    ("/task로 포럼 포스트", "Zero", "zero"),
    ("파이프라인 단계 진행", "봇 + 담당 직원", ""),
    ("승인 대기 시 멘션, 오래 기다리면 한 번 더", "봇", "bot"),
    ("PR merge 확인 뒤 문서 반영", "봇", "bot"),
)

_ARCHITECTURE = """
<svg viewBox="0 0 960 440" role="img" aria-label="ZeroSync 구성도">
<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
 orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" class="head"/></marker></defs>
<rect class="box" x="20" y="150" width="130" height="60" rx="8"/>
<text x="85" y="176" text-anchor="middle">Zero</text>
<text class="sub" x="85" y="194" text-anchor="middle">Discord 앱 · 브라우저</text>
<rect class="box" x="200" y="40" width="180" height="200" rx="8"/>
<text x="290" y="68" text-anchor="middle">Discord 서버</text>
<text class="sub" x="290" y="100" text-anchor="middle">제품 채팅 채널</text>
<text class="sub" x="290" y="124" text-anchor="middle">작업 포럼 (포스트 1개 = 작업 1개)</text>
<text class="sub" x="290" y="148" text-anchor="middle">#대표실 · 아침·주간 보고</text>
<text class="sub" x="290" y="172" text-anchor="middle">#교육 · 개념 노트, 문제 버튼</text>
<text class="sub" x="290" y="196" text-anchor="middle">#비서 · 하루 정리</text>
<rect class="core" x="430" y="20" width="270" height="300" rx="10"/>
<text x="565" y="44" text-anchor="middle">봇 (python -m bot)</text>
<text class="sub" x="565" y="60" text-anchor="middle">launchd 상시 실행 · 결정론적 프로그램</text>
<rect class="box" x="450" y="70" width="230" height="44" rx="6"/>
<text x="565" y="89" text-anchor="middle">Discord 입출력</text>
<text class="sub" x="565" y="105" text-anchor="middle">슬래시 명령 · 버튼 · 웹훅</text>
<rect class="box" x="450" y="126" width="230" height="44" rx="6"/>
<text x="565" y="145" text-anchor="middle">엔진 · 상태 머신</text>
<text class="sub" x="565" y="161" text-anchor="middle">파이프라인 선언대로 단계 진행</text>
<rect class="box" x="450" y="182" width="230" height="44" rx="6"/>
<text x="565" y="201" text-anchor="middle">실행 대기열</text>
<text class="sub" x="565" y="217" text-anchor="middle">한 번에 한 명 · 하루 한도 · 예산</text>
<rect class="box" x="450" y="238" width="230" height="44" rx="6"/>
<text x="565" y="257" text-anchor="middle">일정 실행기</text>
<text class="sub" x="565" y="273" text-anchor="middle">보고 · 교육 · 비서 · 정기 테스트</text>
<rect class="box" x="750" y="20" width="190" height="56" rx="8"/>
<text x="845" y="44" text-anchor="middle">에이전트 CLI</text>
<text class="sub" x="845" y="62" text-anchor="middle">Claude · Codex</text>
<rect class="box" x="750" y="96" width="190" height="56" rx="8"/>
<text x="845" y="120" text-anchor="middle">GitHub</text>
<text class="sub" x="845" y="138" text-anchor="middle">worktree · gh · Issue · PR</text>
<rect class="box" x="750" y="172" width="190" height="56" rx="8"/>
<text x="845" y="196" text-anchor="middle">Obsidian 볼트</text>
<text class="sub" x="845" y="214" text-anchor="middle">제품 문서 · 학습 노트 · Daily</text>
<rect class="box" x="750" y="248" width="190" height="56" rx="8"/>
<text x="845" y="272" text-anchor="middle">앱 테스트</text>
<text class="sub" x="845" y="290" text-anchor="middle">xcodebuild (전역 락)</text>
<rect class="box" x="450" y="360" width="230" height="56" rx="8"/>
<text x="565" y="384" text-anchor="middle">SQLite DB</text>
<text class="sub" x="565" y="402" text-anchor="middle">작업 · 이벤트 · 실행 기록 · 일정</text>
<rect class="box" x="200" y="360" width="180" height="56" rx="8"/>
<text x="290" y="384" text-anchor="middle">대시보드</text>
<text class="sub" x="290" y="402" text-anchor="middle">127.0.0.1:8765 · 읽기 전용</text>
<path class="edge" d="M150,180H200" marker-start="url(#arr)" marker-end="url(#arr)"/>
<path class="edge" d="M380,92H450" marker-start="url(#arr)" marker-end="url(#arr)"/>
<path class="edge" d="M565,114V126" marker-end="url(#arr)"/>
<path class="edge" d="M565,170V182" marker-end="url(#arr)"/>
<path class="edge" d="M565,238V226" marker-end="url(#arr)"/>
<path class="edge" d="M700,48H750" marker-end="url(#arr)"/>
<path class="edge" d="M700,124H750" marker-end="url(#arr)"/>
<path class="edge" d="M700,200H750" marker-end="url(#arr)"/>
<path class="edge" d="M700,276H750" marker-end="url(#arr)"/>
<path class="edge" d="M565,320V360" marker-end="url(#arr)"/>
<path class="edge" d="M380,388H450" marker-end="url(#arr)"/>
<text class="sub" x="415" y="380" text-anchor="middle">읽기</text>
<path class="edge" d="M85,210V388H200" marker-end="url(#arr)"/>
</svg>
"""


def _grouped_table(
    headers: Sequence[str], groups: Sequence[tuple[str, Sequence[Sequence[str]]]]
) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = []
    for name, rows in groups:
        for i, row in enumerate(rows):
            first = f'<td rowspan="{len(rows)}"><strong>{_e(name)}</strong></td>' if i == 0 else ""
            body.append("<tr>" + first + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    rows = "".join(body)
    return f'<table class="grouped"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'


def features_page(
    *,
    org: Org,
    pipelines: Mapping[str, Pipeline],
    schedules: Sequence[ScheduledJob],
    last_runs: Mapping[str, str],
    projects: Sequence[str],
    education_agent: str | None,
    secretary_agent: str | None,
    bot_pid: int | None,
) -> str:
    running = bot_pid is not None
    bot_state = (
        f'<span class="ok">실행 중</span> <span class="muted">PID {bot_pid}</span>'
        if running
        else '<span class="bad">꺼져 있음</span>'
    )
    cards = [
        ("봇", bot_state),
        ("파이프라인", str(len(pipelines))),
        ("일정", str(len(schedules))),
        ("직원", f"{len(org.agents)}명"),
    ]
    body = [
        "<h1>기능</h1>",
        '<p class="muted">지금 봇에 들어 있는 기능이다. 파이프라인·일정·부서는 봇이 읽는 설정에서 '
        "만들고, 일정의 최근 실행은 DB 기록이다.</p>",
        '<div class="cards">'
        + "".join(
            f'<div class="card"><div class="muted">{_e(k)}</div><div class="big">{v}</div></div>'
            for k, v in cards
        )
        + "</div>",
    ]

    departments = [
        (
            "제품",
            _e(", ".join(projects)) or '<span class="muted">등록된 제품 없음</span>',
            _on(bool(projects), "동작 중", "없음"),
        ),
        (
            "교육",
            _e(org.display(education_agent)) + " · 아침 개념 노트, 문제, 주간 보고"
            if education_agent
            else "설정(education) 없음",
            _on(education_agent is not None),
        ),
        (
            "비서",
            _e(org.display(secretary_agent)) + " · 아침판, 저녁 하루 정리, 토큰 집계"
            if secretary_agent
            else "설정(secretary) 없음",
            _on(secretary_agent is not None),
        ),
    ]
    pipeline_rows = [
        (
            _e(p.name),
            _e(" → ".join(_stage_label(s.kind.value) for s in p.stages))
            + f'<div class="muted">시작: {_e(_trigger(p))}</div>',
            _on(True),
        )
        for p in pipelines.values()
    ]
    schedule_rows = []
    for item in schedules:
        last = last_runs.get(item.job)
        schedule_rows.append(
            (
                _e(item.name),
                _e(cron_label(item.cron)),
                f'<span class="ok">최근 {local_time(last)}</span>'
                if last
                else '<span class="muted">아직 실행 기록 없음</span>',
            )
        )
    command_state = _on(running, "받는 중", "봇이 꺼져 있음")
    command_rows = [
        (f"<code>{_e(name)}</code>", _e(desc), command_state) for name, desc in COMMANDS
    ]
    safeguard_rows = [
        (
            _e(name),
            f'{_e(desc)}<div class="muted">{_e(where)}</div>',
            '<span class="ok">적용</span>',
        )
        for name, desc, where in SAFEGUARDS
    ]
    page_rows = [
        (f'<a href="{href}">{_e(name)}</a>', _e(desc), '<span class="ok">열림</span>')
        for href, name, desc in PAGES
    ]
    groups = [
        ("부서", departments),
        ("제품 파이프라인", pipeline_rows),
        ("일정", schedule_rows),
        ("Discord 명령", command_rows),
        ("안전장치", safeguard_rows),
        ("대시보드", page_rows),
    ]
    body.append("<h2>분류별 기능</h2>")
    body.append(_grouped_table(["분류", "기능", "동작", "상태"], [g for g in groups if g[1]]))

    body.append("<h2>구성도</h2>")
    body.append(
        '<p class="muted">봇 하나가 Discord, 에이전트 CLI, GitHub, 볼트 사이를 잇는다. '
        "외부에 쓰는 주체는 봇뿐이고, 대시보드는 DB를 읽기만 한다.</p>"
    )
    body.append(f'<div class="diagram">{_ARCHITECTURE}</div>')

    body.append("<h2>흐름도</h2>")
    body.append("<h3>작업 한 건</h3>")
    body.append('<ol class="flow">' + "".join(_step(t, w, c) for t, w, c in _COMMON_FLOW) + "</ol>")
    body.append(
        '<p class="muted">에이전트가 실패하거나 한도를 넘으면 NEEDS_HUMAN(확인 필요)으로 멈추고, '
        "Zero가 /resume으로 다시 잇거나 /stop으로 끝낸다.</p>"
    )
    for pipeline in pipelines.values():
        body.append(
            f'<h3>{_e(pipeline.name)} <span class="muted">{_e(_trigger(pipeline))}</span></h3>'
        )
        body.append(_pipeline_flow(org, pipeline))
    daily = sorted(
        (
            (cron.hours[0], cron.minutes[0], item)
            for item in schedules
            if len((cron := parse_cron(item.cron)).weekdays) == 7
        ),
        key=lambda entry: (entry[0], entry[1], entry[2].name),
    )
    if daily:
        body.append("<h3>하루 일정</h3>")
        body.append(
            '<ol class="flow">'
            + "".join(_step(f"{h:02d}:{m:02d}", item.name, "bot") for h, m, item in daily)
            + "</ol>"
        )
    return page("기능", "".join(body), active="features")
