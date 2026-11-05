"""상태 전이 테스트 (SPEC 3, 3.5). 파이프라인 선언대로 진행하는 순수 함수를 고정한다."""

from dataclasses import dataclass

import pytest

from bot.org import DEFAULT_ORG
from bot.pipeline import StageKind, default_pipelines, legacy_pipeline
from bot.workflow.state import (
    ALLOWED_COMMANDS,
    ApplyTypeLabel,
    CancelAgentRuns,
    CleanupStopped,
    ClearNeedsHuman,
    Command,
    CommandName,
    CreateIssue,
    FinishMerged,
    FinishTask,
    FixSource,
    Limits,
    MarkNeedsHuman,
    MarkReady,
    NoticeCode,
    PostNotice,
    PrMerged,
    PublishPr,
    ReviewFinished,
    RunTests,
    StageFailed,
    StageSucceeded,
    StartFix,
    StartStage,
    TaskState,
    TestsFinished,
    WriteDocs,
    ZeroMessage,
    stage_index,
    start_task,
    transition,
)

PIPELINES = default_pipelines()
FEATURE = PIPELINES["feature"]
BUGFIX = PIPELINES["bugfix"]
RELEASE = PIPELINES["release"]
NIGHTLY = PIPELINES["nightly-test"]


@dataclass(frozen=True)
class Task:
    state: TaskState
    prev_state: TaskState | None = None
    review_round: int = 0
    test_retry: int = 0
    review_limit_bonus: int = 0
    test_limit_bonus: int = 0
    recommended_fixed: int = 0
    flags: str = ""

    @property
    def flag_set(self) -> frozenset[str]:
        return frozenset(f for f in self.flags.split(",") if f)


def step(state, event, pipeline=FEATURE, **fields):
    return transition(Task(state=state, **fields), event, Limits(), pipeline)


APPROVE = Command(CommandName.APPROVE)


# 시작


def test_feature_starts_with_issue_and_plan():
    result = start_task(FEATURE)
    assert result.state is TaskState.PLANNING
    assert result.effects == (CreateIssue(), StartStage("plan"))


def test_release_and_nightly_start_with_tests_and_no_issue():
    assert start_task(RELEASE).effects == (RunTests("check"),)
    assert start_task(RELEASE).state is TaskState.TESTING
    assert start_task(NIGHTLY).effects == (RunTests("test"),)


def test_bugfix_starts_with_diagnosis():
    result = start_task(BUGFIX)
    assert result.state is TaskState.DIAGNOSING
    assert result.effects == (CreateIssue(), StartStage("diagnose"))


# 기능 개발


def test_plan_with_ui_change_goes_through_ui_design():
    planned = step(TaskState.PLANNING, StageSucceeded(frozenset({"has_ui_change"})))
    assert planned.state is TaskState.AWAIT_PLAN_APPROVAL
    assert planned.updates == {"flags": "has_ui_change"}

    approved = step(TaskState.AWAIT_PLAN_APPROVAL, APPROVE, flags="has_ui_change")
    assert approved.state is TaskState.UI_DESIGNING
    assert approved.effects == (ApplyTypeLabel(), StartStage("ui"))

    designed = step(TaskState.UI_DESIGNING, StageSucceeded(), flags="has_ui_change")
    assert designed.state is TaskState.AWAIT_UI_APPROVAL
    ui_ok = step(TaskState.AWAIT_UI_APPROVAL, APPROVE, flags="has_ui_change")
    assert ui_ok.state is TaskState.DESIGNING
    assert ui_ok.effects == (StartStage("design"),)  # 화면 승인은 type 라벨을 다시 달지 않는다


def test_plan_without_ui_change_skips_ui_design_and_its_gate():
    approved = step(TaskState.AWAIT_PLAN_APPROVAL, APPROVE)
    assert approved.state is TaskState.DESIGNING
    assert approved.effects == (ApplyTypeLabel(), StartStage("design"))


def test_design_flows_into_implementation_without_gate():
    result = step(TaskState.DESIGNING, StageSucceeded())
    assert result.state is TaskState.IMPLEMENTING
    assert result.effects == (StartStage("implement"),)
    tested = step(TaskState.IMPLEMENTING, StageSucceeded())
    assert (tested.state, tested.effects) == (TaskState.TESTING, (RunTests("test"),))


def test_passing_tests_open_pr_and_start_review():
    result = step(TaskState.TESTING, TestsFinished(passed=True))
    assert result.state is TaskState.REVIEWING
    assert result.effects == (PublishPr(), StartStage("review"))


