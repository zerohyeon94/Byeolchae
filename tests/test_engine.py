"""워크플로 엔진 통합 테스트 (SPEC 12.3, ADR-007).

에이전트(FakeAgentRunner), 채팅(FakeChatIO), GitHub(FakeGitHub), git(FakeGit), 테스트 실행,
저장소(메모리 SQLite), 시각(FixedClock)을 가짜로 두고 흐름을 시뮬레이션한다.

TEST_ORG: Beta·Gamma·Alpha와 팀원(Epsilon, Zeta)은 claude 러너, Delta(리뷰·팀장 확인, 읽기 전용)는
codex 러너로 실행한다. claude 러너 응답은 기획 → 화면 디자인 → 설계 → 구현 → Alpha의 팀장 확인
순서이고, codex 러너는 Delta 몫이다.
"""

import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.agents import AgentResult, FakeAgentRunner, RunStatus, Stage
from bot.clock import FixedClock
from bot.github.client import PrState
from bot.github.fake import FakeGit, FakeGitHub
from bot.org import DEFAULT_ORG
from bot.pipeline import default_pipelines
from bot.store import Actor, EventKind, NewEvent, Repository, connect
from bot.testrun import CheckKind, TestReport
from bot.workflow.chat import FakeChatIO
from bot.workflow.engine import Engine, ProjectContext
from bot.workflow.state import CommandName, TaskState

T0 = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
REPO = Path("/repos/gagaessi")
P = "gagaessi"

_delta = DEFAULT_ORG.agents["delta"].model_copy(update={"engine": "codex"})
TEST_ORG = DEFAULT_ORG.model_copy(update={"agents": {**DEFAULT_ORG.agents, "delta": _delta}})


class _Git:
    """응답 함수가 커밋을 남길 FakeGit. Harness가 만들 때마다 바꾼다."""

    git: FakeGit | None = None


def opinion(conclusion, stance="agree", **extra):
    data = {
        "stance": stance,
        "conclusion": conclusion,
        "reasons": ["근거"],
        "risks": ["위험"],
        "proposal": "제안",
        **extra,
    }
    return json.dumps(data, ensure_ascii=False)


def failed(status=RunStatus.TIMEOUT):
    return AgentResult(status, None, "", "시간 초과", None, 1.0, Path("runs/x.log"))


CHANGE = {
    "change_type": "feature_change",
    "target_feature": "예산 배너",
    "slug": "budget-banner",
    "title": "월 예산 초과 시 홈 화면 배너 표시",
    "scope": "budget",
}


def _doc(request, path, commit):
    doc = request.cwd / path
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("전문", encoding="utf-8")
    _Git.git.add_commit(request.cwd, commit)


def plan_reply(has_ui_change=True, **overrides):
    data = {
        **CHANGE,
        "problem": "예산을 넘어도 알기 어렵다",
        "summary": "홈 화면에 초과 배너를 띄운다",
        "in_scope": ["홈 배너"],
        "out_of_scope": ["푸시"],
        "acceptance": ["초과 시 배너가 보인다"],
        "edge_cases": [],
        "plan_doc": "docs/기획/42-budget-banner.md",
        "has_ui_change": has_ui_change,
        **overrides,
    }

    def respond(request):
        _doc(request, data["plan_doc"], "docs(plan): 배너 기획\n\nRefs: #42")
        return json.dumps(data, ensure_ascii=False)

    return respond


def ui_reply():
    data = {
        "summary": "홈 상단 배너",
        "screens": [{"name": "홈", "states": ["초과", "정상"]}],
        "components": ["배너"],
        "design_system_changes": [],
        "out_of_plan": [],
        "design_url": None,
        "design_doc": "docs/디자인/42-budget-banner.md",
    }

    def respond(request):
        _doc(request, data["design_doc"], "docs(ui): 배너 화면\n\nRefs: #42")
        return json.dumps(data, ensure_ascii=False)

    return respond


def design_data(**overrides):
    return {
        **CHANGE,
        "summary": "HomeView에 배너를 붙인다",
        "changes": ["HomeView 배너"],
        "test_plan": ["경계 테스트"],
        "risks": [],
        "design_doc": "docs/설계/42-budget-banner.md",
        **overrides,
    }


def design_reply(**overrides):
    data = design_data(**overrides)

    def respond(request):
        _doc(request, data["design_doc"], "docs(design): 배너 설계\n\nRefs: #42")
        return json.dumps(data, ensure_ascii=False)

    return respond


def work_reply(message="feat(budget): 배너 추가\n\nRefs: #42"):
    def respond(request):
        _Git.git.add_commit(request.cwd, message)
        return json.dumps(
            {
                "changes_summary": "배너를 추가했다",
                "design_intent": "홈에서 바로 보이게",
                "check_points": [],
                "responses": [],
            },
            ensure_ascii=False,
        )

    return respond


def review_reply(required=0):
    findings = [
        {"severity": "required", "file": "HomeView.swift", "line": 3, "problem": f"필수 {i}",
         "reason": "버그", "suggestion": "고친다"}
        for i in range(1, required + 1)
    ]  # fmt: skip
    verdict = "CHANGES_REQUESTED" if required else "APPROVED"
    return json.dumps(
        {"verdict": verdict, "summary": "검토했다", "findings": findings}, ensure_ascii=False
    )


