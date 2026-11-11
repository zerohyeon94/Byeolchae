"""워크플로 엔진 (SPEC 3.4, 3.5, ADR-007).

입력(채팅 대화, /task, 포스트 안의 명령)을 받아 전이를 계산해 저장하고, 효과를 실행한다.
- 작업은 파이프라인 선언대로 진행한다. 전이는 bot.workflow.state의 순수 함수가 정한다
- 전이와 이벤트 기록은 한 트랜잭션으로 저장한다 (Repository.apply_transition)
- 단계 작업은 작업마다 하나씩 실행한다. 시작 직전에 상태를 다시 확인해,
  기다리는 사이 /stop 등으로 단계가 바뀌었으면 실행하지 않는다
- 에이전트 CLI는 대기열(AgentGate)에서 차례를 받아 한 번에 하나만 실행한다
- 의견은 작업이 아니라 제품 채팅 채널의 대화다. /task가 대화를 포럼 포스트로 옮겨 작업을 연다

단계별 작업(기획, 설계, 구현, 테스트, 리뷰 …)은 `bot.workflow.work.WorkStages`에 있다.
"""

import asyncio
import logging
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from bot.agents.base import (
    AgentName,
    AgentRequest,
    AgentResult,
    AgentRunner,
    EngineName,
    Mode,
    RunStatus,
    Stage,
)
from bot.clock import Clock, SystemClock, from_iso, to_iso
from bot.org import DEFAULT_ORG, Org
from bot.pipeline import LEGACY, Pipeline, StageKind, default_pipelines, legacy_pipeline
from bot.prompts import (
    HistoryEntry,
    Persona,
    debate_prompt,
    opinion_prompt,
    persona_for,
    project_report_prompt,
    with_format_error,
)
from bot.render import render_format_error, render_notice, render_opinion, render_opinion_post
from bot.render.hiring import rejected_line, render_hiring_proposal
from bot.schemas import AgentOutputError, Opinion, parse_agent_output
from bot.schemas.project_report import ProjectReport
from bot.store import (
    INVALID_OUTPUT,
    Actor,
    EventKind,
    HiringStatus,
    NewEvent,
    NotFoundError,
    Repository,
)
from bot.store.repo import PendingReport, ReportAssignment, StaleStateError, TaskRecord
from bot.workflow.chat import Attachment, Author, ChatIO
from bot.workflow.gate import AgentGate, Priority
from bot.workflow.mentions import Moment, should_mention
from bot.workflow.project_reports import report_facts
from bot.workflow.state import (
    RUNNING_STATES,
    ApplyTypeLabel,
    CancelAgentRuns,
    CleanupStopped,
    ClearNeedsHuman,
    Command,
    CommandName,
    CreateIssue,
    Effect,
    Event,
    FinishMerged,
    FinishTask,
    Limits,
    MarkNeedsHuman,
    MarkReady,
    NoticeCode,
    PostNotice,
    PublishPr,
    Roster,
    RunTests,
    StageFailed,
    StartFix,
    StartStage,
    TaskState,
    WriteDocs,
    ZeroMessage,
    start_task,
    transition,
)
from bot.workflow.work import WorkStages

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)
Outcome = tuple[Event, NewEvent]
Verify = Callable[[Any], Awaitable[list[str]]]

# Zero의 말에 바로 답하는 단계. 대기열에서 먼저 꺼낸다 (ADR-007)
_ZERO_FACING = frozenset({Stage.OPINION, Stage.DEBATE})
# 구독 사용량 한도 오류로 보는 문구 [검증 필요] (SPEC V15)
_USAGE_LIMIT = re.compile(
    r"usage limit|rate limit|limit reached|too many requests|quota exceeded|\b429\b", re.I
)
PAUSE_KEY = "queue_paused_until"
# 채팅 답에 넣는 최근 대화 수 (SPEC 5.3)
CHAT_HISTORY = 20
# 일정으로 시작한 작업은 보고할 때까지 포스트가 없다. 그 사이의 스레드 자리 표시
SCHEDULED_THREAD = "sched:"

_STAGE_STATES = {
    StageKind.PLAN: TaskState.PLANNING,
    StageKind.UI_DESIGN: TaskState.UI_DESIGNING,
    StageKind.DESIGN: TaskState.DESIGNING,
    StageKind.DIAGNOSE: TaskState.DIAGNOSING,
    StageKind.IMPLEMENT: TaskState.IMPLEMENTING,
    StageKind.REVIEW: TaskState.REVIEWING,
    StageKind.RELEASE_PREP: TaskState.RELEASE_PREPARING,
    StageKind.DOC_SYNC: TaskState.DOC_DRAFTING,
    StageKind.REPORT: TaskState.REPORTING,
}


def is_usage_limited(result: AgentResult) -> bool:
    """CLI가 구독 사용량 한도로 실패했는지. 성공한 실행은 보지 않는다."""
    if result.status is RunStatus.OK:
        return False
    return bool(_USAGE_LIMIT.search(f"{result.stdout}\n{result.stderr}"))