def test_failing_tests_fix_until_limit():
    first = step(TaskState.TESTING, TestsFinished(passed=False))
    assert first.state is TaskState.FIXING
    assert first.effects == (StartFix(FixSource.TESTS),)
    assert first.updates == {"test_retry": 1}
    assert step(TaskState.TESTING, TestsFinished(False), test_retry=1).state is TaskState.FIXING
    over = step(TaskState.TESTING, TestsFinished(False), test_retry=2)
    assert over.state is TaskState.NEEDS_HUMAN
    assert over.updates == {"prev_state": TaskState.TESTING, "test_retry": 3}
    fixed = step(TaskState.FIXING, StageSucceeded())
    assert (fixed.state, fixed.effects) == (TaskState.TESTING, (RunTests("test"),))


def test_environment_error_goes_to_needs_human_without_fix():
    result = step(
        TaskState.TESTING,
        TestsFinished(False, environment_error=True, detail="xcodebuild: error: no device"),
        test_retry=1,
    )
    assert result.state is TaskState.NEEDS_HUMAN
    assert result.effects == (MarkNeedsHuman("테스트 환경 오류: xcodebuild: error: no device"),)
    assert result.updates == {"prev_state": TaskState.TESTING}
    resumed = step(
        TaskState.NEEDS_HUMAN,
        Command(CommandName.RESUME),
        prev_state=TaskState.TESTING,
        test_retry=1,
    )
    assert resumed.state is TaskState.TESTING
    assert resumed.effects == (ClearNeedsHuman(), RunTests("test"))


def test_review_rounds_until_limit():
    approved = step(TaskState.REVIEWING, ReviewFinished(approved=True))
    assert approved.state is TaskState.AWAIT_MERGE
    assert approved.effects == (MarkReady(),)
    changes = step(TaskState.REVIEWING, ReviewFinished(approved=False))
    assert changes.state is TaskState.FIXING
    assert changes.effects == (StartFix(FixSource.REVIEW),)
    assert changes.updates == {"review_round": 1}
    over = step(TaskState.REVIEWING, ReviewFinished(False), review_round=3)
    assert over.state is TaskState.NEEDS_HUMAN


def test_merge_then_doc_sync_with_approval():
    merged = step(TaskState.AWAIT_MERGE, PrMerged())
    assert merged.state is TaskState.DOC_DRAFTING
    assert merged.effects == (FinishMerged(), StartStage("docs"))
    drafted = step(TaskState.DOC_DRAFTING, StageSucceeded())
    assert drafted.state is TaskState.AWAIT_DOC_APPROVAL
    written = step(TaskState.AWAIT_DOC_APPROVAL, APPROVE)
    assert written.state is TaskState.DONE
    assert written.effects == (WriteDocs(), FinishTask())
    skipped = step(TaskState.AWAIT_DOC_APPROVAL, Command(CommandName.SKIP))
    assert (skipped.state, skipped.effects) == (TaskState.DONE, (FinishTask(),))
    revised = step(TaskState.AWAIT_DOC_APPROVAL, Command(CommandName.REVISE, " 확인 이력 "))
    assert revised.effects == (StartStage("docs", "확인 이력"),)


def test_recommended_fix_once_before_merge():
    fix = step(TaskState.AWAIT_MERGE, Command(CommandName.FIX, "권장 반영"))
    assert fix.state is TaskState.FIXING
    assert fix.effects == (StartFix(FixSource.RECOMMENDED),)
    again = step(TaskState.AWAIT_MERGE, Command(CommandName.FIX), recommended_fixed=1)
    assert again.state is TaskState.AWAIT_MERGE
    assert isinstance(again.effects[0], PostNotice)


def test_revise_goes_back_to_previous_stage():
    revised = step(TaskState.AWAIT_PLAN_APPROVAL, Command(CommandName.REVISE, " 범위를 줄여 "))
    assert revised.state is TaskState.PLANNING
    assert revised.effects == (StartStage("plan", "범위를 줄여"),)
    empty = step(TaskState.AWAIT_PLAN_APPROVAL, Command(CommandName.REVISE, " "))
    assert empty.state is TaskState.AWAIT_PLAN_APPROVAL
    assert empty.effects[0].code is NoticeCode.REJECTED


# 버그 수정, 출시, 정기 테스트