def diagnosis_reply(with_change=True, *, action=None, cause="test"):
    data = {
        "summary": "날짜 의존 테스트",
        "reproduction": ["토요일 다음 날 실행"],
        "cause": cause,
        "evidence": ["Tests.swift:10"],
        "fix_direction": "기준 날짜를 고정한다",
        "confidence": "high",
    }
    if action is not None:
        data["action"] = action
    if with_change:
        data |= {**CHANGE, "change_type": "bug_fix", "slug": "date-fix", "title": "날짜 고정"}
    return json.dumps(data, ensure_ascii=False)


def release_reply():
    return json.dumps(
        {
            "version": "1.0.0",
            "checklist": [{"item": "테스트 통과", "state": "ok"}],
            "release_notes": "첫 출시",
            "store_description": None,
            "certificate_expiry": [],
            "blockers": [],
        },
        ensure_ascii=False,
    )


def doc_reply(path="기능/예산 배너.md", action="update"):
    return json.dumps(
        {
            "summary": "기능 정의서 갱신",
            "files": [{"path": path, "action": action, "content": "---\nstatus: 코드확인\n---"}],
        },
        ensure_ascii=False,
    )


def make_report(passed=True, failed=()):
    status = RunStatus.OK if passed else RunStatus.ERROR
    return TestReport(
        status=status,
        exit_code=0 if passed else 65,
        total=10,
        failures=0 if passed else len(failed) or 1,
        failed_tests=list(failed),
        duration_sec=3.0,
        log_path="runs/test.log",
    )


class FakeTests:
    def __init__(self, reports=()):
        self.reports = list(reports)
        self.runs = []

    async def run(self, run_id, argv, *, cwd):
        self.runs.append((run_id, tuple(argv), cwd))
        return self.reports.pop(0) if self.reports else make_report()

    async def cancel(self, run_id):
        pass


class FakeVault:
    def __init__(self):
        self.commits = []

    async def commit(self, root, paths, title, trailer):
        self.commits.append((root, tuple(paths), title))


class Harness:
    def __init__(self, conn, claude, codex, tmp, tests, org, owners):
        self.conn = conn
        self.clock = FixedClock(T0)
        self.slept = []
        self.repo = Repository(conn, self.clock)
        self.chat = FakeChatIO()
        self.claude, self.codex = claude, codex
        self.github = FakeGitHub(repo="zerohyeon94/GagaeSsi")
        self.git = FakeGit()
        _Git.git = self.git
        self.tests = tests
        self.vault = FakeVault()
        self.root = tmp / "vault" / "일일 예산 관리 앱"
        (self.root / "기능").mkdir(parents=True)
        (self.root / "기능" / "예산 배너.md").write_text("기존", encoding="utf-8")
        (self.root / "가게씨.md").write_text("허브", encoding="utf-8")
        self.engine = Engine(
            self.repo,
            self.chat,
            {"claude": claude, "codex": codex},
            {
                project: ProjectContext(
                    owner=owner,
                    repo=REPO,
                    test_command=("xcodebuild", "test"),
                    github=self.github,
                    feature_dir=self.root / "기능",
                    vault_hub="가게씨.md",
                )
                for project, owner in (owners or {P: None}).items()
            },
            timeout_sec=60,
            org=org,
            pipelines=default_pipelines(),
            git=self.git,
            tests=self.tests,
            worktrees_dir=tmp / "worktrees",
            thread_url=lambda thread: f"https://discord.com/channels/1/{thread}",
            clock=self.clock,
            sleep=self.sleep,
            vault=self.vault,
        )

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.clock.advance(timedelta(seconds=seconds))

    async def task(self, task_id=1):
        return await self.repo.get_task(task_id)

    async def runs(self):
        rows = await self.conn.execute_fetchall("SELECT agent, stage, status FROM agent_runs")
        return [tuple(row) for row in rows]

    async def command(self, name, arg="", task_id=1):
        task = await self.task(task_id)
        await self.engine.on_command(task.discord_thread, name, arg)
        return await self.task(task_id)


def scenario(claude=(), codex=(), tests=(), org=TEST_ORG, owners=None):
    """응답 목록으로 Harness를 만들어 시나리오 함수를 바로 실행한다."""

    def wrap(body):
        async def main():
            conn = await connect(":memory:")
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    h = Harness(
                        conn,
                        FakeAgentRunner("claude", claude),
                        FakeAgentRunner("codex", codex),
                        Path(tmp),
                        FakeTests(tests),
                        org,
                        owners,
                    )
                    return await body(h)
                finally:
                    await conn.close()

        return asyncio.run(main())

    return wrap


# 채팅 (ADR-007)


def test_chat_message_gets_answers_from_both_leads():
    @scenario(claude=[opinion("Beta 의견"), opinion("Alpha 의견", "oppose")])
    async def _(h):
        await h.engine.on_chat(P, "예산 초과 배너를 띄우자", message_id="m1")
        beta, alpha = h.claude.requests
        assert beta.stage is Stage.OPINION and beta.mode == "read_only"
        assert "Beta · 기획팀장 (INFJ)" in beta.prompt and "Alpha · 개발팀장" in alpha.prompt
        # 함께 답할 때는 서로의 답을 보지 않는다
        assert "Beta 의견" not in alpha.prompt
        assert [(c.thread, c.author, c.mention) for c in h.chat.chats] == [
            (P, "beta", False),
            (P, "alpha", True),
        ]
        turns = await h.repo.recent_chat_turns(P)
        assert [t["actor"] for t in turns] == ["zero", "beta", "alpha"]
        assert await h.repo.list_tasks() == []  # 대화는 작업이 아니다
        runs = await h.runs()
        assert {(agent, stage) for agent, stage, _ in runs} == {
            ("beta", "opinion"),
            ("alpha", "opinion"),
        }


