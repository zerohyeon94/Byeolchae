"""Discord 계층 테스트. 실제 Discord에는 연결하지 않는다 (SPEC 12.3).

규칙은 순수 함수로, DiscordChat은 가짜 웹훅으로, 봇의 입력 분기는 가짜 메시지·인터랙션으로 확인한다.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.config import load_config
from bot.discord_io.chat import DiscordChat
from bot.discord_io.client import POST_HINT, ZeroSyncBot, project_contexts
from bot.discord_io.rules import (
    IncomingMessage,
    MessageKind,
    author_name,
    chat_command_error,
    classify,
    command_error,
    split_text,
)
from bot.org import DEFAULT_ORG
from bot.render.status import render_ping, render_status
from bot.store.repo import TaskRecord
from bot.workflow.chat import Attachment, ThreadNotFound
from bot.workflow.state import CommandName, TaskState

ZERO = 42
FORUM = 100
OPS = 300
CHAT = 600
DEPT = 800


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("old_name", ["ZeroSync", "별채"])
def test_reuses_owned_webhook_after_rename(old_name):
    owner = SimpleNamespace(id=42)
    other = SimpleNamespace(user=SimpleNamespace(id=7), name="별채")
    own = SimpleNamespace(user=owner, name=old_name)
    channel = SimpleNamespace(
        webhooks=AsyncMock(return_value=[other, own]), create_webhook=AsyncMock()
    )
    assert run(ZeroSyncBot._own_webhook(SimpleNamespace(user=owner), channel, "test")) is own
    channel.create_webhook.assert_not_awaited()


# 규칙


def message(**overrides):
    data = {
        "message_id": 501,
        "author_id": ZERO,
        "from_bot": False,
        "channel_id": 500,
        "parent_id": FORUM,
    }
    data.update(overrides)
    return IncomingMessage(**data)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, MessageKind.REPLY),
        ({"message_id": 500}, MessageKind.POST),  # 첫 메시지 ID == 스레드 ID
        ({"author_id": 7}, MessageKind.IGNORE),  # Zero가 아님
        ({"from_bot": True}, MessageKind.IGNORE),  # 봇·웹훅 (자기 게시 반복 방지)
        ({"parent_id": 999}, MessageKind.IGNORE),  # 등록되지 않은 포럼
        ({"parent_id": None}, MessageKind.IGNORE),  # 스레드가 아닌 채널
        ({"parent_id": None, "channel_id": CHAT}, MessageKind.CHAT),  # 제품 채팅 채널
        ({"parent_id": None, "channel_id": CHAT, "author_id": 7}, MessageKind.IGNORE),
        ({"parent_id": None, "channel_id": DEPT}, MessageKind.DEPT),  # #비서·#교육 (ADR-013)
        ({"parent_id": None, "channel_id": DEPT, "author_id": 7}, MessageKind.IGNORE),
        ({"parent_id": None, "channel_id": DEPT, "from_bot": True}, MessageKind.IGNORE),
        ({"parent_id": DEPT, "channel_id": 801}, MessageKind.DEPT),  # 부서 채널의 회의 스레드
    ],
)
def test_classify(overrides, expected):
    kind = classify(
        message(**overrides),
        zero_user_id=ZERO,
        forum_ids={FORUM},
        chat_ids={CHAT},
        dept_ids={DEPT},
    )
    assert kind is expected


def test_command_error():
    ok = {"zero_user_id": ZERO, "forum_ids": {FORUM}}
    assert command_error(user_id=ZERO, parent_id=FORUM, **ok) is None
    assert "Zero만" in command_error(user_id=7, parent_id=FORUM, **ok)
    assert "포스트 안에서만" in command_error(user_id=ZERO, parent_id=None, **ok)
    assert "포스트 안에서만" in command_error(user_id=ZERO, parent_id=999, **ok)


def test_chat_command_error():
    ok = {"zero_user_id": ZERO, "chat_ids": {CHAT}}
    assert chat_command_error(user_id=ZERO, channel_id=CHAT, **ok) is None
    assert "Zero만" in chat_command_error(user_id=7, channel_id=CHAT, **ok)
    assert "채팅 채널에서만" in chat_command_error(user_id=ZERO, channel_id=FORUM, **ok)


def test_split_text():
    assert split_text("짧다") == ["짧다"]
    assert split_text("") == [""]
    assert split_text("가" * 10, limit=4) == ["가가가가", "가가가가", "가가"]
    # 가능하면 줄바꿈에서 자른다
    assert split_text("aaa\nbbb\nccc", limit=8) == ["aaa\nbbb", "ccc"]
    chunks = split_text("x" * 5000)
    assert [len(c) for c in chunks] == [1900, 1900, 1200]
    with pytest.raises(ValueError):
        split_text("x", limit=0)


def test_author_names_follow_operating_rules():
    # 표시 이름은 `[이름 · 직책]` (운영 규약 1.2, SPEC Q16). 엔진은 드러나지 않는다
    assert [
        author_name(DEFAULT_ORG, a) for a in ("arcturus", "albireo", "rigel", "mizar", "bot")
    ] == [
        "[아르크투루스 · 기획팀장]",
        "[알비레오 · 디자인팀장]",
        "[리겔 · 개발팀장]",
        "[미자르 · QA팀장]",
        "[별채]",
    ]
    # ADR-005 기준 기록의 행위자
    assert author_name(DEFAULT_ORG, "codex") == "[Alpha · Codex]"


# DiscordChat


class FakeSender:
    def __init__(self):
        self.calls = []

    async def send(self, content=None, **kwargs):
        self.calls.append((content, kwargs))
        return SimpleNamespace(id=1000 + len(self.calls))


def make_chat():
    webhook, ops = FakeSender(), FakeSender()
    asked = []

    async def webhook_for(thread_id):
        asked.append(thread_id)
        return webhook

    async def ops_channel():
        return ops

    chat = DiscordChat(zero_user_id=ZERO, webhook_for=webhook_for, ops_channel=ops_channel)
    return chat, webhook, ops, asked


def test_post_silent_without_mention():
    chat, webhook, _, asked = make_chat()
    message_id = run(chat.post("500", "rigel", "의견"))
    assert message_id == "1001"
    assert asked == [500]
    ((content, kwargs),) = webhook.calls
    assert content == "의견"
    assert kwargs["username"] == "[리겔 · 개발팀장]"
    assert kwargs["thread"].id == 500
    assert kwargs["silent"] is True
    assert kwargs["wait"] is True
    assert kwargs["files"] == []
    assert kwargs["allowed_mentions"].users is False


def test_post_without_avatar_leaves_webhook_default():
    chat, webhook, _, _ = make_chat()
    run(chat.post("500", "rigel", "의견"))
    ((_, kwargs),) = webhook.calls
    assert kwargs["avatar_url"] is discord.utils.MISSING


def test_posts_use_agent_and_bot_avatars():
    org = DEFAULT_ORG.model_copy(
        update={
            "agents": {
                **DEFAULT_ORG.agents,
                "rigel": DEFAULT_ORG.agents["rigel"].model_copy(
                    update={"avatar": "https://img.example/rigel.png"}
                ),
            },
            "bot_avatar": "https://img.example/bot.png",
        }
    )
    sender = FakeSender()
    sender_calls = sender.calls

    async def webhook(*_):
        return sender

    chat = DiscordChat(
        zero_user_id=ZERO,
        webhook_for=webhook,
        ops_channel=webhook,
        org=org,
        chat_webhook=webhook,
        forum_webhook=webhook,
        dept_webhook=webhook,
    )
    run(chat.post("500", "rigel", "의견"))
    run(chat.chat("gagae", "rigel", "답"))
    run(chat.dept_chat("secretary", "arcturus", "답"))
    sender.send = AsyncMock(return_value=SimpleNamespace(channel=SimpleNamespace(id=900)))
    run(chat.create_post("gagae", "제목", "\n".join(["가" * 1000] * 2)))
    avatars = [kwargs["avatar_url"] for _, kwargs in sender_calls]
    assert avatars == [
        "https://img.example/rigel.png",
        "https://img.example/rigel.png",
        discord.utils.MISSING,  # arcturus는 사진을 정하지 않았다
    ]
    assert [c.kwargs["avatar_url"] for c in sender.send.await_args_list] == [
        "https://img.example/bot.png",
        "https://img.example/bot.png",
    ]


def chat_with_channel_webhooks(*, has_webhook=True, report_author="polaris"):
    """#대표실·운영 채널 웹훅이 있는 DiscordChat (ADR-016)."""
    hooks, channels = {}, {"report": FakeSender(), "ops": FakeSender()}

    async def channel_webhook(channel):
        if not has_webhook:
            return None
        return hooks.setdefault(id(channel), FakeSender())

    async def ops():
        return channels["ops"]

    async def report():
        return channels["report"]

    chat = DiscordChat(
        zero_user_id=ZERO,
        webhook_for=ops,
        ops_channel=ops,
        report_channel=report,
        channel_webhook=channel_webhook,
        report_author=report_author,
    )
    return chat, channels, hooks


