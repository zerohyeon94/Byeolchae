"""#비서·#교육 대화 (ADR-013).

Zero가 부서 채널에 쓴 메시지에 그 부서 팀장(또는 `@이름`으로 부른 직원)이 답한다.
답하는 직원은 읽기 전용으로 볼트와 제품 저장소를 읽고, 봇이 DB에서 모은 최근 작업 기록을 받는다.
대화에는 상태가 없다. 기록은 chat_turns에 부서 키로 남긴다.
"""

import json
import logging
from collections.abc import Mapping, Sequence
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
    ):
        self._engine = engine
        self._chat = chat
        self._repo = repo
        self._org = org
        self._depts = dict(depts)
        self._workdir = workdir  # 실행 폴더. 파일은 read_dirs로 읽는다
        self._folders = tuple(folders)

    async def on_message(self, dept: str, text: str, *, message_id: str | None = None) -> None:
        spec = self._depts[dept]
        history = await self._history(spec.key)
        await self._repo.add_chat_turn(spec.key, Actor.ZERO, "message", {"text": text}, message_id)
        agent, question = parse_target(self._org, text, spec.agent)
        records = await self._records()
        self._workdir.mkdir(parents=True, exist_ok=True)
        answer = await self._engine.ask_outside_task(
            spec.key,
            agent,
            Stage.DEPT_CHAT,
            lambda persona: dept_chat_prompt(
                persona,
                channel=spec.channel,
                question=question or text,
                folders=[(label, str(path)) for label, path in self._folders],
                records=records,
                history=history,
            ),
            model=DeptAnswer,
            cwd=self._workdir,
            read_dirs=[path for _, path in self._folders if path.is_dir()],
        )
        await self._publish(spec, answer, mention=should_mention(Moment.OPINIONS_READY))

    async def _publish(self, spec: Dept, answer: Any, *, mention: bool) -> None:
        name = self._org.display(answer.agent)
        if answer.result is not None:
            author, text = answer.agent, answer.result.answer
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
        message_id = await self._chat.dept_chat(spec.key, author, text, mention=mention)
        actor = Actor.BOT if author == "bot" else author
        await self._repo.add_chat_turn(spec.key, actor, kind, payload, message_id)

    async def _history(self, key: str) -> list[HistoryEntry]:
        entries = []
        for turn in await self._repo.recent_chat_turns(key, HISTORY):
            payload = turn["payload"]
            if turn["kind"] == "dept_answer":
                text = DeptAnswer.model_validate(payload["answer"]).answer
            elif turn["kind"] == "message":
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