def test_mention_calls_only_that_agent_and_history_is_passed():
    @scenario(claude=[opinion("a"), opinion("b")], codex=[opinion("Delta 의견")])
    async def _(h):
        await h.engine.on_chat(P, "배너?")
        await h.engine.on_chat(P, "@delta 확인 기준은?")
        (delta,) = h.codex.requests
        assert "Delta · QA팀장" in delta.prompt
        assert "확인 기준은?" in delta.prompt and "Beta · 기획팀장" in delta.prompt
        assert h.chat.chats[-1].author == "delta"


def test_debate_needs_two_answers_and_sends_opponent():
    @scenario(claude=[opinion("a"), opinion("b"), opinion("반박 a"), opinion("반박 b")])
    async def _(h):
        assert "두 팀장의 답" in await h.engine.debate(P)
        await h.engine.on_chat(P, "배너?")
        assert await h.engine.debate(P) == ""
        beta_debate = h.claude.requests[2]
        assert beta_debate.stage is Stage.DEBATE
        assert "Alpha · 개발팀장" in beta_debate.prompt
        assert len(h.chat.chats) == 4


def test_cli_failure_in_chat_posts_notice_and_alerts_ops():
    @scenario(claude=[opinion("a"), failed()])
    async def _(h):
        await h.engine.on_chat(P, "배너?")
        notice = h.chat.chats[-1]
        assert notice.author == "bot" and "Alpha · 개발팀장" in notice.text
        assert any("실행 실패" in text for text, _ in h.chat.ops)


def test_idea_is_recorded():
    @scenario()
    async def _(h):
        number = await h.engine.add_idea(P, "  위젯  ")
        ideas = await h.repo.list_ideas(P)
        assert number == 1 and ideas[0]["body"] == "위젯" and ideas[0]["status"] == "open"


# /task → 기능 개발 전체 (ADR-007)


def test_feature_cycle_from_chat_to_docs():
    @scenario(
        claude=[
            opinion("Beta 의견"),
            opinion("Alpha 의견"),
            plan_reply(),
            ui_reply(),
            design_reply(),
            work_reply(),
            review_reply(),  # Alpha의 팀장 확인
            doc_reply(),
        ],
        codex=[review_reply()],
    )
    async def _(h):
        await h.engine.on_chat(P, "예산 배너?")
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        # 포럼에 포스트를 열고 채팅 의견을 옮겼다
        (created,) = h.chat.created
        assert created[0] == P and created[1] == "예산 배너" and "Beta 의견" in created[2]
        assert await h.repo.recent_chat_turns(P) == []  # 대화가 작업에 붙었다
        task = await h.task()
        assert task.state is TaskState.AWAIT_PLAN_APPROVAL
        assert task.pipeline == "feature" and task.issue_number == 42
        assert task.branch == "feat/42-budget-banner" and task.flag_set == {"has_ui_change"}
        plan_request = h.claude.requests[2]
        assert plan_request.stage is Stage.PLAN and plan_request.mode == "write_worktree"
        assert "Beta 의견" in plan_request.prompt
        assert h.chat.posts[-1].mention and "/approve" in h.chat.posts[-1].text

        task = await h.command(CommandName.APPROVE)
        assert task.state is TaskState.AWAIT_UI_APPROVAL
        ui_request = h.claude.requests[3]
        assert ui_request.stage is Stage.UI_DESIGN and "docs/기획/42" in ui_request.prompt

        task = await h.command(CommandName.APPROVE)
        # 설계는 승인 없이 구현·테스트·리뷰로 이어진다
        assert task.state is TaskState.AWAIT_MERGE
        design_request, implement_request = h.claude.requests[4:6]
        assert design_request.stage is Stage.DESIGN
        assert implement_request.stage is Stage.IMPLEMENT
        assert "Epsilon · 개발팀원" in implement_request.prompt
        assert "docs/디자인/42" in implement_request.prompt
        check = h.claude.requests[6]
        assert check.stage is Stage.LEAD_CHECK and "Alpha · 개발팀장" in check.prompt
        assert "배너를 추가했다" in check.prompt  # 팀원의 구현 보고를 본다
        (review,) = h.codex.requests
        assert review.stage is Stage.REVIEW and "Delta · QA팀장" in review.prompt
        assert "Epsilon · 개발팀원" in review.prompt  # 구현 담당은 팀원이다
        (pr,) = h.github.named("create_pr")
        assert pr[1:4] == (43, "develop", "feat/42-budget-banner")
        assert pr[4] == "feat(budget): 월 예산 초과 시 홈 화면 배너 표시"
        assert "docs/설계/42-budget-banner.md" in pr[5]
        assert ("mark_ready", 43) in h.github.calls

        h.github.pr_states[43] = PrState("MERGED", True)
        await h.engine.poll_merges()
        task = await h.task()
        assert task.state is TaskState.AWAIT_DOC_APPROVAL
        doc_request = h.claude.requests[7]
        assert doc_request.stage is Stage.DOC_SYNC and "기존" in doc_request.prompt

        task = await h.command(CommandName.APPROVE)
        assert task.state is TaskState.DONE
        written = (h.root / "기능" / "예산 배너.md").read_text(encoding="utf-8")
        assert "코드확인" in written
        assert h.vault.commits and h.vault.commits[0][1] == ("기능/예산 배너.md",)