def test_report_and_ops_post_as_staff_through_channel_webhooks():
    chat, channels, hooks = chat_with_channel_webhooks()
    run(chat.report("**아침 보고**", mention=True))
    run(chat.notify_ops("오류"))
    ((content, kwargs),) = hooks[id(channels["report"])].calls
    assert content == f"<@{ZERO}>\n**아침 보고**"
    assert kwargs["username"] == "[폴라리스 · 비서실장]"  # #대표실은 비서실장 (ADR-016)
    assert kwargs["silent"] is False and kwargs["wait"] is True
    ((_, kwargs),) = hooks[id(channels["ops"])].calls
    assert kwargs["username"] == "[별채]" and kwargs["silent"] is True  # 운영 채널은 [별채]
    # 봇 계정으로는 아무것도 보내지 않는다
    assert channels["report"].calls == [] and channels["ops"].calls == []


def test_send_puts_author_and_view_on_last_chunk():
    chat, channels, hooks = chat_with_channel_webhooks()
    view = discord.ui.View(timeout=None)
    channel = channels["report"]
    run(chat.send(channel, "\n".join(["가" * 1000] * 2), author="thuban", view=view))
    first, last = (kwargs for _, kwargs in hooks[id(channel)].calls)
    assert first["username"] == last["username"] == "[투반 · 교육팀장]"
    assert "view" not in first and last["view"] is view


