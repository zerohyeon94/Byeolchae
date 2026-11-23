"""조사 보고 테스트 (ADR-018).

부서 대화의 답에 research가 있으면 담당이 조사하고, 팀장이 한 번 확인하고, 봇이 볼트에 쓰는지 본다.
엔진(가짜 러너, 메모리 SQLite)과 FakeChatIO를 쓴다.
"""

import asyncio
import json
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from bot.agents import AgentResult, FakeAgentRunner, RunStatus
from bot.clock import FixedClock
from bot.org import DEFAULT_ORG
from bot.render.research import report_path
from bot.schemas import ResearchRequest
from bot.store import Repository, connect
from bot.workflow.chat import FakeChatIO
from bot.workflow.dept_chat import Dept, DeptChat
from bot.workflow.engine import Engine, ProjectContext
from bot.workflow.research import DEFAULT_AGENT, ResearchDesk, research_agent

T0 = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
SECRETARY = Dept("secretary", "비서", "theta")
QUESTION = "유튜브에서 AI 다루는 채널들 분석해서 보고해줘"


def answer(text, **extra):
    return json.dumps({"answer": text, **extra}, ensure_ascii=False)


def report(title="AI 유튜브 채널 분석", summary="결론: 세 채널이 앞선다"):
    return json.dumps(
        {
            "title": title,
            "summary": summary,
            "body": "## 채널\n\n- **채널 A:** 구독자 10만 (2026-11-16 확인)",
            "sources": [{"title": "채널 A", "url": "https://www.youtube.com/@a"}],
            "unverified": ["채널 B 조회수"],
        },
        ensure_ascii=False,
    )


def review(required=()):
    findings = [
        {
            "severity": "required",
            "file": None,
            "line": None,
            "problem": problem,
            "reason": "근거가 없다",
            "suggestion": "출처를 붙인다",
        }
        for problem in required
    ]
    verdict = "CHANGES_REQUESTED" if findings else "APPROVED"
    return json.dumps({"verdict": verdict, "summary": "확인", "findings": findings})


def failed():
    return AgentResult(RunStatus.TIMEOUT, None, "", "시간 초과", None, 1.0, Path("runs/x.log"))


class FakeVault:
    def __init__(self):
        self.commits = []

    async def commit(self, root, paths, title, trailer):
        self.commits.append((list(paths), title, trailer))
        return True


def scenario(claude=(), codex=()):
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
                        vault=tmp / "vault",
                        git=FakeVault(),
                        conn=conn,
                    )
                    h.vault.mkdir()
                    repo = Repository(conn, FixedClock(T0))
                    engine = Engine(
                        repo,
                        h.chat,
                        {"claude": h.claude, "codex": h.codex},
                        {"gagaessi": ProjectContext(repo=tmp / "repo")},
                        timeout_sec=60,
                        org=DEFAULT_ORG,
                        clock=FixedClock(T0),
                    )
                    desk = ResearchDesk(
                        engine,
                        h.git,
                        h.vault,
                        DEFAULT_ORG,
                        FixedClock(T0),
                        h.chat,
                        workdir=tmp / "research",
                        folders=[("비서 볼트", h.vault)],
                    )
                    h.dept = DeptChat(
                        engine,
                        h.chat,
                        repo,
                        DEFAULT_ORG,
                        {"secretary": SECRETARY},
                        workdir=tmp / "dept",
                        folders=[("비서 볼트", h.vault)],
                        research=desk,
                    )
                    h.day = FixedClock(T0).now().astimezone().date()
                    return await body(h)
                finally:
                    await conn.close()

        return asyncio.run(main())

    return wrap


async def runs(h):
    rows = await h.conn.execute_fetchall("SELECT project, agent, stage FROM agent_runs")
    return [tuple(row) for row in rows]


def test_member_research_is_checked_by_lead_and_saved_to_vault():
    research = {"topic": "AI 유튜브 채널 분석", "agent": "iota"}

    @scenario(
        claude=[answer("리서치에 맡깁니다", research=research), review()],
        codex=[report()],
    )
    async def _(h):
        await h.dept.on_message("secretary", QUESTION)
        # 대화 프롬프트에 research 안내가 있고, 조사 프롬프트에 원래 질문이 간다
        assert "- research: 웹에서" in h.claude.requests[0].prompt
        work = h.codex.requests[0]
        assert QUESTION in work.prompt and "AI 유튜브 채널 분석" in work.prompt
        assert work.read_dirs == (h.vault,)
        check = h.claude.requests[1]
        assert "Iota" in check.prompt and "https://www.youtube.com/@a" in check.prompt

        first, second = h.chat.depts
        assert (first.author, first.mention) == ("theta", False)
        assert "에게 조사 보고를 맡긴다: AI 유튜브 채널 분석" in first.text
        assert (second.author, second.mention) == ("iota", True)
        path = f"조사/{h.day:%Y-%m}/{h.day.isoformat()}-AI-유튜브-채널-분석.md"
        assert f"볼트: `{path}` (출처 1개)" in second.text
        assert "결론: 세 채널이 앞선다" in second.text

        text = (h.vault / path).read_text(encoding="utf-8")
        assert text.startswith("# AI 유튜브 채널 분석\n")
        assert "확인 Eta" in text
        assert "- [채널 A](https://www.youtube.com/@a)" in text
        assert "## 확인하지 못함\n\n- 채널 B 조회수" in text
        title = f"docs(research): {h.day.isoformat()} AI 유튜브 채널 분석"
        assert h.git.commits == [([path], title, "ZeroSync-Department: research")]
        assert await runs(h) == [
            ("secretary", "theta", "dept_chat"),
            ("research", "iota", "research"),
            ("research", "eta", "lead_check"),
        ]
        rows = await h.conn.execute_fetchall("SELECT kind, payload FROM chat_turns")
        assert [row[0] for row in rows] == ["message", "dept_answer", "research"]
        assert json.loads(rows[-1][1])["path"] == path