def test_plan_without_ui_change_skips_ui_design():
    @scenario(claude=[plan_reply(has_ui_change=False), design_reply(), work_reply(),
                      review_reply()],
              codex=[review_reply()])  # fmt: skip
    async def _(h):
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        task = await h.command(CommandName.APPROVE)
        assert task.state is TaskState.AWAIT_MERGE
        assert [r.stage for r in h.claude.requests] == [
            Stage.PLAN,
            Stage.DESIGN,
            Stage.IMPLEMENT,
            Stage.LEAD_CHECK,
        ]


def test_design_must_keep_approved_change_fields():
    @scenario(
        claude=[
            plan_reply(has_ui_change=False),
            design_reply(slug="other-slug", design_doc="docs/설계/42-other-slug.md"),
            design_reply(),
            work_reply(),
            review_reply(),
        ],
        codex=[review_reply()],
    )
    async def _(h):
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        await h.command(CommandName.APPROVE)
        first, again = h.claude.requests[1:3]
        # 승인된 값은 필드 그대로(JSON) 준다. 표시용 제목을 title에 옮기는 실수를 막는다
        assert '"title": "월 예산 초과 시 홈 화면 배너 표시"' in first.prompt
        assert again.stage is Stage.DESIGN  # 다시 요청했다
        assert "slug는 승인된 값 'budget-banner'을 그대로 쓴다" in again.prompt
        assert (await h.task()).state is TaskState.AWAIT_MERGE


def test_revise_plan_reruns_plan_with_feedback():
    @scenario(claude=[plan_reply(), plan_reply()])
    async def _(h):
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        task = await h.command(CommandName.REVISE, "범위를 줄여")
        assert task.state is TaskState.AWAIT_PLAN_APPROVAL
        revised = h.claude.requests[1]
        assert revised.stage is Stage.PLAN and "범위를 줄여" in revised.prompt


def test_test_failure_is_fixed_by_implementer_then_reviewed():
    @scenario(
        claude=[plan_reply(has_ui_change=False), design_reply(), work_reply(), review_reply(),
                work_reply("fix(budget): 경계 수정\n\nRefs: #42"), review_reply()],
        codex=[review_reply()],
        tests=[make_report(False, ["BudgetTests testOver"]), make_report()],
    )  # fmt: skip
    async def _(h):
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        task = await h.command(CommandName.APPROVE)
        assert task.state is TaskState.AWAIT_MERGE and task.test_retry == 1
        fix, check = h.claude.requests[4:6]
        assert fix.stage is Stage.FIX and "BudgetTests testOver" in fix.prompt
        # 수정도 팀원 결과라 팀장이 다시 확인한다 (ADR-008)
        assert check.stage is Stage.LEAD_CHECK and "BudgetTests testOver" not in check.prompt


def test_lead_check_changes_are_fixed_by_member_then_rechecked():
    @scenario(
        claude=[plan_reply(has_ui_change=False), design_reply(), work_reply(),
                review_reply(required=1), work_reply("fix(budget): 확인 반영\n\nRefs: #42"),
                review_reply()],
        codex=[review_reply()],
    )  # fmt: skip
    async def _(h):
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        task = await h.command(CommandName.APPROVE)
        assert task.state is TaskState.AWAIT_MERGE
        stages = [r.stage for r in h.claude.requests[2:]]
        assert stages == [Stage.IMPLEMENT, Stage.LEAD_CHECK, Stage.FIX, Stage.LEAD_CHECK]
        fix, recheck = h.claude.requests[4:6]
        assert "Alpha · 개발팀장(팀장)의 확인 [필수] 지적" in fix.prompt and "필수 1" in fix.prompt
        assert "지난 확인의 [필수] 지적" in recheck.prompt
        checks = await h.repo.list_events(1, {EventKind.LEAD_CHECK})
        assert [e.payload["round"] for e in checks] == [1, 2]
        assert [e.actor for e in checks] == ["alpha", "alpha"]
        fixes = await h.repo.list_events(1, {EventKind.FIX})
        assert [e.payload["source"] for e in fixes] == ["lead_check"]
        assert any("구현 팀장 확인 1회: 수정 요청" in post.text for post in h.chat.posts)


def test_lead_check_over_limit_needs_human():
    @scenario(
        claude=[plan_reply(has_ui_change=False), design_reply(), work_reply(),
                review_reply(required=1), work_reply("fix(budget): 확인 반영\n\nRefs: #42"),
                review_reply(required=1)],
    )  # fmt: skip
    async def _(h):
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        task = await h.command(CommandName.APPROVE)
        assert task.state is TaskState.NEEDS_HUMAN
        assert task.prev_state is TaskState.IMPLEMENTING
        assert any("팀장 확인을 2회 통과하지 못했다" in post.text for post in h.chat.posts)
        assert h.codex.requests == []  # 확인을 통과하지 못하면 테스트·리뷰로 가지 않는다


# 버그 수정, 출시, 정기 테스트