def test_send_without_webhook_falls_back_to_bot_account():
    chat, channels, hooks = chat_with_channel_webhooks(has_webhook=False)
    run(chat.report("보고"))
    ((content, kwargs),) = channels["report"].calls
    assert content == "보고" and "username" not in kwargs and hooks == {}


def test_report_author_defaults_to_bot():
    chat, channels, hooks = chat_with_channel_webhooks(report_author="bot")
    run(chat.report("보고"))
    ((_, kwargs),) = hooks[id(channels["report"])].calls
    assert kwargs["username"] == "[별채]"


@pytest.mark.parametrize("when", ["lookup", "send"])
@pytest.mark.parametrize("code", [10003, 10015])
def test_post_translates_only_missing_channel(when, code):
    error = discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"),
        {"code": code, "message": "Unknown Channel" if code == 10003 else "Unknown Webhook"},
    )

    class MissingSender:
        async def send(self, *args, **kwargs):
            raise error

    async def webhook_for(thread_id):
        if when == "lookup":
            raise error
        return MissingSender()

    async def ops_channel():
        return FakeSender()

    chat = DiscordChat(zero_user_id=ZERO, webhook_for=webhook_for, ops_channel=ops_channel)
    with pytest.raises(ThreadNotFound if code == 10003 else discord.NotFound) as caught:
        run(chat.post("500", "bot", "결과"))
    if code == 10003:
        assert caught.value.thread == "500" and caught.value.__cause__ is error


def test_post_with_mention_splits_and_attaches_on_last():
    chat, webhook, _, _ = make_chat()
    text = "\n".join(["가" * 1000] * 3)
    message_id = run(
        chat.post("500", "arcturus", text, mention=True, attachments=[Attachment("a.md", "전문")])
    )
    assert message_id == "1001"
    assert len(webhook.calls) == 3
    first, middle, last = (kwargs for _, kwargs in webhook.calls)
    assert webhook.calls[0][0].startswith(f"<@{ZERO}>\n")
    assert [u.id for u in first["allowed_mentions"].users] == [ZERO]
    assert middle["allowed_mentions"].users is False  # 멘션은 첫 조각에만
    assert all(kwargs["silent"] is False for kwargs in (first, middle, last))
    assert first["files"] == [] and middle["files"] == []
    assert [f.filename for f in last["files"]] == ["a.md"]
    assert all(len(content) <= 1900 for content, _ in webhook.calls)


def test_send_attaches_image_files_on_last_chunk(tmp_path):
    chat, _, _, _ = make_chat()
    channel = FakeSender()
    image = tmp_path / "그림.png"
    image.write_bytes(b"png")
    run(chat.send(channel, "\n".join(["가" * 1000] * 2), files=[image]))
    run(chat.send(channel, "그림 없음"))
    first, last, plain = (kwargs for _, kwargs in channel.calls)
    assert "files" not in first and "files" not in plain
    assert [f.filename for f in last["files"]] == ["그림.png"]
    assert last["silent"] is True


def test_dept_chat_posts_to_department_webhook():
    sender = FakeSender()
    asked = []

    async def dept_webhook(dept):
        asked.append(dept)
        return sender

    async def unused(*args):
        raise AssertionError("쓰지 않는다")

    chat = DiscordChat(
        zero_user_id=ZERO, webhook_for=unused, ops_channel=unused, dept_webhook=dept_webhook
    )
    assert run(chat.dept_chat("secretary", "arcturus", "답", mention=True)) == "1001"
    ((content, kwargs),) = sender.calls
    assert asked == ["secretary"]
    assert content == f"<@{ZERO}>\n답" and kwargs["username"] == "[아르크투루스 · 기획팀장]"
    assert "thread" not in kwargs and kwargs["silent"] is False


def test_open_thread_on_zero_message():
    created = []

    class Message:
        async def create_thread(self, **kwargs):
            created.append(kwargs)
            return SimpleNamespace(id=777)

    class Channel:
        def get_partial_message(self, message_id):
            created.append(message_id)
            return Message()

    async def dept_channel(dept):
        assert dept == "secretary"
        return Channel()

    async def unused(*args):
        raise AssertionError("쓰지 않는다")

    chat = DiscordChat(
        zero_user_id=ZERO, webhook_for=unused, ops_channel=unused, dept_channel=dept_channel
    )
    assert run(chat.open_thread("secretary", "901", "가" * 120)) == "777"
    message_id, kwargs = created
    assert message_id == 901 and kwargs["name"] == "가" * 100


def test_notify_ops():
    chat, _, ops, _ = make_chat()
    run(chat.notify_ops("오류", mention=True))
    run(chat.notify_ops("시작"))
    (first, kwargs1), (second, kwargs2) = ops.calls
    assert first == f"<@{ZERO}>\n오류" and kwargs1["silent"] is False
    assert second == "시작" and kwargs2["silent"] is True


# /status, /ping 문구


