"""#비서·#교육 대화 (ADR-013).

Zero가 부서 채널에 쓴 메시지에 그 부서 팀장(또는 `@이름`으로 부른 직원)이 답한다.
답하는 직원은 읽기 전용으로 볼트와 제품 저장소를 읽고, 봇이 DB에서 모은 최근 작업 기록을 받는다.
다른 팀장이 답할 질문이면 답의 handoff대로 그 팀장을 바로 불러 답을 잇는다. 한 번만 넘긴다.
웹에서 새로 찾아봐야 하는 일이면 답의 research대로 조사 보고를 맡긴다 (ADR-018).
대화에는 상태가 없다. 기록은 chat_turns에 부서 키로 남긴다.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.agents.base import AgentName, RunStatus, Stage
from bot.org import Org
from bot.prompts import HistoryEntry, dept_chat_prompt
from bot.render import render_notice
from bot.schemas import DeptAnswer
from bot.store import Actor, EventKind, Repository
from bot.workflow.chat import ChatIO
from bot.workflow.mentions import Moment, should_mention
from bot.workflow.research import ResearchDesk, research_agent
from bot.workflow.state import NoticeCode

log = logging.getLogger(__name__)

# 답에 넣는 최근 대화 수 (제품 채팅과 같다)
HISTORY = 20
# 프롬프트에 넣는 최근 작업과 작업마다의 결정 이벤트 수
RECENT_TASKS = 10
TASK_EVENTS = 5
EVENT_CHARS = 300
_DECISION_EVENTS = {
    EventKind.DECISION,
    EventKind.PLAN,
    EventKind.UI_DESIGN,
    EventKind.DESIGN,
    EventKind.DIAGNOSIS,
    EventKind.REVIEW,
    EventKind.COMMAND,
    EventKind.MERGE,
    EventKind.RELEASE,
}


@dataclass(frozen=True)
class Dept:
    key: str  # 기록 키 (secretary, education)
    channel: str  # 표시용 채널 이름 (비서, 교육)
    agent: AgentName  # 기본으로 답하는 팀장


def parse_target(org: Org, text: str, default: AgentName) -> tuple[AgentName, str]:
    """`@이름`으로 시작하면 그 직원, 아니면 부서 팀장이 답한다. 제품 채팅과 같은 규칙."""
    stripped = text.lstrip()
    for agent in org.agent_names:
        prefix = f"@{agent}"
        if stripped.lower().startswith(prefix):
            rest = stripped[len(prefix) :]
            if not rest or rest[0].isspace():
                return agent, rest.strip()
    return default, text.strip()


def render_answer(org: Org, answer: DeptAnswer) -> str:
    """답 본문과 넘긴 사람·질문. 게시와 다음 대화 맥락에 같이 쓴다."""
    lines = [answer.answer]
    if answer.handoff:
        lines.append("")
        lines += [
            f"→ **{org.display(item.agent)}**에게 넘긴다: {item.question}"
            for item in answer.handoff
        ]
    if answer.research is not None:
        who = org.display(answer.research.agent) if answer.research.agent else "리서치팀"
        lines += ["", f"→ **{who}**에게 조사 보고를 맡긴다: {answer.research.topic}"]
    return "\n".join(lines)


async def _no_handoff(answer: DeptAnswer) -> list[str]:
    """넘겨받은 직원은 다시 넘기지 않는다 (호출이 꼬리를 무는 것 방지)."""
    problems = []
    if answer.handoff:
        problems.append("넘겨받은 질문은 다시 넘기지 않는다. handoff는 빈 목록으로 둔다")
    if answer.research is not None:
        problems.append("넘겨받은 질문에서는 조사를 맡기지 않는다. research는 null로 둔다")
    return problems


@dataclass(frozen=True)
class _Place:
    """답이 올라갈 곳. 스레드면 기록 키도 스레드마다 따로 둔다."""

    spec: Dept
    thread: str | None

    @property
    def key(self) -> str:
        return self.spec.key if self.thread is None else f"{self.spec.key}:{self.thread}"


class DeptChat:
    def __init__(
        self,
        engine: Any,
        chat: ChatIO,
        repo: Repository,
        org: Org,
        depts: Mapping[str, Dept],
        *,
        workdir: Path,
        folders: Sequence[tuple[str, Path]],
        research: ResearchDesk | None = None,
    ):
        self._engine = engine
        self._chat = chat
        self._repo = repo
        self._org = org
        self._depts = dict(depts)
        self._workdir = workdir  # 실행 폴더. 파일은 read_dirs로 읽는다
        self._folders = tuple(folders)
        self._research = research  # 없으면 조사 보고를 맡기지 않는다

    async def on_message(
        self,
        dept: str,
        text: str,
        *,
        message_id: str | None = None,
        thread: str | None = None,
    ) -> None:
        """thread가 있으면 그 부서 채널의 스레드(회의) 안 메시지다."""
        spec = self._depts[dept]
        place = _Place(spec, thread)
        history = await self._history(place.key)
        agent, question = parse_target(self._org, text, spec.agent)
        question = question or text
        records = await self._records()
        candidates = [
            (key, self._org.display(key)) for key in self._org.agent_names if key != agent
        ]
        answer = await self._ask(
            spec,
            agent,
            question,
            history,
            records,
            verify=self._answer_problems(agent),
            candidates=candidates,
            in_thread=thread is not None,
        )
        title = answer.result.thread_title if answer.result is not None else None
        if title and thread is None and message_id is not None:
            place = await self._open_thread(spec, message_id, title)
        # Zero 메시지는 답이 올라갈 곳(채널 또는 새 스레드)의 기록에 남긴다
        await self._repo.add_chat_turn(place.key, Actor.ZERO, "message", {"text": text}, message_id)
        handoffs = answer.result.handoff if answer.result is not None else []
        request = answer.result.research if answer.result is not None else None
        last = should_mention(Moment.OPINIONS_READY)
        await self._publish(place, answer, mention=last and not handoffs and request is None)
        if handoffs:
            # 조사 보고가 뒤따르면 Zero 호출은 보고를 올릴 때 한다
            mention = last and request is None
            await self._hand_off(
                place, agent, question, history, records, answer, handoffs, mention
            )
        if request is not None and self._research is not None:
            outcome = await self._research.run(
                channel=spec.channel,
                agent=research_agent(self._org, request, agent),
                topic=request.topic,
                question=question,
                requester=agent,
            )
            await self._post(
                place, outcome.author, outcome.text, outcome.kind, outcome.payload, last
            )

    async def _hand_off(
        self,
        place: "_Place",
        agent: AgentName,
        question: str,
        history: Sequence[HistoryEntry],
        records: str,
        answer: Any,
        handoffs: Sequence[Any],
        last: bool,
    ) -> None:
        spec = place.spec
        # 넘겨받은 직원은 서로의 답을 보지 않고 독립 작성한다 (운영 규약 1.6)
        passer = self._org.display(agent)
        context = [
            *history,
            HistoryEntry("Zero", question),
            HistoryEntry(passer, answer.result.answer),
        ]
        answers = await asyncio.gather(
            *(
                self._ask(
                    spec,
                    item.agent,
                    item.question,
                    context,
                    records,
                    verify=_no_handoff,
                    handoff_from=passer,
                    original=question,
                    in_thread=True,
                )
                for item in handoffs
            )
        )
        for index, reply in enumerate(answers):
            await self._publish(place, reply, mention=last and index == len(answers) - 1)

    async def _open_thread(self, spec: Dept, message_id: str, title: str) -> "_Place":
        """Zero 메시지에 스레드를 연다. 못 열면 채널에 그대로 답하고 운영 채널에 알린다."""
        try:
            thread = await self._chat.open_thread(spec.key, message_id, title)
        except Exception as error:
            log.exception("부서 스레드 열기 실패 (%s)", spec.key)
            await self._chat.notify_ops(
                f"[{spec.channel}] 회의 스레드를 열지 못해 채널에 답한다: {error}",
                mention=should_mention(Moment.BOT_ERROR),
            )
            return _Place(spec, None)
        return _Place(spec, thread)

    async def _ask(
        self,
        spec: Dept,
        agent: AgentName,
        question: str,
        history: Sequence[HistoryEntry],
        records: str,
        *,
        verify: Callable[[DeptAnswer], Awaitable[list[str]]],
        **prompt: Any,
    ) -> Any:
        self._workdir.mkdir(parents=True, exist_ok=True)
        return await self._engine.ask_outside_task(
            spec.key,
            agent,
            Stage.DEPT_CHAT,
            lambda persona: dept_chat_prompt(
                persona,
                channel=spec.channel,
                question=question,
                folders=[(label, str(path)) for label, path in self._folders],
                records=records,
                history=history,
                research=self._research is not None,
                **prompt,
            ),
            model=DeptAnswer,
            cwd=self._workdir,
            verify=verify,
            read_dirs=[path for _, path in self._folders if path.is_dir()],
        )

    def _answer_problems(self, agent: AgentName) -> Callable[[DeptAnswer], Awaitable[list[str]]]:
        """넘길 담당자가 조직에 있고, 자기 자신이 아니며, 겹치지 않는지 (ADR-013).
        조사를 맡기면 담당이 조직에 있는지도 본다 (ADR-018).
        """

        async def check(answer: DeptAnswer) -> list[str]:
            problems = []
            seen: set[str] = set()
            for item in answer.handoff:
                if item.agent not in self._org.agents:
                    problems.append(f"handoff의 {item.agent!r}는 조직에 없는 이름이다")
                elif item.agent == agent:
                    problems.append("handoff에 자기 자신을 넣지 않는다")
                elif item.agent in seen:
                    problems.append(f"handoff에 {item.agent!r}가 두 번 있다")
                seen.add(item.agent)
            if answer.research is not None:
                if self._research is None:
                    problems.append("조사 보고를 맡길 수 없는 설정이다. research는 null로 둔다")
                elif answer.research.agent and answer.research.agent not in self._org.agents:
                    problems.append(
                        f"research.agent {answer.research.agent!r}는 조직에 없는 이름이다"
                    )
            return problems

        return check

    async def _publish(self, place: "_Place", answer: Any, *, mention: bool) -> None:
        spec = place.spec
        name = self._org.display(answer.agent)
        if answer.result is not None:
            author, text = answer.agent, render_answer(self._org, answer.result)
            kind, payload = "dept_answer", {"answer": answer.result.model_dump(mode="json")}
        elif not answer.failed_to_run:
            reason = answer.error.strip().splitlines()[0] if answer.error.strip() else "형식 오류"
            author, text = answer.agent, f"**형식 오류** — 답이 형식에 맞지 않았다 ({reason})"
            kind, payload = "error", {"reason": "invalid_output", "error": answer.error}
        else:
            author = "bot"
            text = render_notice(NoticeCode.AGENT_FAILED, f"[{name}] {answer.failure}")
            kind, payload = "error", {"agent": answer.agent, "status": answer.failure}
            if answer.status is not RunStatus.CANCELLED and not answer.blocked:
                await self._chat.notify_ops(
                    f"[{spec.channel}] {name} 대화 실행 실패: {answer.failure}. "
                    f"로그: {answer.log_path}",
                    mention=should_mention(Moment.BOT_ERROR),
                )
        await self._post(place, author, text, kind, payload, mention)

    async def _post(
        self,
        place: "_Place",
        author: str,
        text: str,
        kind: str,
        payload: dict[str, Any],
        mention: bool,
    ) -> None:
        if place.thread is None:
            message_id = await self._chat.dept_chat(place.spec.key, author, text, mention=mention)
        else:
            message_id = await self._chat.post(place.thread, author, text, mention=mention)
        actor = Actor.BOT if author == "bot" else author
        await self._repo.add_chat_turn(place.key, actor, kind, payload, message_id)

    async def _history(self, key: str) -> list[HistoryEntry]:
        entries = []
        for turn in await self._repo.recent_chat_turns(key, HISTORY):
            payload = turn["payload"]
            if turn["kind"] == "dept_answer":
                text = render_answer(self._org, DeptAnswer.model_validate(payload["answer"]))
            elif turn["kind"] in ("message", "research"):
                text = str(payload.get("text", ""))
            else:
                continue
            entries.append(HistoryEntry(self._org.display(turn["actor"]), text))
        return entries

    async def _records(self) -> str:
        """최근에 바뀐 작업과 그 결정 이벤트. 비서가 '어떻게 결정됐나'에 답할 근거다."""
        tasks = sorted(await self._repo.list_tasks(), key=lambda t: t.updated_at, reverse=True)
        blocks = []
        for task in tasks[:RECENT_TASKS]:
            links = ", ".join(
                f"{label} #{number}"
                for label, number in (("Issue", task.issue_number), ("PR", task.pr_number))
                if number
            )
            lines = [
                f"### 작업 {task.id} [{task.project}] {task.title}",
                f"- 상태: {task.state.value}, 갱신 {task.updated_at[:16]}",
            ]
            if links:
                lines.append(f"- {links}")
            events = await self._repo.list_events(task.id, _DECISION_EVENTS)
            for event in events[-TASK_EVENTS:]:
                body = json.dumps(event.payload, ensure_ascii=False)
                if len(body) > EVENT_CHARS:
                    body = body[:EVENT_CHARS] + " ..."
                who = self._org.display(str(event.actor))
                lines.append(f"- {event.created_at[:16]} {who} {event.kind.value}: {body}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
