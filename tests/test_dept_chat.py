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


# 바로 넘기기 (ADR-013)


def by_persona(answers):
    """인격 이름으로 응답을 고른다.

    넘겨받은 직원은 동시에 불리므로 순서에 기대지 않는다."""

    def respond(request):
        first_line = request.prompt.splitlines()[0]
        for name, answer in answers.items():
            if name in first_line:
                return answer
        raise AssertionError(f"준비하지 않은 인격: {first_line}")

    return respond


def test_secretary_hands_off_to_planning_lead():
    handoff = [{"agent": "beta", "question": "카테고리 범위를 왜 줄였나요?"}]

    @scenario(
        claude=[reply("기획 결정이라 기획팀장에게 넘깁니다", handoff=handoff), reply("범위 이유")]
    )
    async def _(h):
        await h.dept.on_message("secretary", "카테고리 추가는 어떻게 결정된 건가요?")
        secretary, beta = h.claude.requests
        assert "`beta`: Beta · 기획팀장" in secretary.prompt  # 넘길 수 있는 직원 목록
        assert "`theta`" not in secretary.prompt  # 자기 자신은 없다
        assert "Theta · 비서팀장가 Zero의 질문 중" in beta.prompt
        assert "카테고리 추가는 어떻게 결정된 건가요?" in beta.prompt
        assert "카테고리 범위를 왜 줄였나요?" in beta.prompt
        assert "기획 결정이라 기획팀장에게 넘깁니다" in beta.prompt  # 비서의 답이 맥락으로 간다
        assert "넘길 수 있는 직원" not in beta.prompt

        first, second = h.chat.depts
        assert (first.author, first.mention) == ("theta", False)
        assert "→ **Beta · 기획팀장**에게 넘긴다: 카테고리 범위를 왜 줄였나요?" in first.text
        assert (second.author, second.mention, second.text) == ("beta", True, "범위 이유")
        assert await h.runs() == [
            ("secretary", "theta", "dept_chat"),
            ("secretary", "beta", "dept_chat"),
        ]


def test_several_handoffs_answer_independently():
    handoff = [
        {"agent": "beta", "question": "기획 쪽 이유는?"},
        {"agent": "gamma", "question": "화면은 어떻게 바뀌나요?"},
    ]
    answers = {
        "Theta": reply("둘에게 넘깁니다", handoff=handoff),
        "Beta": reply("Beta의 답"),
        "Gamma": reply("Gamma의 답"),
    }

    @scenario(claude=[by_persona(answers)] * 3)
    async def _(h):
        await h.dept.on_message("secretary", "배너 건 정리해 줘")
        prompts = {r.prompt.splitlines()[0]: r.prompt for r in h.claude.requests}
        beta = next(p for line, p in prompts.items() if "Beta" in line)
        gamma = next(p for line, p in prompts.items() if "Gamma" in line)
        assert "Gamma의 답" not in beta and "Beta의 답" not in gamma
        authors = [(p.author, p.mention) for p in h.chat.depts]
        assert authors == [("theta", False), ("beta", False), ("gamma", True)]


def test_unknown_or_self_handoff_is_asked_again():
    bad = [{"agent": "nobody", "question": "q"}, {"agent": "theta", "question": "q"}]

    @scenario(claude=[reply("넘김", handoff=bad), reply("직접 답합니다")])
    async def _(h):
        await h.dept.on_message("secretary", "질문")
        retry = h.claude.requests[1].prompt
        assert "'nobody'는 조직에 없는 이름" in retry and "자기 자신" in retry
        (post,) = h.chat.depts
        assert (post.author, post.text, post.mention) == ("theta", "직접 답합니다", True)


def test_handed_off_agent_cannot_hand_off_again():
    to_beta = [{"agent": "beta", "question": "q"}]
    again = [{"agent": "alpha", "question": "q"}]

    @scenario(
        claude=[reply("넘김", handoff=to_beta), reply("또 넘김", handoff=again), reply("답합니다")]
    )
    async def _(h):
        await h.dept.on_message("secretary", "질문")
        assert "다시 넘기지 않는다" in h.claude.requests[2].prompt
        assert [p.author for p in h.chat.depts] == ["theta", "beta"]
        assert h.chat.depts[-1].text == "답합니다"
        assert len(h.claude.requests) == 3  # alpha는 부르지 않는다


# 회의 스레드 (ADR-013)


def test_meeting_question_opens_thread_and_continues_there():
    handoff = [{"agent": "beta", "question": "범위는?"}]
    first = reply("회의로 정리합니다", thread_title="카테고리 회의", handoff=handoff)

    @scenario(claude=[first, reply("Beta 답"), reply("스레드 안 답")])
    async def _(h):
        await h.dept.on_message("secretary", "카테고리 건 같이 봐요", message_id="m1")
        assert h.chat.threads == [("secretary", "m1", "카테고리 회의")]
        assert h.chat.depts == []  # 채널에는 올리지 않는다
        thread = "thread-secretary-1"
        assert [(p.thread, p.author, p.mention) for p in h.chat.posts] == [
            (thread, "theta", False),
            (thread, "beta", True),
        ]
        key = f"secretary:{thread}"
        turns = await h.repo.recent_chat_turns(key)
        assert [t["actor"] for t in turns] == ["zero", "theta", "beta"]
        assert await h.repo.recent_chat_turns("secretary") == []

        # 스레드 안의 다음 메시지는 그 스레드 기록으로 이어 간다
        await h.dept.on_message("secretary", "정리해 줘", message_id="m2", thread=thread)
        follow = h.claude.requests[-1].prompt
        assert "카테고리 건 같이 봐요" in follow and "Beta 답" in follow
        assert "thread_title: 여러 직원이" not in follow  # 이미 스레드 안
        assert h.chat.posts[-1].thread == thread and h.chat.posts[-1].mention is True
        assert len(await h.repo.recent_chat_turns(key)) == 5


def test_thread_open_failure_falls_back_to_channel():
    @scenario(claude=[reply("답", thread_title="회의")])
    async def _(h):
        async def broken(*args):
            raise RuntimeError("권한 없음")

        h.chat.open_thread = broken
        await h.dept.on_message("secretary", "질문", message_id="m1")
        (post,) = h.chat.depts
        assert post.author == "theta" and post.text == "답"
        assert any("권한 없음" in text for text, _ in h.chat.ops)
        assert [t["actor"] for t in await h.repo.recent_chat_turns("secretary")] == [
            "zero",
            "theta",
        ]