def test_render_status_and_ping():
    task = TaskRecord(
        id=3,
        project="gagaessi",
        discord_thread="500",
        title="예산 배너",
        state=TaskState.NEEDS_HUMAN,
        prev_state=TaskState.REVIEWING,
        review_round=2,
        test_retry=1,
        opinion_rounds=4,
        issue_number=None,
        pr_number=None,
        branch=None,
        worktree=None,
        claude_session=None,
        waiting_since=None,
        reminded_at=None,
        created_at="2026-10-10T09:00:00+00:00",
        updated_at="2026-10-10T09:00:00+00:00",
    )
    now = datetime(2026, 10, 10, 9, 0, tzinfo=UTC) + timedelta(hours=2, minutes=5)
    assert render_status(task, now) == (
        "작업 3 · 예산 배너\n상태: NEEDS_HUMAN\n직전 상태: REVIEWING\n질문 수: 4\n"
        "리뷰 라운드: 2\n테스트 재시도: 1\n마지막 변경 후: 2시간 5분"
    )
    assert render_ping(running_cli=1, active_tasks=2) == (
        "살아 있다. 실행 중 CLI 1개, 대기 0개, 진행 중 작업 2개."
    )
    assert "10-28 09:00까지 대기열을 멈췄다" in render_ping(
        running_cli=0, active_tasks=0, waiting=3, paused="10-28 09:00"
    )


# 봇 입력 분기


@pytest.fixture
def bot(tmp_path):
    return make_bot(tmp_path)


@pytest.fixture
def dept_bot(tmp_path):
    # #비서 채널이 있는 봇 (ADR-013)
    return make_bot(tmp_path, f'secretary:\n  vault: /svault\n  channel_id: "{DEPT}"\n')


def make_bot(tmp_path, extra=""):
    projects = tmp_path / "projects.yaml"
    projects.write_text(
        "projects:\n"
        "  gagaessi:\n"
        f'    forum_channel_id: "{FORUM}"\n'
        "    repo: /repo\n"
        "    github: owner/GagaeSsi\n"
        "    test: swift test\n"
        f'    chat_channel_id: "{CHAT}"\n'
        "    vault_dir: d\n    vault_hub: h.md\n    vault_tag: brain/g\n    vault_slug: g\n"
        "paths:\n  vault_projects: /vault\n  company: /nonexistent/zerosync-company\n" + extra,
        encoding="utf-8",
    )
    environ = {
        "DISCORD_TOKEN": "t",
        "DISCORD_GUILD_ID": "1",
        "ZERO_USER_ID": str(ZERO),
        "OPS_CHANNEL_ID": str(OPS),
    }
    config = load_config(projects, tmp_path / ".env", environ)
    tasks = {"500": object()}

    class Repo:
        async def find_task_by_thread(self, thread):
            return tasks.get(thread)

    instance = ZeroSyncBot(config, Repo(), {})
    calls = []

    class Engine:
        pipelines: dict = {}

        def pipeline_for_command(self, command):
            return object() if command in ("/task feature", "/task bug", "/release") else None

        async def on_chat(self, project, text, *, message_id=None):
            calls.append(("chat", project, text, message_id))

        async def task_from_chat(self, project, command, title):
            calls.append(("task", project, command, title))

        async def debate(self, project):
            calls.append(("debate", project))
            return ""

        async def add_idea(self, project, text):
            calls.append(("idea", project, text))
            return 7

        async def on_message(self, thread, text, *, message_id=None):
            calls.append(("message", thread, text, message_id))

        async def on_command(self, thread, name, arg=""):
            calls.append(("command", thread, name, arg))

        async def decide_hiring(self, number, *, accept, note=""):
            calls.append(("hire", number, accept, note))
            return f"채용 제안 #{number} 기록"

    instance.engine = Engine()
    instance.calls = calls
    return instance


def discord_message(*, message_id, channel_id=500, author_id=ZERO, content="질문", bot=False):
    channel = SimpleNamespace(id=channel_id, parent_id=FORUM, name="예산 배너")
    author = SimpleNamespace(id=author_id, bot=bot)
    return SimpleNamespace(
        id=message_id, channel=channel, author=author, content=content, webhook_id=None
    )


async def drain(bot):
    while bot._background:
        await asyncio.gather(*bot._background)


def test_forum_post_gets_hint_and_reply_reaches_engine(bot):
    hints = []

    async def post(thread, author, text, **kwargs):
        hints.append((thread, author, text))

    bot.chat.post = post

    async def main():
        # Zero가 포럼에 직접 연 포스트는 작업을 만들지 않고 안내한다 (ADR-007)
        await bot.on_message(discord_message(message_id=700, channel_id=700, content="  "))
        await bot.on_message(discord_message(message_id=501, content="추가 질문"))
        await bot.on_message(discord_message(message_id=502, author_id=7))
        await drain(bot)

    run(main())
    assert bot.calls == [("message", "500", "추가 질문", "501")]
    assert hints == [("700", "bot", POST_HINT)]


def test_chat_message_reaches_engine(bot):
    chat = SimpleNamespace(id=CHAT, name="gagaessi-chat")
    message = SimpleNamespace(
        id=900,
        channel=chat,
        author=SimpleNamespace(id=ZERO, bot=False),
        content="  배너 어때?  ",
        webhook_id=None,
    )

    async def main():
        await bot.on_message(message)
        await drain(bot)

    run(main())
    assert bot.calls == [("chat", "gagaessi", "배너 어때?", "900")]