def test_required_findings_get_one_revision():
    research = {"topic": "AI 채널", "agent": "iota"}

    @scenario(
        claude=[answer("맡깁니다", research=research), review(["구독자 숫자 근거"])],
        codex=[report(summary="첫 보고"), report(summary="고친 보고")],
    )
    async def _(h):
        await h.dept.on_message("secretary", QUESTION)
        revision = h.codex.requests[1].prompt
        assert "팀장 확인의 [필수] 지적" in revision
        assert "- 구독자 숫자 근거 — 출처를 붙인다" in revision
        assert "첫 보고" in revision  # 지난 보고를 함께 준다
        post = h.chat.depts[-1].text
        assert "고친 보고" in post and "첫 보고" not in post
        assert "[필수] 지적 1개를 반영했다" in post
        # 고친 뒤에는 다시 확인하지 않는다
        assert [stage for _, _, stage in await runs(h)].count("lead_check") == 1


def test_lead_does_not_check_own_research():
    # 기본 조직에는 데네브가 없어 처음 답한 비서팀장이 맡고, 팀장이라 확인을 건너뛴다
    @scenario(claude=[answer("직접 찾아보겠습니다", research={"topic": "AI 채널"}), report()])
    async def _(h):
        await h.dept.on_message("secretary", QUESTION)
        assert await runs(h) == [
            ("secretary", "theta", "dept_chat"),
            ("research", "theta", "research"),
        ]
        assert "→ **리서치팀**에게 조사 보고를 맡긴다" in h.chat.depts[0].text
        path = h.vault / f"조사/{h.day:%Y-%m}/{h.day.isoformat()}-AI-유튜브-채널-분석.md"
        assert "확인 없음 (팀장이 직접 조사)" in path.read_text(encoding="utf-8")


def test_unknown_research_agent_is_asked_again():
    @scenario(
        claude=[
            answer("맡깁니다", research={"topic": "AI 채널", "agent": "nobody"}),
            answer("볼트로 답합니다"),
        ]
    )
    async def _(h):
        await h.dept.on_message("secretary", QUESTION)
        assert "'nobody'는 조직에 없는 이름이다" in h.claude.requests[1].prompt
        assert [post.text for post in h.chat.depts] == ["볼트로 답합니다"]


def test_failed_research_posts_notice_and_alerts_ops():
    @scenario(
        claude=[answer("맡깁니다", research={"topic": "AI 채널", "agent": "iota"})],
        codex=[failed()],
    )
    async def _(h):
        await h.dept.on_message("secretary", QUESTION)
        notice = h.chat.depts[-1]
        assert notice.author == "bot" and "Iota" in notice.text
        assert any("조사 실행 실패" in text for text, _ in h.chat.ops)
        assert h.git.commits == []


def test_handed_off_answer_cannot_request_research():
    handoff = [{"agent": "beta", "question": "범위는?"}]

    @scenario(
        claude=[
            answer("넘깁니다", handoff=handoff),
            answer("범위", research={"topic": "AI"}),
            answer("범위"),
        ]
    )
    async def _(h):
        await h.dept.on_message("secretary", QUESTION)
        assert "research는 null로 둔다" in h.claude.requests[2].prompt
        assert h.chat.depts[-1].text == "범위"


def test_default_agent_is_deneb_when_hired():
    org = SimpleNamespace(agents={DEFAULT_AGENT: object()})
    assert research_agent(org, ResearchRequest(topic="AI"), "theta") == "deneb"
    picked = ResearchRequest(topic="AI", agent="denebola")
    assert research_agent(org, picked, "theta") == "denebola"
    empty = SimpleNamespace(agents={})
    assert research_agent(empty, ResearchRequest(topic="AI"), "theta") == "theta"


def test_report_path_makes_safe_unique_names():
    day = date(2026, 11, 23)
    assert report_path(day, "AI / 유튜브: 채널?") == "조사/2026-11/2026-11-23-AI-유튜브-채널.md"
    taken = {"조사/2026-11/2026-11-23-AI.md"}
    assert report_path(day, "AI", taken) == "조사/2026-11/2026-11-23-AI-2.md"
    assert report_path(day, "///") == "조사/2026-11/2026-11-23-조사.md"