def test_bugfix_flow_creates_branch_from_diagnosis():
    @scenario(claude=[diagnosis_reply(), work_reply("fix(budget): 날짜 고정\n\nRefs: #42"),
                      review_reply()],
              codex=[review_reply(), review_reply()])  # fmt: skip
    async def _(h):
        await h.engine.task_from_chat(P, "/task bug", "날짜 테스트")
        task = await h.task()
        assert task.state is TaskState.AWAIT_FIX_APPROVAL and task.branch is None
        # Zeta가 진단하고 Delta가 확인한다. 승인 안내는 확인을 통과한 뒤에 붙는다
        diagnose = h.claude.requests[0]
        assert diagnose.stage is Stage.DIAGNOSE and "Zeta · QA팀원" in diagnose.prompt
        check = h.codex.requests[0]
        assert check.stage is Stage.LEAD_CHECK and "날짜 의존 테스트" in check.prompt
        member_post = next(p for p in h.chat.posts if "날짜 의존 테스트" in p.text)
        assert "/approve" not in member_post.text
        assert h.chat.posts[-1].mention and "/approve" in h.chat.posts[-1].text
        task = await h.command(CommandName.APPROVE)
        assert task.state is TaskState.AWAIT_MERGE
        assert task.branch == "fix/42-date-fix"
        assert "기준 날짜를 고정한다" in h.claude.requests[1].prompt
        h.github.pr_states[43] = PrState("MERGED", True)
        await h.engine.poll_merges()
        assert (await h.task()).state is TaskState.DONE  # 버그 수정은 문서 반영 없이 끝난다


def test_bugfix_diagnosis_without_change_fields_is_retried():
    @scenario(claude=[diagnosis_reply(with_change=False), diagnosis_reply()],
              codex=[review_reply()])  # fmt: skip
    async def _(h):
        await h.engine.task_from_chat(P, "/task bug", "날짜 테스트")
        assert len(h.claude.requests) == 2
        assert (await h.task()).state is TaskState.AWAIT_FIX_APPROVAL


def test_diagnosis_lead_check_changes_rerun_diagnosis():
    @scenario(claude=[diagnosis_reply(), diagnosis_reply()],
              codex=[review_reply(required=1), review_reply()])  # fmt: skip
    async def _(h):
        await h.engine.task_from_chat(P, "/task bug", "날짜 테스트")
        assert (await h.task()).state is TaskState.AWAIT_FIX_APPROVAL
        again = h.claude.requests[1]
        assert again.stage is Stage.DIAGNOSE
        assert "Delta · QA팀장(팀장)의 확인 [필수] 지적" in again.prompt
        assert len(await h.repo.list_events(1, {EventKind.DIAGNOSIS})) == 2


def test_release_flow_needs_approval_and_zero_submits():
    @scenario(claude=[release_reply()])
    async def _(h):
        await h.engine.task_from_chat(P, "/release", "1.0.0")
        task = await h.task()
        assert task.state is TaskState.AWAIT_RELEASE_APPROVAL
        (request,) = h.claude.requests
        assert request.stage is Stage.RELEASE_PREP and "1.0.0" in request.prompt
        assert any("릴리스 노트" in post.text for post in h.chat.posts)
        task = await h.command(CommandName.APPROVE)
        assert task.state is TaskState.DONE


def test_nightly_test_pass_is_silent():
    @scenario()
    async def _(h):
        task = await h.engine.start_scheduled(P, "nightly-test")
        assert task.state is TaskState.DONE
        assert h.chat.posts == [] and h.chat.created == []
        assert task.discord_thread.startswith("sched:")


def nightly_reply(action="report", cause="test"):
    return diagnosis_reply(with_change=False, action=action, cause=cause)


async def report_payload(h, task_id=1):
    (event,) = await h.repo.list_events(task_id, {EventKind.REPORT})
    return event.payload


def test_nightly_failure_is_retried_diagnosed_and_reported():
    @scenario(claude=[nightly_reply()], codex=[review_reply()],
              tests=[make_report(False, ["T"]), make_report(False, ["T"])])  # fmt: skip
    async def _(h):
        task = await h.engine.start_scheduled(P, "nightly-test")
        assert task.state is TaskState.DONE
        assert len(h.tests.runs) == 2  # 진단 전에 한 번 더 돌린다
        (created,) = h.chat.created
        assert created[0] == P and "정기 테스트: 테스트 결함" in created[1]
        assert "무엇이 문제인가" in created[2] and "무엇을 개선해야 하나" in created[2]
        assert "/task bug" in created[2]
        assert task.discord_thread == "thread-gagaessi-1"
        mentions = [post for post in h.chat.posts if post.mention]
        assert [post.thread for post in mentions] == [task.discord_thread]
        payload = await report_payload(h)
        assert payload["outcome"] == "posted" and payload["signature"] == "tests:T"


def test_nightly_flaky_failure_is_recorded_without_post():
    @scenario(tests=[make_report(False, ["T"]), make_report()])
    async def _(h):
        task = await h.engine.start_scheduled(P, "nightly-test")
        assert task.state is TaskState.DONE
        assert h.chat.created == [] and h.chat.posts == []
        assert h.claude.requests == []
        assert (await report_payload(h))["outcome"] == "flaky"


def test_nightly_no_action_needed_is_recorded_without_post():
    @scenario(claude=[nightly_reply("none")], codex=[review_reply()],
              tests=[make_report(False, ["T"]), make_report(False, ["T"])])  # fmt: skip
    async def _(h):
        task = await h.engine.start_scheduled(P, "nightly-test")
        assert task.state is TaskState.DONE
        assert h.chat.created == []
        assert (await report_payload(h))["outcome"] == "quiet"


