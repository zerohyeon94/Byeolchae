"""Discord 계층 테스트. 실제 Discord에는 연결하지 않는다 (SPEC 12.3).

규칙은 순수 함수로, DiscordChat은 가짜 웹훅으로, 봇의 입력 분기는 가짜 메시지·인터랙션으로 확인한다.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
from bot.workflow.chat import Attachment
from bot.workflow.state import CommandName, TaskState

ZERO = 42
FORUM = 100
OPS = 300
CHAT = 600


def run(coro):
    return asyncio.run(coro)


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
    ],
)
def test_classify(overrides, expected):
    kind = classify(message(**overrides), zero_user_id=ZERO, forum_ids={FORUM}, chat_ids={CHAT})
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
    assert [author_name(DEFAULT_ORG, a) for a in ("beta", "gamma", "alpha", "delta", "bot")] == [
        "[Beta · 기획팀장]",
        "[Gamma · 디자인팀장]",
        "[Alpha · 개발팀장]",
        "[Delta · QA팀장]",
        "[ZeroSync]",
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
    message_id = run(chat.post("500", "alpha", "의견"))
    assert message_id == "1001"
    assert asked == [500]
    ((content, kwargs),) = webhook.calls
    assert content == "의견"
    assert kwargs["username"] == "[Alpha · 개발팀장]"
    assert kwargs["thread"].id == 500
    assert kwargs["silent"] is True
    assert kwargs["wait"] is True
    assert kwargs["files"] == []
    assert kwargs["allowed_mentions"].users is False


def test_post_with_mention_splits_and_attaches_on_last():
    chat, webhook, _, _ = make_chat()
    text = "\n".join(["가" * 1000] * 3)
    message_id = run(
        chat.post("500", "beta", text, mention=True, attachments=[Attachment("a.md", "전문")])
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
        "paths:\n  vault_projects: /vault\n  company: /nonexistent/zerosync-company\n",
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
        "resume", "stop", "hire", "status", "ping", "daily", "weekly",
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
    assert [item.item.custom_id for item in view.children] == ["study:done:2026-11-02"]
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
