"""파이프라인 단계 작업 (운영 규약 1.5, 1.7~1.9, 3.3, 3.9, 4장, ADR-007).

Engine이 이 클래스를 상속해 쓴다. 각 작업은 작업마다 하나씩(Engine._stage) 실행되고,
끝나면 (내부 이벤트, 기록할 이벤트)를 돌려준다. 엔진이 그 이벤트로 다음 상태를 정한다.

- 앱 저장소에 쓰는 것은 봇 전용 worktree(`<worktrees>/<project>-<Issue>`)뿐이다 (3.3)
- push는 GitOps가 보호 조건(4.7)을 검사한 뒤에만 한다. force push는 하지 않는다
- 에이전트 커밋은 push 전에 메시지 형식(4.4)을 검사하고, 틀리면 1회 고쳐 달라고 다시 요청한다
- 볼트에는 문서 반영 승인 뒤 허용 경로(기능 정의서, 허브)만 쓴다 (3.4)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from bot.agents.base import Stage
from bot.github.rules import commit_message_problems
from bot.pipeline import Pipeline, StageDecl, StageKind
from bot.prompts import (
    design_prompt,
    design_text,
    diagnose_prompt,
    diagnosis_text,
    doc_sync_prompt,
    fix_prompt,
    implement_prompt,
    lead_check_prompt,
    plan_prompt,
    plan_text,
    release_prep_prompt,
    review_prompt,
    ui_design_prompt,
    ui_text,
)
from bot.render import render_format_error, render_notice
from bot.render.opinion import split_attachment
from bot.render.work import (
    ReviewRound,
    SpecPart,
    render_design,
    render_diagnosis,
    render_doc_sync,
    render_findings,
    render_implementation,
    render_issue_body,
    render_lead_check,
    render_nightly_report,
    render_plan,
    render_pr_body,
    render_release_prep,
    render_review,
    render_test_report,
    render_ui_design,
)
from bot.schemas import (
    FEATURE_NAMES,
    ISSUE_NUMBER,
    ChangeSummary,
    ChangeType,
    DesignSummary,
    DiagnosisReport,
    DocSyncProposal,
    ImplementationReport,
    PlanSummary,
    ReleasePrep,
    ReportAction,
    Review,
    UiDesignSummary,
)
from bot.store import Actor, EventKind, NewEvent
from bot.store.repo import TaskRecord
from bot.testrun import TestReport
from bot.workflow.chat import Attachment
from bot.workflow.mentions import Moment, should_mention
from bot.workflow.state import (
    FixSource,
    NoticeCode,
    ReviewFinished,
    StageFailed,
    StageSucceeded,
    TestsFinished,
)

if TYPE_CHECKING:
    from bot.workflow.engine import Outcome, ProjectContext, _Answer

log = logging.getLogger(__name__)

TYPE_LABELS = {
    ChangeType.FEATURE_ADD: "type:feature",
    ChangeType.FEATURE_CHANGE: "type:feature",
    ChangeType.BUG_FIX: "type:fix",
    ChangeType.INTERNAL: "type:refactor",
}
# 리뷰 프롬프트에 넣는 diff 최대 길이. 넘으면 잘라 내고 표시한다
MAX_DIFF_CHARS = 80_000
APPROVE_HINT = "\n\n`/approve`로 승인하거나 `/revise <요청>`으로 고친다."
DIAGNOSIS_HINT = "\n\n`/approve`로 수정 방향을 승인하거나 `/revise <요청>`으로 다시 진단한다."
_VERSION = re.compile(r"\d+(?:\.\d+){1,3}")

PromptFor = Callable[[TaskRecord, "list[str] | None", "ChangeSummary | None", str], Awaitable[str]]


class WorkStages:
    # Engine이 채운다
    _repo: Any
    _chat: Any
    _git: Any
    _tests: Any
    _worktrees: Path
    _projects: Any
    _running: Any
    _thread_url: Any
    _org: Any
    _name: Any
    _persona: Any
    _vault: Any
    _clock: Any
    _ask: Any
    _say: Any
    _post: Any
    _postable: Any
    _open_post: Any
    _side_effect: Any
    _advance: Any
    _pipeline: Any
    _limits: Any

    # 공통

    def _project(self, task: TaskRecord) -> ProjectContext:
        return self._projects[task.project]

    def _github(self, task: TaskRecord) -> Any:
        github = self._project(task).github
        if github is None:
            raise RuntimeError(f"{task.project}의 GitHub 연결이 없다")
        return github

    def _web(self, task: TaskRecord, path: str) -> str:
        return f"https://github.com/{self._github(task).repo}/{path}"

    def _pipe(self, task: TaskRecord) -> Pipeline:
        pipeline = self._pipeline(task)
        if pipeline is None:
            raise RuntimeError(f"작업 {task.id}의 파이프라인 {task.pipeline}이 없다")
        return pipeline

    def _next_is_gate(self, task: TaskRecord, stage: StageDecl) -> bool:
        pipeline = self._pipe(task)
        i = pipeline.index(stage.id)
        return i + 1 < len(pipeline.stages) and pipeline.stages[i + 1].kind is StageKind.GATE

    async def _latest(self, task_id: int, kind: EventKind) -> dict[str, Any] | None:
        events = await self._repo.list_events(task_id, {kind})
        return events[-1].payload if events else None

    async def _decision(self, task_id: int) -> str:
        """작업을 시작한 Zero의 원문과 채팅 의견 요약."""
        payload = await self._latest(task_id, EventKind.DECISION)
        if not payload:
            return ""
        text = str(payload.get("text", ""))
        context = str(payload.get("context", "")).strip()
        return f"{text}\n\n채팅 의견 요약:\n{context}" if context else text

    async def _plan_summary(self, task_id: int) -> PlanSummary | None:
        payload = await self._latest(task_id, EventKind.PLAN)
        return PlanSummary.model_validate(payload["summary"]) if payload else None

    async def _ui_summary(self, task_id: int) -> UiDesignSummary | None:
        payload = await self._latest(task_id, EventKind.UI_DESIGN)
        return UiDesignSummary.model_validate(payload["summary"]) if payload else None

    async def _design_summary(self, task_id: int) -> DesignSummary | None:
        payload = await self._latest(task_id, EventKind.DESIGN)
        return DesignSummary.model_validate(payload["summary"]) if payload else None

    async def _diagnosis(self, task_id: int) -> DiagnosisReport | None:
        payload = await self._latest(task_id, EventKind.DIAGNOSIS)
        return DiagnosisReport.model_validate(payload["report"]) if payload else None

    async def _change(self, task_id: int) -> ChangeSummary | None:
        """흐름의 첫 단계가 정한 변경 유형·대상 기능·slug (기획 → 진단 → 설계 순)."""
        plan = await self._plan_summary(task_id)
        if plan is not None:
            return plan
        diagnosis = await self._diagnosis(task_id)
        if diagnosis is not None and diagnosis.change() is not None:
            return diagnosis.change()
        return await self._design_summary(task_id)

    async def _specs(self, task_id: int) -> list[SpecPart]:
        """승인된 산출물 (구현·수정·리뷰·PR이 함께 참조한다)."""
        parts = []
        if (plan := await self._plan_summary(task_id)) is not None:
            parts.append(SpecPart("기획", plan.plan_doc, plan.summary, plan_text(plan)))
        if (ui := await self._ui_summary(task_id)) is not None:
            parts.append(SpecPart("화면 디자인", ui.design_doc, ui.summary, ui_text(ui)))
        if (design := await self._design_summary(task_id)) is not None:
            parts.append(SpecPart("설계", design.design_doc, design.summary, design_text(design)))
        if (diagnosis := await self._diagnosis(task_id)) is not None:
            parts.append(SpecPart("진단", None, diagnosis.summary, diagnosis_text(diagnosis)))
        return parts

    async def _spec_text(self, task_id: int) -> str:
        return "\n\n".join(part.text for part in await self._specs(task_id))

    async def _spec_docs(self, task_id: int) -> str:
        lines = [
            f"{part.label} 전문: `{part.doc}`" for part in await self._specs(task_id) if part.doc
        ]
        return "\n".join(lines) or "문서 없음 (진단 보고를 기준으로 고친다)"

    async def _latest_report(self, task_id: int) -> ImplementationReport | None:
        events = await self._repo.list_events(task_id, {EventKind.IMPL, EventKind.FIX})
        if not events:
            return None
        return ImplementationReport.model_validate(events[-1].payload["report"])

    async def _latest_tests(self, task_id: int) -> TestReport | None:
        payload = await self._latest(task_id, EventKind.TEST)
        return TestReport.from_payload(payload) if payload else None

    async def _reviews(self, task_id: int) -> list[ReviewRound]:
        return [
            ReviewRound(event.payload["round"], Review.model_validate(event.payload["review"]))
            for event in await self._repo.list_events(task_id, {EventKind.REVIEW})
        ]

    def _worktree(self, task: TaskRecord) -> Path:
        if task.worktree is None:
            raise RuntimeError("작업 worktree가 없다")
        return Path(task.worktree)

    async def _ensure_worktree(self, task: TaskRecord) -> tuple[TaskRecord, Path]:
        """봇 전용 worktree를 develop 최신에서 만든다 (브랜치 없이, 4.3)."""
        if task.worktree is not None:
            return task, Path(task.worktree)
        project = self._project(task)
        name = f"{task.project}-{task.issue_number or f't{task.id}'}"
        worktree = self._worktrees / name
        await self._git.prepare_worktree(project.repo, worktree, project.base_branch)
        task = await self._repo.update_fields(task.id, {"worktree": str(worktree)})
        return task, worktree

    async def _ensure_issue(self, task: TaskRecord) -> TaskRecord:
        if task.issue_number is None:
            # 재시작 등으로 Issue 없이 단계에 들어왔다
            await self._create_issue(task.id)
            task = await self._repo.get_task(task.id)
        return task

    async def _commit_problems(self, task: TaskRecord, worktree: Path, before: int) -> list[str]:
        """에이전트 작업 뒤 확인: 커밋하지 않은 변경, 새 커밋 유무, 커밋 메시지 형식 (4.4)."""
        problems = []
        if not await self._git.is_clean(worktree):
            problems.append("커밋하지 않은 변경이 남아 있다. 모두 커밋한다")
        commits = await self._git.commits(worktree, self._project(task).base_branch)
        if len(commits) <= before:
            problems.append("새 커밋이 없다. 변경을 커밋한다")
        for commit in commits[before:]:
            for problem in commit_message_problems(commit.message, task.issue_number or 0):
                problems.append(f"커밋 {commit.sha[:7]}: {problem}")
        return problems

    async def _failed(self, task: TaskRecord, stage: str, answer: _Answer[Any]) -> Outcome:
        """에이전트 단계 실패. 원문이나 실패 안내를 게시하고 NEEDS_HUMAN으로 보낸다 (SPEC 3.3)."""
        name = self._name(answer.agent)
        if answer.failed_to_run:
            reason = f"{name} {stage} 실행 실패 ({answer.failure})"
            await self._say(
                task, render_notice(NoticeCode.AGENT_FAILED, f"[{name}] {answer.failure}")
            )
            await self._chat.notify_ops(
                f"[{task.project}] 작업 {task.id} {name} {stage} 실행 실패: {answer.failure}. "
                f"로그: {answer.log_path}",
                mention=should_mention(Moment.BOT_ERROR),
            )
        else:
            reason = f"{name} {stage} 출력이 재요청 후에도 맞지 않았다"
            rendered = render_format_error(answer.raw, answer.error)
            await self._post(task, answer.agent, rendered.text, Moment.NOTICE, rendered.attachments)
        payload = {"stage": stage, "reason": reason, "error": answer.error, "raw": answer.raw}
        return StageFailed(reason), NewEvent(Actor.BOT, EventKind.ERROR, payload)

    def _context(self, task: TaskRecord) -> tuple[dict[str, Any], list[str] | None]:
        features = self._project(task).feature_names()
        context: dict[str, Any] = {}
        if task.issue_number is not None:
            context[ISSUE_NUMBER] = task.issue_number
        if features is not None:
            context[FEATURE_NAMES] = features
        return context, None if features is None else sorted(features)

    # Issue (운영 규약 1.8)

    async def _create_issue(self, task_id: int) -> None:
        task = await self._repo.get_task(task_id)
        if task.issue_number is not None:
            return
        payload = await self._latest(task_id, EventKind.DECISION) or {}
        body = render_issue_body(
            decision=str(payload.get("text", "")),
            post=str(payload.get("context", "")) or "(채팅 의견 없음)",
            opinions=[],
            discord_url=self._thread_url(task.discord_thread),
        )
        number = await self._github(task).create_issue(task.title, body, ["zerosync"])
        task = await self._repo.update_fields(task.id, {"issue_number": number})
        url = self._web(task, f"issues/{number}")
        await self._say(
            task, render_notice(NoticeCode.DECISION_RECORDED, f"Issue #{number}: {url}")
        )

    # 브랜치 (4.3)

    async def _push_branch(self, task: TaskRecord, worktree: Path, branch: str) -> str:
        """작업 브랜치를 만들거나(첫 문서 단계) 이름을 바꾸고 push한다.

        수정으로 이름이 바뀌면 옛 원격 브랜치를 지운다.
        """
        old = task.branch
        if old is None:
            await self._git.create_branch(worktree, branch)
        elif old != branch:
            await self._git.rename_branch(worktree, old, branch)
        # push 보호는 SQLite에 기록된 이름과 비교하므로 먼저 기록한다 (4.7)
        await self._repo.update_fields(task.id, {"branch": branch})
        await self._git.push(worktree, branch, recorded=branch)
        if old is not None and old != branch:
            await self._git.delete_remote_branch(worktree, old, recorded=old)
        return branch

    async def _apply_type_label(self, task: TaskRecord) -> None:
        change = await self._change(task.id)
        if task.issue_number is not None and change is not None:
            labels = [TYPE_LABELS[change.change_type]]
            await self._github(task).add_labels(task.issue_number, labels)

    async def _document_stage(
        self,
        task: TaskRecord,
        stage: StageDecl,
        revision: str,
        *,
        label: str,
        prompt_for: PromptFor,
        model: type[BaseModel],
        doc_of: Callable[[Any], str],
        branch_of: Callable[[Any, TaskRecord], str],
        render: Callable[..., str],
        kind: EventKind,
        agent_stage: Stage,
        flags_of: Callable[[Any], frozenset[str]] | None = None,
    ) -> Outcome:
        """기획·화면 디자인·설계: worktree에 문서를 쓰고 커밋한 뒤 브랜치를 push한다."""
        task = await self._ensure_issue(task)
        task, worktree = await self._ensure_worktree(task)
        before = len(await self._git.commits(worktree, self._project(task).base_branch))
        context, features = self._context(task)
        change = await self._change(task.id)

        async def verify(summary: Any) -> list[str]:
            problems = []
            if not (worktree / doc_of(summary)).is_file():
                problems.append(f"{doc_of(summary)} 파일이 없다")
            approved = change is not None and kind is EventKind.DESIGN
            if approved and summary.change_fields() != change.change_fields():
                # 기획·진단에서 승인된 변경 필드를 설계가 바꾸지 않는다
                expected = change.change_fields()
                for key, value in summary.change_fields().items():
                    if value != expected[key]:
                        want = getattr(expected[key], "value", expected[key])
                        problems.append(f"{key}는 승인된 값 '{want}'을 그대로 쓴다")
            return problems + await self._commit_problems(task, worktree, before)

        agent = stage.agent
        verb = f"{label}을 시작한다" if not revision else f"{label}을 고친다"
        await self._say(task, f"{self._name(agent)}이 {verb}.")
        prompt = await prompt_for(task, features, change, revision)
        answer = await self._ask(
            task,
            agent,
            agent_stage,
            prompt,
            model=model,
            mode="write_worktree",
            cwd=worktree,
            context=context,
            resume=task.claude_session if revision else None,
            verify=verify,
        )
        if answer.result is None:
            return await self._failed(task, label, answer)
        summary = answer.result
        task = await self._repo.update_fields(task.id, {"claude_session": answer.session_id})
        branch = await self._push_branch(task, worktree, branch_of(summary, task))
        doc_url = self._web(task, f"blob/{branch}/{doc_of(summary)}")
        text = render(summary, doc_url=doc_url)
        if task.issue_number is not None:
            await self._github(task).comment_issue(task.issue_number, text)
        gate = self._next_is_gate(task, stage)
        message_id = await self._post(
            task,
            agent,
            text + (APPROVE_HINT if gate else ""),
            Moment.DESIGN_READY if gate else Moment.STAGE_DONE,
        )
        payload = {
            "summary": summary.model_dump(mode="json"),
            "revision": revision,
            "branch": branch,
        }
        flags = flags_of(summary) if flags_of else frozenset()
        return StageSucceeded(flags), NewEvent(agent, kind, payload, message_id)

    # 기획 (기획팀, ADR-007)

    async def _plan(self, task_id: int, stage: StageDecl, revision: str) -> Outcome:
        task = await self._repo.get_task(task_id)

        async def prompt_for(
            task: TaskRecord, features: list[str] | None, change: Any, revision: str
        ) -> str:
            previous = await self._plan_summary(task.id) if revision else None
            return plan_prompt(
                await self._persona(stage.agent),
                title=task.title,
                decision=await self._decision(task.id),
                issue=task.issue_number,
                features=features,
                revision=revision,
                previous=previous,
            )

        return await self._document_stage(
            task,
            stage,
            revision,
            label="기획",
            prompt_for=prompt_for,
            model=PlanSummary,
            doc_of=lambda s: s.plan_doc,
            branch_of=lambda s, t: s.branch_name(t.issue_number),
            render=render_plan,
            kind=EventKind.PLAN,
            agent_stage=Stage.PLAN,
            flags_of=lambda s: frozenset({"has_ui_change"}) if s.has_ui_change else frozenset(),
        )

    # 화면 디자인 (디자인팀, ADR-007)

    async def _ui_design(self, task_id: int, stage: StageDecl, revision: str) -> Outcome:
        task = await self._repo.get_task(task_id)
        approved = await self._change(task_id)
        if approved is None:
            raise RuntimeError("화면 디자인 전에 승인된 기획이 없다")

        async def prompt_for(
            task: TaskRecord, features: list[str] | None, change: Any, revision: str
        ) -> str:
            previous = await self._ui_summary(task.id) if revision else None
            system = self._project(task).design_system_url or (
                "아직 없다. 기존 화면 코드에서 색·글꼴·간격 규칙을 읽어 명세에 적는다"
            )
            return ui_design_prompt(
                await self._persona(stage.agent),
                title=task.title,
                issue=task.issue_number,
                slug=approved.slug,
                spec=await self._spec_text(task.id),
                design_system=system,
                revision=revision,
                previous=previous,
            )

        return await self._document_stage(
            task,
            stage,
            revision,
            label="화면 디자인",
            prompt_for=prompt_for,
            model=UiDesignSummary,
            doc_of=lambda s: s.design_doc,
            branch_of=lambda s, t: t.branch or approved.branch_name(t.issue_number),
            render=render_ui_design,
            kind=EventKind.UI_DESIGN,
            agent_stage=Stage.UI_DESIGN,
        )

    # 설계 (개발팀)

    async def _design(self, task_id: int, stage: StageDecl, revision: str) -> Outcome:
        task = await self._repo.get_task(task_id)

        async def prompt_for(
            task: TaskRecord, features: list[str] | None, change: Any, revision: str
        ) -> str:
            previous = await self._design_summary(task.id) if revision else None
            return design_prompt(
                await self._persona(stage.agent),
                title=task.title,
                decision=await self._decision(task.id),
                issue=task.issue_number,
                features=features,
                revision=revision,
                previous=previous,
                spec=await self._spec_text(task.id),
                change=change,
            )

        return await self._document_stage(
            task,
            stage,
            revision,
            label="설계",
            prompt_for=prompt_for,
            model=DesignSummary,
            doc_of=lambda s: s.design_doc,
            branch_of=lambda s, t: s.branch_name(t.issue_number),
            render=render_design,
            kind=EventKind.DESIGN,
            agent_stage=Stage.DESIGN,
        )

    # 진단 (QA팀, 읽기 전용)

    async def _diagnose(self, task_id: int, stage: StageDecl, revision: str) -> Outcome:
        if not revision:
            repeat = await self._repeat_diagnosis(task_id, stage)
            if repeat is not None:
                return repeat
        note = f"Zero의 수정 요청: {revision}" if revision else ""
        outcome = await self._diagnose_once(task_id, stage, note, revision)

        async def subject() -> str:
            report = await self._diagnosis(task_id)
            return diagnosis_text(report) if report else "진단 보고가 없다"

        async def again(problems: str) -> Outcome:
            return await self._diagnose_once(task_id, stage, problems, revision)

        return await self._lead_checked(
            task_id, stage, "진단", outcome, subject, again, approve_hint=DIAGNOSIS_HINT
        )

    def _needs_change(self, task: TaskRecord, stage: StageDecl) -> bool:
        """진단 뒤에 구현이 있는가. 없으면 실패를 보고만 하는 흐름(정기 테스트)이다."""
        pipeline = self._pipe(task)
        implement = pipeline.first_of(StageKind.IMPLEMENT)
        return implement is not None and pipeline.index(implement.id) > pipeline.index(stage.id)

    async def _previous_report(self, task: TaskRecord) -> tuple[int, dict[str, Any]] | None:
        """같은 제품·같은 흐름의 직전 작업이 남긴 보고 (정기 테스트 묶기)."""
        earlier = [
            t
            for t in await self._repo.list_tasks()
            if t.project == task.project and t.pipeline == task.pipeline and t.id < task.id
        ]
        for previous in sorted(earlier, key=lambda t: t.id, reverse=True):
            payload = await self._latest(previous.id, EventKind.REPORT)
            if payload is not None:
                return previous.id, payload
        return None

    async def _repeat_diagnosis(self, task_id: int, stage: StageDecl) -> Outcome | None:
        """직전 정기 테스트와 같은 실패면 다시 진단하지 않고 그 진단을 이어 쓴다."""
        task = await self._repo.get_task(task_id)
        if self._needs_change(task, stage):
            return None
        tests = await self._latest_tests(task_id)
        previous = await self._previous_report(task)
        if tests is None or previous is None:
            return None
        previous_id, report = previous
        if not tests.signature or report.get("signature") != tests.signature:
            return None
        diagnosis = await self._latest(previous_id, EventKind.DIAGNOSIS)
        if diagnosis is None:
            return None
        payload = {
            "report": diagnosis["report"],
            "revision": "",
            "repeat_of": previous_id,
            "thread": report.get("thread"),
        }
        return StageSucceeded(), NewEvent(Actor.BOT, EventKind.DIAGNOSIS, payload)

    async def _diagnose_once(
        self, task_id: int, stage: StageDecl, note: str, revision: str
    ) -> Outcome:
        """진단 1회. note는 Zero의 수정 요청이나 팀장 확인 지적이다."""
        task = await self._repo.get_task(task_id)
        needs_change = self._needs_change(task, stage)
        context, features = self._context(task)
        tests = await self._latest_tests(task_id)

        async def verify(report: DiagnosisReport) -> list[str]:
            if not needs_change:
                if report.action is None:
                    return [
                        "정기 테스트 진단은 개선이 필요한지 action(report | none)을 정해야 한다"
                    ]
                return []
            names = {FEATURE_NAMES: context[FEATURE_NAMES]} if FEATURE_NAMES in context else None
            try:
                change = report.change(names)
            except ValidationError as error:
                return [f"변경 필드가 틀렸다: {error.errors()[0]['msg']}"]
            if change is None:
                return [
                    "수정 방향 승인 뒤 바로 구현하므로 "
                    "change_type·target_feature·slug·title이 필요하다"
                ]
            return []

        agent = stage.agent
        await self._say(task, f"{self._name(agent)}이 진단을 시작한다.")
        decision = await self._decision(task_id)
        if note:
            decision += f"\n\n{note}"
        prompt = diagnose_prompt(
            await self._persona(agent),
            title=task.title,
            decision=decision,
            tests=self._tests_text(tests, with_log=not needs_change),
            features=features,
            needs_change=needs_change,
        )
        answer = await self._ask(
            task,
            agent,
            Stage.DIAGNOSE,
            prompt,
            model=DiagnosisReport,
            cwd=Path(task.worktree) if task.worktree else None,
            verify=verify,
        )
        if answer.result is None:
            return await self._failed(task, "진단", answer)
        report: DiagnosisReport = answer.result
        text = render_diagnosis(report)
        if task.issue_number is not None:
            await self._github(task).comment_issue(task.issue_number, text)
        # 팀장 확인이 있으면 승인 안내는 확인을 통과한 뒤에 붙인다
        gate = self._next_is_gate(task, stage) and stage.check is None
        message_id = await self._post(
            task,
            agent,
            text + (DIAGNOSIS_HINT if gate else ""),
            Moment.DESIGN_READY if gate else Moment.STAGE_DONE,
        )
        payload = {"report": report.model_dump(mode="json"), "revision": revision}
        return StageSucceeded(), NewEvent(agent, EventKind.DIAGNOSIS, payload, message_id)

    # 구현과 수정 (개발팀)

    async def _implement(self, task_id: int, stage: StageDecl, revision: str) -> Outcome:
        task = await self._ensure_issue(await self._repo.get_task(task_id))
        task, worktree = await self._ensure_worktree(task)
        if task.branch is None:
            # 문서 단계가 없는 흐름(버그 수정): 승인된 진단의 slug로 브랜치를 만든다 (4.3)
            change = await self._change(task_id)
            if change is None:
                raise RuntimeError("브랜치 이름을 정할 승인된 변경 유형이 없다")
            branch = change.branch_name(task.issue_number)
            await self._git.create_branch(worktree, branch)
            task = await self._repo.update_fields(task.id, {"branch": branch})
        agent = stage.agent
        prompt = implement_prompt(
            await self._persona(agent),
            title=task.title,
            decision=await self._decision(task_id),
            issue=task.issue_number,
            branch=task.branch,
            spec=await self._spec_text(task_id),
        )
        await self._say(task, f"{self._name(agent)}이 구현을 시작한다.", Moment.IMPLEMENT_STARTED)
        outcome = await self._work(task, agent, Stage.IMPLEMENT, prompt, EventKind.IMPL, {}, "구현")
        return await self._code_checked(task_id, stage, "구현", outcome)

    async def _fix(self, task_id: int, source: FixSource) -> Outcome:
        task = await self._repo.get_task(task_id)
        agent = self._pipe(task).implementer
        if source is FixSource.RESUME:
            last = await self._latest(task_id, EventKind.FIX)
            source = FixSource(last["source"]) if last else FixSource.TESTS
            if source is FixSource.RESUME:  # pragma: no cover - 기록에는 RESUME이 남지 않는다
                source = FixSource.TESTS
        problems = await self._fix_problems(task, source)
        prompt = fix_prompt(
            await self._persona(agent),
            title=task.title,
            issue=task.issue_number,
            branch=task.branch,
            spec_docs=await self._spec_docs(task_id),
            problems=problems,
        )
        labels = {
            FixSource.TESTS: "테스트 실패",
            FixSource.REVIEW: "리뷰 [필수] 지적",
            FixSource.RECOMMENDED: "[권장] 지적",
            FixSource.LEAD_CHECK: "팀장 확인 [필수] 지적",
        }
        await self._say(
            task, f"{self._name(agent)}이 고친다: {labels[source]}", Moment.IMPLEMENT_STARTED
        )
        payload = {"source": source.value}
        outcome = await self._work(task, agent, Stage.FIX, prompt, EventKind.FIX, payload, "수정")
        implement = self._pipe(task).first_of(StageKind.IMPLEMENT)
        assert implement is not None  # 수정은 구현 단계가 있는 흐름에서만 일어난다
        return await self._code_checked(task_id, implement, "수정", outcome)

    async def _fix_problems(self, task: TaskRecord, source: FixSource) -> str:
        if source is FixSource.LEAD_CHECK:
            payload = await self._latest(task.id, EventKind.LEAD_CHECK)
            if payload is None:
                raise RuntimeError("팀장 확인 결과가 없다")
            return self._lead_problems(payload["lead"], Review.model_validate(payload["review"]))
        if source is FixSource.TESTS:
            tests = await self._latest_tests(task.id)
            body = render_test_report(tests) if tests else "테스트 결과가 없다"
            return f"봇이 실행한 테스트가 실패했다.\n\n{body}"
        reviews = await self._reviews(task.id)
        if not reviews:
            raise RuntimeError("리뷰 결과가 없다")
        review = reviews[-1].review
        if source is FixSource.RECOMMENDED:
            return "Zero가 merge 전에 [권장] 지적 반영을 요청했다.\n\n" + render_findings(
                review.recommended_findings
            )
        reviewer = self._pipe(task).first_of(StageKind.REVIEW)
        name = self._name(reviewer.agent) if reviewer else "리뷰 담당"
        required = render_findings(review.required_findings)
        text = f"{name}의 [필수] 지적 (번호마다 대응한다):\n\n{required}"
        if review.recommended_findings:
            text += (
                "\n\n참고: [권장] 지적 (반영 여부는 판단에 맡긴다. 번호 대응 대상 아님)\n\n"
                + render_findings(review.recommended_findings)
            )
        return text

    async def _work(
        self,
        task: TaskRecord,
        agent: str,
        stage: Stage,
        prompt: str,
        kind: EventKind,
        payload: dict[str, Any],
        label: str,
    ) -> Outcome:
        worktree = self._worktree(task)
        base = self._project(task).base_branch
        before = len(await self._git.commits(worktree, base))

        async def verify(_report: ImplementationReport) -> list[str]:
            return await self._commit_problems(task, worktree, before)

        answer = await self._ask(
            task,
            agent,
            stage,
            prompt,
            model=ImplementationReport,
            mode="write_worktree",
            cwd=worktree,
            resume=task.claude_session,
            verify=verify,
        )
        if answer.result is None:
            return await self._failed(task, label, answer)
        report: ImplementationReport = answer.result
        if answer.session_id:
            task = await self._repo.update_fields(task.id, {"claude_session": answer.session_id})
        await self._git.push(worktree, task.branch, recorded=task.branch)
        commits = await self._git.commits(worktree, base)
        message_id = await self._post(
            task, agent, render_implementation(report, heading=label), Moment.IMPLEMENT_STARTED
        )
        data = {
            **payload,
            "report": report.model_dump(mode="json"),
            "commits": [commit.sha for commit in commits[before:]],
        }
        return StageSucceeded(), NewEvent(agent, kind, data, message_id)

    # 팀장 확인 (ADR-008)

    def _lead_problems(self, lead: str, review: Review) -> str:
        text = (
            f"{self._name(lead)}(팀장)의 확인 [필수] 지적 (번호마다 대응한다):\n\n"
            + render_findings(review.required_findings)
        )
        if review.recommended_findings:
            text += "\n\n참고: [권장] 지적 (반영 여부는 판단에 맡긴다)\n\n" + render_findings(
                review.recommended_findings
            )
        return text

    async def _code_checked(
        self, task_id: int, stage: StageDecl, label: str, outcome: Outcome
    ) -> Outcome:
        """구현·수정 결과의 팀장 확인. 지적이 있으면 구현 담당이 같은 세션에서 고친다."""

        async def subject() -> str:
            task = await self._repo.get_task(task_id)
            report = await self._latest_report(task_id)
            diff = await self._git.diff(self._worktree(task), self._project(task).base_branch)
            if len(diff) > MAX_DIFF_CHARS:
                diff = (
                    diff[:MAX_DIFF_CHARS] + "\n... (diff가 길어 잘랐다. 나머지는 저장소에서 읽는다)"
                )
            body = render_implementation(report, heading=label) if report else "보고 없음"
            return f"{body}\n\n변경 (develop 대비 diff):\n\n```diff\n{diff}\n```"

        async def again(problems: str) -> Outcome:
            task = await self._repo.get_task(task_id)
            agent = stage.agent
            prompt = fix_prompt(
                await self._persona(agent),
                title=task.title,
                issue=task.issue_number,
                branch=task.branch,
                spec_docs=await self._spec_docs(task_id),
                problems=problems,
            )
            await self._say(
                task,
                f"{self._name(agent)}이 고친다: 팀장 확인 [필수] 지적",
                Moment.IMPLEMENT_STARTED,
            )
            payload = {"source": FixSource.LEAD_CHECK.value}
            return await self._work(task, agent, Stage.FIX, prompt, EventKind.FIX, payload, "수정")

        return await self._lead_checked(task_id, stage, label, outcome, subject, again)

    async def _lead_checked(
        self,
        task_id: int,
        stage: StageDecl,
        label: str,
        outcome: Outcome,
        subject: Callable[[], Awaitable[str]],
        again: Callable[[str], Awaitable[Outcome]],
        *,
        approve_hint: str = "",
    ) -> Outcome:
        """팀원 결과를 팀장(stage.check)이 확인한다. 팀장 확인이 없는 단계는 그대로 돌려준다.

        [필수] 지적이 있으면 팀원이 고치고 다시 확인한다. 중간 결과는 바로 기록하고
        마지막 확인 결과를 단계 결과로 돌려준다. 정해진 횟수 안에 통과하지 못하면 단계 실패다.
        """
        lead = stage.check
        if lead is None:
            return outcome
        limit = stage.check_rounds or self._limits.max_check_rounds
        previous = ""
        for round_ in range(1, limit + 1):
            event, record = outcome
            if not isinstance(event, StageSucceeded):
                return outcome
            await self._repo.add_event(task_id, record)
            task = await self._repo.get_task(task_id)
            prompt = lead_check_prompt(
                await self._persona(lead),
                member=self._name(stage.agent),
                label=label,
                title=task.title,
                decision=await self._decision(task_id),
                round_=round_,
                limit=limit,
                spec=await self._spec_text(task_id),
                subject=await subject(),
                previous_required=previous,
            )
            await self._say(task, f"{self._name(lead)}이 {label} 결과를 확인한다.")
            answer = await self._ask(
                task,
                lead,
                Stage.LEAD_CHECK,
                prompt,
                model=Review,
                cwd=Path(task.worktree) if task.worktree else None,
            )
            if answer.result is None:
                return await self._failed(task, "팀장 확인", answer)
            review: Review = answer.result
            approved = not review.required_findings
            gate = approved and self._next_is_gate(task, stage)
            message_id = await self._post(
                task,
                lead,
                render_lead_check(review, label=label, round_=round_)
                + (approve_hint if gate else ""),
                Moment.DESIGN_READY if gate else Moment.STAGE_DONE,
            )
            payload = {
                "review": review.model_dump(mode="json"),
                "round": round_,
                "stage": stage.id,
                "lead": lead,
                "member": stage.agent,
            }
            record = NewEvent(lead, EventKind.LEAD_CHECK, payload, message_id or None)
            if approved:
                return StageSucceeded(event.flags), record
            if round_ == limit:
                reason = f"{label} 팀장 확인을 {limit}회 통과하지 못했다"
                return StageFailed(reason), record
            await self._repo.add_event(task_id, record)
            previous = render_findings(review.required_findings)
            outcome = await again(self._lead_problems(lead, review))
        raise AssertionError("unreachable")  # pragma: no cover

    # 테스트 (운영 규약 1.7)

    async def _run_tests(self, task_id: int, stage_id: str) -> Outcome | None:
        task = await self._repo.get_task(task_id)
        project = self._project(task)
        if not project.test_command:
            raise RuntimeError(f"{task.project}의 test 명령이 없다")
        task, worktree = await self._ensure_worktree(task)
        count = len(await self._repo.list_events(task.id, {EventKind.TEST}))
        run_id = f"t{task.id}-test-{count + 1}"
        await self._say(task, "봇이 테스트를 실행한다.", Moment.TEST_RUNNING)
        self._running.tests[task.id] = run_id
        try:
            report = await self._tests.run(run_id, project.test_command, cwd=worktree)
        finally:
            self._running.tests.pop(task.id, None)
        if report.status.value == "cancelled":
            return None  # /stop
        message_id = await self._say(task, render_test_report(report), Moment.TEST_RESULT)
        record = NewEvent(Actor.BOT, EventKind.TEST, report.to_payload(), message_id or None)
        detail = report.errors[0] if report.errors else ""
        return TestsFinished(report.passed, report.environment_error, detail), record

    # PR (운영 규약 1.9, 4.5)

    async def _pr_body(self, task: TaskRecord) -> str:
        return render_pr_body(
            decision=await self._decision(task.id),
            specs=await self._specs(task.id),
            report=await self._latest_report(task.id),
            tests=await self._latest_tests(task.id),
            reviews=await self._reviews(task.id),
            discord_url=self._thread_url(task.discord_thread),
            branch=task.branch,
            issue=task.issue_number,
        )

    async def _publish_pr(self, task_id: int) -> None:
        task = await self._repo.get_task(task_id)
        github = self._github(task)
        body = await self._pr_body(task)
        if task.pr_number is not None:
            await github.edit_pr_body(task.pr_number, body)
            return
        change = await self._change(task_id)
        if change is None:
            raise RuntimeError("PR 제목을 정할 변경 유형이 없다")
        number = await github.create_pr(
            base=self._project(task).base_branch,
            head=task.branch,
            title=change.conventional_title,
            body=body,
        )
        task = await self._repo.update_fields(task.id, {"pr_number": number})
        await self._repo.add_event(
            task.id, NewEvent(Actor.BOT, EventKind.PR_OPENED, {"pr": number})
        )
        await github.add_labels(number, ["zerosync", TYPE_LABELS[change.change_type]])
        await self._say(task, f"draft PR #{number}을 열었다: {self._web(task, f'pull/{number}')}")

    # 리뷰 (QA팀, 운영 규약 2.3)

    async def _review(self, task_id: int, stage: StageDecl, revision: str) -> Outcome:
        task = await self._repo.get_task(task_id)
        reviews = await self._reviews(task_id)
        round_ = len(reviews) + 1
        diff = await self._git.diff(self._worktree(task), self._project(task).base_branch)
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n... (diff가 길어 잘랐다. 나머지는 저장소에서 읽는다)"
        tests = await self._latest_tests(task_id)
        previous = ""
        if reviews and reviews[-1].review.required_findings:
            previous = render_findings(reviews[-1].review.required_findings)
        agent = stage.agent
        prompt = review_prompt(
            await self._persona(agent),
            author=self._name(self._pipe(task).implementer),
            title=task.title,
            decision=await self._decision(task_id),
            issue=task.issue_number,
            round_=round_,
            spec=await self._spec_text(task_id),
            tests=render_test_report(tests) if tests else "테스트 결과 없음",
            diff=diff,
            previous_required=previous,
        )
        name = self._name(agent)
        await self._say(task, f"{name}이 리뷰 {round_}라운드를 시작한다.", Moment.REVIEW_RUNNING)
        answer = await self._ask(
            task, agent, Stage.REVIEW, prompt, model=Review, cwd=self._worktree(task)
        )
        if answer.result is None:
            return await self._failed(task, "리뷰", answer)
        review: Review = answer.result
        text = render_review(review, round_=round_)
        message_id = await self._post(task, agent, text, Moment.REVIEW_ROUND_RESULT)
        if task.pr_number is not None:
            await self._github(task).comment_pr(task.pr_number, f"### {name}\n\n{text}")
        payload = {"review": review.model_dump(mode="json"), "round": round_}
        approved = review.verdict.value == "APPROVED"
        return ReviewFinished(approved), NewEvent(agent, EventKind.REVIEW, payload, message_id)

    async def _mark_ready(self, task: TaskRecord) -> None:
        github = self._github(task)
        await github.edit_pr_body(task.pr_number, await self._pr_body(task))
        await github.mark_ready(task.pr_number)
        reviews = await self._reviews(task.id)
        remaining = len(reviews[-1].review.recommended_findings) if reviews else 0
        hint = " merge 전에 `/fix`로 1회 반영할 수 있다." if remaining else ""
        reviewer = self._pipe(task).first_of(StageKind.REVIEW)
        who = self._name(reviewer.agent) if reviewer else "리뷰 담당"
        await self._say(
            task,
            f"{who}이 APPROVED를 냈다. PR #{task.pr_number}을 확인하고 merge한다: "
            f"{self._web(task, f'pull/{task.pr_number}')}\n남은 [권장] {remaining}개.{hint}",
            Moment.FINAL_APPROVAL,
        )

    # 출시 준비 (배포 겸임)

    async def _release_prep(self, task_id: int, stage: StageDecl, revision: str) -> Outcome:
        task = await self._repo.get_task(task_id)
        found = _VERSION.search(task.title)
        version = found.group(0) if found else "0.0.0"
        tests = await self._latest_tests(task_id)
        agent = stage.agent
        await self._say(task, f"{self._name(agent)}이 출시 준비물을 만든다.")
        decision = await self._decision(task_id)
        if revision:
            decision += f"\n\nZero의 수정 요청: {revision}"
        prompt = release_prep_prompt(
            await self._persona(agent),
            title=task.title,
            version=version,
            decision=decision,
            tests=render_test_report(tests) if tests else "",
        )
        answer = await self._ask(
            task,
            agent,
            Stage.RELEASE_PREP,
            prompt,
            model=ReleasePrep,
            cwd=Path(task.worktree) if task.worktree else None,
        )
        if answer.result is None:
            return await self._failed(task, "출시 준비", answer)
        prep: ReleasePrep = answer.result
        rendered = split_attachment(render_release_prep(prep), "release.md")
        message_id = await self._post(
            task,
            agent,
            rendered.text + "\n\n`/approve`로 출시를 승인한다. 스토어 제출은 Zero가 직접 한다. "
            "`/revise <요청>`으로 고친다.",
            Moment.DESIGN_READY,
            rendered.attachments,
        )
        payload = {"prep": prep.model_dump(mode="json"), "revision": revision}
        return StageSucceeded(), NewEvent(agent, EventKind.RELEASE, payload, message_id)

    # 문서 반영 (운영 규약 3.9)

    def _vault_paths(self, task: TaskRecord, change: ChangeSummary) -> tuple[Path, set[str]]:
        """볼트 프로젝트 폴더와 이 작업이 쓸 수 있는 상대 경로 (기능 정의서, 허브)."""
        project = self._project(task)
        if project.feature_dir is None:
            raise RuntimeError(f"{task.project}의 볼트 기능 폴더가 없다")
        root = project.feature_dir.parent
        allowed = {f"기능/{change.target_feature}.md"}
        if project.vault_hub:
            allowed.add(project.vault_hub)
        return root, allowed

    async def _doc_sync(self, task_id: int, stage: StageDecl, revision: str) -> Outcome:
        task = await self._repo.get_task(task_id)
        change = await self._change(task_id)
        if change is None:
            raise RuntimeError("문서 반영 대상 기능을 정할 변경 유형이 없다")
        root, allowed = self._vault_paths(task, change)
        docs = []
        for rel in sorted(allowed):
            path = root / rel
            content = path.read_text(encoding="utf-8") if path.is_file() else "(아직 없다)"
            docs.append(f"### {rel}\n\n{content}")
        project = self._project(task)

        async def verify(proposal: DocSyncProposal) -> list[str]:
            problems = []
            for file in proposal.files:
                if file.path not in allowed:
                    allowed_text = ", ".join(sorted(allowed))
                    problems.append(f"{file.path}는 쓸 수 없는 경로다. 허용: {allowed_text}")
                    continue
                exists = (root / file.path).is_file()
                if file.action == "update" and not exists:
                    problems.append(f"{file.path}가 없는데 update다. create로 쓴다")
                if file.action == "create" and exists:
                    problems.append(f"{file.path}가 이미 있는데 create다. update로 쓴다")
            return problems

        agent = stage.agent
        await self._say(task, f"{self._name(agent)}이 문서 반영안을 만든다.")
        title = task.title + (f"\n\nZero의 수정 요청: {revision}" if revision else "")
        prompt = doc_sync_prompt(
            await self._persona(agent),
            title=title,
            pr=task.pr_number,
            change=change,
            spec=await self._spec_text(task_id),
            docs="\n\n".join(docs),
            hub=project.vault_hub or "(허브 없음)",
            today=self._clock.now().astimezone().strftime("%Y-%m-%d"),
        )
        answer = await self._ask(
            task, agent, Stage.DOC_SYNC, prompt, model=DocSyncProposal, verify=verify
        )
        if answer.result is None:
            return await self._failed(task, "문서 반영", answer)
        proposal: DocSyncProposal = answer.result
        attachments = tuple(Attachment(Path(f.path).name, f.content) for f in proposal.files)
        message_id = await self._post(
            task,
            agent,
            render_doc_sync(proposal)
            + "\n\n`/approve`로 볼트에 쓰고, `/revise <요청>`으로 고치고, `/skip`으로 건너뛴다.",
            Moment.DOC_SYNC_READY,
            attachments,
        )
        payload = {"proposal": proposal.model_dump(mode="json"), "revision": revision}
        return StageSucceeded(), NewEvent(agent, EventKind.DOC_SYNC, payload, message_id)

    async def _write_docs(self, task: TaskRecord) -> None:
        """승인된 문서 반영안을 볼트 허용 경로에만 쓰고, 볼트가 git이면 그 파일만 커밋한다."""
        payload = await self._latest(task.id, EventKind.DOC_SYNC)
        change = await self._change(task.id)
        if payload is None or change is None:
            raise RuntimeError("반영할 문서 반영안이 없다")
        proposal = DocSyncProposal.model_validate(payload["proposal"])
        root, allowed = self._vault_paths(task, change)
        written = []
        for file in proposal.files:
            if file.path not in allowed:  # 검증을 통과했어도 쓰기 직전에 다시 막는다 (3.4)
                raise RuntimeError(f"볼트 허용 경로 밖이다: {file.path}")
            target = root / file.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file.content, encoding="utf-8")
            written.append(file.path)
        if self._vault is not None:
            await self._vault.commit(
                root,
                written,
                f"docs({task.project}): ZeroSync 기능 - {task.title}",
                f"ZeroSync-Task: {task.project}-{task.issue_number or task.id}",
            )
        await self._say(task, f"볼트에 반영했다: {', '.join(written)}")

    @staticmethod
    def _tests_text(tests: TestReport | None, *, with_log: bool = False) -> str:
        if tests is None:
            return ""
        text = render_test_report(tests)
        if with_log and tests.log_path:
            text += f"\n\n전체 로그: `{tests.log_path}`"
        return text

    # 정기 테스트 보고 (2026-11-05 Zero 결정)

    async def _report(self, task_id: int, stage: StageDecl, revision: str) -> Outcome:
        """개선이 필요한 실패만 포럼에 보고한다.

        통과·일시적 실패·개선 불필요는 기록만 남긴다(아침 보고에 한 줄). 직전과 같은 실패면
        새 포스트를 열지 않고 그 포스트에 날짜를 덧붙인다.
        """
        task = await self._repo.get_task(task_id)
        tests = await self._latest_tests(task_id)
        signature = tests.signature if tests else ""

        def done(outcome: str, thread: str | None = None) -> Outcome:
            payload = {
                "posted": outcome == "posted",
                "outcome": outcome,
                "thread": thread,
                "signature": signature,
            }
            return StageSucceeded(), NewEvent(Actor.BOT, EventKind.REPORT, payload)

        if "test_failed" not in task.flag_set:
            return done("flaky" if "test_flaky" in task.flag_set else "passed")
        found = await self._latest(task_id, EventKind.DIAGNOSIS)
        diagnosis = DiagnosisReport.model_validate(found["report"]) if found else None
        if found and found.get("repeat_of") is not None:
            thread = found.get("thread")
            if not thread:
                return done("quiet")  # 직전에도 개선 불필요로 본 같은 실패
            day = self._clock.now().astimezone().strftime("%m-%d")
            await self._chat.post(
                thread, "bot", f"{day} 정기 테스트에서도 같은 문제로 실패했다.", mention=False
            )
            return done("appended", thread)
        if diagnosis is not None and diagnosis.action is ReportAction.NONE:
            return done("quiet")
        title = f"{task.title}: {diagnosis.cause.label}" if diagnosis else f"{task.title} 실패"
        task = await self._open_post(task, title, render_nightly_report(diagnosis, tests))
        await self._say(task, "정기 테스트에서 개선이 필요한 문제를 찾았다.", Moment.DESIGN_READY)
        return done("posted", task.discord_thread)

    # NEEDS_HUMAN, merge, 끝, 중지

    async def _label_needs_human(self, task: TaskRecord, *, add: bool) -> None:
        github = self._project(task).github
        if github is None:
            return
        for number in (task.issue_number, task.pr_number):
            if number is None:
                continue
            if add:
                await github.add_labels(number, ["needs-human"])
            else:
                await github.remove_label(number, "needs-human")

    async def _mark_needs_human(self, task_id: int, reason: str) -> None:
        task = await self._repo.get_task(task_id)
        await self._say(task, render_notice(NoticeCode.NEEDS_HUMAN, reason), Moment.NEEDS_HUMAN)
        await self._side_effect(task_id, lambda t: self._label_needs_human(t, add=True))

    async def _finish_merged(self, task: TaskRecord) -> None:
        """merge 감지 (4.6): Issue를 닫는다. 문서 반영은 파이프라인 다음 단계가 한다."""
        github = self._github(task)
        if task.issue_number is not None:
            await github.close_issue(
                task.issue_number,
                not_planned=False,
                comment=f"develop merge 완료 (PR #{task.pr_number}). ZeroSync가 Issue를 닫는다.",
            )
        await self._say(
            task, f"PR #{task.pr_number} merge를 확인하고 Issue #{task.issue_number}을 닫았다."
        )

    async def _finish_task(self, task: TaskRecord) -> None:
        """파이프라인 끝: worktree 정리와 완료 안내."""
        await self._remove_worktree(task)
        if self._pipe(task).first_of(StageKind.RELEASE_PREP) is not None:
            await self._say(task, "출시를 승인했다. 스토어 제출은 Zero가 직접 한다. 작업을 마친다.")
        elif self._postable(task):
            await self._say(task, "작업을 마친다.")

    async def _cleanup_stopped(self, task: TaskRecord) -> None:
        """/stop (1.8, 3.3).

        Issue는 not planned로 닫고, PR이 있으면 닫고, 없으면 원격 작업 브랜치를 지운다.
        """
        github = self._project(task).github
        if github is not None and task.issue_number is not None:
            await github.close_issue(
                task.issue_number, not_planned=True, comment="Zero가 /stop으로 작업을 중지했다."
            )
        if github is not None and task.pr_number is not None:
            await github.close_pr(task.pr_number, "Zero가 /stop으로 작업을 중지했다.")
        elif task.branch is not None and task.worktree is not None:
            await self._git.delete_remote_branch(
                Path(task.worktree), task.branch, recorded=task.branch
            )
        await self._remove_worktree(task)

    async def _remove_worktree(self, task: TaskRecord) -> None:
        if task.worktree is not None and self._git is not None:
            await self._git.remove_worktree(self._project(task).repo, Path(task.worktree))

    async def poll_merges(self) -> None:
        """AWAIT_MERGE 작업의 PR 상태를 조회해 merge를 감지한다 (SPEC 6.2)."""
        from bot.workflow.state import PrMerged, TaskState

        for task in await self._repo.list_tasks({TaskState.AWAIT_MERGE}):
            if task.pr_number is None or self._project(task).github is None:
                continue
            try:
                state = await self._github(task).pr_state(task.pr_number)
            except Exception as error:
                log.warning("PR 상태 조회 실패 %s: %s", task.pr_number, error)
                continue
            if state.merged:
                record = NewEvent(Actor.BOT, EventKind.MERGE, {"pr": task.pr_number})
                await self._advance(task.id, PrMerged(), record)

    async def prepare_github(self) -> None:
        """라벨이 없으면 만든다 (4.8). 실패해도 봇은 계속 뜬다."""
        for key, project in self._projects.items():
            if project.github is None:
                continue
            try:
                await project.github.ensure_labels()
            except Exception as error:
                await self._chat.notify_ops(
                    f"[{key}] GitHub 라벨 준비 실패: {error}",
                    mention=should_mention(Moment.BOT_ERROR),
                )
