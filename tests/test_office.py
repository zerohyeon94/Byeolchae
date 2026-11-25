"""#대표실 대장 셋의 라운드 대화와 첫 인사 테스트 (ADR-019).

엔진(가짜 러너, 메모리 SQLite)과 FakeChatIO를 쓴다.
대장마다 엔진이 달라 러너별 응답 순서가 곧 그 대장의 순서다.
"""

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from orgs import LEGACY_ORG

from bot.agents import AgentResult, FakeAgentRunner, RunStatus
from bot.clock import FixedClock
from bot.org import AgentSpec, Org, TeamSpec
from bot.store import Repository, connect
from bot.workflow.chat import FakeChatIO
from bot.workflow.engine import Engine, ProjectContext
from bot.workflow.office import OfficeRoom, office_members

T0 = datetime(2026, 11, 25, 3, 0, tzinfo=UTC)


def chief(display, engine):
    return AgentSpec(
        display=display,
        team="office",
        title=" 대장",
        engine=engine,
        perspective="결론과 근거",
        basic_question="무엇을 정하려는가",
    )


ORG = Org(
    teams={**LEGACY_ORG.teams, "office": TeamSpec(name="대표실", lead="alpheratz")},
    agents={
        **LEGACY_ORG.agents,
        "mirach": chief("미라크", "codex"),
        "alpheratz": chief("알페라츠", "claude"),
        "almach": chief("알마크", "gemini"),
    },
    roles=LEGACY_ORG.roles,
)


def say(text):
    return json.dumps({"answer": text}, ensure_ascii=False)


def failed():
    return AgentResult(RunStatus.TIMEOUT, None, "", "시간 초과", None, 1.0, Path("runs/x.log"))


def scenario(claude=(), codex=(), gemini=()):
    def wrap(body):
        async def main():
            conn = await connect(":memory:")
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                try:
                    h = SimpleNamespace(
                        chat=FakeChatIO(),
                        claude=FakeAgentRunner("claude", claude),
                        codex=FakeAgentRunner("codex", codex),
                        gemini=FakeAgentRunner("gemini", gemini),
                        conn=conn,
                    )
                    repo = Repository(conn, FixedClock(T0))
                    engine = Engine(
                        repo,
                        h.chat,
                        {"claude": h.claude, "codex": h.codex, "gemini": h.gemini},
                        {"gagaessi": ProjectContext(repo=tmp / "repo")},
                        timeout_sec=60,
                        org=ORG,
                        clock=FixedClock(T0),
                    )
                    h.office = OfficeRoom(
                        engine,
                        h.chat,
                        repo,
                        ORG,
                        workdir=tmp / "office",
                        folders=[("비서 볼트", tmp)],
                    )
                    return await body(h)
                finally:
                    await conn.close()

        return asyncio.run(main())

    return wrap


def posts(h):
    return [(post.author, post.text, post.mention) for post in h.chat.depts]


def test_members_put_lead_first():
    assert office_members(ORG) == ["alpheratz", "mirach", "almach"]
    assert office_members(LEGACY_ORG) == []
    assert ORG.display("almach") == "알마크 · 대표실 대장"


def test_round_answers_replies_and_summary():
    @scenario(
        claude=[say("A 첫 답"), say("A 대화"), say("정리: 합의 1")],
        codex=[say("M 첫 답"), say("M 대화")],
        gemini=[say("G 첫 답"), say("G 대화")],
    )
    async def _(h):
        await h.office.on_message("다음 분기에 무엇을 먼저 할까?", message_id="z1")
        assert posts(h) == [
            ("alpheratz", "A 첫 답", False),
            ("mirach", "M 첫 답", False),
            ("almach", "G 첫 답", False),
            ("alpheratz", "A 대화", False),
            ("mirach", "M 대화", False),
            ("almach", "G 대화", False),
            ("alpheratz", "정리: 합의 1", True),  # Zero 호출은 마지막 정리에만
        ]
        assert {post.thread for post in h.chat.depts} == {"office"}
        # 첫 답은 서로의 답을 보지 않는다
        first = h.codex.requests[0].prompt
        assert "다음 분기에 무엇을 먼저 할까?" in first and "A 첫 답" not in first
        # 대화에서는 다른 대장의 첫 답만 본다
        reply = h.codex.requests[1].prompt
        assert "A 첫 답" in reply and "G 첫 답" in reply and "M 첫 답" not in reply
        summary = h.claude.requests[2].prompt
        assert all(text in summary for text in ("M 첫 답", "G 대화", "A 대화"))
        assert "미라크 · 대표실 대장" in summary  # 대장 셋을 표시 이름으로 안내한다
        rows = await h.conn.execute_fetchall("SELECT project, agent, stage FROM agent_runs")
        assert {tuple(row) for row in rows} == {
            ("office", agent, "dept_chat") for agent in ("alpheratz", "mirach", "almach")
        }
        rows = await h.conn.execute_fetchall("SELECT kind FROM chat_turns WHERE project='office'")
        assert [row[0] for row in rows] == ["message", *["office"] * 7]


def test_next_message_sees_previous_conversation():
    @scenario(
        claude=[say("A1"), say("A2"), say("S1"), say("A3"), say("A4"), say("S2")],
        codex=[say("M1"), say("M2"), say("M3"), say("M4")],
        gemini=[say("G1"), say("G2"), say("G3"), say("G4")],
    )
    async def _(h):
        await h.office.on_message("첫 질문")
        await h.office.on_message("더 이야기해 봐")
        prompt = h.gemini.requests[2].prompt
        assert "지금까지의 흐름" in prompt and "첫 질문" in prompt and "S1" in prompt


def test_named_chief_answers_alone():
    @scenario(codex=[say("미라크 혼자 답")])
    async def _(h):
        await h.office.on_message("@mirach 반론만 들려줘")
        assert posts(h) == [("mirach", "미라크 혼자 답", True)]
        assert "반론만 들려줘" in h.codex.requests[0].prompt
        assert h.claude.requests == [] and h.gemini.requests == []


def test_failed_chief_is_reported_and_others_continue():
    @scenario(
        claude=[say("A 첫 답"), say("A 대화"), say("정리")],
        codex=[say("M 첫 답"), say("M 대화")],
        gemini=[failed()],
    )
    async def _(h):
        await h.office.on_message("질문")
        authors = [author for author, _, _ in posts(h)]
        assert authors == ["alpheratz", "mirach", "bot", "alpheratz", "mirach", "alpheratz"]
        assert any("알마크" in text for text, _ in h.chat.ops)
        assert len(h.gemini.requests) == 1  # 실패한 대장은 대화에 끼지 않는다


def test_greet_once_per_chief():
    @scenario(
        claude=[say("알페라츠입니다")],
        codex=[say("미라크입니다")],
        gemini=[failed(), say("알마크입니다")],
    )
    async def _(h):
        await h.office.greet()
        assert posts(h) == [
            ("alpheratz", "알페라츠입니다", False),
            ("mirach", "미라크입니다", False),
        ]
        assert "알페라츠입니다" in h.codex.requests[0].prompt  # 먼저 한 인사를 보고 잇는다
        # 실패한 알마크만 다음 시작 때 인사한다
        await h.office.greet()
        assert posts(h)[-1] == ("almach", "알마크입니다", False)
        assert len(h.claude.requests) == 1 and len(h.codex.requests) == 1