def test_nightly_diagnosis_must_decide_action():
    @scenario(claude=[nightly_reply(None), nightly_reply()], codex=[review_reply()],
              tests=[make_report(False, ["T"]), make_report(False, ["T"])])  # fmt: skip
    async def _(h):
        task = await h.engine.start_scheduled(P, "nightly-test")
        assert task.state is TaskState.DONE
        assert len(h.claude.requests) == 2
        assert "action" in h.claude.requests[1].prompt
        (check,) = h.codex.requests  # Delta가 판정까지 확인한다
        assert "정기 테스트 판정: 개선 필요" in check.prompt
        assert len(h.chat.created) == 1


def no_device():
    return TestReport(
        status=RunStatus.ERROR,
        exit_code=70,
        total=None,
        failures=None,
        errors=["xcodebuild: error: Unable to find a device matching the provided destination"],
        log_path="runs/t.log",
    )


def test_nightly_environment_error_is_diagnosed_and_reported():
    """시뮬레이터가 없어 테스트를 못 돌려도 멈추지 않고, 원인을 진단해 보고한다."""

    @scenario(claude=[nightly_reply(cause="environment")], codex=[review_reply()],
              tests=[no_device(), no_device()])  # fmt: skip
    async def _(h):
        task = await h.engine.start_scheduled(P, "nightly-test")
        assert task.state is TaskState.DONE
        assert "environment_error" in task.flag_set
        (request,) = h.claude.requests
        assert request.stage is Stage.DIAGNOSE and "전체 로그: `runs/t.log`" in request.prompt
        (created,) = h.chat.created
        assert "환경 문제" in created[1] and "실행 환경 문제" in created[2]
        assert "Unable to find a device" in created[2]


def test_nightly_same_failure_appends_to_previous_post():
    @scenario(claude=[nightly_reply()], codex=[review_reply()],
              tests=[make_report(False, ["T"])] * 4)  # fmt: skip
    async def _(h):
        await h.engine.start_scheduled(P, "nightly-test")
        h.clock.advance(timedelta(days=1))
        second = await h.engine.start_scheduled(P, "nightly-test")
        assert second.state is TaskState.DONE
        assert len(h.claude.requests) == 1  # 같은 실패는 다시 진단하지 않는다
        assert len(h.chat.created) == 1
        appended = h.chat.posts[-1]
        assert appended.thread == "thread-gagaessi-1" and not appended.mention
        assert "같은 문제" in appended.text
        payload = await report_payload(h, second.id)
        assert payload["outcome"] == "appended" and payload["thread"] == "thread-gagaessi-1"


def test_nightly_different_failure_opens_new_post():
    @scenario(claude=[nightly_reply(), nightly_reply()], codex=[review_reply(), review_reply()],
              tests=[make_report(False, ["T"])] * 2 + [make_report(False, ["U"])] * 2)  # fmt: skip
    async def _(h):
        await h.engine.start_scheduled(P, "nightly-test")
        h.clock.advance(timedelta(days=1))
        await h.engine.start_scheduled(P, "nightly-test")
        assert len(h.claude.requests) == 2 and len(h.chat.created) == 2


def test_nightly_build_success_records_build_instead_of_test_pass():
    build = TestReport(RunStatus.OK, 0, None, 0, kind=CheckKind.BUILD)

    @scenario(tests=[build])
    async def _(h):
        task = await h.engine.start_scheduled(P, "nightly-test")
        payload = await report_payload(h, task.id)
        assert task.state is TaskState.DONE and payload["verification"] == "빌드 성공"
        (event,) = await h.repo.list_events(task.id, {EventKind.TEST})
        assert event.payload["kind"] == "build" and event.payload["total"] is None


def test_restart_opens_post_for_scheduled_task_stuck_without_one():
    @scenario()
    async def _(h):
        await h.repo.create_task(
            project=P,
            discord_thread="sched:gagaessi:nightly-test:old",
            title="정기 테스트",
            state=TaskState.NEEDS_HUMAN,
            event=NewEvent(Actor.ZERO, EventKind.DECISION, {"text": "정기 테스트"}),
            updates={"prev_state": TaskState.TESTING},
            pipeline="nightly-test",
        )
        await h.engine.recover_interrupted()
        task = await h.task()
        assert task.discord_thread == "thread-gagaessi-1"
        assert await h.command(CommandName.STOP) is not None
        assert (await h.task()).state is TaskState.STOPPED
        await h.engine.recover_interrupted()
        assert len(h.chat.created) == 1


# 예산, 한도, 대기열 (ADR-007)


def limited_org(daily_calls):
    beta = TEST_ORG.agents["beta"].model_copy(update={"daily_calls": daily_calls})
    return TEST_ORG.model_copy(update={"agents": {**TEST_ORG.agents, "beta": beta}})


def test_daily_budget_blocks_agent():
    @scenario(claude=[opinion("첫"), opinion("a"), opinion("b"), opinion("c")],
              org=limited_org(1))  # fmt: skip
    async def _(h):
        await h.engine.on_chat(P, "질문")
        await h.engine.on_chat(P, "한 번 더")
        # Beta는 오늘 1회를 썼다. 두 번째 질문에서는 Alpha만 CLI를 부른다
        assert len(h.claude.requests) == 3
        assert any(c.author == "bot" and "Beta" in c.text for c in h.chat.chats)
        h.clock.advance(timedelta(days=1))
        await h.engine.on_chat(P, "@beta 다음 날")
        assert len(h.claude.requests) == 4


