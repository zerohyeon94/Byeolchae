"""채팅 출력 인터페이스 (SPEC 6.1).

워크플로는 이 인터페이스만 알고, Discord 구현은 `bot/discord_io/`가 맡는다.
게시 이름(`[Beta · 기획팀장]` 등), 멘션 문자열, 1,900자 분할 같은 표시 세부는 구현 쪽 책임이다.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

# 게시 주체: 에이전트 이름(beta, alpha …) 또는 "bot". 에이전트는 웹훅 이름 `[이름 · 직책]`으로,
# 봇은 [별채]로 게시한다 (운영 규약 1.2, SPEC Q16)
Author = str


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: str


class ChatIO(Protocol):
    async def post(
        self,
        thread: str,
        author: Author,
        text: str,
        *,
        mention: bool = False,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        """게시글(스레드)에 메시지를 올리고 메시지 ID를 돌려준다.

        mention=True면 Zero를 멘션하고, False면 알림 없는(silent) 메시지로 보낸다 (운영 규약 2.5).
        """
        ...

    async def chat(
        self,
        project: str,
        author: Author,
        text: str,
        *,
        mention: bool = False,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        """제품 채팅 채널(`#<제품>-chat`)에 메시지를 올린다 (ADR-007)."""
        ...

    async def create_post(self, project: str, title: str, text: str) -> str:
        """제품 포럼에 봇이 새 포스트를 열고 스레드 ID를 돌려준다 (/task, 정기 테스트 보고)."""
        ...

    async def notify_ops(self, text: str, *, mention: bool = False) -> None:
        """#zerosync-ops 채널에 알린다."""
        ...

    async def report(self, text: str, *, mention: bool = False) -> None:
        """#대표실 채널에 보고한다 (아침·주간 보고, ADR-007)."""
        ...


@dataclass(frozen=True)
class Post:
    thread: str
    author: Author
    text: str
    mention: bool
    attachments: tuple[Attachment, ...]


@dataclass
class FakeChatIO:
    """테스트용 ChatIO. 게시와 운영 알림을 기록한다."""

    posts: list[Post] = field(default_factory=list)
    chats: list[Post] = field(default_factory=list)  # thread 자리에 제품 키
    created: list[tuple[str, str, str]] = field(default_factory=list)
    ops: list[tuple[str, bool]] = field(default_factory=list)
    reports: list[tuple[str, bool]] = field(default_factory=list)

    async def post(
        self,
        thread: str,
        author: Author,
        text: str,
        *,
        mention: bool = False,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        self.posts.append(Post(thread, author, text, mention, tuple(attachments)))
        return f"msg-{len(self.posts)}"

    async def chat(
        self,
        project: str,
        author: Author,
        text: str,
        *,
        mention: bool = False,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        self.chats.append(Post(project, author, text, mention, tuple(attachments)))
        return f"chat-{len(self.chats)}"

    async def create_post(self, project: str, title: str, text: str) -> str:
        self.created.append((project, title, text))
        return f"thread-{project}-{len(self.created)}"

    async def notify_ops(self, text: str, *, mention: bool = False) -> None:
        self.ops.append((text, mention))

    async def report(self, text: str, *, mention: bool = False) -> None:
        self.reports.append((text, mention))