def test_department_message_reaches_dept_chat(dept_bot):
    bot = dept_bot
    calls = []

    class Dept:
        async def on_message(self, dept, text, *, message_id=None, thread=None):
            calls.append((dept, text, message_id, thread))

    bot.dept_chat = Dept()

    def dept_message(message_id, author_id=ZERO, thread=None):
        channel = (
            SimpleNamespace(id=DEPT, name="비서")
            if thread is None
            else SimpleNamespace(id=thread, parent_id=DEPT, name="회의")
        )
        return SimpleNamespace(
            id=message_id,
            channel=channel,
            author=SimpleNamespace(id=author_id, bot=False),
            content="  이건 어떻게 결정됐나요?  ",
            webhook_id=None,
        )

    async def main():
        await bot.on_message(dept_message(901))
        await bot.on_message(dept_message(902, author_id=7))
        await bot.on_message(dept_message(903, thread=801))
        await drain(bot)

    run(main())
    assert calls == [
        ("secretary", "이건 어떻게 결정됐나요?", "901", None),
        ("secretary", "이건 어떻게 결정됐나요?", "903", "801"),
    ]
    assert bot.calls == []


def test_dept_folders_include_vaults_company_and_repos_once(dept_bot):
    bot = dept_bot
    from bot.discord_io.client import dept_folders, dept_specs

    folders = dept_folders(bot.config)
    assert [str(path) for _, path in folders] == [
        "/svault",
        "/vault",
        "/nonexistent/zerosync-company",
        "/repo",
    ]
    assert list(dept_specs(bot.config)) == ["secretary"]


def test_chat_commands_start_work(bot):
    async def main():
        task = interaction(channel_id=CHAT, parent_id=None)
        await bot.run_task(task, "/task feature", "예산 배너")
        release = interaction(channel_id=CHAT, parent_id=None)
        await bot.run_task(release, "/release", "1.0.0")
        idea = interaction(channel_id=CHAT, parent_id=None)
        await bot.run_idea(idea, "위젯")
        debate = interaction(channel_id=CHAT, parent_id=None)
        await bot.run_debate(debate)
        wrong = interaction()  # 포럼 포스트 안에서는 쓸 수 없다
        await bot.run_task(wrong, "/task bug", "x")
        await drain(bot)
        return task, idea, wrong

    task, idea, wrong = run(main())
    assert task.response.sent == [("/task feature 예산 배너", {"silent": True})]
    assert idea.response.sent == [("아이디어 #7로 기록했다: 위젯", {"silent": True})]
    assert "채팅 채널에서만" in wrong.response.sent[0][0]
    # /idea는 바로 기록하고, 나머지는 백그라운드에서 실행한다
    assert sorted(bot.calls) == sorted(
        [
            ("task", "gagaessi", "/task feature", "예산 배너"),
            ("task", "gagaessi", "/release", "1.0.0"),
            ("idea", "gagaessi", "위젯"),
            ("debate", "gagaessi"),
        ]
    )


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content, **kwargs):
        self.sent.append((content, kwargs))


def interaction(user_id=ZERO, channel_id=500, parent_id=FORUM):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        channel=SimpleNamespace(parent_id=parent_id),
        channel_id=channel_id,
        response=FakeResponse(),
    )


def test_fix_command_passes_recommended_arg(bot):
    async def main():
        it = interaction()
        await bot.run_command(it, CommandName.FIX, "권장 반영")
        await drain(bot)
        return it

    it = run(main())
    assert it.response.sent == [("/fix 권장 반영", {"silent": True})]


def test_command_is_echoed_and_sent_to_engine(bot):
    async def main():
        it = interaction()
        await bot.run_command(it, CommandName.REVISE, "배너 색")
        await drain(bot)
        return it

    it = run(main())
    assert it.response.sent == [("/revise 배너 색", {"silent": True})]
    assert bot.calls == [("command", "500", CommandName.REVISE, "배너 색")]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"user_id": 7}, "Zero만"),
        ({"parent_id": None}, "포스트 안에서만"),
        ({"channel_id": 999}, "연결된 작업이 없다"),
    ],
)
def test_command_rejected_privately(bot, kwargs, expected):
    it = interaction(**kwargs)
    run(bot.run_command(it, CommandName.STOP))
    ((content, options),) = it.response.sent
    assert expected in content
    assert options == {"ephemeral": True}
    assert bot.calls == []


def test_background_error_is_reported_to_ops(bot, caplog):
    alerts = []

    async def notify_ops(text, *, mention=False):
        alerts.append((text, mention))

    bot.chat.notify_ops = notify_ops

    async def boom():
        raise RuntimeError("터짐")

    async def main():
        bot.spawn(boom(), "게시글 500")
        await drain(bot)

    run(main())
    assert alerts == [("게시글 500 처리 중 오류: RuntimeError: 터짐", True)]


