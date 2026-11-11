"""ChatIO의 Discord 구현 (SPEC 6.1).

- 모든 게시는 포럼 채널의 웹훅으로 보내고, 게시자 이름으로 주체를 구분한다 (운영 규약 1.2)
- 멘션은 Zero가 행동해야 할 때만 붙이고, 나머지는 알림 없는(silent) 메시지로 보낸다 (운영 규약 2.5)
- 긴 본문은 1,900자 단위로 나누고, 첨부 파일은 마지막 조각에 붙인다
"""

import io
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import discord

from bot.discord_io.rules import author_name, mention_text, split_text
from bot.org import DEFAULT_ORG, Org
from bot.workflow.chat import Attachment, Author


class WebhookLike(Protocol):
    async def send(self, content: str = ..., **kwargs: Any) -> Any: ...


class ChannelLike(Protocol):
    async def send(self, content: str | None = None, **kwargs: Any) -> Any: ...


class DiscordChat:
    def __init__(
        self,
        *,
        zero_user_id: int,
        webhook_for: Callable[[int], Awaitable[WebhookLike]],
        ops_channel: Callable[[], Awaitable[ChannelLike]],
        org: Org = DEFAULT_ORG,
        report_channel: Callable[[], Awaitable[ChannelLike]] | None = None,
        chat_webhook: Callable[[str], Awaitable[WebhookLike]] | None = None,
        forum_webhook: Callable[[str], Awaitable[WebhookLike]] | None = None,
    ):
        """webhook_for(thread_id)는 그 게시글이 속한 포럼 채널의 웹훅을 돌려준다."""
        self._zero = zero_user_id
        self._org = org
        # 제품 키 → 채팅 채널 웹훅, 포럼 채널 웹훅 (ADR-007)
        self._chat_webhook = chat_webhook
        self._forum_webhook = forum_webhook
        # #대표실이 없으면 운영 채널로 보고한다
        self._report_channel = report_channel or ops_channel
        self._webhook_for = webhook_for
        self._ops_channel = ops_channel

    def _mentions(self, mention: bool) -> discord.AllowedMentions:
        if mention:
            return discord.AllowedMentions(
                everyone=False, roles=False, users=[discord.Object(self._zero)]
            )
        return discord.AllowedMentions.none()

    def _content(self, text: str, mention: bool) -> list[str]:
        if mention:
            text = f"{mention_text(self._zero)}\n{text}"
        return split_text(text)

    async def post(
        self,
        thread: str,
        author: Author,
        text: str,
        *,
        mention: bool = False,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        thread_id = int(thread)
        webhook = await self._webhook_for(thread_id)
        chunks = self._content(text, mention)
        first_id: str | None = None
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            files = [_file(a) for a in attachments] if last else []
            message = await webhook.send(
                chunk,
                username=author_name(self._org, author),
                thread=discord.Object(thread_id),
                allowed_mentions=self._mentions(mention and index == 0),
                silent=not mention,
                files=files,
                wait=True,
            )
            if first_id is None:
                first_id = str(message.id)
        assert first_id is not None
        return first_id

    async def chat(
        self,
        project: str,
        author: Author,
        text: str,
        *,
        mention: bool = False,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._chat_webhook is None:
            raise RuntimeError("제품 채팅 채널이 설정되지 않았다")
        webhook = await self._chat_webhook(project)
        chunks = self._content(text, mention)
        first_id: str | None = None
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            message = await webhook.send(
                chunk,
                username=author_name(self._org, author),
                allowed_mentions=self._mentions(mention and index == 0),
                silent=not mention,
                files=[_file(a) for a in attachments] if last else [],
                wait=True,
            )
            if first_id is None:
                first_id = str(message.id)
        assert first_id is not None
        return first_id

    async def create_post(self, project: str, title: str, text: str) -> str:
        """포럼에 [별채] 이름으로 포스트를 연다. 첫 조각이 포스트 본문이다."""
        if self._forum_webhook is None:
            raise RuntimeError("포럼 웹훅이 없다")
        webhook = await self._forum_webhook(project)
        chunks = split_text(text)
        message = await webhook.send(
            chunks[0],
            username=author_name(self._org, "bot"),
            thread_name=title[:100],
            allowed_mentions=discord.AllowedMentions.none(),
            silent=True,
            wait=True,
        )
        thread_id = str(message.channel.id)
        for chunk in chunks[1:]:
            await webhook.send(
                chunk,
                username=author_name(self._org, "bot"),
                thread=discord.Object(int(thread_id)),
                silent=True,
                wait=True,
            )
        return thread_id

    async def notify_ops(self, text: str, *, mention: bool = False) -> None:
        await self._send(await self._ops_channel(), text, mention)

    async def report(self, text: str, *, mention: bool = False) -> None:
        await self._send(await self._report_channel(), text, mention)

    async def report_target(self) -> ChannelLike:
        """#대표실(없으면 운영 채널). 교육 알림이 #교육 없이 쓸 곳이다."""
        return await self._report_channel()

    async def send(
        self, channel: ChannelLike, text: str, *, mention: bool = False, files: Sequence[Path] = ()
    ) -> None:
        """설정에 등록된 다른 채널(#교육 등)에 봇 이름으로 보낸다. 파일은 마지막 조각에 붙인다."""
        await self._send(channel, text, mention, files)

    async def _send(
        self, channel: ChannelLike, text: str, mention: bool, files: Sequence[Path] = ()
    ) -> None:
        chunks = self._content(text, mention)
        for index, chunk in enumerate(chunks):
            extra: dict[str, Any] = {}
            if files and index == len(chunks) - 1:
                extra["files"] = [discord.File(path) for path in files]
            await channel.send(
                chunk,
                allowed_mentions=self._mentions(mention and index == 0),
                silent=not mention,
                **extra,
            )


def _file(attachment: Attachment) -> discord.File:
    return discord.File(io.BytesIO(attachment.content.encode("utf-8")), attachment.filename)