def test_bugfix_flow_ends_at_merge():
    diagnosed = step(TaskState.DIAGNOSING, StageSucceeded(), BUGFIX)
    assert diagnosed.state is TaskState.AWAIT_FIX_APPROVAL
    approved = step(TaskState.AWAIT_FIX_APPROVAL, APPROVE, BUGFIX)
    assert approved.state is TaskState.IMPLEMENTING
    assert approved.effects == (ApplyTypeLabel(), StartStage("implement"))
    merged = step(TaskState.AWAIT_MERGE, PrMerged(), BUGFIX)
    assert merged.state is TaskState.DONE
    assert merged.effects == (FinishMerged(), FinishTask())


def test_release_flow():
    passed = step(TaskState.TESTING, TestsFinished(True), RELEASE)
    assert passed.state is TaskState.RELEASE_PREPARING
    assert passed.effects == (StartStage("prepare"),)  # 코드를 바꾸지 않으므로 PR이 없다
    failed = step(TaskState.TESTING, TestsFinished(False), RELEASE)
    assert failed.state is TaskState.NEEDS_HUMAN
    prepared = step(TaskState.RELEASE_PREPARING, StageSucceeded(), RELEASE)
    assert prepared.state is TaskState.AWAIT_RELEASE_APPROVAL
    done = step(TaskState.AWAIT_RELEASE_APPROVAL, APPROVE, RELEASE)
    assert (done.state, done.effects) == (TaskState.DONE, (FinishTask(),))


def test_nightly_test_reports_only_failures():
    passed = step(TaskState.TESTING, TestsFinished(True), NIGHTLY)
    assert passed.state is TaskState.REPORTING  # 진단은 건너뛰고 보고 단계가 조용히 끝낸다
    retried = step(TaskState.TESTING, TestsFinished(False), NIGHTLY)
    assert retried.state is TaskState.TESTING  # 진단 전에 한 번 더 돌린다
    assert (retried.effects, retried.updates) == ((RunTests("test"),), {"test_retry": 1})
    failed = step(TaskState.TESTING, TestsFinished(False), NIGHTLY, test_retry=1)
    assert failed.state is TaskState.DIAGNOSING
    assert failed.updates == {"flags": "test_failed"}
    diagnosed = step(TaskState.DIAGNOSING, StageSucceeded(), NIGHTLY, flags="test_failed")
    assert diagnosed.state is TaskState.REPORTING
    reported = step(TaskState.REPORTING, StageSucceeded(), NIGHTLY, flags="test_failed")
    assert reported.state is TaskState.DONE


def test_nightly_flaky_pass_is_flagged():
    passed = step(TaskState.TESTING, TestsFinished(True), NIGHTLY, test_retry=1)
    assert passed.state is TaskState.REPORTING
    assert passed.updates == {"flags": "test_flaky"}


def test_nightly_environment_error_is_diagnosed_not_needs_human():
    env = TestsFinished(False, environment_error=True, detail="no device")
    retried = step(TaskState.TESTING, env, NIGHTLY)
    assert retried.state is TaskState.TESTING
    failed = step(TaskState.TESTING, env, NIGHTLY, test_retry=1)
    assert failed.state is TaskState.DIAGNOSING
    assert failed.updates == {"flags": "environment_error,test_failed"}
    # 출시 점검은 그대로 Zero가 환경을 고친 뒤 /resume한다
    assert step(TaskState.TESTING, env, RELEASE).state is TaskState.NEEDS_HUMAN


# 공통 규칙


@pytest.mark.parametrize(
    "state", [s for s in TaskState if not s.is_terminal and s is not TaskState.OPINIONS]
)
def test_stop_from_any_active_state(state):
    result = step(state, Command(CommandName.STOP))
    assert result.state is TaskState.STOPPED
    assert result.effects == (CancelAgentRuns(), CleanupStopped(), PostNotice(NoticeCode.STOPPED))


def test_stop_after_done_is_rejected():
    result = step(TaskState.DONE, Command(CommandName.STOP))
    assert result.state is TaskState.DONE


def test_stage_failure_goes_to_needs_human_and_resume_restarts_stage():
    failed = step(TaskState.PLANNING, StageFailed("형식 오류 반복"))
    assert failed.state is TaskState.NEEDS_HUMAN
    assert failed.effects == (MarkNeedsHuman("형식 오류 반복"),)
    assert failed.updates == {"prev_state": TaskState.PLANNING}
    resumed = step(
        TaskState.NEEDS_HUMAN, Command(CommandName.RESUME), prev_state=TaskState.PLANNING
    )
    assert resumed.state is TaskState.PLANNING
    assert resumed.effects == (ClearNeedsHuman(), StartStage("plan"))