def test_intents_include_message_content(bot):
    assert bot.intents.message_content is True
    assert isinstance(bot.guild_ref, discord.Object) and bot.guild_ref.id == 1
    names = {command.name for command in bot.tree.get_commands(guild=bot.guild_ref)}
    assert names == {
        "task", "release", "debate", "idea", "approve", "revise", "skip", "fix",
        "resume", "stop", "hire", "status", "ping", "daily", "weekly", "evening", "hold",
    }  # fmt: skip
    project = project_contexts(bot.config)["gagaessi"]
    assert project.github.repo == "owner/GagaeSsi"
    assert project.feature_dir.parts[-2:] == ("d", "기능")
    assert bot.thread_url("5") == "https://discord.com/channels/1/5"


def test_startup_failure_alerts_ops_and_closes(bot):
    alerts, closed = [], []

    async def notify_ops(text, *, mention=False):
        alerts.append((text, mention))

    async def prepare():
        raise RuntimeError("gagaessi의 forum_channel_id가 포럼 채널이 아니다 (#일반, 종류 text)")

    async def close():
        closed.append(True)

    bot.chat.notify_ops = notify_ops
    bot._prepare_webhooks = prepare
    bot.close = close
    run(bot.on_ready())

    assert closed == [True]
    assert isinstance(bot.startup_error, RuntimeError)
    ((text, mention),) = alerts
    assert "시작 실패" in text and "#일반" in text
    assert mention is True


def test_hire_is_zero_only_and_reaches_engine(bot):
    # 채용 제안 답은 작업 게시글이 아니라 운영 채널 등 어디서든 쓴다 (운영 규약 1.12)
    it = interaction(parent_id=None, channel_id=OPS)
    run(bot.run_hire(it, accept=False, number=3, note="아직 이르다"))
    assert bot.calls == [("hire", 3, False, "아직 이르다")]
    assert it.response.sent == [("채용 제안 #3 기록", {"silent": True})]

    other = interaction(user_id=7)
    run(bot.run_hire(other, accept=True, number=3, note=""))
    assert "Zero만" in other.response.sent[0][0]
    assert len(bot.calls) == 1


def test_due_reports_run_once_and_go_to_report_channel(bot):
    from bot.clock import FixedClock
    from bot.store import Repository, connect

    async def main():
        conn = await connect(":memory:")
        try:
            clock = FixedClock(datetime(2026, 10, 25, 23, 0, tzinfo=UTC))  # 한국 월 08:00
            bot.clock = clock
            bot.repo = Repository(conn, clock)
            sent = []

            async def report(text, *, mention=False):
                sent.append((text.splitlines()[0], mention))

            bot.chat.report = report
            first = await bot.run_due_schedules()
            again = await bot.run_due_schedules()
            clock.advance(timedelta(days=1))
            next_day = await bot.run_due_schedules()
            return first, again, next_day, sent
        finally:
            await conn.close()

    first, again, next_day, sent = run(main())
    assert sorted(first) == ["morning_report", "weekly_report"]
    assert again == [] and next_day == ["morning_report"]
    assert sent[0][0].startswith("**아침 보고**") and sent[1][0].startswith("**주간 보고**")


def test_education_daily_and_weekly_run_on_their_schedules_once(bot, tmp_path):
    from bot.clock import FixedClock
    from bot.config import EducationConfig
    from bot.store import Repository, connect

    bot.config = bot.config.model_copy(
        update={"education": EducationConfig(vault=tmp_path, schedule="30 7 * * *")}
    )
    runs = []

    class Desk:
        async def run_daily(self):
            runs.append("daily")
            return SimpleNamespace(created=True)

        async def run_weekly(self):
            runs.append("weekly")

    bot.education = Desk()

    async def main():
        conn = await connect(":memory:")
        try:
            clock = FixedClock(datetime(2026, 10, 28, 22, 40, tzinfo=UTC))  # 한국 목 07:40
            bot.clock = clock
            bot.repo = Repository(conn, clock)

            async def report(text, *, mention=False):
                pass

            bot.chat.report = report
            first = await bot.run_due_schedules()
            again = await bot.run_due_schedules()
            await drain(bot)
            return first, again
        finally:
            await conn.close()

    first, again = run(main())
    assert "education_daily" in first and "education_daily" not in again
    # 주간(일요일 20:00)은 지난 일요일 몫을 한 번 따라잡는다
    assert "education_weekly" in first and "education_weekly" not in again
    assert sorted(runs) == ["daily", "weekly"]


@pytest.mark.parametrize("created", [True, False])
def test_new_morning_note_makes_secretary_write_entry_daily(bot, created):
    from bot.clock import FixedClock

    runs = []

    class Desk:
        async def run_daily(self):
            return SimpleNamespace(created=created)

    class Secretary:
        async def run_morning(self, day):
            runs.append(day.isoformat())

    bot.education, bot.secretary = Desk(), Secretary()
    bot.clock = FixedClock(datetime(2026, 10, 28, 22, 31, tzinfo=UTC))  # 한국 목 07:31
    run(bot.education_morning())
    # 이미 낸 날(/daily 다시 누름)은 아침판을 다시 쓰지 않는다
    assert runs == (["2026-10-29"] if created else [])


