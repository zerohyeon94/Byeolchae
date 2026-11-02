"""아침·주간 보고 (ADR-007, 회사 재정의 05-운영 1장, SPEC 6.6).

보고는 봇이 DB에서 만든다. 에이전트를 부르지 않는다. `#대표실`에 게시하고,
아침 보고는 Zero가 할 일(승인 대기)이 있을 때만 멘션한다.
비서 부서가 있으면 아침 보고는 비서가 `#비서`에 짧게 한다 (`morning_brief`, ADR-010).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.clock import from_iso, to_iso
from bot.org import Org
from bot.store import EventKind, HiringStatus, Repository, TaskRecord
from bot.workflow.state import TaskState

ThreadUrl = Callable[[str], str | None]


@dataclass(frozen=True)
class Report:
    text: str
    mention: bool  # Zero가 행동해야 할 것이 있는가


def _hours(since: str | None, now: datetime) -> str:
    if since is None:
        return "-"
    hours = int((now - from_iso(since)).total_seconds() // 3600)
    return f"{hours}시간" if hours < 48 else f"{hours // 24}일"


def _task_line(task: TaskRecord, url: ThreadUrl, extra: str = "") -> str:
    link = url(task.discord_thread)
    title = f"[{task.title}]({link})" if link else task.title
    return f"- [{task.project}] #{task.id} {title} — {task.state}{extra}"


def _runs_line(org: Org, stats: dict[str, dict[str, int]]) -> str:
    if not stats:
        return "없음"
    parts = []
    for agent in sorted(stats, key=lambda a: (a not in org.agents, a)):
        counts = stats[agent]
        total = sum(counts.values())
        failed = total - counts.get("ok", 0) - counts.get("running", 0)
        parts.append(f"{org.display(agent)} {total}회" + (f" (실패 {failed})" if failed else ""))
    return ", ".join(parts)


def _local_midnight(now: datetime) -> datetime:
    return now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


async def pending_decisions(repo: Repository, org: Org, now: datetime, url: ThreadUrl) -> list[str]:
    """Zero가 결정할 것: 승인 대기 작업과 열린 채용 제안. "- " 없이 한 줄씩."""
    waiting = [t for t in await repo.list_tasks() if t.state.waits_for_zero]
    proposals = await repo.list_hiring_proposals(status=HiringStatus.OPEN)
    lines = [_task_line(t, url, f" (대기 {_hours(t.waiting_since, now)})")[2:] for t in waiting]
    lines += [
        f"채용 제안 #{p.id} ({org.display(p.proposed_by)}): /hire로 답한다" for p in proposals
    ]
    return lines


async def morning_brief(
    repo: Repository, org: Org, now: datetime, url: ThreadUrl, *, daily: str = "", paused: str = ""
) -> Report:
    """비서의 짧은 아침 보고. 오늘 결정할 것을 먼저, 나머지는 건수만 (ADR-010)."""
    today = _local_midnight(now)
    yesterday = today - timedelta(days=1)
    decisions = await pending_decisions(repo, org, now, url)
    tasks = await repo.list_tasks()
    running = sum(1 for t in tasks if not t.state.is_terminal and not t.state.waits_for_zero)
    finished = sum(
        1 for t in tasks if t.state.is_terminal and yesterday <= from_iso(t.updated_at) < today
    )
    lines = [f"**아침 보고** ({now.astimezone():%m-%d})", ""]
    lines.append(f"**오늘 결정할 것** ({len(decisions)})")
    lines += [f"- {line}" for line in decisions] or ["- 없음"]
    lines += ["", f"진행 중 {running}건 · 어제 끝난 일 {finished}건"]
    if daily:
        lines.append(f"어제 하루 정리: `{daily}`")
    if paused:
        lines.append(f"**사용량 한도 정지**: {paused}까지")
    return Report("\n".join(lines), mention=bool(decisions))


async def morning_report(
    repo: Repository, org: Org, now: datetime, url: ThreadUrl, *, paused: str = ""
) -> Report:
    today = _local_midnight(now)
    yesterday = today - timedelta(days=1)
    tasks = await repo.list_tasks()
    waiting = [t for t in tasks if t.state.waits_for_zero]
    running = [t for t in tasks if not t.state.is_terminal and not t.state.waits_for_zero]
    finished = [
        t for t in tasks if t.state.is_terminal and yesterday <= from_iso(t.updated_at) < today
    ]
    stats = await repo.agent_run_stats_since(to_iso(yesterday))  # 어제 0시부터 지금까지
    proposals = await repo.list_hiring_proposals(status=HiringStatus.OPEN)

    lines = [f"**아침 보고** ({now.astimezone():%m-%d})", ""]
    lines.append(f"**Zero가 할 일** ({len(waiting) + len(proposals)})")
    lines += [_task_line(t, url, f" (대기 {_hours(t.waiting_since, now)})") for t in waiting]
    lines += [
        f"- 채용 제안 #{p.id} ({org.display(p.proposed_by)}): /hire로 답한다" for p in proposals
    ]
    if not waiting and not proposals:
        lines.append("- 없음")
    lines += ["", f"**진행 중** ({len(running)})"]
    lines += [_task_line(t, url) for t in running] or ["- 없음"]
    lines += ["", f"**어제 끝난 일** ({len(finished)})"]
    lines += [_task_line(t, url) for t in finished] or ["- 없음"]
    lines += ["", f"**에이전트 실행 (어제부터)**: {_runs_line(org, stats)}"]
    if paused:
        lines.append(f"**사용량 한도 정지**: {paused}까지")
    return Report("\n".join(lines), mention=bool(waiting or proposals))


@dataclass(frozen=True)
class ProductWeek:
    project: str
    started: int  # 도달
    merged: int  # 채택 (merge까지 끝난 작업)
    dropped: int  # 자동 제외 (Zero가 중지)
    needs_human: int  # 지금 확인 필요


def _week(tasks: Sequence[TaskRecord], project: str, since: datetime) -> ProductWeek:
    mine = [t for t in tasks if t.project == project]
    recent = [t for t in mine if from_iso(t.updated_at) >= since]
    return ProductWeek(
        project,
        started=sum(1 for t in mine if from_iso(t.created_at) >= since),
        merged=sum(1 for t in recent if t.state is TaskState.DONE),
        dropped=sum(1 for t in recent if t.state is TaskState.STOPPED),
        needs_human=sum(1 for t in mine if t.state is TaskState.NEEDS_HUMAN),
    )


async def weekly_report(
    repo: Repository, org: Org, now: datetime, projects: Sequence[str]
) -> Report:
    since = _local_midnight(now) - timedelta(days=7)
    tasks = await repo.list_tasks()
    weeks = [_week(tasks, project, since) for project in projects]
    failures = await repo.count_events_since(EventKind.ERROR, to_iso(since))
    stats = await repo.agent_run_stats_since(to_iso(since))
    proposals = await repo.list_hiring_proposals(status=HiringStatus.OPEN)

    lines = [f"**주간 보고** ({since:%m-%d} ~ {now.astimezone():%m-%d})", ""]
    lines.append("**제품별** (시작 / merge 완료 / 중지 / 확인 필요)")
    for week in weeks:
        lines.append(
            f"- {week.project}: {week.started} / {week.merged} / {week.dropped} / "
            f"{week.needs_human}"
        )
    total_started = sum(w.started for w in weeks)
    total_merged = sum(w.merged for w in weeks)
    lines += [
        "",
        "**지표** (대시보드 정의, SPEC 9.3-1)",
        f"- 도달 {total_started} / 채택 {total_merged} / "
        f"자동 제외 {sum(w.dropped for w in weeks)} / 시스템 실패 {failures}",
        "",
        f"**에이전트 실행 (7일)**: {_runs_line(org, stats)}",
        "",
        f"**열린 채용 제안** ({len(proposals)})",
    ]
    lines += [
        f"- #{p.id} {org.display(p.proposed_by)}: {p.payload.get('team')} {p.payload.get('role')}"
        for p in proposals
    ] or ["- 없음"]
    return Report("\n".join(lines), mention=bool(proposals))