def test_pipeline_budget_stops_the_task():
    @scenario(claude=[plan_reply(has_ui_change=False)])
    async def _(h):
        feature = h.engine._pipelines["feature"]
        budget = feature.budget.model_copy(update={"max_calls": 1})
        h.engine._pipelines["feature"] = feature.model_copy(update={"budget": budget})
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        task = await h.command(CommandName.APPROVE)
        assert task.state is TaskState.NEEDS_HUMAN
        assert len(h.claude.requests) == 1


def usage_limited():
    return AgentResult(
        RunStatus.ERROR, 1, "", "Claude usage limit reached", None, 1.0, Path("runs/u.log")
    )


def test_usage_limit_pauses_queue_and_retries_once():
    @scenario(claude=[usage_limited(), opinion("다시 성공"), opinion("a")])
    async def _(h):
        await h.engine.on_chat(P, "질문")
        assert len(h.claude.requests) == 3
        assert sum(h.slept) >= 3600
        assert any("한도" in text for text, _ in h.chat.ops)
        assert sorted(c.author for c in h.chat.chats) == ["alpha", "beta"]


# 채용 제안 (운영 규약 1.12)

HIRING = {
    "kind": "team",
    "team": "운영팀",
    "role": "운영팀장",
    "reason": "운영 요청이 개발을 밀어낸다",
    "cases": ["인증서 만료"],
    "duties": "운영 요청 처리",
    "mbti": "ESTJ",
    "competencies": ["빌드 관리"],
    "if_not_hired": "개발이 늦어진다",
}


def test_hiring_proposal_is_raised_and_decided():
    @scenario(claude=[opinion("a", hiring_proposal=HIRING), opinion("b")])
    async def _(h):
        await h.engine.on_chat(P, "질문")
        (proposal,) = await h.repo.list_hiring_proposals()
        assert proposal.proposed_by == "beta" and proposal.task_id is None
        text, mention = h.chat.reports[-1]
        assert mention and "/hire" in text
        assert "거절" in await h.engine.decide_hiring(1, accept=False, note="아직 이르다")
        assert "이미" in await h.engine.decide_hiring(1, accept=True)


# 재멘션, 재시작, 중지, 옛 작업


def test_waiting_task_is_reminded_once_after_24_hours():
    @scenario(claude=[plan_reply()])
    async def _(h):
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        h.clock.advance(timedelta(hours=23))
        assert await h.engine.remind_waiting(timedelta(hours=24)) == 0
        h.clock.advance(timedelta(hours=2))
        assert await h.engine.remind_waiting(timedelta(hours=24)) == 1
        assert h.chat.posts[-1].mention
        assert await h.engine.remind_waiting(timedelta(hours=24)) == 0


def test_restart_marks_running_agent_runs_failed():
    @scenario(claude=[plan_reply()])
    async def _(h):
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        await h.repo.start_agent_run("left", task_id=1, agent="beta", stage=Stage.PLAN)
        await h.engine.recover_interrupted()
        assert (await h.repo.get_agent_run("left")).status == "error"


def test_stop_closes_issue_and_deletes_branch():
    @scenario(claude=[plan_reply()])
    async def _(h):
        await h.engine.task_from_chat(P, "/task feature", "예산 배너")
        task = await h.command(CommandName.STOP)
        assert task.state is TaskState.STOPPED
        assert h.github.named("close_issue")


def test_legacy_task_resumes_with_new_roles():
    """ADR-005 기준으로 시작해 테스트에서 멈춘 작업(v6 이전)을 /resume으로 이어 간다."""

    @scenario(codex=[review_reply()])
    async def _(h):
        task = await h.repo.create_task(
            project=P,
            discord_thread="old",
            title="옛 작업",
            state=TaskState.NEEDS_HUMAN,
            event=NewEvent(Actor.ZERO, EventKind.DECISION, {"text": "고친다"}),
            updates={"prev_state": TaskState.TESTING, "branch": "fix/34-old",
                     "issue_number": 34, "worktree": str(h.engine._worktrees / "old")},
        )  # fmt: skip
        (h.engine._worktrees / "old").mkdir(parents=True)
        summary = design_data(change_type="bug_fix", slug="old", design_doc="docs/설계/34-old.md")
        await h.repo.add_event(
            task.id, NewEvent(Actor.CLAUDE, EventKind.DESIGN, {"summary": summary})
        )
        assert task.pipeline == "legacy"
        await h.engine.on_command("old", CommandName.RESUME)
        task = await h.task()
        assert task.state is TaskState.AWAIT_MERGE
        assert h.codex.requests[0].stage is Stage.REVIEW
        assert "Delta · QA팀장" in h.codex.requests[0].prompt


PROJECT_OWNERS = {"gagaessi": "kappa", "jday": "lambda", "orot": "mu"}


def owner_reply(done, blockers=()):
    return json.dumps(
        {"done": [done], "blockers": list(blockers), "next_step": "다음 회의에서 확인한다"}
    )


