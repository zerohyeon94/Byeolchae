"""#대표실 대장 셋의 라운드 대화 (ADR-019).

대표실 팀(`org.yaml`의 `office`)의 대장 셋이 Zero의 글에 답한다.
1. 첫 답: 셋이 서로의 답을 보지 않고 동시에 답한다 (같은 질문 비교)
2. 서로 대화: 첫 답을 낸 대장이 다른 대장의 첫 답을 읽고 동의, 반박, 보완을 말한다
3. 정리: 팀장(형식상)이 합의한 점, 갈린 점, Zero가 정할 것을 정리한다
`@이름`으로 한 대장만 부르면 그 대장만 답한다. 더 듣고 싶으면 Zero가 다시 쓰면 된다.
새 대장은 봇이 시작할 때 한 번 인사한다. 대화에는 상태가 없고 기록은 chat_turns에 남긴다.
"""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bot.agents.base import AgentName, RunStatus, Stage
from bot.org import Org
from bot.prompts import HistoryEntry, office_prompt
from bot.render import render_notice
from bot.schemas import OfficeAnswer
from bot.store import Actor, Repository
from bot.workflow.chat import ChatIO
from bot.workflow.dept_chat import parse_target
from bot.workflow.mentions import Moment, should_mention
from bot.workflow.state import NoticeCode

log = logging.getLogger(__name__)

TEAM = "office"  # org.yaml 팀 키이자 부서 채널 키
CHANNEL = "대표실"
GREETINGS = "office:greeting"  # 인사 기록 키. 대화 기록과 따로 둔다
HISTORY = 24  # 최근 대화 수. 라운드 대화 한 번이 최대 8개(Zero 글 포함)라 세 번 치


def office_members(org: Org) -> list[AgentName]:
    """대표실 대장. 팀장이 먼저, 나머지는 org.yaml 순서."""
    team = org.teams.get(TEAM)
    if team is None:
        return []
    rest = [key for key, spec in org.agents.items() if spec.team == TEAM and key != team.lead]
    return [team.lead, *rest]