@pytest.mark.parametrize(
    ("fields", "expected_state", "expected_effects", "updates"),
    [
        (
            {"prev_state": TaskState.TESTING, "test_retry": 3},
            TaskState.FIXING,
            (ClearNeedsHuman(), StartFix(FixSource.TESTS)),
            {"test_limit_bonus": 1},
        ),
        (
            {"prev_state": TaskState.REVIEWING, "review_round": 4},
            TaskState.FIXING,
            (ClearNeedsHuman(), StartFix(FixSource.REVIEW)),
            {"review_limit_bonus": 1},
        ),
        (
            {"prev_state": TaskState.REVIEWING, "review_round": 1},
            TaskState.REVIEWING,
            (ClearNeedsHuman(), StartStage("review")),
            {},
        ),
        (
            {"prev_state": TaskState.FIXING},
            TaskState.FIXING,
            (ClearNeedsHuman(), StartFix(FixSource.RESUME)),
            {},
        ),
    ],
)
def test_resume_raises_only_the_exceeded_limit(fields, expected_state, expected_effects, updates):
    result = step(TaskState.NEEDS_HUMAN, Command(CommandName.RESUME), **fields)
    assert result.state is expected_state
    assert result.effects == expected_effects
    assert dict(result.updates) == updates


def test_messages_in_posts_are_only_recorded():
    result = step(TaskState.AWAIT_PLAN_APPROVAL, ZeroMessage("범위는?"))
    assert result == transition(
        Task(TaskState.AWAIT_PLAN_APPROVAL), ZeroMessage("x"), None, FEATURE
    )
    assert result.effects == ()


def test_late_internal_event_is_ignored():
    assert step(TaskState.STOPPED, StageSucceeded()).state is TaskState.STOPPED
    assert step(TaskState.AWAIT_MERGE, StageSucceeded()).effects == ()


def test_wrong_command_is_rejected_with_allowed_list():
    result = step(TaskState.REVIEWING, APPROVE)
    assert result.state is TaskState.REVIEWING
    assert "가능한 명령: /stop" in result.effects[0].detail


def test_legacy_opinions_task_points_to_chat():
    result = transition(Task(TaskState.OPINIONS), APPROVE, Limits(), FEATURE)
    assert result.state is TaskState.OPINIONS
    assert "/task" in result.effects[0].detail


def test_gate_states_allow_approve_and_revise():
    for state in (
        TaskState.AWAIT_PLAN_APPROVAL,
        TaskState.AWAIT_UI_APPROVAL,
        TaskState.AWAIT_FIX_APPROVAL,
        TaskState.AWAIT_RELEASE_APPROVAL,
    ):
        assert {CommandName.APPROVE, CommandName.REVISE} <= ALLOWED_COMMANDS[state]
        assert state.waits_for_zero and state.is_gate


def test_stage_index_from_state():
    assert stage_index(FEATURE, TaskState.AWAIT_UI_APPROVAL) == 3
    assert stage_index(FEATURE, TaskState.FIXING) == FEATURE.index("implement")
    assert stage_index(FEATURE, TaskState.AWAIT_DOC_APPROVAL) == FEATURE.index("docs")
    with pytest.raises(ValueError):
        stage_index(RELEASE, TaskState.REVIEWING)


def test_legacy_pipeline_resumes_old_tasks():
    legacy = legacy_pipeline(DEFAULT_ORG)
    assert legacy.first_of(StageKind.REVIEW).agent == "delta"
    resumed = transition(
        Task(TaskState.NEEDS_HUMAN, prev_state=TaskState.TESTING),
        Command(CommandName.RESUME),
        Limits(),
        legacy,
    )
    assert resumed.effects == (ClearNeedsHuman(), RunTests("test"))
    approved = transition(Task(TaskState.AWAIT_DESIGN_APPROVAL), APPROVE, Limits(), legacy)
    assert approved.effects == (ApplyTypeLabel(), StartStage("implement"))


def test_test_retries_from_pipeline_override_limits():
    stages = tuple(
        s.model_copy(update={"retries": 0}) if s.kind is StageKind.TEST else s
        for s in FEATURE.stages
    )
    strict = FEATURE.model_copy(update={"stages": stages})
    result = transition(Task(TaskState.TESTING), TestsFinished(False), Limits(), strict)
    assert result.state is TaskState.NEEDS_HUMAN