@dataclass(frozen=True)
class ProjectContext:
    """프로젝트 하나의 실행 정보. github·feature_dir가 없으면 그 기능을 쓰는 단계는 실패한다."""

    repo: Path
    base_branch: str = "develop"
    test_command: tuple[str, ...] = ()
    github: Any = None  # GitHubClient 또는 테스트용 FakeGitHub
    feature_dir: Path | None = None  # 볼트 `기능/` 폴더
    vault_hub: str | None = None  # 볼트 허브 파일 이름 (문서 반영)
    design_system_url: str | None = None  # 제품 Claude 디자인 시스템 링크 (ADR-007)

    owner: str | None = None

    def feature_names(self) -> set[str] | None:
        if self.feature_dir is None or not self.feature_dir.is_dir():
            return None
        # macOS·iCloud 파일 이름은 한글이 분해형(NFD)으로 저장될 수 있다. 조합형(NFC)으로 맞춘다
        return {unicodedata.normalize("NFC", path.stem) for path in self.feature_dir.glob("*.md")}


@dataclass(frozen=True)
class ChatScope:
    """작업 없이 에이전트를 부를 때(채팅 답)의 범위. TaskRecord와 같은 이름의 속성을 쓴다."""

    project: str
    id: None = None


Scope = TaskRecord | ChatScope


@dataclass(frozen=True)
class _Answer(Generic[M]):
    """에이전트 한 번의 응답 결과. result가 없으면 형식 오류이거나 실행 실패다."""

    agent: AgentName
    result: M | None
    raw: str = ""
    error: str = ""
    status: RunStatus = RunStatus.OK
    log_path: str = ""
    session_id: str | None = None
    blocked: str = ""  # 실행하지 않은 이유 (하루 예산·흐름 예산 초과 등)

    @property
    def failed_to_run(self) -> bool:
        return self.status is not RunStatus.OK or bool(self.blocked)

    @property
    def failure(self) -> str:
        return self.blocked or str(self.status)


@dataclass(frozen=True)
class _Outgoing:
    author: Author
    text: str
    attachments: tuple[Attachment, ...]
    kind: str
    payload: Any
    ops_alert: str = ""  # 비어 있지 않으면 #zerosync-ops에도 알린다


@dataclass
class _Running:
    tests: dict[int, str] = field(default_factory=dict)  # 작업 → 실행 중인 테스트 run_id