def test_secretary_catches_up_missed_evening_and_takes_morning_report(bot, tmp_path):
    from bot.clock import FixedClock
    from bot.config import SecretaryConfig
    from bot.store import Repository, connect

    bot.config = bot.config.model_copy(update={"secretary": SecretaryConfig(vault=tmp_path)})
    days = []

    class Desk:
        async def run_evening(self, day):
            days.append(day.isoformat())

    bot.secretary = Desk()

    async def main():
        conn = await connect(":memory:")
        try:
            clock = FixedClock(datetime(2026, 11, 2, 22, 40, tzinfo=UTC))  # 한국 화 07:40
            bot.clock = clock
            bot.repo = Repository(conn, clock)
            reports, sent = [], []

            async def report(text, *, mention=False):
                reports.append(text)

            async def target():
                return "#대표실"

            async def send(channel, text, *, mention=False, author="bot"):
                sent.append((channel, author, text.splitlines()[0]))

            bot.chat.report, bot.chat.report_target, bot.chat.send = report, target, send
            # 지난 아침·주간 보고는 이미 냈다
            await bot.repo.record_schedule_run("morning_report", "2026-11-02T00:00:00+00:00")
            await bot.repo.record_schedule_run("weekly_report", "2026-11-02T00:00:00+00:00")
            first = await bot.run_due_schedules()
            await drain(bot)
            clock.advance(timedelta(minutes=20))  # 08:00 아침 보고
            morning = await bot.run_due_schedules()
            return first, morning, reports, sent
        finally:
            await conn.close()

    first, morning, reports, sent = run(main())
    # 11-03 아침에 따라잡아도 11-02 하루 정리다
    assert first == ["secretary_evening"] and days == ["2026-11-02"]
    assert morning == ["morning_report"] and reports == []
    # 비서 이름은 본문 머리가 아니라 웹훅 게시자로 붙인다 (ADR-016)
    assert sent == [("#대표실", "polaris", "**아침 보고** (11-03)")]


def text_channel(channel_id, name):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.name = channel_id, name
    return channel


def test_channel_webhook_is_made_once_and_skips_other_channels(bot):
    channel, hook, made = text_channel(700, "대표실"), object(), []

    async def own(channel, name):
        made.append(name)
        return hook

    bot._own_webhook = own
    assert run(bot._channel_webhook(channel)) is hook
    assert run(bot._channel_webhook(channel)) is hook
    assert made == ["대표실"]
    assert run(bot._channel_webhook(SimpleNamespace(id=701))) is None  # 텍스트 채널이 아니다


def test_channel_webhook_without_permission_falls_back(bot):
    async def own(channel, name):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing")

    bot._own_webhook = own
    assert run(bot._channel_webhook(text_channel(702, "운영"))) is None


def test_report_author_is_secretary_lead(bot, dept_bot):
    assert bot.chat._report_author == "bot"  # 비서가 없으면 [별채]
    assert dept_bot.chat._report_author == dept_bot.config.secretary.agent


def test_quiz_and_done_buttons_post_as_education_lead(bot, tmp_path):
    from bot.config import EducationConfig
    from bot.schemas.education import QuizQuestion

    bot.config = bot.config.model_copy(update={"education": EducationConfig(vault=tmp_path)})
    sent = []

    async def channel():
        return "#교육"

    async def send(channel, text, *, mention=False, files=(), author="bot", view=None):
        sent.append((channel, author, view is not None))

    bot._education_channel, bot.chat.send = channel, send
    question = QuizQuestion(
        prompt="오늘 개념 질문", options=["가", "나", "다", "라"], answer="A", explanation="설명"
    )
    run(bot._post_quiz(1, "오늘 문제", [(1, question)], 1))
    run(bot._post_done("2026-11-19", "개념"))
    agent = bot.config.education.agent
    assert sent == [("#교육", agent, True), ("#교육", agent, True)]


def test_evening_command_is_zero_only_and_needs_secretary(bot):
    other = interaction(user_id=7)
    run(bot.run_evening(other))
    assert "Zero만" in other.response.sent[0][0]
    zero = interaction()
    run(bot.run_evening(zero))
    assert "비서 설정" in zero.response.sent[0][0]


@pytest.mark.parametrize("command", ["run_daily", "run_weekly"])
def test_education_commands_are_zero_only_and_need_education(bot, command):
    other = interaction(user_id=7)
    run(getattr(bot, command)(other))
    assert "Zero만" in other.response.sent[0][0]
    zero = interaction()
    run(getattr(bot, command)(zero))
    assert "교육 설정" in zero.response.sent[0][0]


def test_quiz_button_round_trips_custom_id_and_only_zero_answers(bot):
    from bot.discord_io.quiz import QUIZ_ID, QuizButton, quiz_view

    view = quiz_view(12, 3)
    ids = [item.item.custom_id for item in view.children]
    assert ids == ["quiz:12:3:A", "quiz:12:3:B", "quiz:12:3:C", "quiz:12:3:D"]
    match = QUIZ_ID.fullmatch(ids[1])
    button = run(QuizButton.from_custom_id(None, None, match))
    assert (button.quiz_id, button.number, button.choice) == (12, 3, "B")

    answers = []

    class Desk:
        async def answer(self, quiz_id, number, choice):
            answers.append((quiz_id, number, choice))
            return "정답이다 (B)."

    bot.education = Desk()
    other = interaction(user_id=7)
    run(bot.answer_quiz(other, 12, 3, "B"))
    zero = interaction()
    run(bot.answer_quiz(zero, 12, 3, "B"))
    assert "Zero의 문제" in other.response.sent[0][0]
    assert zero.response.sent == [("정답이다 (B).", {"ephemeral": True})]
    assert answers == [(12, 3, "B")]