def test_owners_report_their_own_chat_to_beta_and_projects_stay_separate():
    @scenario(owners=PROJECT_OWNERS)
    async def _(h):
        for project, owner in PROJECT_OWNERS.items():
            h.claude.add_response(opinion(f"{project} 검토 결과"))
            await h.engine.on_chat(project, f"@{owner} 개선 후보를 살펴봐")
        for project in PROJECT_OWNERS:
            h.claude.add_response(owner_reply(f"{project} 후보를 정리했다"))
        await asyncio.gather(h.engine.process_project_reports(), h.engine.process_project_reports())
        reports = h.claude.requests[3:]
        assert len(reports) == 3
        for (project, owner), request in zip(PROJECT_OWNERS.items(), reports, strict=True):
            assert request.stage is Stage.PROJECT_REPORT and request.mode == "read_only"
            assert f"-{owner}-" in request.run_id and "Beta · 기획팀장" in request.prompt
            assert f"{project} 검토 결과" in request.prompt
            (receipt,) = await h.repo.project_report_inbox(project, "beta")
            assert receipt["owner"] == owner and receipt["body"]["done"] == [
                f"{project} 후보를 정리했다"
            ]
            assert not await h.repo.project_report_inbox(project, "alpha")
        h.claude.add_response(opinion("검토했다"))
        await h.engine.on_chat("orot", "@beta 어떤 보고가 왔어?")
        prompt = h.claude.requests[-1].prompt
        assert "orot 후보를 정리했다" in prompt and "jday 후보를 정리했다" not in prompt
        assert all(not c.mention for c in h.chat.chats if "에게 보고" in c.text)


def test_done_is_reported_once_even_if_worker_runs_again_after_recovery():
    @scenario(owners={P: "kappa"}, claude=[owner_reply("정기 테스트 통과를 확인했다")])
    async def _(h):
        task = await h.engine.start_scheduled(P, "nightly-test")
        assert task.state is TaskState.DONE
        await h.engine.recover_interrupted()
        await h.engine.process_project_reports()
        await h.engine.recover_interrupted()
        await h.engine.process_project_reports()
        assert len(h.claude.requests) == 1
        assert '"passed": true' in h.claude.requests[0].prompt
        assert len(await h.repo.project_report_inbox(P, "beta")) == 1
        assert h.chat.created == [] and h.chat.posts == []


def test_blocked_report_retries_independently_and_keeps_original_evidence():
    @scenario(owners={P: "kappa"}, tests=[make_report(False, ["출시 점검"])])
    async def _(h):
        await h.engine.task_from_chat(P, "/release", "1.0")
        task = await h.task()
        assert task.state is TaskState.NEEDS_HUMAN
        await h.repo.add_event(
            task.id, NewEvent(Actor.ZERO, EventKind.MESSAGE, {"text": "뒤늦은 의견"})
        )
        h.claude.add_response(failed())
        await h.engine.process_project_reports()
        assert not await h.repo.project_report_inbox(P, "beta")
        await h.engine.process_project_reports()
        assert len(h.claude.requests) == 1
        h.clock.advance(timedelta(hours=1))
        h.claude.add_response(owner_reply("출시 점검을 확인했다", ["테스트 실패"]))
        await h.engine.process_project_reports()
        assert (await h.task()).state is TaskState.NEEDS_HUMAN
        assert "테스트가 실패했다" in h.claude.requests[-1].prompt
        assert "뒤늦은 의견" not in h.claude.requests[-1].prompt
        assert "막힌 점: 테스트 실패" in h.chat.posts[-1].text and not h.chat.posts[-1].mention
        h.claude.add_response(owner_reply("중지를 확인했다"))
        await h.command(CommandName.STOP)
        await h.command(CommandName.STOP)
        await h.engine.process_project_reports()
        assert len(await h.repo.project_report_inbox(P, "beta")) == 2


def test_restart_queues_a_single_report_for_an_interrupted_stage():
    @scenario(owners={P: "kappa"}, claude=[owner_reply("재시작 기록을 확인했다", ["구현 중단"])])
    async def _(h):
        await h.repo.create_task(
            project=P,
            discord_thread="thread",
            title="복구",
            pipeline="feature",
            state=TaskState.IMPLEMENTING,
            event=NewEvent(Actor.ZERO, EventKind.DECISION, {"text": "복구"}),
        )
        await h.engine.recover_interrupted()
        await h.engine.recover_interrupted()
        await h.engine.process_project_reports()
        (receipt,) = await h.repo.project_report_inbox(P, "beta")
        assert "재시작" in receipt["subject"] and "IMPLEMENTING" in receipt["subject"]


def test_new_owner_picks_up_an_existing_blocker_once():
    @scenario(owners={P: "kappa"}, claude=[owner_reply("기존 기록을 확인했다", ["빌드 실패"])])
    async def _(h):
        task = await h.repo.create_task(
            project=P,
            discord_thread="thread",
            title="출시",
            state=TaskState.NEEDS_HUMAN,
            updates={"prev_state": TaskState.TESTING},
            event=NewEvent(Actor.BOT, EventKind.ERROR, {"reason": "빌드 실패"}),
        )
        await h.engine.recover_interrupted()
        await h.engine.process_project_reports()
        await h.engine.on_message(task.discord_thread, "나중에 재개")
        await h.engine.recover_interrupted()
        assert await h.repo.pending_project_reports() == []
        assert "빌드 실패" in h.claude.requests[0].prompt
