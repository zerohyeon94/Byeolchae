"""작업 상태와 전이 규칙 (SPEC 3, 3.5).

전이는 I/O 없는 순수 함수다: (현재 작업, 이벤트, 파이프라인) → (다음 상태, 실행할 효과, 바꿀 필드).
효과는 무엇을 해야 하는지만 나타내고, 실제 실행(에이전트 호출, GitHub, 볼트, 게시)은 엔진이 한다.

작업은 파이프라인 선언(bot.pipeline)의 단계를 차례로 지난다. 단계 종류마다 상태가 하나씩 있고
(예: plan → PLANNING), 승인 게이트는 바로 앞 단계 종류로 대기 상태가 정해진다
(예: plan 뒤의 gate → AWAIT_PLAN_APPROVAL). 그래서 (파이프라인, 상태)만으로 지금 단계를 안다.
같은 종류의 단계는 파이프라인에 한 번만 둔다 (gate 제외, bot.pipeline 규칙).

의견은 상태 머신 밖의 채팅 대화다 (ADR-007). OPINIONS는 ADR-005 기준 기록을 읽기 위해 남긴다.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from bot.agents.base import AgentName
from bot.pipeline import Pipeline, StageDecl, StageKind


@dataclass(frozen=True)
class Roster:
    """의견을 낼 에이전트 (회사 설정 org.yaml의 roles, 에이전트 목록).

    opinions: 아무도 부르지 않았을 때 답하는 팀장. callable: `@이름`으로 부를 수 있는 에이전트.
    """

    opinions: tuple[AgentName, ...] = ("beta", "alpha")
    callable: tuple[AgentName, ...] = ("beta", "gamma", "alpha", "delta")


DEFAULT_ROSTER = Roster()


class TaskState(StrEnum):
    OPINIONS = "OPINIONS"  # ADR-005 기준 기록 (포럼 의견 단계)
    PLANNING = "PLANNING"
    AWAIT_PLAN_APPROVAL = "AWAIT_PLAN_APPROVAL"
    UI_DESIGNING = "UI_DESIGNING"
    AWAIT_UI_APPROVAL = "AWAIT_UI_APPROVAL"
    DESIGNING = "DESIGNING"
    AWAIT_DESIGN_APPROVAL = "AWAIT_DESIGN_APPROVAL"
    DIAGNOSING = "DIAGNOSING"
    AWAIT_FIX_APPROVAL = "AWAIT_FIX_APPROVAL"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    FIXING = "FIXING"
    AWAIT_MERGE = "AWAIT_MERGE"
    DOC_DRAFTING = "DOC_DRAFTING"
    AWAIT_DOC_APPROVAL = "AWAIT_DOC_APPROVAL"
    RELEASE_PREPARING = "RELEASE_PREPARING"
    AWAIT_RELEASE_APPROVAL = "AWAIT_RELEASE_APPROVAL"
    REPORTING = "REPORTING"
    DONE = "DONE"
    STOPPED = "STOPPED"
    NEEDS_HUMAN = "NEEDS_HUMAN"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def waits_for_zero(self) -> bool:
        """Zero 대기 상태. 멘션·재멘션 대상이다 (운영 규약 2.5)."""
        return self in _WAITING

    @property
    def is_gate(self) -> bool:
        return self in _GATES


_TERMINAL = frozenset({TaskState.DONE, TaskState.STOPPED})

# 단계 종류 → 일하는 상태
KIND_STATES: Mapping[StageKind, TaskState] = {
    StageKind.PLAN: TaskState.PLANNING,
    StageKind.UI_DESIGN: TaskState.UI_DESIGNING,
    StageKind.DESIGN: TaskState.DESIGNING,
    StageKind.DIAGNOSE: TaskState.DIAGNOSING,
    StageKind.IMPLEMENT: TaskState.IMPLEMENTING,
    StageKind.TEST: TaskState.TESTING,
    StageKind.REVIEW: TaskState.REVIEWING,
    StageKind.MERGE_GATE: TaskState.AWAIT_MERGE,
    StageKind.DOC_SYNC: TaskState.DOC_DRAFTING,
    StageKind.RELEASE_PREP: TaskState.RELEASE_PREPARING,
    StageKind.REPORT: TaskState.REPORTING,
}
# 승인 게이트: 바로 앞 단계 종류 → 대기 상태
GATE_STATES: Mapping[StageKind, TaskState] = {
    StageKind.PLAN: TaskState.AWAIT_PLAN_APPROVAL,
    StageKind.UI_DESIGN: TaskState.AWAIT_UI_APPROVAL,
    StageKind.DESIGN: TaskState.AWAIT_DESIGN_APPROVAL,
    StageKind.DIAGNOSE: TaskState.AWAIT_FIX_APPROVAL,
    StageKind.RELEASE_PREP: TaskState.AWAIT_RELEASE_APPROVAL,
}
_GATES = frozenset(GATE_STATES.values())
_WAITING = frozenset(
    {
        TaskState.OPINIONS,
        *_GATES,
        TaskState.AWAIT_MERGE,
        TaskState.AWAIT_DOC_APPROVAL,
        TaskState.NEEDS_HUMAN,
    }
)
# 에이전트나 봇이 일하는 상태 (재시작 시 NEEDS_HUMAN, SPEC Q8)
RUNNING_STATES = frozenset(
    {
        TaskState.PLANNING,
        TaskState.UI_DESIGNING,
        TaskState.DESIGNING,
        TaskState.DIAGNOSING,
        TaskState.IMPLEMENTING,
        TaskState.TESTING,
        TaskState.REVIEWING,
        TaskState.FIXING,
        TaskState.DOC_DRAFTING,
        TaskState.RELEASE_PREPARING,
        TaskState.REPORTING,
    }
)


class CommandName(StrEnum):
    """상태를 바꿀 수 있는 명령. /status, /ping, /task 등은 전이 대상이 아니다."""

    APPROVE = "approve"
    REVISE = "revise"
    SKIP = "skip"
    FIX = "fix"
    STOP = "stop"
    RESUME = "resume"


# 상태별 허용 명령 (SPEC 4장). /stop은 종료 상태 외 전부
ALLOWED_COMMANDS: Mapping[TaskState, frozenset[CommandName]] = {
    state: frozenset({CommandName.STOP}) if not state.is_terminal else frozenset()
    for state in TaskState
} | {
    **{
        gate: frozenset({CommandName.APPROVE, CommandName.REVISE, CommandName.STOP})
        for gate in _GATES
    },
    TaskState.AWAIT_MERGE: frozenset({CommandName.FIX, CommandName.STOP}),
    TaskState.AWAIT_DOC_APPROVAL: frozenset(
        {CommandName.APPROVE, CommandName.REVISE, CommandName.SKIP, CommandName.STOP}
    ),
    TaskState.NEEDS_HUMAN: frozenset({CommandName.RESUME, CommandName.STOP}),
}


# 이벤트


@dataclass(frozen=True)
class ZeroMessage:
    """포스트 안의 Zero 일반 메시지. 진행 중인 흐름에서는 기록만 한다."""

    text: str


@dataclass(frozen=True)
class Command:
    name: CommandName
    arg: str = ""


# 엔진이 넣는 내부 이벤트


@dataclass(frozen=True)
class StageSucceeded:
    """단계 작업이 검증까지 끝났다. flags는 다음 단계의 when에 쓰는 사실이다."""

    flags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class StageFailed:
    """단계 작업이 끝내 실패했다 (형식 오류 반복, CLI 실패, 커밋 규칙 위반, 예산 초과 등)."""

    reason: str


@dataclass(frozen=True)
class TestsFinished:
    __test__ = False  # pytest가 테스트 클래스로 모으지 않게

    passed: bool
    environment_error: bool = False  # 테스트를 실행하지 못한 환경 문제 (시뮬레이터 등)
    detail: str = ""


@dataclass(frozen=True)
class ReviewFinished:
    approved: bool


@dataclass(frozen=True)
class PrMerged:
    """Zero가 GitHub에서 PR을 merge했다 (봇이 상태 조회로 감지)."""


Event = (
    ZeroMessage | Command | StageSucceeded | StageFailed | TestsFinished | ReviewFinished | PrMerged
)


@dataclass(frozen=True)
class Limits:
    """한도 (SPEC 3.3). NEEDS_HUMAN에서 /resume하면 초과한 한도만 1회 늘어난다 (Q4)."""

    max_review_rounds: int = 3
    max_test_retries: int = 2
    max_check_rounds: int = 2  # 팀장 확인 (ADR-008). 넘으면 단계 실패


# 실패를 보고만 하는 흐름(정기 테스트)에서 진단 전에 테스트를 다시 돌리는 횟수 (2026-11-05)
REPORT_RETRIES = 1


class FixSource(StrEnum):
    TESTS = "tests"  # 테스트 실패
    REVIEW = "review"  # 리뷰 담당의 [필수] 지적
    RECOMMENDED = "recommended"  # merge 전 /fix 권장 반영
    LEAD_CHECK = "lead_check"  # 팀장 확인의 [필수] 지적 (ADR-008)
    RESUME = "resume"  # NEEDS_HUMAN에서 재개: 직전 수정 맥락을 다시 쓴다


# 효과


@dataclass(frozen=True)
class CreateIssue:
    """작업 시작 원문으로 GitHub Issue를 만든다 (운영 규약 1.8)."""


@dataclass(frozen=True)
class StartStage:
    """파이프라인 단계 하나를 실행한다. revision이 있으면 /revise 요청으로 같은 산출물을 고친다."""

    stage: str
    revision: str = ""


@dataclass(frozen=True)
class RunTests:
    """봇이 test 명령을 실행한다 (운영 규약 1.7)."""

    stage: str


@dataclass(frozen=True)
class PublishPr:
    """draft PR을 만들거나(첫 회) 본문을 갱신한다 (운영 규약 1.9)."""


@dataclass(frozen=True)
class StartFix:
    source: FixSource


@dataclass(frozen=True)
class ApplyTypeLabel:
    """변경 유형 승인 시 Issue에 type:* 라벨 (운영 규약 4.8)."""


@dataclass(frozen=True)
class MarkReady:
    """APPROVED: PR draft 해제, 남은 [권장] 개수 표시, 멘션."""


@dataclass(frozen=True)
class MarkNeedsHuman:
    """needs-human 라벨, 멘션 (운영 규약 1.8, 2.5)."""

    reason: str


@dataclass(frozen=True)
class ClearNeedsHuman:
    """재개 시 needs-human 라벨을 뗀다."""


@dataclass(frozen=True)
class FinishMerged:
    """merge 감지: Issue 코멘트·종료 (운영 규약 4.6)."""


@dataclass(frozen=True)
class WriteDocs:
    """문서 반영안 승인: 볼트에 쓰고 커밋한다 (운영 규약 3.9)."""


@dataclass(frozen=True)
class FinishTask:
    """파이프라인 끝: worktree 정리, 열린 Issue 닫기, 완료 안내."""


@dataclass(frozen=True)
class CleanupStopped:
    """/stop: Issue를 not planned로 닫고, PR이 없으면 원격 작업 브랜치를 지운다 (1.8, 3.3)."""


@dataclass(frozen=True)
class CancelAgentRuns:
    """이 작업에서 실행 중인 CLI를 모두 종료한다."""


class NoticeCode(StrEnum):
    STOPPED = "stopped"
    REJECTED = "rejected"
    # 아래는 전이가 아니라 엔진이 효과를 실행하면서 남기는 안내다
    DECISION_RECORDED = "decision_recorded"
    AGENT_FAILED = "agent_failed"
    NOT_IMPLEMENTED = "not_implemented"
    INTERRUPTED = "interrupted"
    NEEDS_HUMAN = "needs_human"
    REMINDER = "reminder"


@dataclass(frozen=True)
class PostNotice:
    """[ZeroSync] 이름으로 포스트에 짧은 안내를 남긴다."""

    code: NoticeCode
    detail: str = ""


Effect = (
    CreateIssue
    | StartStage
    | RunTests
    | PublishPr
    | StartFix
    | ApplyTypeLabel
    | MarkReady
    | MarkNeedsHuman
    | ClearNeedsHuman
    | FinishMerged
    | WriteDocs
    | FinishTask
    | CleanupStopped
    | CancelAgentRuns
    | PostNotice
)


@dataclass(frozen=True)
class Transition:
    state: TaskState
    effects: tuple[Effect, ...] = ()
    # 함께 저장할 tasks 필드
    updates: Mapping[str, object] = field(default_factory=dict)


class TaskView(Protocol):
    @property
    def state(self) -> TaskState: ...

    @property
    def prev_state(self) -> TaskState | None: ...

    @property
    def review_round(self) -> int: ...

    @property
    def test_retry(self) -> int: ...

    @property
    def review_limit_bonus(self) -> int: ...

    @property
    def test_limit_bonus(self) -> int: ...

    @property
    def recommended_fixed(self) -> int: ...

    @property
    def flag_set(self) -> frozenset[str]: ...


# 파이프라인 위치


def stage_index(pipeline: Pipeline, state: TaskState) -> int:
    """지금 상태가 가리키는 단계 위치. FIXING은 구현 단계로 본다."""
    if state is TaskState.FIXING:
        state = TaskState.IMPLEMENTING
    if state is TaskState.AWAIT_DOC_APPROVAL:
        state = TaskState.DOC_DRAFTING
    for i, stage in enumerate(pipeline.stages):
        if stage.kind is StageKind.GATE:
            if i > 0 and GATE_STATES.get(pipeline.stages[i - 1].kind) is state:
                return i
        elif KIND_STATES.get(stage.kind) is state:
            return i
    raise ValueError(f"{pipeline.key} 파이프라인에 {state} 단계가 없다")


def _gate_state(pipeline: Pipeline, i: int) -> TaskState:
    previous = pipeline.stages[i - 1].kind
    if previous not in GATE_STATES:
        raise ValueError(f"{pipeline.key}: gate 앞 단계({previous})는 승인 대상이 아니다")
    return GATE_STATES[previous]


def _active(stage: StageDecl, flags: frozenset[str]) -> bool:
    return stage.when is None or stage.when in flags


def enter(pipeline: Pipeline, i: int, flags: frozenset[str]) -> Transition:
    """i번째 단계부터 when이 맞는 첫 단계에 들어간다. 끝이면 DONE."""
    while i < len(pipeline.stages) and not _active(pipeline.stages[i], flags):
        i += 1
    if i >= len(pipeline.stages):
        return Transition(TaskState.DONE, (FinishTask(),))
    stage = pipeline.stages[i]
    if stage.kind is StageKind.GATE:
        return Transition(_gate_state(pipeline, i))
    if stage.kind is StageKind.TEST:
        return Transition(TaskState.TESTING, (RunTests(stage.id),))
    if stage.kind is StageKind.MERGE_GATE:
        return Transition(TaskState.AWAIT_MERGE, (MarkReady(),))
    return Transition(KIND_STATES[stage.kind], (StartStage(stage.id),))


def _then(first: tuple[Effect, ...], result: Transition, **updates: object) -> Transition:
    return Transition(result.state, first + result.effects, {**result.updates, **updates})


def start_task(pipeline: Pipeline) -> Transition:
    """작업 시작. 코드를 바꾸는 흐름(구현 단계가 있는)은 Issue부터 만든다 (운영 규약 1.8)."""
    result = enter(pipeline, 0, frozenset())
    if pipeline.first_of(StageKind.IMPLEMENT) is not None:
        return _then((CreateIssue(),), result)
    return result


# 전이


def transition(
    task: TaskView, event: Event, limits: Limits | None = None, pipeline: Pipeline | None = None
) -> Transition:
    limits = limits or Limits()
    state = task.state
    if isinstance(event, Command) and event.name is CommandName.STOP:
        if state.is_terminal:
            return _reject(state, "이미 끝난 작업이다")
        return Transition(
            TaskState.STOPPED,
            (CancelAgentRuns(), CleanupStopped(), PostNotice(NoticeCode.STOPPED)),
        )
    if isinstance(event, ZeroMessage):
        return Transition(state)  # 진행 중인 포스트의 일반 메시지는 기록만 한다
    if state is TaskState.OPINIONS or pipeline is None:
        # ADR-005 기준 포럼 의견 작업. 의견은 이제 채팅에서 나눈다 (ADR-007)
        if isinstance(event, Command):
            return _reject(state, "옛 방식의 작업이다. 제품 채팅 채널에서 /task로 다시 시작한다")
        return Transition(state)
    if state is TaskState.NEEDS_HUMAN:
        return _needs_human_state(task, event, limits, pipeline)
    if state.is_terminal:
        return Transition(state)  # 끝난 뒤 늦게 도착한 내부 이벤트
    result = _advance(task, event, limits, pipeline)
    if result is not None:
        return result
    if isinstance(event, Command):
        return _reject(state)
    # 상태와 맞지 않는 내부 이벤트 (예: /stop 뒤 늦게 끝난 작업)는 무시한다
    return Transition(state)


def _needs_human(state: TaskState, reason: str, **updates: object) -> Transition:
    return Transition(
        TaskState.NEEDS_HUMAN, (MarkNeedsHuman(reason),), {"prev_state": state, **updates}
    )


def _advance(task: TaskView, event: Event, limits: Limits, pipeline: Pipeline) -> Transition | None:
    state = task.state
    if isinstance(event, StageFailed):
        if state in RUNNING_STATES:
            return _needs_human(state, event.reason)
        return None
    i = stage_index(pipeline, state)
    if state.is_gate:
        return _gate(task, event, pipeline, i)
    if state is TaskState.TESTING:
        return _testing(task, event, limits, pipeline, i)
    if state is TaskState.REVIEWING:
        return _reviewing(task, event, limits, pipeline, i)
    if state is TaskState.FIXING:
        if isinstance(event, StageSucceeded):
            test = pipeline.first_of(StageKind.TEST)
            assert test is not None  # 구현 뒤 test는 파이프라인 규칙이다
            return Transition(TaskState.TESTING, (RunTests(test.id),))
        return None
    if state is TaskState.AWAIT_MERGE:
        return _await_merge(task, event, pipeline, i)
    if state is TaskState.DOC_DRAFTING:
        if isinstance(event, StageSucceeded):
            return Transition(TaskState.AWAIT_DOC_APPROVAL)
        return None
    if state is TaskState.AWAIT_DOC_APPROVAL:
        return _await_doc(task, event, pipeline, i)
    if isinstance(event, StageSucceeded):
        flags = task.flag_set | event.flags
        result = enter(pipeline, i + 1, flags)
        if flags != task.flag_set:
            return _then((), result, flags=_flag_text(flags))
        return result
    return None


def _flag_text(flags: frozenset[str]) -> str:
    return ",".join(sorted(flags))


def _gate(task: TaskView, event: Event, pipeline: Pipeline, i: int) -> Transition | None:
    if not isinstance(event, Command):
        return None
    previous = pipeline.stages[i - 1]
    if event.name is CommandName.APPROVE:
        result = enter(pipeline, i + 1, task.flag_set)
        # 변경 유형을 정하는 단계(기획, 진단, 설계)의 승인이면 Issue에 type 라벨 (4.8)
        sets_change = previous.kind in (StageKind.PLAN, StageKind.DIAGNOSE, StageKind.DESIGN)
        if sets_change and pipeline.first_of(StageKind.IMPLEMENT) is not None:
            return _then((ApplyTypeLabel(),), result)
        return result
    if event.name is CommandName.REVISE:
        request = event.arg.strip()
        if not request:
            return _reject(task.state, "/revise 뒤에 고칠 내용을 적어야 한다")
        return Transition(KIND_STATES[previous.kind], (StartStage(previous.id, request),))
    return None


def _testing(
    task: TaskView, event: Event, limits: Limits, pipeline: Pipeline, i: int
) -> Transition | None:
    if not isinstance(event, TestsFinished):
        return None
    stage = pipeline.stages[i]
    implement = pipeline.first_of(StageKind.IMPLEMENT)
    fixes_code = implement is not None and pipeline.index(implement.id) < i
    nxt = pipeline.stages[i + 1] if i + 1 < len(pipeline.stages) else None
    reports_failure = not fixes_code and nxt is not None and nxt.when == "test_failed"
    if not event.passed and reports_failure:
        # 정기 테스트: 한 번 더 돌려 보고, 그래도 실패하면 환경 오류까지 진단 단계가 다룬다
        allowed = stage.retries if stage.retries is not None else REPORT_RETRIES
        if task.test_retry < allowed:
            return Transition(
                TaskState.TESTING, (RunTests(stage.id),), {"test_retry": task.test_retry + 1}
            )
        flags = task.flag_set | {"test_failed"}
        if event.environment_error:
            flags |= {"environment_error"}
        return _then((), enter(pipeline, i + 1, flags), flags=_flag_text(flags))
    if event.environment_error:
        # 코드로 고칠 수 없으므로 수정을 부르지 않고 재시도 횟수도 세지 않는다.
        # 환경을 고친 뒤 /resume하면 테스트만 다시 돈다
        return _needs_human(TaskState.TESTING, f"테스트 환경 오류: {event.detail}".strip())
    if event.passed:
        flags = task.flag_set - {"test_failed"}
        if reports_failure and task.test_retry > 0:
            flags |= {"test_flaky"}  # 다시 돌려 통과한 일시적 실패. 보고 단계가 기록만 한다
        result = enter(pipeline, i + 1, flags)
        if fixes_code and pipeline.first_of(StageKind.MERGE_GATE) is not None:
            # 처음 통과하면 draft PR을 열고, 이후에는 본문을 갱신한다 (운영 규약 1.9)
            result = _then((PublishPr(),), result)
        if flags != task.flag_set:
            return _then((), result, flags=_flag_text(flags))
        return result
    if not fixes_code:
        # 출시 점검: 고칠 구현 담당도, 실패를 다룰 다음 단계도 없다
        return _needs_human(TaskState.TESTING, f"{stage.id} 테스트가 실패했다")
    retry = task.test_retry + 1
    allowed = stage.retries if stage.retries is not None else limits.max_test_retries
    if retry > allowed + task.test_limit_bonus:
        return _needs_human(TaskState.TESTING, f"테스트 실패가 {retry}회째다", test_retry=retry)
    # 테스트 실패는 리뷰 담당을 부르지 않고 바로 수정 (운영 규약 1.7)
    return Transition(TaskState.FIXING, (StartFix(FixSource.TESTS),), {"test_retry": retry})


def _reviewing(
    task: TaskView, event: Event, limits: Limits, pipeline: Pipeline, i: int
) -> Transition | None:
    if not isinstance(event, ReviewFinished):
        return None
    if event.approved:
        return enter(pipeline, i + 1, task.flag_set)
    stage = pipeline.stages[i]
    rounds = stage.rounds if stage.rounds is not None else limits.max_review_rounds
    round_ = task.review_round + 1
    if round_ > rounds + task.review_limit_bonus:
        return _needs_human(TaskState.REVIEWING, f"리뷰가 {round_}라운드째다", review_round=round_)
    return Transition(TaskState.FIXING, (StartFix(FixSource.REVIEW),), {"review_round": round_})


def _await_merge(task: TaskView, event: Event, pipeline: Pipeline, i: int) -> Transition | None:
    if isinstance(event, PrMerged):
        return _then((FinishMerged(),), enter(pipeline, i + 1, task.flag_set))
    if isinstance(event, Command) and event.name is CommandName.FIX:
        if task.recommended_fixed:
            return _reject(task.state, "[권장] 반영은 1회만 할 수 있고 이미 했다")
        return Transition(
            TaskState.FIXING, (StartFix(FixSource.RECOMMENDED),), {"recommended_fixed": 1}
        )
    return None


def _await_doc(task: TaskView, event: Event, pipeline: Pipeline, i: int) -> Transition | None:
    if not isinstance(event, Command):
        return None
    stage = pipeline.stages[i]
    if event.name is CommandName.APPROVE:
        return _then((WriteDocs(),), enter(pipeline, i + 1, task.flag_set))
    if event.name is CommandName.SKIP:
        return enter(pipeline, i + 1, task.flag_set)
    if event.name is CommandName.REVISE:
        request = event.arg.strip()
        if not request:
            return _reject(task.state, "/revise 뒤에 고칠 내용을 적어야 한다")
        return Transition(TaskState.DOC_DRAFTING, (StartStage(stage.id, request),))
    return None


def _needs_human_state(
    task: TaskView, event: Event, limits: Limits, pipeline: Pipeline
) -> Transition:
    if not (isinstance(event, Command) and event.name is CommandName.RESUME):
        if isinstance(event, Command):
            return _reject(task.state)
        return Transition(task.state)
    prev = task.prev_state
    clear: tuple[Effect, ...] = (ClearNeedsHuman(),)
    if prev is None or prev not in RUNNING_STATES:
        return _reject(task.state, f"직전 상태({prev})에서는 재개할 수 없다")
    if prev is TaskState.TESTING:
        test = pipeline.stages[stage_index(pipeline, TaskState.TESTING)]
        allowed = test.retries if test.retries is not None else limits.max_test_retries
        if task.test_retry > allowed + task.test_limit_bonus:
            # 초과한 한도만 1회 늘린다. 카운터는 그대로 (Q4)
            return Transition(
                TaskState.FIXING,
                (*clear, StartFix(FixSource.TESTS)),
                {"test_limit_bonus": task.test_limit_bonus + 1},
            )
        return Transition(TaskState.TESTING, (*clear, RunTests(test.id)))
    if prev is TaskState.REVIEWING:
        review = pipeline.stages[stage_index(pipeline, TaskState.REVIEWING)]
        rounds = review.rounds if review.rounds is not None else limits.max_review_rounds
        if task.review_round > rounds + task.review_limit_bonus:
            return Transition(
                TaskState.FIXING,
                (*clear, StartFix(FixSource.REVIEW)),
                {"review_limit_bonus": task.review_limit_bonus + 1},
            )
        return Transition(TaskState.REVIEWING, (*clear, StartStage(review.id)))
    if prev is TaskState.FIXING:
        return Transition(TaskState.FIXING, (*clear, StartFix(FixSource.RESUME)))
    stage = pipeline.stages[stage_index(pipeline, prev)]
    return Transition(prev, (*clear, StartStage(stage.id)))


def _reject(state: TaskState, reason: str = "") -> Transition:
    allowed = sorted(f"/{name}" for name in ALLOWED_COMMANDS[state])
    detail = f"현재 상태: {state}. 가능한 명령: {', '.join(allowed) or '없음'}"
    if reason:
        detail = f"{reason}. {detail}"
    return Transition(state, (PostNotice(NoticeCode.REJECTED, detail),))