def test_study_done_button_round_trips_and_only_zero_marks(bot):
    from bot.discord_io.quiz import STUDY_DONE_ID, StudyDoneButton, study_done_view

    view = study_done_view("2026-11-02")
    assert view.children[0].item.custom_id == "study:done:2026-11-02"
    match = STUDY_DONE_ID.fullmatch("study:done:2026-11-02")
    assert run(StudyDoneButton.from_custom_id(None, None, match)).day == "2026-11-02"

    marked = []

    class Desk:
        async def mark_done(self, day):
            marked.append(day)
            return "완료로 기록했다."

    bot.education = Desk()
    other = interaction(user_id=7)
    run(bot.mark_study_done(other, "2026-11-02"))
    zero = interaction()
    run(bot.mark_study_done(zero, "2026-11-02"))
    assert "Zero의 학습" in other.response.sent[0][0]
    assert zero.response.sent == [("완료로 기록했다.", {"ephemeral": True})]
    assert marked == ["2026-11-02"]


def test_optional_done_button_round_trips_and_only_zero_marks(bot):
    from bot.discord_io.quiz import OPTIONAL_DONE_ID, OptionalDoneButton, study_done_view

    view = study_done_view("2026-11-02")
    assert [item.item.custom_id for item in view.children] == [
        "study:done:2026-11-02",
        "study:optional:2026-11-02",
    ]
    match = OPTIONAL_DONE_ID.fullmatch("study:optional:2026-11-02")
    assert run(OptionalDoneButton.from_custom_id(None, None, match)).day == "2026-11-02"

    marked = []

    class Desk:
        async def mark_optional(self, day):
            marked.append(day)
            return "했음으로 기록했다."

    bot.education = Desk()
    other = interaction(user_id=7)
    run(bot.mark_optional_done(other, "2026-11-02"))
    zero = interaction()
    run(bot.mark_optional_done(zero, "2026-11-02"))
    assert "Zero의 학습" in other.response.sent[0][0]
    assert zero.response.sent == [("했음으로 기록했다.", {"ephemeral": True})]
    assert marked == ["2026-11-02"]


def test_held_job_is_skipped_and_not_caught_up(bot, tmp_path):
    """홀딩 중 예정 시각이 지나면 기록만 하고, 풀어도 따라잡지 않는다 (ADR-014)."""
    from bot.clock import FixedClock
    from bot.store import Repository, connect

    async def main():
        conn = await connect(":memory:")
        try:
            clock = FixedClock(datetime(2026, 10, 25, 23, 0, tzinfo=UTC))  # 한국 월 08:00
            bot.clock = clock
            bot.repo = Repository(conn, clock)
            sent = []

            async def report(text, *, mention=False):
                sent.append(text)

            bot.chat.report = report
            with pytest.raises(KeyError):
                await bot.set_hold("nope", True)
            status = await bot.set_hold("morning_report", True, source="웹")
            held = await bot.run_due_schedules()
            await bot.set_hold("morning_report", False)
            released = await bot.run_due_schedules()
            clock.advance(timedelta(days=1))
            next_day = await bot.run_due_schedules()
            await bot.set_hold("*", True)
            clock.advance(timedelta(days=1))
            all_held = await bot.run_due_schedules()
            label = await bot.held_label()
            return status, held, released, next_day, all_held, label, sent
        finally:
            await conn.close()

    status, held, released, next_day, all_held, label, sent = run(main())
    jobs = {job["job"]: job for job in status["jobs"]}
    assert jobs["morning_report"]["held"] and not jobs["weekly_report"]["held"]
    assert held == ["weekly_report"]  # 아침 보고는 홀딩, 주간 보고만 돈다
    assert released == []  # 지나간 아침 보고를 따라잡지 않는다
    assert next_day == ["morning_report"]
    assert all_held == [] and label == "전체"
    assert sent[0] == "주기 작업 홀딩: **아침 보고** (웹)"
    assert sent[2] == "주기 작업 홀딩 해제: **아침 보고** (웹). 지나간 실행은 따라잡지 않는다"


def test_hold_command_needs_zero_and_known_job(bot):
    async def main():
        stranger = interaction(user_id=ZERO + 1)
        await bot.run_hold(stranger, "morning_report", held=True)

        async def fail(job, held, *, source):
            raise KeyError(job)

        bot.set_hold = fail
        unknown = interaction()
        await bot.run_hold(unknown, "nope", held=True)
        return stranger, unknown

    stranger, unknown = run(main())
    assert "Zero만" in stranger.response.sent[0][0]
    assert "모르는 주기 작업" in unknown.response.sent[0][0]
    assert ("*", "전체 주기 작업") == bot.hold_choices()[0]
