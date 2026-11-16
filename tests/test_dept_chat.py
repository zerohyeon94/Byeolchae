"""#비서·#교육 대화 테스트 (ADR-013).

엔진(가짜 러너, 메모리 SQLite)과 FakeChatIO로 부서 대화의 호출·게시·기록을 확인한다.
"""

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from bot.agents import AgentResult, FakeAgentRunner, RunStatus, Stage
from bot.clock import FixedClock
from bot.org import DEFAULT_ORG
from bot.store import Actor, EventKind, NewEvent, Repository, connect
from bot.workflow.chat import FakeChatIO
from bot.workflow.dept_chat import Dept, DeptChat, parse_target
from bot.workflow.engine import Engine, ProjectContext
from bot.workflow.state import TaskState

T0 = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
SECRETARY = Dept("secretary", "비서", "theta")
EDUCATION = Dept("education", "교육", "eta")


def reply(answer, **extra):
    return json.dumps({"answer": answer, **extra}, ensure_ascii=False)


def failed():
    return AgentResult(RunStatus.TIMEOUT, None, "", "시간 초과", None, 1.0, Path("runs/x.log"))


class Harness:
    def __init__(self, conn, claude, codex, tmp):
        self.conn = conn
        self.repo = Repository(conn, FixedClock(T0))
        self.chat = FakeChatIO()
        self.claude, self.codex = claude, codex
        self.vault = tmp / "vault"
        self.vault.mkdir()
        self.engine = Engine(
            self.repo,
            self.chat,
            {"claude": claude, "codex": codex},
            {"gagaessi": ProjectContext(repo=tmp / "repo")},
            timeout_sec=60,
            org=DEFAULT_ORG,
            clock=FixedClock(T0),
        )
        self.workdir = tmp / "dept"
        self.dept = DeptChat(
            self.engine,
            self.chat,
            self.repo,
            DEFAULT_ORG,
            {"secretary": SECRETARY, "education": EDUCATION},
            workdir=self.workdir,
            # 없는 폴더는 목록에만 두고 --add-dir로는 넘기지 않는다
            folders=[("비서 볼트", self.vault), ("gagaessi 저장소", tmp / "repo")],
        )

    async def runs(self):
        rows = await self.conn.execute_fetchall("SELECT project, agent, stage FROM agent_runs")
        return [tuple(row) for row in rows]


def scenario(claude=(), codex=()):
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
                    )
                    return await body(h)
                finally:
                    await conn.close()

        return asyncio.run(main())

    return wrap


def test_parse_target():
    assert parse_target(DEFAULT_ORG, "결정 이유?", "theta") == ("theta", "결정 이유?")
    assert parse_target(DEFAULT_ORG, "  @beta 범위는?", "theta") == ("beta", "범위는?")
    # 이름이 이어지면 지정이 아니다
    assert parse_target(DEFAULT_ORG, "@betamax 뭐야", "theta") == ("theta", "@betamax 뭐야")


def test_secretary_answers_in_channel_with_materials():
    @scenario(claude=[reply("기획 승인 대기입니다. 근거: 작업 1")])
    async def _(h):
        task = await h.repo.create_task(
            project="gagaessi",
            discord_thread="t1",
            title="카테고리 추가",
            state=TaskState.AWAIT_PLAN_APPROVAL,
            event=NewEvent(Actor.ZERO, EventKind.DECISION, {"title": "카테고리 추가"}),
        )
        await h.dept.on_message("secretary", "카테고리 추가는 어떻게 됐나요?", message_id="m1")

        (request,) = h.claude.requests
        assert request.stage is Stage.DEPT_CHAT and request.mode == "read_only"
        assert request.cwd == h.workdir and request.read_dirs == (h.vault,)
        assert "Theta · 비서팀장" in request.prompt and "`#비서`" in request.prompt
        assert f"작업 {task.id} [gagaessi] 카테고리 추가" in request.prompt
        assert "AWAIT_PLAN_APPROVAL" in request.prompt and "decision" in request.prompt

        (post,) = h.chat.depts
        assert (post.thread, post.author, post.mention) == ("secretary", "theta", True)
        assert post.text == "기획 승인 대기입니다. 근거: 작업 1"
        turns = await h.repo.recent_chat_turns("secretary")
        assert [(t["actor"], t["kind"]) for t in turns] == [
            ("zero", "message"),
            ("theta", "dept_answer"),
        ]
        # 제품 채팅 기록과 섞이지 않는다
        assert await h.repo.recent_chat_turns("gagaessi") == []
        assert await h.runs() == [("secretary", "theta", "dept_chat")]


def test_history_and_mention_target():
    @scenario(claude=[reply("첫 답"), reply("Beta 답")])
    async def _(h):
        await h.dept.on_message("education", "오늘 개념 요약해 줘")
        await h.dept.on_message("education", "@beta 이 개념을 가게씨에 쓸 수 있나요?")
        first, second = h.claude.requests
        assert "Eta · 교육팀장" in first.prompt
        assert "Beta · 기획팀장" in second.prompt and "`#교육`" in second.prompt
        # 같은 채널의 앞 대화가 함께 간다
        assert "오늘 개념 요약해 줘" in second.prompt and "첫 답" in second.prompt
        assert [p.author for p in h.chat.depts] == ["eta", "beta"]


def test_failure_posts_notice_and_alerts_ops():
    @scenario(claude=[failed()])
    async def _(h):
        await h.dept.on_message("secretary", "질문")
        (post,) = h.chat.depts
        assert post.author == "bot" and "Theta · 비서팀장" in post.text
        assert any("대화 실행 실패" in text for text, _ in h.chat.ops)


def test_invalid_output_twice_posts_format_error():
    @scenario(claude=["답이 아님", "여전히 아님"])
    async def _(h):
        await h.dept.on_message("secretary", "질문")
        assert len(h.claude.requests) == 2  # 1회 다시 요청한다
        (post,) = h.chat.depts
        assert post.author == "theta" and "형식 오류" in post.text