class OfficeRoom:
    def __init__(
        self,
        engine: Any,
        chat: ChatIO,
        repo: Repository,
        org: Org,
        *,
        workdir: Path,
        folders: Sequence[tuple[str, Path]],
    ):
        self._engine = engine
        self._chat = chat
        self._repo = repo
        self._org = org
        self._members = office_members(org)
        self._workdir = workdir
        self._folders = tuple(folders)

    @property
    def members(self) -> list[AgentName]:
        return list(self._members)

    async def on_message(
        self, text: str, *, message_id: str | None = None, thread: str | None = None
    ) -> None:
        """thread가 있으면 #대표실 스레드 안 메시지다. 답도 그 스레드에 올린다."""
        key = TEAM if thread is None else f"{TEAM}:{thread}"
        history = await self._history(key)
        await self._repo.add_chat_turn(key, Actor.ZERO, "message", {"text": text}, message_id)
        target, question = parse_target(self._org, text, "")
        question = question or text
        if target in self._members:
            answer = await self._ask(target, "single", question, history)
            await self._publish(key, thread, answer, "single", mention=True)
            return

        first = await asyncio.gather(
            *(self._ask(agent, "answer", question, history) for agent in self._members)
        )
        said = []
        for answer in first:
            if await self._publish(key, thread, answer, "answer", mention=False):
                said.append(HistoryEntry(self._org.display(answer.agent), answer.result.answer))
        if not said:
            text = "대장 셋 모두 답하지 못했다."
            await self._post(key, thread, "bot", text, "error", {}, mention=True)
            return

        if len(said) > 1:
            speakers = [answer.agent for answer in first if answer.result is not None]
            replies = await asyncio.gather(
                *(
                    self._ask(
                        agent,
                        "reply",
                        question,
                        history,
                        others=[e for e in said if e.speaker != self._org.display(agent)],
                    )
                    for agent in speakers
                )
            )
            for answer in replies:
                if await self._publish(key, thread, answer, "reply", mention=False):
                    said.append(HistoryEntry(self._org.display(answer.agent), answer.result.answer))

        summary = await self._ask(self._members[0], "summary", question, history, others=said)
        await self._publish(key, thread, summary, "summary", mention=True)

    async def greet(self) -> None:
        """아직 인사하지 않은 대장이 #대표실에 한 번 인사한다. 실패하면 다음 시작 때 다시 한다."""
        turns = await self._repo.recent_chat_turns(GREETINGS, 100)
        greeted = {turn["actor"] for turn in turns if turn["kind"] == "greeting"}
        said: list[HistoryEntry] = []
        for agent in self._members:
            if agent in greeted:
                continue
            answer = await self._ask(agent, "greeting", "", [], others=said)
            if answer.result is None:
                log.warning("대표실 인사 실패 (%s): %s", agent, answer.failure or answer.error)
                continue
            text = answer.result.answer
            message_id = await self._chat.dept_chat(TEAM, agent, text)
            await self._repo.add_chat_turn(GREETINGS, agent, "greeting", {"text": text}, message_id)
            said.append(HistoryEntry(self._org.display(agent), text))

    async def _ask(
        self,
        agent: AgentName,
        step: str,
        question: str,
        history: Sequence[HistoryEntry],
        *,
        others: Sequence[HistoryEntry] = (),
    ) -> Any:
        self._workdir.mkdir(parents=True, exist_ok=True)
        members = [self._org.display(member) for member in self._members]
        return await self._engine.ask_outside_task(
            TEAM,
            agent,
            Stage.DEPT_CHAT,
            lambda persona: office_prompt(
                persona,
                step=step,
                members=members,
                question=question,
                folders=[(label, str(path)) for label, path in self._folders],
                history=history,
                others=others,
            ),
            model=OfficeAnswer,
            cwd=self._workdir,
            read_dirs=[path for _, path in self._folders if path.is_dir()],
        )

    async def _publish(
        self, key: str, thread: str | None, answer: Any, step: str, *, mention: bool
    ) -> bool:
        """답을 올린다. 답을 냈으면 True, 형식 오류나 실행 실패를 알렸으면 False."""
        if answer.result is not None:
            text = answer.result.answer
            payload = {"step": step, "text": text}
            await self._post(key, thread, answer.agent, text, "office", payload, mention=mention)
            return True
        if not answer.failed_to_run:
            reason = answer.error.strip().splitlines()[0] if answer.error.strip() else "형식 오류"
            text = f"**형식 오류** — 답이 형식에 맞지 않았다 ({reason})"
            payload = {"reason": "invalid_output", "error": answer.error}
            await self._post(key, thread, answer.agent, text, "error", payload, mention=mention)
            return False
        name = self._org.display(answer.agent)
        text = render_notice(NoticeCode.AGENT_FAILED, f"[{name}] {answer.failure}")
        payload = {"agent": answer.agent, "status": answer.failure}
        if answer.status is not RunStatus.CANCELLED and not answer.blocked:
            await self._chat.notify_ops(
                f"[{CHANNEL}] {name} 대화 실행 실패: {answer.failure}. 로그: {answer.log_path}",
                mention=should_mention(Moment.BOT_ERROR),
            )
        await self._post(key, thread, "bot", text, "error", payload, mention=mention)
        return False

    async def _post(
        self,
        key: str,
        thread: str | None,
        author: str,
        text: str,
        kind: str,
        payload: dict[str, Any],
        *,
        mention: bool,
    ) -> None:
        if thread is None:
            message_id = await self._chat.dept_chat(TEAM, author, text, mention=mention)
        else:
            message_id = await self._chat.post(thread, author, text, mention=mention)
        actor = Actor.BOT if author == "bot" else author
        await self._repo.add_chat_turn(key, actor, kind, payload, message_id)

    async def _history(self, key: str) -> list[HistoryEntry]:
        return [
            HistoryEntry(self._org.display(turn["actor"]), str(turn["payload"]["text"]))
            for turn in await self._repo.recent_chat_turns(key, HISTORY)
            if turn["kind"] in ("message", "office")
        ]