class Engine(WorkStages):
    def __init__(
        self,
        repo: Repository,
        chat: ChatIO,
        runners: Mapping[EngineName, AgentRunner],
        projects: Mapping[str, ProjectContext],
        *,
        timeout_sec: float,
        org: Org = DEFAULT_ORG,
        pipelines: Mapping[str, Pipeline] | None = None,
        git: Any = None,
        tests: Any = None,
        limits: Limits | None = None,
        worktrees_dir: Path | None = None,
        thread_url: Callable[[str], str | None] | None = None,
        clock: Clock | None = None,
        gate: AgentGate | None = None,
        usage_pause: timedelta = timedelta(hours=1),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        vault: Any = None,
    ):
        self._repo = repo
        self._chat = chat
        self._runners = runners
        self._org = org
        self._roster = Roster(opinions=org.roles.opinions, callable=org.agent_names)
        self._pipelines = dict(pipelines if pipelines is not None else default_pipelines())
        legacy = legacy_pipeline(org)
        if legacy is not None:
            self._pipelines.setdefault(LEGACY, legacy)
        self._projects = projects
        self._timeout = timeout_sec
        self._git = git
        self._tests = tests
        self._limits = limits or Limits()
        self._worktrees = worktrees_dir or Path.home() / ".zerosync" / "worktrees"
        self._thread_url = thread_url or (lambda thread: None)
        self._project_report_lock = asyncio.Lock()
        self._task_locks: dict[int, asyncio.Lock] = {}
        self._running = _Running()
        self._clock = clock or SystemClock()
        self._gate = gate or AgentGate()
        self._usage_pause = usage_pause
        self._sleep = sleep
        self._vault = vault

    @property
    def waiting(self) -> int:
        """대기열에서 차례를 기다리는 에이전트 실행 수."""
        return self._gate.waiting

    @property
    def pipelines(self) -> Mapping[str, Pipeline]:
        return self._pipelines

    def pipeline_for_command(self, command: str) -> Pipeline | None:
        return next((p for p in self._pipelines.values() if p.command == command), None)

    def _pipeline(self, task: TaskRecord) -> Pipeline | None:
        return self._pipelines.get(task.pipeline)

    # 작업 시작

    async def start(
        self,
        *,
        project: str,
        pipeline: str,
        title: str,
        text: str,
        thread: str,
        context: str = "",
    ) -> TaskRecord:
        """작업을 만들고 파이프라인 첫 단계를 시작한다. thread는 이미 연 포럼 포스트."""
        if project not in self._projects:
            raise ValueError(f"알 수 없는 프로젝트: {project}")
        decl = self._pipelines[pipeline]
        result = start_task(decl)
        payload = {"title": title, "text": text, "context": context}
        task = await self._repo.create_task(
            project=project,
            discord_thread=thread,
            title=title,
            state=result.state,
            event=NewEvent(Actor.ZERO, EventKind.DECISION, payload),
            updates=result.updates,
            pipeline=pipeline,
        )
        await self._run_effects(task, result.effects)
        return await self._repo.get_task(task.id)

    async def start_scheduled(self, project: str, pipeline: str) -> TaskRecord:
        """일정으로 흐름을 시작한다. 보고할 일이 생길 때 포스트를 연다 (SPEC 6.6)."""
        decl = self._pipelines[pipeline]
        thread = f"{SCHEDULED_THREAD}{project}:{pipeline}:{uuid.uuid4().hex[:8]}"
        return await self.start(
            project=project, pipeline=pipeline, title=decl.name, text=decl.name, thread=thread
        )

    # 포스트 안의 입력

    async def on_message(self, thread: str, text: str, *, message_id: str | None = None) -> None:
        event = NewEvent(Actor.ZERO, EventKind.MESSAGE, {"text": text}, message_id)
        await self._handle(thread, ZeroMessage(text), event)

    async def on_command(
        self, thread: str, name: CommandName, arg: str = "", *, message_id: str | None = None
    ) -> None:
        payload = {"name": name.value, "arg": arg}
        event = NewEvent(Actor.ZERO, EventKind.COMMAND, payload, message_id)
        await self._handle(thread, Command(name, arg), event)

    async def _handle(self, thread: str, event: Event, record: NewEvent) -> None:
        task = await self._repo.find_task_by_thread(thread)
        if task is None:
            return
        await self._advance(task.id, event, record)

    async def _advance(self, task_id: int, event: Event, record: NewEvent) -> None:
        """현재 상태로 전이를 계산해 저장하고 효과를 실행한다."""
        task = await self._repo.get_task(task_id)
        result = transition(task, event, self._limits, self._pipeline(task))
        report = None
        if result.state != task.state and (
            result.state.is_terminal or result.state is TaskState.NEEDS_HUMAN
        ):
            reason = next((e.reason for e in result.effects if isinstance(e, MarkNeedsHuman)), "")
            report = self._assignment(
                task.project, f"작업 #{task.id} {task.title} · {result.state} {reason}".rstrip()
            )
        try:
            task = await self._repo.apply_transition(
                task.id,
                expected_state=task.state,
                new_state=result.state,
                report=report,
                event=record,
                updates=result.updates,
            )
        except StaleStateError:
            # 그 사이 다른 입력이 상태를 바꿨다. 바뀐 상태 기준으로 다시 판단한다
            await self._advance(task_id, event, record)
            return
        await self._run_effects(task, result.effects)

    async def recover_interrupted(self) -> None:
        """재시작 전에 끝나지 않은 작업을 정리한다 (SPEC 3.4, Q8).

        중간 결과는 신뢰하지 않는다. 남은 에이전트 실행은 error로 닫고,
        진행 중 단계는 NEEDS_HUMAN으로 보낸다 (2026-10-10 Zero 결정).
        """
        by_task: dict[int | None, list[str]] = {}
        for run in await self._repo.list_running_agent_runs():
            await self._repo.finish_agent_run(
                run.id, status=RunStatus.ERROR, exit_code=None, log_path=run.log_path
            )
            by_task.setdefault(run.task_id, []).append(run.agent)

        for task in await self._repo.list_tasks(set(RUNNING_STATES)):
            agents = by_task.get(task.id, [])
            await self._repo.apply_transition(
                task.id,
                expected_state=task.state,
                new_state=TaskState.NEEDS_HUMAN,
                event=NewEvent(Actor.BOT, EventKind.ERROR, {"reason": "restart"}),
                report=self._assignment(
                    task.project, f"작업 #{task.id} {task.title} · 재시작으로 {task.state} 중단"
                ),
                updates={"prev_state": task.state},
            )
            await self._say(task, render_notice(NoticeCode.INTERRUPTED), Moment.NEEDS_HUMAN)
            await self._label_needs_human(task, add=True)
            await self._chat.notify_ops(
                f"[{task.project}] 작업 {task.id}: 봇 재시작으로 중단했다 "
                f"(상태 {task.state}, 중단한 실행 {len(agents)}건)",
                mention=should_mention(Moment.BOT_ERROR),
            )

        # 포스트 없이 NEEDS_HUMAN에 멈춘 일정 작업(이 수정 전 기록)에 포스트를 열어 준다
        for task in await self._repo.list_tasks({TaskState.NEEDS_HUMAN}):
            assignment = self._assignment(
                task.project,
                f"작업 #{task.id} {task.title} · {task.prev_state or ''} 단계에서 확인 대기",
            )
            if assignment:
                await self._repo.queue_existing_blocker(task.id, assignment)
            if not self._postable(task):
                await self._say(
                    task,
                    render_notice(NoticeCode.NEEDS_HUMAN, "포스트 없이 멈춰 있었다"),
                    Moment.NEEDS_HUMAN,
                )

    async def remind_waiting(self, after: timedelta) -> int:
        """Zero 대기가 after를 넘은 작업에 1회만 다시 멘션한다 (운영 규약 2.5). 보낸 수."""
        now = self._clock.now()
        sent = 0
        waiting = {state for state in TaskState if state.waits_for_zero}
        for task in await self._repo.list_tasks(waiting):
            if task.waiting_since is None or task.reminded_at is not None:
                continue
            if now - from_iso(task.waiting_since) < after:
                continue
            hours = int((now - from_iso(task.waiting_since)).total_seconds() // 3600)
            await self._say(
                task,
                render_notice(NoticeCode.REMINDER, f"현재 상태: {task.state} ({hours}시간째)"),
                Moment.REMINDER,
            )
            await self._repo.update_fields(task.id, {"reminded_at": to_iso(now)})
            sent += 1
        return sent

    # 효과

    async def _run_effects(self, task: TaskRecord, effects: Sequence[Effect]) -> None:
        for effect in effects:
            if isinstance(effect, CancelAgentRuns):
                await self._cancel_runs(task.id)
            elif isinstance(effect, PostNotice):
                await self._notice(task, effect.code, effect.detail)
            elif isinstance(effect, CreateIssue):
                await self._side_effect(task.id, lambda t: self._create_issue(t.id))
            elif isinstance(effect, StartStage):
                await self._start_stage(task, effect)
            elif isinstance(effect, RunTests):
                await self._stage(
                    task.id, TaskState.TESTING, lambda e=effect: self._run_tests(task.id, e.stage)
                )
            elif isinstance(effect, PublishPr):
                await self._side_effect(task.id, lambda t: self._publish_pr(t.id))
            elif isinstance(effect, StartFix):
                await self._stage(
                    task.id, TaskState.FIXING, lambda e=effect: self._fix(task.id, e.source)
                )
            elif isinstance(effect, ApplyTypeLabel):
                await self._side_effect(task.id, self._apply_type_label)
            elif isinstance(effect, MarkReady):
                await self._side_effect(task.id, self._mark_ready)
            elif isinstance(effect, MarkNeedsHuman):
                await self._mark_needs_human(task.id, effect.reason)
            elif isinstance(effect, ClearNeedsHuman):
                await self._side_effect(task.id, lambda t: self._label_needs_human(t, add=False))
            elif isinstance(effect, FinishMerged):
                await self._side_effect(task.id, self._finish_merged)
            elif isinstance(effect, WriteDocs):
                await self._side_effect(task.id, self._write_docs)
            elif isinstance(effect, FinishTask):
                await self._side_effect(task.id, self._finish_task)
            elif isinstance(effect, CleanupStopped):
                await self._side_effect(task.id, self._cleanup_stopped)
            else:  # pragma: no cover - 새 효과 타입을 추가하면 여기서 드러난다
                raise TypeError(f"처리하지 않은 효과: {effect!r}")

    async def _start_stage(self, task: TaskRecord, effect: StartStage) -> None:
        pipeline = self._pipeline(task)
        assert pipeline is not None
        stage = pipeline.stages[pipeline.index(effect.stage)]
        handlers = {
            StageKind.PLAN: self._plan,
            StageKind.UI_DESIGN: self._ui_design,
            StageKind.DESIGN: self._design,
            StageKind.DIAGNOSE: self._diagnose,
            StageKind.IMPLEMENT: self._implement,
            StageKind.REVIEW: self._review,
            StageKind.RELEASE_PREP: self._release_prep,
            StageKind.DOC_SYNC: self._doc_sync,
            StageKind.REPORT: self._report,
        }
        work = handlers[stage.kind]
        await self._stage(
            task.id, _STAGE_STATES[stage.kind], lambda: work(task.id, stage, effect.revision)
        )

    async def _serialized(
        self, task_id: int, expected: TaskState, work: Callable[[], Awaitable[Any]]
    ) -> Any:
        """작업마다 하나씩 실행한다. 시작 직전 상태가 expected가 아니면 실행하지 않는다."""
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            current = await self._repo.get_task(task_id)
            if current.state is not expected:
                return None
            return await work()

    async def _stage(
        self, task_id: int, expected: TaskState, work: Callable[[], Awaitable[Outcome | None]]
    ) -> None:
        """단계 작업을 실행하고 결과 이벤트로 다음 상태로 간다. 예외는 단계 실패로 바꾼다."""

        async def guarded() -> Outcome | None:
            try:
                return await work()
            except Exception as error:
                log.exception("작업 %s 단계 실패", task_id)
                reason = f"{type(error).__name__}: {error}"
                task = await self._repo.get_task(task_id)
                await self._chat.notify_ops(
                    f"[{task.project}] 작업 {task.id} {task.state} 중 오류: {reason}",
                    mention=should_mention(Moment.BOT_ERROR),
                )
                return StageFailed(reason), NewEvent(
                    Actor.BOT, EventKind.ERROR, {"state": task.state.value, "reason": reason}
                )

        outcome = await self._serialized(task_id, expected, guarded)
        if outcome is not None:
            await self._advance(task_id, *outcome)

    async def _side_effect(
        self, task_id: int, work: Callable[[TaskRecord], Awaitable[None]]
    ) -> None:
        """GitHub 라벨·정리처럼 실패해도 상태를 바꾸지 않는 작업. 실패는 운영 채널에 알린다."""
        task = await self._repo.get_task(task_id)
        try:
            await work(task)
        except Exception as error:
            log.exception("작업 %s 부수 작업 실패", task_id)
            await self._chat.notify_ops(
                f"[{task.project}] 작업 {task.id} 처리 중 오류: {type(error).__name__}: {error}",
                mention=should_mention(Moment.BOT_ERROR),
            )

    async def _cancel_runs(self, task_id: int) -> None:
        for run in await self._repo.list_running_agent_runs(task_id):
            await self._runner(run.agent).cancel(run.id)
        test_run = self._running.tests.get(task_id)
        if test_run is not None and self._tests is not None:
            await self._tests.cancel(test_run)

    async def _notice(self, task: TaskRecord, code: NoticeCode, detail: str = "") -> None:
        await self._say(task, render_notice(code, detail))

    def _postable(self, task: TaskRecord) -> bool:
        return not task.discord_thread.startswith(SCHEDULED_THREAD)

    async def _open_post(self, task: TaskRecord, title: str, body: str) -> TaskRecord:
        """포스트가 없는 일정 작업에 포럼 포스트를 열고 작업에 붙인다."""
        day = self._clock.now().astimezone().strftime("%m-%d")
        thread = await self._chat.create_post(task.project, f"{title} ({day})", body)
        return await self._repo.update_fields(task.id, {"discord_thread": thread})

    async def _say(self, task: TaskRecord, text: str, moment: Moment = Moment.NOTICE) -> str:
        """작업 포스트에 [별채]로 게시한다.

        포스트가 없는 일정 작업은 Zero의 행동이 필요할 때 포스트를 연다.
        그래야 그 포스트에서 /resume, /stop을 쓸 수 있다. 그 밖의 안내는 게시하지 않는다.
        """
        if not self._postable(task):
            if not should_mention(moment):
                return ""
            task = await self._open_post(
                task,
                f"{task.title} 확인 필요",
                f"일정으로 실행한 {task.title}이 Zero의 확인을 기다린다. "
                "이 포스트에서 /resume 또는 /stop을 쓴다.",
            )
        return await self._chat.post(
            task.discord_thread, "bot", text, mention=should_mention(moment)
        )

    async def _post(
        self,
        task: TaskRecord,
        author: Author,
        text: str,
        moment: Moment,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if not self._postable(task):
            return ""
        return await self._chat.post(
            task.discord_thread,
            author,
            text,
            mention=should_mention(moment),
            attachments=attachments,
        )

    # 프로젝트 담당자 보고

    def _assignment(self, project: str, subject: str) -> ReportAssignment | None:
        context = self._projects.get(project)
        if context is None or context.owner is None:
            return None
        lead = self._org.teams[self._org.agents[context.owner].team].lead
        return ReportAssignment(context.owner, lead, subject)

    async def process_project_reports(self) -> None:
        """접수된 보고를 처리한다. 실행 중 재시작하면 같은 사건부터 다시 시도한다."""
        async with self._project_report_lock:
            for job in await self._repo.pending_project_reports():
                try:
                    await self._deliver_project_report(job)
                except Exception:
                    log.exception("프로젝트 보고 #%s 처리 실패", job.id)
                    await self._repo.defer_project_report(
                        job.id, to_iso(self._clock.now() + timedelta(hours=1))
                    )

    async def _deliver_project_report(self, job: PendingReport) -> None:
        evidence = report_facts(await self._repo.project_report_evidence(job))
        answer = await self.ask_outside_task(
            job.project,
            job.owner,
            Stage.PROJECT_REPORT,
            lambda persona: project_report_prompt(
                persona,
                project=job.project,
                lead=self._name(job.lead),
                subject=job.subject,
                facts=evidence,
            ),
            model=ProjectReport,
            cwd=self._projects[job.project].repo,
        )
        if answer.result is None:
            raise RuntimeError(answer.error or answer.failure)
        if not await self._repo.deliver_project_report(
            job.id, answer.result.model_dump(mode="json")
        ):
            return
        # 원본은 팀장 수신함에 있다. 조용한 정기 테스트는 사본을 위해 포스트를 열지 않는다.
        text = f"{self._name(job.lead)}에게 보고 · {job.subject}\n{answer.result.text()}"
        if job.task_id is not None:
            await self._post(await self._repo.get_task(job.task_id), job.owner, text, Moment.NOTICE)
        else:
            await self._chat.chat(job.project, job.owner, text)

    # 에이전트 호출

    def _runner(self, agent: AgentName) -> AgentRunner:
        return self._runners[self._org.engine(agent)]

    def _name(self, agent: str) -> str:
        return self._org.display(agent)

    async def _persona(self, agent: AgentName) -> Persona:
        """인격과 이 팀장이 거절당한 채용 제안 (같은 제안 반복 방지, 운영 규약 1.12)."""
        rejected = await self._repo.list_hiring_proposals(
            proposed_by=agent, status=HiringStatus.REJECTED
        )
        lines = [rejected_line(r.payload, r.decided_note) for r in rejected]
        return persona_for(self._org, agent, lines)

    async def ask_outside_task(
        self,
        scope_name: str,
        agent: AgentName,
        stage: Stage,
        build_prompt: Callable[[Persona], str],
        *,
        model: type[M],
        cwd: Path,
        verify: Verify | None = None,
    ) -> _Answer[M]:
        """작업(포럼 포스트) 없이 에이전트를 부른다. 교육 Daily처럼 제품 밖의 일에 쓴다 (ADR-009).

        대기열·하루 한도·사용량 정지·실행 기록은 제품 일과 같다.
        실행 기록의 project는 scope_name이다.
        """
        prompt = build_prompt(await self._persona(agent))
        return await self._ask(
            ChatScope(scope_name), agent, stage, prompt, model=model, cwd=cwd, verify=verify
        )

    async def _ask(
        self,
        scope: Scope,
        agent: AgentName,
        stage: Stage,
        prompt: str,
        *,
        model: type[M] = Opinion,  # type: ignore[assignment]
        mode: Mode = "read_only",
        cwd: Path | None = None,
        context: dict[str, Any] | None = None,
        resume: str | None = None,
        verify: Verify | None = None,
    ) -> _Answer[M]:
        """에이전트를 부르고 스키마로 검증한다.

        형식 오류이거나 verify가 문제를 찾으면 그 내용을 붙여 1회 재요청한다 (운영 규약 2.1).
        """
        inbox = []
        project = self._projects.get(scope.project)
        if project and project.owner and agent == self._org.teams["planning"].lead:
            inbox = await self._repo.project_report_inbox(scope.project, agent)
            if inbox:
                lines = [
                    f"[{self._name(row['owner'])}] {row['subject']}\n"
                    f"{ProjectReport.model_validate(row['body']).text()}"
                    for row in inbox
                ]
                prompt += "\n\n## 프로젝트 담당자에게 받은 보고 (참고 기록)\n\n" + "\n\n".join(
                    lines
                )
        call = dict(model=model, mode=mode, cwd=cwd, context=context, verify=verify)
        answer = await self._attempt(scope, agent, stage, prompt, resume=resume, **call)
        if answer.result is None and not answer.failed_to_run:
            retry = with_format_error(prompt, answer.error)
            answer = await self._attempt(
                scope, agent, stage, retry, resume=answer.session_id or resume, **call
            )
        if inbox and answer.result is not None:
            await self._repo.mark_project_reports_read([row["id"] for row in inbox])
        proposal = getattr(answer.result, "hiring_proposal", None)
        if proposal is not None:
            await self._record_hiring(scope, agent, proposal)
        return answer

    # 채용 제안 (운영 규약 1.12, SPEC Q19)

    async def _record_hiring(self, scope: Scope, agent: AgentName, proposal: Any) -> None:
        """제안을 저장하고 숫자 근거를 붙여 Zero에게 올린다. 실패해도 단계는 계속한다."""
        try:
            evidence = {
                "runs": await self._repo.count_agent_runs(agent),
                "errors": await self._repo.count_errors(),
            }
            record = await self._repo.add_hiring_proposal(
                task_id=scope.id,
                proposed_by=agent,
                payload=proposal.model_dump(mode="json"),
                evidence=evidence,
            )
            text = render_hiring_proposal(record.id, self._name(agent), proposal, evidence)
            await self._chat.report(text, mention=should_mention(Moment.HIRING_PROPOSAL))
        except Exception:
            log.exception("채용 제안 기록 실패 (%s, %s)", scope.project, agent)

    async def decide_hiring(self, proposal_id: int, *, accept: bool, note: str = "") -> str:
        """Zero의 /hire 답. 수락은 기록만 하고 org.yaml 변경은 Zero가 한다."""
        try:
            record = await self._repo.decide_hiring_proposal(
                proposal_id, accept=accept, note=note.strip()
            )
        except NotFoundError:
            return f"채용 제안 #{proposal_id}이 없다."
        except StaleStateError as error:
            return str(error)
        name = self._name(record.proposed_by)
        if accept:
            return (
                f"채용 제안 #{proposal_id}({name})을 수락으로 기록했다. "
                "org.yaml과 인격 파일을 추가하고 봇을 다시 시작하면 반영된다."
            )
        return (
            f"채용 제안 #{proposal_id}({name})을 거절로 기록했다. "
            "다음부터 이 팀장의 프롬프트에 거절 이유가 들어간다."
        )

    async def _attempt(
        self,
        scope: Scope,
        agent: AgentName,
        stage: Stage,
        prompt: str,
        **call: Any,
    ) -> _Answer[Any]:
        """대기열에서 차례를 받아 한 번 실행한다 (ADR-007: 한 번에 하나, 예산, 한도 정지)."""
        priority: Priority = (
            0 if stage in _ZERO_FACING else 1,
            self._project_rank(scope.project),
            scope.id or 0,
        )
        async with self._gate.slot(priority):
            if scope.id is not None:
                current = await self._repo.get_task(scope.id)
                if current.state.is_terminal:
                    # 기다리는 사이 /stop됐다. 호출하지 않는다
                    return _Answer(agent, None, status=RunStatus.CANCELLED)
            await self._wait_if_paused()
            blocked = await self._over_budget(scope, agent)
            if blocked:
                where = f"작업 {scope.id}: " if scope.id is not None else ""
                await self._chat.notify_ops(
                    f"[{scope.project}] {where}{blocked}",
                    mention=should_mention(Moment.NEEDS_HUMAN),
                )
                return _Answer(agent, None, status=RunStatus.ERROR, blocked=blocked)
            answer, limited = await self._run_once(scope, agent, stage, prompt, **call)
            if limited:
                # 한도가 풀리면 같은 요청을 한 번 다시 실행한다
                await self._pause_for_usage(scope, agent)
                await self._wait_if_paused()
                answer, _ = await self._run_once(scope, agent, stage, prompt, **call)
            return answer

    def _project_rank(self, project: str) -> int:
        # 제품 순서 = 설정의 프로젝트 순서 (가게씨 → 제이데이 → 오롯, ADR-007)
        keys = list(self._projects)
        return keys.index(project) if project in keys else len(keys)

    def _day_start(self) -> str:
        """이 맥 시간대의 오늘 0시 (UTC ISO). 하루 예산의 기준이다."""
        local = self._clock.now().astimezone()
        return to_iso(local.replace(hour=0, minute=0, second=0, microsecond=0))

    async def _over_budget(self, scope: Scope, agent: AgentName) -> str:
        spec = self._org.agents.get(agent)
        limit = spec.daily_calls if spec is not None else None
        if limit is not None:
            used = await self._repo.count_agent_runs_since(agent, self._day_start())
            if used >= limit:
                return f"{self._name(agent)}의 오늘 호출 한도 {limit}회를 넘었다"
        if scope.id is not None:
            task = await self._repo.get_task(scope.id)
            pipeline = self._pipeline(task)
            if pipeline is not None:
                calls, seconds = await self._repo.task_usage(task.id)
                budget = pipeline.budget
                if calls >= budget.max_calls:
                    return f"{pipeline.name} 1회 호출 한도 {budget.max_calls}회를 넘었다"
                if seconds >= budget.max_minutes * 60:
                    return f"{pipeline.name} 1회 실행 시간 한도 {budget.max_minutes}분을 넘었다"
        return ""

    async def _paused_until(self) -> datetime | None:
        value = await self._repo.get_state(PAUSE_KEY)
        return from_iso(value) if value else None

    async def _wait_if_paused(self) -> None:
        while True:
            until = await self._paused_until()
            if until is None:
                return
            remaining = (until - self._clock.now()).total_seconds()
            if remaining <= 0:
                await self._repo.set_state(PAUSE_KEY, None)
                await self._chat.notify_ops(
                    "사용량 한도 정지가 끝나 대기열을 다시 시작한다.",
                    mention=should_mention(Moment.NOTICE),
                )
                return
            await self._sleep(min(remaining, 60.0))

    async def _pause_for_usage(self, scope: Scope, agent: AgentName) -> None:
        until = self._clock.now() + self._usage_pause
        await self._repo.set_state(PAUSE_KEY, to_iso(until))
        local = until.astimezone().strftime("%m-%d %H:%M")
        await self._chat.notify_ops(
            f"[{scope.project}] {self._name(agent)} 실행이 구독 사용량 한도에 걸렸다. "
            f"대기열을 {local}까지 멈추고 그 뒤 다시 실행한다.",
            mention=should_mention(Moment.BOT_ERROR),
        )

    async def _run_once(
        self,
        scope: Scope,
        agent: AgentName,
        stage: Stage,
        prompt: str,
        *,
        model: type[M],
        mode: Mode,
        cwd: Path | None,
        context: dict[str, Any] | None,
        resume: str | None,
        verify: Verify | None,
    ) -> tuple[_Answer[M], bool]:
        """CLI를 한 번 실행하고 검증한다. 두 번째 값은 사용량 한도로 실패했는지."""
        prefix = f"t{scope.id}" if scope.id is not None else f"c-{scope.project}"
        run_id = f"{prefix}-{agent}-{stage.value}-{uuid.uuid4().hex[:8]}"
        engine = self._org.engine(agent)
        await self._repo.start_agent_run(
            run_id,
            task_id=scope.id,
            agent=agent,
            stage=stage,
            engine=engine,
            project=scope.project,
        )
        request = AgentRequest(
            run_id=run_id,
            task_id=scope.id or 0,
            stage=stage,
            prompt=prompt,
            cwd=cwd or self._projects[scope.project].repo,
            mode=mode,
            timeout_sec=self._timeout,
            resume_session=resume if engine == "claude" else None,
            output_schema=model.model_json_schema(),
            model=self._org.model(agent, stage),
        )
        try:
            result = await self._runner(agent).run(request)
        except BaseException:
            await self._repo.finish_agent_run(
                run_id, status=RunStatus.ERROR, exit_code=None, log_path=None
            )
            raise

        log_path = str(result.log_path)
        status: RunStatus | str = result.status
        parsed, error = None, ""
        if result.status is RunStatus.OK:
            try:
                parsed = parse_agent_output(result.stdout, model, context)
                problems = await verify(parsed) if verify is not None else []
                if problems:
                    parsed = None
                    error = "출력은 형식에 맞지만 작업 결과에 문제가 있다:\n" + "\n".join(
                        f"- {problem}" for problem in problems
                    )
                    status = INVALID_OUTPUT
            except AgentOutputError as e:
                status, error = INVALID_OUTPUT, str(e)
        await self._repo.finish_agent_run(
            run_id, status=status, exit_code=result.exit_code, log_path=log_path
        )
        answer = _Answer(
            agent, parsed, result.stdout, error, result.status, log_path, result.session_id
        )
        return answer, is_usage_limited(result)

    # 채팅 (ADR-007: 의견은 제품 채팅 채널의 대화)

    def _parse_target(self, text: str) -> tuple[tuple[AgentName, ...], str]:
        """`@이름`으로 시작하면 그 팀장만, 아니면 의견 담당(roles.opinions)이 답한다."""
        stripped = text.lstrip()
        for agent in self._roster.callable:
            prefix = f"@{agent}"
            if stripped.lower().startswith(prefix):
                rest = stripped[len(prefix) :]
                # "@betamax"처럼 이름이 이어지면 지정으로 보지 않는다
                if not rest or rest[0].isspace():
                    return (agent,), rest.strip()
        return self._roster.opinions, text.strip()

    async def _chat_history(self, project: str) -> list[HistoryEntry]:
        entries = []
        for turn in await self._repo.recent_chat_turns(project, CHAT_HISTORY):
            payload = turn["payload"]
            if turn["kind"] in ("opinion", "debate"):
                opinion = Opinion.model_validate(payload["opinion"])
                heading = "반박" if turn["kind"] == "debate" else ""
                text = render_opinion(opinion, heading=heading)
            else:
                text = str(payload.get("text", ""))
            entries.append(HistoryEntry(self._name(turn["actor"]), text))
        return entries

    async def on_chat(self, project: str, text: str, *, message_id: str | None = None) -> None:
        """제품 채팅 채널의 Zero 메시지에 팀장이 답한다. 상태 머신은 없다."""
        if project not in self._projects:
            raise ValueError(f"알 수 없는 프로젝트: {project}")
        history = await self._chat_history(project)
        await self._repo.add_chat_turn(project, Actor.ZERO, "message", {"text": text}, message_id)
        agents, question = self._parse_target(text)
        scope = ChatScope(project)
        # 함께 답할 때는 서로의 답을 보지 않고 독립 작성한다 (운영 규약 1.6)
        prompts = {
            agent: opinion_prompt(
                await self._persona(agent),
                title=f"{project} 채팅",
                post=question or text,
                question=question or text,
                history=history,
            )
            for agent in agents
        }
        answers = await asyncio.gather(
            *(self._ask(scope, agent, Stage.OPINION, prompts[agent]) for agent in agents)
        )
        await self._publish_chat(project, answers, "opinion", heading="", question=question or text)

    async def debate(self, project: str) -> str:
        """직전에 답한 두 팀장이 상대 답에 반박 1회씩. 안 되면 이유를 돌려준다."""
        latest: dict[str, Opinion] = {}
        for turn in await self._repo.recent_chat_turns(project, CHAT_HISTORY):
            if turn["kind"] in ("opinion", "debate") and self._org.has(turn["actor"]):
                latest[turn["actor"]] = Opinion.model_validate(turn["payload"]["opinion"])
        pair = [agent for agent in self._roster.opinions if agent in latest][:2]
        if len(pair) < 2:
            return "/debate는 두 팀장의 답이 모두 있어야 한다. 먼저 질문한다."
        first, second = pair
        history = await self._chat_history(project)
        await self._repo.add_chat_turn(project, Actor.ZERO, "command", {"text": "/debate"})
        scope = ChatScope(project)
        prompts = {
            agent: debate_prompt(
                await self._persona(agent),
                title=f"{project} 채팅",
                post=history[0].text if history else "",
                own=render_opinion(latest[agent]),
                other_name=self._name(other),
                other=render_opinion(latest[other]),
                history=history,
            )
            for agent, other in ((first, second), (second, first))
        }
        answers = await asyncio.gather(
            *(self._ask(scope, agent, Stage.DEBATE, prompts[agent]) for agent in pair)
        )
        await self._publish_chat(project, answers, "debate", heading="반박")
        return ""

    async def _publish_chat(
        self,
        project: str,
        answers: Sequence[_Answer[Opinion]],
        kind: str,
        *,
        heading: str,
        question: str = "",
    ) -> None:
        """답을 의견 담당 순서로 게시하고 기록한다. 마지막 게시에서 멘션한다."""
        order = list(self._roster.opinions) + list(self._org.agent_names)
        ordered = sorted(answers, key=lambda answer: order.index(answer.agent))
        mention = should_mention(Moment.OPINIONS_READY)
        for index, answer in enumerate(ordered):
            item = self._outgoing(project, answer, kind, heading)
            message_id = await self._chat.chat(
                project,
                item.author,
                item.text,
                mention=mention and index == len(ordered) - 1,
                attachments=item.attachments,
            )
            actor = item.author if item.author != "bot" else Actor.BOT
            report = None
            if answer.agent == self._projects[project].owner:
                report = self._assignment(project, f"담당자 채팅 · {question[:200]}")
            await self._repo.add_chat_turn(
                project, actor, item.kind, item.payload, message_id, report=report
            )
            if item.ops_alert:
                await self._chat.notify_ops(
                    item.ops_alert, mention=should_mention(Moment.BOT_ERROR)
                )

    def _outgoing(
        self, project: str, answer: _Answer[Opinion], kind: str, heading: str
    ) -> _Outgoing:
        if answer.result is not None:
            rendered = render_opinion_post(answer.result, heading=heading)
            payload = {"opinion": answer.result.model_dump(mode="json")}
            return _Outgoing(answer.agent, rendered.text, rendered.attachments, kind, payload)
        if not answer.failed_to_run:
            rendered = render_format_error(answer.raw, answer.error)
            payload = {"reason": INVALID_OUTPUT, "error": answer.error, "raw": answer.raw}
            return _Outgoing(answer.agent, rendered.text, rendered.attachments, "error", payload)
        # CLI 실행 실패: [별채] 안내 + 운영 채널 알림 (운영 규약 2.5)
        name = self._name(answer.agent)
        alert = (
            f"[{project}] {name} {kind} 실행 실패: {answer.failure}. 로그: {answer.log_path}"
            if answer.status is not RunStatus.CANCELLED and not answer.blocked
            else ""
        )
        return _Outgoing(
            "bot",
            render_notice(NoticeCode.AGENT_FAILED, f"[{name}] {answer.failure}"),
            (),
            "error",
            {"agent": answer.agent, "status": answer.failure},
            alert,
        )

    async def task_from_chat(self, project: str, command: str, title: str) -> TaskRecord | None:
        """/task, /release: 포럼에 포스트를 열고 채팅의 의견 요약을 옮겨 흐름을 시작한다."""
        pipeline = self.pipeline_for_command(command)
        if pipeline is None:
            return None
        history = await self._chat_history(project)
        context = "\n\n".join(f"[{entry.speaker}]\n{entry.text}" for entry in history)
        body = f"{pipeline.name}: {title}"
        if context:
            body += f"\n\n채팅 의견 요약\n\n{context}"
        if len(body) > 1800:
            body = body[:1800] + "\n... (전문은 Issue에)"
        thread = await self._chat.create_post(project, title, body)
        task = await self.start(
            project=project,
            pipeline=pipeline.key,
            title=title,
            text=title,
            thread=thread,
            context=context,
        )
        if history:
            await self._repo.attach_chat_turns(project, task.id)
        return task

    async def add_idea(self, project: str, text: str) -> int:
        idea = await self._repo.add_idea(project, text.strip())
        await self._repo.add_chat_turn(project, Actor.ZERO, "command", {"text": f"/idea {text}"})
        return idea
