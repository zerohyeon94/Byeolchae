"""조사 보고 (ADR-018).

부서 대화(#비서·#교육)의 답에 research가 있으면 담당 직원이 웹으로 조사해 보고를 낸다.
담당이 팀원이면 그 팀 팀장이 한 번 확인하고, [필수] 지적이 있으면 담당이 한 번 고친다.
보고는 봇이 비서 볼트의 `조사/YYYY-MM/`에 쓰고 커밋한 뒤, 대화가 있던 곳에 요약을 올린다.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.agents.base import AgentName, RunStatus, Stage
from bot.clock import Clock
from bot.org import Org
from bot.prompts import research_check_prompt, research_prompt
from bot.render import render_notice
from bot.render.research import FOLDER, report_file, report_path, report_post
from bot.schemas import ResearchReport, ResearchRequest, Review
from bot.workflow.chat import ChatIO
from bot.workflow.mentions import Moment, should_mention
from bot.workflow.state import NoticeCode

log = logging.getLogger(__name__)

SCOPE = "research"  # 실행 기록의 project
DEFAULT_AGENT = "deneb"  # 담당을 정하지 않으면 리서치팀 데네브 (2026-11-23 Zero 결정)
COMMIT_TRAILER = "ZeroSync-Department: research"


def research_agent(org: Org, request: ResearchRequest, fallback: AgentName) -> AgentName:
    """정한 담당, 없으면 기본 담당. 기본 담당이 조직에 없으면 처음 답한 직원이 맡는다."""
    if request.agent:
        return request.agent
    return DEFAULT_AGENT if DEFAULT_AGENT in org.agents else fallback


@dataclass(frozen=True)
class ResearchOutcome:
    author: str  # 게시자. 실행 실패면 bot
    text: str
    kind: str  # chat_turns 기록 종류 (research, error)
    payload: dict[str, Any] = field(default_factory=dict)


class ResearchDesk:
    def __init__(
        self,
        engine: Any,
        vault_git: Any,
        vault: Path,
        org: Org,
        clock: Clock,
        chat: ChatIO,
        *,
        workdir: Path,
        folders: Sequence[tuple[str, Path]] = (),
    ):
        self._engine = engine
        self._vault_git = vault_git
        self._vault = vault
        self._org = org
        self._clock = clock
        self._chat = chat
        self._workdir = workdir
        self._folders = tuple(folders)

    async def run(
        self, *, channel: str, agent: AgentName, topic: str, question: str, requester: AgentName
    ) -> ResearchOutcome:
        day = self._clock.now().astimezone().date()
        name = self._org.display(agent)

        def build(previous: ResearchReport | None = None, required: str = "") -> Any:
            return lambda persona: research_prompt(
                persona,
                channel=channel,
                day=day.isoformat(),
                requester=self._org.display(requester),
                topic=topic,
                question=question,
                folders=[(label, str(path)) for label, path in self._folders],
                previous=previous,
                required=required,
            )

        first = await self._report(agent, build())
        if first.result is None:
            return await self._failed(channel, first)
        report, note = first.result, ""
        lead = self._lead(agent)
        checker = self._org.display(lead) if lead else "없음 (팀장이 직접 조사)"
        if lead is not None:
            check = await self._engine.ask_outside_task(
                SCOPE,
                lead,
                Stage.LEAD_CHECK,
                lambda persona: research_check_prompt(
                    persona, member=name, topic=topic, question=question, report=report
                ),
                model=Review,
                cwd=self._prepared_workdir(),
            )
            if check.result is None:
                reason = check.failure if check.failed_to_run else "형식 오류"
                note = f"{checker}가 확인하지 못해 확인 없이 올린다 ({reason})"
            elif required := check.result.required_findings:
                lines = "\n".join(f"- {item.problem} — {item.suggestion}" for item in required)
                second = await self._report(agent, build(report, lines))
                if second.result is None:
                    note = (
                        f"{checker}의 [필수] 지적 {len(required)}개를 반영하지 못해 "
                        "첫 보고를 올린다"
                    )
                else:
                    report = second.result
                    note = f"{checker}의 [필수] 지적 {len(required)}개를 반영했다"
        path = await self._save(day, report, topic=topic, author=name, checker=checker, note=note)
        text = report_post(report, path=path, check_note=note)
        payload = {
            "agent": agent,
            "topic": topic,
            "path": path,
            "report": report.model_dump(mode="json"),
            "text": text,
        }
        return ResearchOutcome(agent, text, "research", payload)

    def _lead(self, agent: AgentName) -> AgentName | None:
        """담당 팀의 팀장. 담당이 팀장이면 확인하지 않는다."""
        spec = self._org.agents.get(agent)
        if spec is None:
            return None
        lead = self._org.teams[spec.team].lead
        return lead if lead != agent and lead in self._org.agents else None

    def _prepared_workdir(self) -> Path:
        self._workdir.mkdir(parents=True, exist_ok=True)
        return self._workdir

    async def _report(self, agent: AgentName, build: Any) -> Any:
        return await self._engine.ask_outside_task(
            SCOPE,
            agent,
            Stage.RESEARCH,
            build,
            model=ResearchReport,
            cwd=self._prepared_workdir(),
            read_dirs=[path for _, path in self._folders if path.is_dir()],
        )

    async def _save(
        self, day: Any, report: ResearchReport, *, topic: str, author: str, checker: str, note: str
    ) -> str:
        folder = self._vault / FOLDER / f"{day:%Y-%m}"
        taken = {p.relative_to(self._vault).as_posix() for p in folder.glob("*.md")}
        path = report_path(day, report.title, taken)
        target = self._vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        text = report_file(
            report, day=day, topic=topic, author=author, checker=checker, check_note=note
        )
        target.write_text(text, encoding="utf-8")
        title = f"docs(research): {day.isoformat()} {report.title}"
        try:
            await self._vault_git.commit(self._vault, [path], title, COMMIT_TRAILER)
        except Exception as error:
            # 파일은 이미 썼다. 커밋만 못 한 것은 알리고 넘어간다
            log.exception("조사 보고 커밋 실패")
            await self._chat.notify_ops(
                f"[조사] 보고 파일 `{path}`를 썼지만 볼트 커밋에 실패했다: {error}",
                mention=should_mention(Moment.BOT_ERROR),
            )
        return path

    async def _failed(self, channel: str, answer: Any) -> ResearchOutcome:
        name = self._org.display(answer.agent)
        if not answer.failed_to_run:
            reason = answer.error.strip().splitlines()[0] if answer.error.strip() else "형식 오류"
            text = f"**형식 오류** — 조사 보고가 형식에 맞지 않았다 ({reason})"
            payload = {"reason": "invalid_output", "error": answer.error}
            return ResearchOutcome(answer.agent, text, "error", payload)
        if answer.status is not RunStatus.CANCELLED and not answer.blocked:
            await self._chat.notify_ops(
                f"[{channel}] {name} 조사 실행 실패: {answer.failure}. 로그: {answer.log_path}",
                mention=should_mention(Moment.BOT_ERROR),
            )
        text = render_notice(NoticeCode.AGENT_FAILED, f"[{name}] {answer.failure}")
        payload = {"agent": answer.agent, "status": answer.failure}
        return ResearchOutcome("bot", text, "error", payload)
