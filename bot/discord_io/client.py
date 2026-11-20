"""Discord 연결 (SPEC 2.1, 4, 6.1).

- 포럼 게시글과 그 안의 Zero 메시지를 엔진에 넘긴다. Zero 외 입력, 봇·웹훅 메시지는 무시한다
- 슬래시 명령은 서버(guild) 명령으로 등록한다. Discord는 3초 안에 응답을 요구하므로
  먼저 접수 응답을 보내고 엔진 작업은 백그라운드 태스크로 실행한다
- 시작하면 포럼 채널마다 웹훅을 찾거나 만들고(Q6), 재시작 전에 끝나지 않은 실행을 정리한다(Q8)
"""

import asyncio
import logging
from collections.abc import Coroutine, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

from bot import control
from bot.agents.base import AgentRunner, EngineName
from bot.clock import Clock, SystemClock, from_iso, to_iso
from bot.config import Config
from bot.control import ensure_token
from bot.discord_io.chat import DiscordChat
from bot.discord_io.quiz import (
    OptionalDoneButton,
    QuizButton,
    StudyDoneButton,
    quiz_view,
    study_done_view,
)
from bot.discord_io.rules import (
    WEBHOOK_NAME,
    IncomingMessage,
    MessageKind,
    chat_command_error,
    classify,
    command_error,
)
from bot.education.quiz import render_question
from bot.github import GitHubClient, VaultGit
from bot.render.status import render_ping, render_status
from bot.reports import Report, morning_brief, morning_report, weekly_report
from bot.scheduler import (
    BUILTIN_SCHEDULES,
    EDUCATION_DAILY,
    EDUCATION_WEEKLY,
    HOLD_ALL,
    HOLD_KEY,
    MORNING_REPORT,
    SECRETARY_EVENING,
    ScheduledJob,
    dump_holds,
    is_due,
    is_held,
    latest_due,
    parse_cron,
    parse_holds,
    scheduled_jobs,
)
from bot.schemas import QuizQuestion
from bot.secretary.daily import daily_path
from bot.store import Repository
from bot.workflow.dept_chat import Dept, DeptChat
from bot.workflow.education import EducationDesk
from bot.workflow.engine import PAUSE_KEY, Engine, ProjectContext
from bot.workflow.gate import AgentGate
from bot.workflow.mentions import Moment, should_mention
from bot.workflow.secretary import SecretaryDesk
from bot.workflow.state import CommandName, Limits, TaskState

log = logging.getLogger(__name__)

EMPTY_POST = "(본문 없음)"
POST_HINT = (
    "의견은 제품 채팅 채널에서 나누고, 일은 그 채널에서 `/task feature <제목>`, "
    "`/task bug <제목>`, `/release <버전>`으로 시작한다. 이 포스트는 작업과 연결되지 않았다."
)
REMIND_CHECK_SEC = 600
ACTIVE_STATES = {state for state in TaskState if not state.is_terminal}


def project_contexts(config: Config) -> dict[str, ProjectContext]:
    """설정의 프로젝트를 엔진 실행 정보로 바꾼다. 볼트 `기능/` 폴더는 읽기만 한다."""
    return {
        key: ProjectContext(
            repo=project.repo,
            base_branch=project.base_branch,
            test_command=project.test or (),
            owner=project.owner,
            github=GitHubClient(project.github),
            feature_dir=config.paths.vault_projects / project.vault_dir / "기능",
            vault_hub=project.vault_hub,
            design_system_url=project.design_system_url,
        )
        for key, project in config.projects.items()
    }


def dept_specs(config: Config) -> dict[str, Dept]:
    """설정의 비서·교육 부서를 대화 상대로 바꾼다 (ADR-013)."""
    specs = {}
    if config.secretary is not None:
        specs["secretary"] = Dept("secretary", "비서", config.secretary.agent)
    if config.education is not None:
        specs["education"] = Dept("education", "교육", config.education.agent)
    return specs


def dept_folders(config: Config) -> list[tuple[str, Path]]:
    """부서 대화에서 읽기 전용으로 여는 폴더. 같은 폴더는 한 번만 넣는다."""
    folders = []
    if config.secretary is not None:
        folders.append(("비서 볼트 (Daily)", config.secretary.vault))
    if config.education is not None:
        folders.append(("교육 볼트 (개념 노트, 대기열)", config.education.vault))
    folders.append(("제품 볼트 (기능 정의서, 결정 노트)", config.paths.vault_projects))
    folders.append(("회사 설정 (org.yaml 조직, pipelines/ 단계와 일정)", config.paths.company))
    for key, project in config.projects.items():
        folders.append((f"{key} 저장소 (docs/기획·디자인·설계, git 기록)", project.repo))
    unique: dict[Path, str] = {}
    for label, path in folders:
        unique.setdefault(path, label)
    return [(label, path) for path, label in unique.items()]


class ZeroSyncBot(discord.Client):
    def __init__(
        self,
        config: Config,
        repo: Repository,
        runners: Mapping[EngineName, AgentRunner],
        *,
        clock: Clock | None = None,
        git: Any = None,
        tests: Any = None,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.repo = repo
        self.clock = clock or SystemClock()
        self.zero_user_id = int(config.secrets.zero_user_id)
        self.guild_ref = discord.Object(int(config.secrets.discord_guild_id))
        self.ops_channel_id = int(config.secrets.ops_channel_id)
        report_id = config.secrets.report_channel_id
        self.report_channel_id = int(report_id) if report_id else None
        self.forums: dict[int, str] = {
            int(project.forum_channel_id): key for key, project in config.projects.items()
        }
        # 제품 채팅 채널 (ADR-007). 없는 제품은 포럼만 쓴다
        self.chats: dict[int, str] = {
            int(project.chat_channel_id): key
            for key, project in config.projects.items()
            if project.chat_channel_id
        }
        # 부서 채널 (ADR-013). 채널 ID가 있는 부서만 대화를 연다
        self.depts: dict[int, str] = {
            int(desk.channel_id): key
            for key, desk in (("secretary", config.secretary), ("education", config.education))
            if desk is not None and desk.channel_id
        }
        self.tree = app_commands.CommandTree(self)
        self.chat = DiscordChat(
            zero_user_id=self.zero_user_id,
            webhook_for=self._webhook_for,
            ops_channel=self._ops_channel,
            org=config.org,
            report_channel=self._report_channel if self.report_channel_id else None,
            chat_webhook=self._chat_webhook,
            forum_webhook=self._forum_webhook,
            dept_webhook=self._dept_webhook,
            dept_channel=self._dept_channel,
            channel_webhook=self._channel_webhook,
            # #대표실 보고는 비서실장 이름으로 쓴다. 비서가 없으면 [별채] (ADR-016)
            report_author=config.secretary.agent if config.secretary is not None else "bot",
        )
        self.engine = Engine(
            repo,
            self.chat,
            runners,
            project_contexts(config),
            timeout_sec=config.limits.cli_timeout_sec,
            org=config.org,
            pipelines=config.pipelines,
            git=git,
            tests=tests,
            limits=Limits(
                max_review_rounds=config.limits.max_review_rounds,
                max_test_retries=config.limits.max_test_retries,
            ),
            worktrees_dir=config.paths.worktrees,
            thread_url=self.thread_url,
            clock=self.clock,
            gate=AgentGate(config.limits.concurrent_agents),
            usage_pause=timedelta(minutes=config.limits.usage_pause_minutes),
            vault=VaultGit(),
        )
        self.education: EducationDesk | None = None
        if config.education is not None:
            self.education = EducationDesk(
                self.engine,
                VaultGit(),
                config.education.vault,
                config.education.agent,
                config.org.display(config.education.agent),
                self._education_notify,
                self.clock,
                repo=repo,
                daily_link=config.secretary is not None,
                post_quiz=self._post_quiz,
                post_done=self._post_done,
                illustrator=config.education.illustrator,
                images=config.education.codex_images,
            )
        self.secretary: SecretaryDesk | None = None
        if config.secretary is not None:
            self.secretary = SecretaryDesk(
                self.engine,
                VaultGit(),
                config.secretary.vault,
                config.secretary.agent,
                self._secretary_notify,
                repo=repo,
                org=config.org,
                claude_dir=config.secretary.claude_projects,
                codex_dir=config.secretary.codex_sessions,
                workdir=config.paths.runs.parent / "secretary",
            )
        self.dept_chat = DeptChat(
            self.engine,
            self.chat,
            repo,
            config.org,
            dept_specs(config),
            workdir=config.paths.runs.parent / "dept",
            folders=dept_folders(config),
        )
        self._webhooks: dict[int, discord.Webhook] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._started = False
        self.startup_error: Exception | None = None
        _register_commands(self)

    # 시작

    async def setup_hook(self) -> None:
        # 주간 문제 버튼은 재시작 뒤에도 눌려야 한다
        self.add_dynamic_items(QuizButton, StudyDoneButton, OptionalDoneButton)
        await self.tree.sync(guild=self.guild_ref)

    async def on_ready(self) -> None:
        # 재연결 때도 on_ready가 다시 오므로 처음 한 번만 준비한다
        if self._started:
            return
        self._started = True
        log.info("Discord 연결: %s", self.user)
        try:
            await self._prepare_webhooks()
        except Exception as error:
            # 웹훅 없이 떠 있으면 게시글을 받아도 답할 수 없다. 알리고 종료한다
            log.exception("시작 준비 실패")
            self.startup_error = error
            try:
                await self.chat.notify_ops(
                    f"별채 봇 시작 실패: {error}", mention=should_mention(Moment.BOT_ERROR)
                )
            except Exception:
                log.exception("운영 채널 알림 실패")
            await self.close()
            return
        await self.engine.recover_interrupted()
        await self.engine.prepare_github()
        self._loops = [
            asyncio.create_task(self._poll_merges()),
            asyncio.create_task(self._remind_loop()),
            asyncio.create_task(self._schedule_loop()),
            asyncio.create_task(self._project_report_loop()),
        ]
        await self._start_control()
        await self.chat.notify_ops(
            f"별채 봇을 시작했다. 프로젝트: {', '.join(self.config.projects)}. "
            f"조직: {', '.join(self.config.org.display(a) for a in self.config.org.agent_names)}",
            mention=should_mention(Moment.NOTICE),
        )

    def thread_url(self, thread: str) -> str:
        return f"https://discord.com/channels/{self.guild_ref.id}/{thread}"

    async def _poll_merges(self) -> None:
        """AWAIT_MERGE 작업의 PR merge를 주기적으로 확인한다 (SPEC 6.2)."""
        while not self.is_closed():
            try:
                await self.engine.poll_merges()
            except Exception:
                log.exception("merge 확인 실패")
            await asyncio.sleep(self.config.limits.merge_poll_sec)

    async def _remind_loop(self) -> None:
        """Zero 대기가 길어진 작업을 10분마다 확인해 1회 다시 멘션한다 (운영 규약 2.5)."""
        after = timedelta(hours=self.config.limits.remind_after_hours)
        while not self.is_closed():
            try:
                await self.engine.remind_waiting(after)
            except Exception:
                log.exception("재멘션 확인 실패")
            await asyncio.sleep(REMIND_CHECK_SEC)

    async def _project_report_loop(self) -> None:
        while not self.is_closed():
            try:
                await self.engine.process_project_reports()
            except Exception:
                log.exception("프로젝트 담당자 보고 확인 실패")
            await asyncio.sleep(60)

    async def _schedule_loop(self) -> None:
        """내장 일정(아침·주간 보고)을 1분마다 확인한다. 놓친 실행은 한 번만 (SPEC 6.6)."""
        while not self.is_closed():
            try:
                await self.run_due_schedules()
            except Exception:
                log.exception("일정 실행 실패")
            await asyncio.sleep(60)

    def schedule_list(self) -> list[ScheduledJob]:
        """지금 설정으로 도는 주기 작업 (웹과 /hold가 같은 목록을 쓴다, ADR-014)."""
        config = self.config
        education = config.education
        secretary = config.secretary
        return scheduled_jobs(
            self.engine.pipelines,
            config.projects,
            without_test=frozenset(key for key, p in config.projects.items() if not p.test),
            education=(
                (education.schedule, education.weekly)
                if self.education is not None and education is not None
                else None
            ),
            secretary=(
                secretary.schedule if self.secretary is not None and secretary is not None else None
            ),
        )

    async def run_due_schedules(self) -> list[str]:
        """지금 실행할 일정을 실행하고 이름을 돌려준다. 홀딩 중이면 기록만 하고 건너뛴다."""
        now = self.clock.now()
        local = now.astimezone()
        last = await self.repo.schedule_runs()
        holds = parse_holds(await self.repo.get_state(HOLD_KEY))
        ran = []
        for job in self.schedule_list():
            cron = parse_cron(job.cron)
            previous = from_iso(last[job.job]) if job.job in last else None
            if not is_due(cron, local, previous):
                continue
            # 실행 전에 기록한다. 실패해도 1분마다 다시 돌지 않고,
            # 홀딩 중에 지난 실행은 풀어도 따라잡지 않는다 (ADR-014)
            await self.repo.record_schedule_run(job.job, to_iso(now))
            if is_held(holds, job.job):
                log.info("홀딩 중이라 건너뛴다: %s", job.job)
                continue
            due = latest_due(cron, local)
            assert due is not None  # is_due가 참이면 예정 시각이 있다
            await self._start_schedule(job.job, now, due)
            ran.append(job.job)
        return ran

    async def _start_schedule(self, job: str, now: datetime, due: datetime) -> None:
        if job in BUILTIN_SCHEDULES:
            report = await self._build_report(job, now)
            if job == MORNING_REPORT and self.secretary is not None:
                await self._secretary_notify(report.text, report.mention)
            else:
                await self.chat.report(report.text, mention=report.mention)
        elif job == SECRETARY_EVENING:
            assert self.secretary is not None
            # 놓친 저녁은 다음에 켜질 때(아침) 그날 것으로 쓴다
            self.spawn(self.secretary.run_evening(due.date()), "비서 하루 정리")
        elif job == EDUCATION_DAILY:
            self.spawn(self.education_morning(), "교육 아침 학습")
        elif job == EDUCATION_WEEKLY:
            assert self.education is not None
            self.spawn(self.education.run_weekly(), "교육 주간 보고")
        else:  # 파이프라인 일정 (정기 테스트 등): 제품마다 따로 (SPEC 6.6, ADR-007)
            project, key = job.split(":", 1)
            self.spawn(self.engine.start_scheduled(project, key), f"일정 {job}")

    # 주기 작업 홀딩 (ADR-014)

    async def holds(self) -> frozenset[str]:
        return parse_holds(await self.repo.get_state(HOLD_KEY))

    async def held_label(self) -> str:
        """홀딩 중인 작업 이름 (아침 보고·/ping 표시). 없으면 빈 문자열."""
        holds = await self.holds()
        if HOLD_ALL in holds:
            return "전체"
        return ", ".join(job.name for job in self.schedule_list() if job.job in holds)

    async def schedule_status(self) -> dict[str, Any]:
        """웹이 보여 줄 주기 작업 목록과 홀딩 상태."""
        holds = await self.holds()
        last = await self.repo.schedule_runs()
        return {
            "all": HOLD_ALL in holds,
            "jobs": [
                {
                    "job": job.job,
                    "name": job.name,
                    "cron": job.cron,
                    "held": job.job in holds,
                    "last_run": last.get(job.job),
                }
                for job in self.schedule_list()
            ],
        }

    async def set_hold(self, job: str, held: bool, *, source: str = "웹") -> dict[str, Any]:
        """작업 하나(또는 "*" 전체)를 홀딩하거나 푼다. 모르는 키면 KeyError."""
        names = {item.job: item.name for item in self.schedule_list()}
        names[HOLD_ALL] = "전체 주기 작업"
        if job not in names:
            raise KeyError(job)
        holds = await self.holds()
        updated = holds | {job} if held else holds - {job}
        if updated != holds:
            await self.repo.set_state(HOLD_KEY, dump_holds(updated))
            verb = "홀딩" if held else "홀딩 해제"
            note = "" if held else ". 지나간 실행은 따라잡지 않는다"
            await self.chat.report(
                f"주기 작업 {verb}: **{names[job]}** ({source}){note}",
                mention=should_mention(Moment.NOTICE),
            )
        return await self.schedule_status()

    async def start_task_from_web(self, project: str, command: str, title: str) -> dict[str, Any]:
        """웹의 작업 만들기 (POST /tasks). 포스트와 작업만 만들고 첫 단계는 백그라운드로 돈다."""
        if project not in self.config.projects:
            raise KeyError(project)
        label = f"웹 {command} {title}"
        task = await self.engine.task_from_chat(
            project, command, title, background=lambda work: self.spawn(work, label)
        )
        if task is None:
            raise KeyError(command)
        await self.chat.notify_ops(f"[{project}] 웹에서 작업 {task.id}을 만들었다: {title}")
        return {"task_id": task.id, "thread": task.discord_thread}

    async def _start_control(self) -> None:
        """웹 홀딩 버튼을 받는 로컬 제어 서버. 못 열어도 봇은 계속 돈다."""
        try:
            token = ensure_token(self.config.paths.control_token)
            self._control = await control.start(
                token,
                schedules=self.schedule_status,
                set_hold=self.set_hold,
                start_task=self.start_task_from_web,
            )
        except Exception as error:
            log.exception("제어 서버 시작 실패")
            await self.chat.notify_ops(
                f"제어 서버를 열지 못해 웹 홀딩 버튼을 쓸 수 없다: {error}",
                mention=should_mention(Moment.BOT_ERROR),
            )

    async def _build_report(self, job: str, now: datetime) -> Report:
        if job == MORNING_REPORT and self.config.secretary is not None:
            paused = await self.repo.get_state(PAUSE_KEY) or ""
            yesterday = now.astimezone().date() - timedelta(days=1)
            path = daily_path(yesterday)
            daily = path if (self.config.secretary.vault / path).exists() else ""
            return await morning_brief(
                self.repo,
                self.config.org,
                now,
                self.thread_url,
                daily=daily,
                paused=paused,
                held=await self.held_label(),
            )
        if job == MORNING_REPORT:
            paused = await self.repo.get_state(PAUSE_KEY) or ""
            return await morning_report(
                self.repo,
                self.config.org,
                now,
                self.thread_url,
                paused=paused,
                held=await self.held_label(),
            )
        return await weekly_report(self.repo, self.config.org, now, list(self.config.projects))

    async def _secretary_notify(self, text: str, mention: bool) -> None:
        """비서 이름으로 #비서에 알린다. 채널이 없으면 #대표실(없으면 운영 채널)."""
        secretary = self.config.secretary
        assert secretary is not None
        if secretary.channel_id is None:
            channel = await self.chat.report_target()
        else:
            channel_id = int(secretary.channel_id)
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        await self.chat.send(channel, text, mention=mention, author=secretary.agent)

    async def run_evening(self, interaction: discord.Interaction) -> None:
        """가장 최근 저녁 일정의 하루 정리를 지금 만든다 (/evening). 이미 있으면 쓰지 않는다."""
        if interaction.user.id != self.zero_user_id:
            await interaction.response.send_message(
                "이 봇의 명령은 Zero만 쓸 수 있다.", ephemeral=True
            )
            return
        secretary = self.config.secretary
        if self.secretary is None or secretary is None:
            await interaction.response.send_message(
                "비서 설정(projects.yaml의 secretary)이 없다.", ephemeral=True
            )
            return
        due = latest_due(parse_cron(secretary.schedule), self.clock.now().astimezone())
        if due is None:
            await interaction.response.send_message("저녁 일정을 찾지 못했다.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{due.date().isoformat()} 하루 정리를 만든다.", ephemeral=True
        )
        self.spawn(self.secretary.run_evening(due.date()), "비서 하루 정리")

    async def _education_notify(self, text: str, mention: bool, files: Sequence[Path] = ()) -> None:
        """교육팀장 이름으로 #교육에 알린다. 채널이 없으면 #대표실(없으면 운영 채널)."""
        education = self.config.education
        assert education is not None
        await self.chat.send(
            await self._education_channel(),
            text,
            mention=mention,
            files=files,
            author=education.agent,
        )

    async def _education_channel(self) -> Any:
        education = self.config.education
        assert education is not None
        if education.channel_id is None:
            return await self.chat.report_target()
        channel_id = int(education.channel_id)
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def _post_quiz(
        self, quiz_id: int, title: str, items: list[tuple[int, QuizQuestion]], total: int
    ) -> None:
        """교육 문제(오늘·복습·주간)를 한 문제씩 A~D 버튼과 함께 올린다."""
        channel = await self._education_channel()
        agent = self._education_agent()
        for number, question in items:
            await self.chat.send(
                channel,
                render_question(question, title, number, total),
                author=agent,
                view=quiz_view(quiz_id, number),
            )

    async def _post_done(self, day: str, label: str) -> None:
        await self.chat.send(
            await self._education_channel(),
            f"**오늘 개념 완료**: {label}",
            author=self._education_agent(),
            view=study_done_view(day),
        )

    def _education_agent(self) -> str:
        education = self.config.education
        assert education is not None
        return education.agent

    async def mark_study_done(self, interaction: discord.Interaction, day: str) -> None:
        """완료 버튼. Zero만 누를 수 있고, 결과는 Zero에게만 보인다."""
        if interaction.user.id != self.zero_user_id:
            await interaction.response.send_message("Zero의 학습이다.", ephemeral=True)
            return
        if self.education is None:
            await interaction.response.send_message("교육 설정이 없다.", ephemeral=True)
            return
        text = await self.education.mark_done(day)
        await interaction.response.send_message(text, ephemeral=True)
        await self.refresh_daily()

    async def mark_optional_done(self, interaction: discord.Interaction, day: str) -> None:
        """선택 했음 버튼. Zero만 누를 수 있고, 결과는 Zero에게만 보인다."""
        if interaction.user.id != self.zero_user_id:
            await interaction.response.send_message("Zero의 학습이다.", ephemeral=True)
            return
        if self.education is None:
            await interaction.response.send_message("교육 설정이 없다.", ephemeral=True)
            return
        text = await self.education.mark_optional(day)
        await interaction.response.send_message(text, ephemeral=True)
        await self.refresh_daily()

    async def answer_quiz(
        self, interaction: discord.Interaction, quiz_id: int, number: int, choice: str
    ) -> None:
        """문제 버튼. Zero만 답할 수 있고, 결과는 Zero에게만 보인다."""
        if interaction.user.id != self.zero_user_id:
            await interaction.response.send_message("Zero의 문제다.", ephemeral=True)
            return
        if self.education is None:
            await interaction.response.send_message("교육 설정이 없다.", ephemeral=True)
            return
        text = await self.education.answer(quiz_id, number, choice)
        await interaction.response.send_message(text, ephemeral=True)
        await self.refresh_daily()

    async def refresh_daily(self) -> None:
        """버튼 뒤 낮 갱신. 실패해도 DB 기록은 남고 저녁판이 다시 쓰므로 로그만 남긴다."""
        if self.secretary is None:
            return
        try:
            await self.secretary.refresh_daily()
        except Exception:
            log.exception("Daily 낮 갱신 실패")

    async def run_daily(self, interaction: discord.Interaction) -> None:
        """오늘 개념 노트와 문제를 지금 낸다 (/daily). 이미 냈으면 다시 내지 않는다."""
        if await self._education_guard(interaction):
            assert self.education is not None
            await interaction.response.send_message("오늘 학습을 만든다.", ephemeral=True)
            self.spawn(self.education_morning(), "교육 아침 학습")

    async def education_morning(self) -> None:
        """교육 아침 학습. 노트가 새로 나오면 비서가 아침판 Daily를 쓴다 (노트로 가는 입구)."""
        assert self.education is not None
        result = await self.education.run_daily()
        if result.created and self.secretary is not None:
            await self.secretary.run_morning(self.clock.now().astimezone().date())

    async def run_weekly(self, interaction: discord.Interaction) -> None:
        """이번 주 학습 보고와 주간 문제를 지금 만든다 (/weekly). 문제는 주마다 한 번."""
        if await self._education_guard(interaction):
            assert self.education is not None
            await interaction.response.send_message("주간 학습 보고를 만든다.", ephemeral=True)
            self.spawn(self.education.run_weekly(), "교육 주간 보고")

    async def _education_guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.zero_user_id:
            await interaction.response.send_message(
                "이 봇의 명령은 Zero만 쓸 수 있다.", ephemeral=True
            )
            return False
        if self.education is None:
            await interaction.response.send_message(
                "교육 설정(projects.yaml의 education)이 없다.", ephemeral=True
            )
            return False
        return True

    async def _report_channel(self) -> Any:
        assert self.report_channel_id is not None
        return self.get_channel(self.report_channel_id) or await self.fetch_channel(
            self.report_channel_id
        )

    async def close(self) -> None:
        for loop in getattr(self, "_loops", []):
            loop.cancel()
        if getattr(self, "_control", None) is not None:
            await self._control.cleanup()
        await super().close()

    async def _prepare_webhooks(self) -> None:
        assert self.user is not None
        for forum_id, project in self.forums.items():
            try:
                channel = self.get_channel(forum_id) or await self.fetch_channel(forum_id)
            except (discord.NotFound, discord.Forbidden) as error:
                raise RuntimeError(
                    f"{project}의 forum_channel_id 채널을 찾을 수 없거나 볼 권한이 없다"
                ) from error
            if not isinstance(channel, discord.ForumChannel):
                name = getattr(channel, "name", "?")
                raise RuntimeError(
                    f"{project}의 forum_channel_id가 포럼 채널이 아니다 "
                    f"(#{name}, 종류 {getattr(channel, 'type', '?')})"
                )
            self._webhooks[forum_id] = await self._own_webhook(channel, project)
        for chat_id, project in self.chats.items():
            try:
                channel = self.get_channel(chat_id) or await self.fetch_channel(chat_id)
            except (discord.NotFound, discord.Forbidden) as error:
                raise RuntimeError(
                    f"{project}의 chat_channel_id 채널을 찾을 수 없거나 볼 권한이 없다"
                ) from error
            if not isinstance(channel, discord.TextChannel):
                raise RuntimeError(f"{project}의 chat_channel_id가 텍스트 채널이 아니다")
            self._webhooks[chat_id] = await self._own_webhook(channel, project)
        for dept_id, dept in self.depts.items():
            try:
                channel = self.get_channel(dept_id) or await self.fetch_channel(dept_id)
            except (discord.NotFound, discord.Forbidden) as error:
                raise RuntimeError(
                    f"{dept}.channel_id 채널을 찾을 수 없거나 볼 권한이 없다"
                ) from error
            if not isinstance(channel, discord.TextChannel):
                raise RuntimeError(f"{dept}.channel_id가 텍스트 채널이 아니다")
            self._webhooks[dept_id] = await self._own_webhook(channel, dept)

    async def _channel_webhook(self, channel: Any) -> discord.Webhook | None:
        """#대표실·운영 채널·#비서·#교육처럼 일반 채널의 웹훅. 처음 쓸 때 찾거나 만든다.
        텍스트 채널이 아니거나 웹훅 권한이 없으면 None (봇 계정으로 보낸다)."""
        if not isinstance(channel, discord.TextChannel):
            return None
        hook = self._webhooks.get(channel.id)
        if hook is None:
            try:
                hook = await self._own_webhook(channel, channel.name)
            except discord.HTTPException:
                log.exception("웹훅을 얻지 못해 봇 계정으로 보낸다: #%s", channel.name)
                return None
            self._webhooks[channel.id] = hook
        return hook

    async def _own_webhook(self, channel: Any, project: str) -> discord.Webhook:
        assert self.user is not None
        hooks = await channel.webhooks()
        hook = next(
            (
                h
                for h in hooks
                if h.name in (WEBHOOK_NAME, "ZeroSync")
                and h.user is not None
                and h.user.id == self.user.id
            ),
            None,
        )
        if hook is None:
            hook = await channel.create_webhook(name=WEBHOOK_NAME, reason="별채 게시용")
            log.info("웹훅 생성: %s #%s", project, getattr(channel, "name", "?"))
        return hook

    async def _chat_webhook(self, project: str) -> discord.Webhook:
        for chat_id, key in self.chats.items():
            if key == project:
                return self._webhooks[chat_id]
        raise RuntimeError(f"{project}의 채팅 채널이 설정되지 않았다")

    async def _dept_channel(self, dept: str) -> Any:
        for dept_id, key in self.depts.items():
            if key == dept:
                return self.get_channel(dept_id) or await self.fetch_channel(dept_id)
        raise RuntimeError(f"{dept} 부서 채널이 설정되지 않았다")

    async def _dept_webhook(self, dept: str) -> discord.Webhook:
        for dept_id, key in self.depts.items():
            if key == dept:
                return self._webhooks[dept_id]
        raise RuntimeError(f"{dept} 부서 채널이 설정되지 않았다")

    async def _forum_webhook(self, project: str) -> discord.Webhook:
        for forum_id, key in self.forums.items():
            if key == project:
                return self._webhooks[forum_id]
        raise RuntimeError(f"{project}의 포럼 웹훅이 없다")

    async def _webhook_for(self, thread_id: int) -> discord.Webhook:
        thread = self.get_channel(thread_id) or await self.fetch_channel(thread_id)
        parent_id = getattr(thread, "parent_id", None)
        if parent_id not in self._webhooks:
            raise RuntimeError(f"스레드 {thread_id}의 포럼 웹훅이 없다")
        return self._webhooks[parent_id]

    async def _ops_channel(self) -> Any:
        return self.get_channel(self.ops_channel_id) or await self.fetch_channel(
            self.ops_channel_id
        )

    # 메시지

    async def on_message(self, message: discord.Message) -> None:
        incoming = IncomingMessage(
            message_id=message.id,
            author_id=message.author.id,
            from_bot=message.author.bot or message.webhook_id is not None,
            channel_id=message.channel.id,
            parent_id=getattr(message.channel, "parent_id", None),
        )
        kind = classify(
            incoming,
            zero_user_id=self.zero_user_id,
            forum_ids=self.forums,
            chat_ids=self.chats,
            dept_ids=self.depts,
        )
        if kind is MessageKind.IGNORE:
            return
        if kind is MessageKind.DEPT:
            text = message.content.strip() or EMPTY_POST
            # 스레드 안이면 부모 채널이 부서, 채널 ID가 회의 스레드다
            in_thread = incoming.parent_id is not None
            dept = self.depts[incoming.parent_id if in_thread else incoming.channel_id]
            self.spawn(
                self.dept_chat.on_message(
                    dept,
                    text,
                    message_id=str(message.id),
                    thread=str(incoming.channel_id) if in_thread else None,
                ),
                f"부서 대화 {message.id}",
            )
            return
        if kind is MessageKind.CHAT:
            project = self.chats[incoming.channel_id]
            text = message.content.strip() or EMPTY_POST
            self.spawn(
                self.engine.on_chat(project, text, message_id=str(message.id)),
                f"채팅 {message.id}",
            )
            return
        thread = str(message.channel.id)
        if kind is MessageKind.POST:
            # 일은 제품 채팅 채널의 /task로 시작한다 (ADR-007). 직접 연 포스트는 안내만 한다
            if await self.repo.find_task_by_thread(thread) is None:
                await self.chat.post(thread, "bot", POST_HINT)
        else:
            self.spawn(
                self.engine.on_message(thread, message.content, message_id=str(message.id)),
                f"메시지 {message.id}",
            )

    # 명령

    async def run_command(
        self, interaction: discord.Interaction, name: CommandName, arg: str = ""
    ) -> None:
        error = command_error(
            user_id=interaction.user.id,
            zero_user_id=self.zero_user_id,
            parent_id=getattr(interaction.channel, "parent_id", None),
            forum_ids=self.forums,
        )
        thread = str(interaction.channel_id)
        if error is None and await self.repo.find_task_by_thread(thread) is None:
            error = "이 게시글에 연결된 작업이 없다."
        if error is not None:
            await interaction.response.send_message(error, ephemeral=True)
            return
        # 명령을 게시글 흐름에 남긴다 (2026-10-10 Zero 결정)
        echo = f"/{name.value} {arg}".rstrip()
        await interaction.response.send_message(echo, silent=True)
        self.spawn(self.engine.on_command(thread, name, arg), echo)

    def _chat_project(self, interaction: discord.Interaction) -> tuple[str | None, str | None]:
        error = chat_command_error(
            user_id=interaction.user.id,
            zero_user_id=self.zero_user_id,
            channel_id=interaction.channel_id or 0,
            chat_ids=self.chats,
        )
        if error is not None:
            return None, error
        return self.chats[interaction.channel_id or 0], None

    async def run_task(self, interaction: discord.Interaction, command: str, title: str) -> None:
        """/task feature|bug, /release: 포럼 포스트를 열고 흐름을 시작한다 (ADR-007)."""
        project, error = self._chat_project(interaction)
        if error is None and not title.strip():
            error = "제목을 적는다."
        if error is None and self.engine.pipeline_for_command(command) is None:
            error = f"{command}로 시작하는 흐름이 회사 설정에 없다."
        if error is not None or project is None:
            await interaction.response.send_message(error, ephemeral=True)
            return
        echo = f"{command} {title}".strip()
        await interaction.response.send_message(echo, silent=True)
        self.spawn(self.engine.task_from_chat(project, command, title.strip()), echo)

    async def run_debate(self, interaction: discord.Interaction) -> None:
        project, error = self._chat_project(interaction)
        if error is not None or project is None:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.send_message("/debate", silent=True)

        async def debate() -> None:
            problem = await self.engine.debate(project)
            if problem:
                await self.chat.chat(project, "bot", problem)

        self.spawn(debate(), "/debate")

    async def run_idea(self, interaction: discord.Interaction, text: str) -> None:
        project, error = self._chat_project(interaction)
        if error is None and not text.strip():
            error = "아이디어 내용을 적는다."
        if error is not None or project is None:
            await interaction.response.send_message(error, ephemeral=True)
            return
        number = await self.engine.add_idea(project, text)
        await interaction.response.send_message(
            f"아이디어 #{number}로 기록했다: {text.strip()}", silent=True
        )

    async def show_status(self, interaction: discord.Interaction) -> None:
        error = command_error(
            user_id=interaction.user.id,
            zero_user_id=self.zero_user_id,
            parent_id=getattr(interaction.channel, "parent_id", None),
            forum_ids=self.forums,
        )
        task = None if error else await self.repo.find_task_by_thread(str(interaction.channel_id))
        if error is None and task is None:
            error = "이 게시글에 연결된 작업이 없다."
        text = error if task is None else render_status(task, self.clock.now())
        await interaction.response.send_message(text, ephemeral=True)

    async def show_ping(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.zero_user_id:
            await interaction.response.send_message(
                "이 봇의 명령은 Zero만 쓸 수 있다.", ephemeral=True
            )
            return
        running = await self.repo.list_running_agent_runs()
        active = await self.repo.list_tasks(ACTIVE_STATES)
        paused = await self.repo.get_state(PAUSE_KEY) or ""
        text = render_ping(
            running_cli=len(running),
            active_tasks=len(active),
            waiting=self.engine.waiting,
            paused=paused,
            held=await self.held_label(),
        )
        await interaction.response.send_message(text, ephemeral=True)

    def hold_choices(self) -> list[tuple[str, str]]:
        return [(HOLD_ALL, "전체 주기 작업")] + [(j.job, j.name) for j in self.schedule_list()]

    async def run_hold(self, interaction: discord.Interaction, job: str, *, held: bool) -> None:
        """주기 작업 홀딩 (/hold, ADR-014). 어느 채널에서든 Zero만 쓸 수 있다."""
        if interaction.user.id != self.zero_user_id:
            await interaction.response.send_message(
                "이 봇의 명령은 Zero만 쓸 수 있다.", ephemeral=True
            )
            return
        try:
            await self.set_hold(job, held, source="/hold")
        except KeyError:
            await interaction.response.send_message(f"모르는 주기 작업이다: {job}", ephemeral=True)
            return
        label = await self.held_label()
        await interaction.response.send_message(
            f"홀딩 중인 주기 작업: {label or '없음'}", ephemeral=True
        )

    async def run_hire(
        self, interaction: discord.Interaction, *, accept: bool, number: int, note: str
    ) -> None:
        """팀장의 채용 제안에 답한다 (운영 규약 1.12). 어느 채널에서든 Zero만 쓸 수 있다."""
        if interaction.user.id != self.zero_user_id:
            await interaction.response.send_message(
                "이 봇의 명령은 Zero만 쓸 수 있다.", ephemeral=True
            )
            return
        text = await self.engine.decide_hiring(number, accept=accept, note=note)
        await interaction.response.send_message(text, silent=True)

    # 백그라운드 실행

    def spawn(self, work: Coroutine[Any, Any, Any], label: str) -> None:
        task = asyncio.create_task(self._guard(work, label))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _guard(self, work: Coroutine[Any, Any, Any], label: str) -> None:
        try:
            await work
        except Exception as error:
            log.exception("%s 처리 중 오류", label)
            try:
                await self.chat.notify_ops(
                    f"{label} 처리 중 오류: {type(error).__name__}: {error}",
                    mention=should_mention(Moment.BOT_ERROR),
                )
            except Exception:
                log.exception("운영 채널 알림 실패")


def _register_commands(bot: ZeroSyncBot) -> None:
    tree, guild = bot.tree, bot.guild_ref

    # 제품 채팅 채널 (ADR-007)

    @tree.command(
        name="task", description="채팅의 의견을 포럼 포스트로 옮겨 일을 시작한다", guild=guild
    )
    @app_commands.rename(kind="종류", title="제목")
    @app_commands.describe(kind="기능 개발 또는 버그 수정", title="작업 제목")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="기능 개발", value="feature"),
            app_commands.Choice(name="버그 수정", value="bug"),
        ]
    )
    async def task(
        interaction: discord.Interaction, kind: app_commands.Choice[str], title: str
    ) -> None:
        await bot.run_task(interaction, f"/task {kind.value}", title)

    @tree.command(name="release", description="출시 준비를 시작한다", guild=guild)
    @app_commands.rename(version="버전")
    @app_commands.describe(version="예: 1.0.0")
    async def release(interaction: discord.Interaction, version: str) -> None:
        await bot.run_task(interaction, "/release", version)

    @tree.command(name="debate", description="직전에 답한 두 팀장이 서로 반박 1회", guild=guild)
    async def debate(interaction: discord.Interaction) -> None:
        await bot.run_debate(interaction)

    @tree.command(name="idea", description="바로 하지 않을 아이디어를 기록한다", guild=guild)
    @app_commands.rename(text="내용")
    @app_commands.describe(text="아이디어")
    async def idea(interaction: discord.Interaction, text: str) -> None:
        await bot.run_idea(interaction, text)

    # 포럼 포스트 안의 진행 명령

    @tree.command(name="approve", description="산출물을 승인하고 다음 단계로 간다", guild=guild)
    async def approve(interaction: discord.Interaction) -> None:
        await bot.run_command(interaction, CommandName.APPROVE)

    @tree.command(name="revise", description="산출물을 고쳐 달라고 요청한다", guild=guild)
    @app_commands.rename(request="요청")
    @app_commands.describe(request="고칠 내용")
    async def revise(interaction: discord.Interaction, request: str) -> None:
        await bot.run_command(interaction, CommandName.REVISE, request)

    @tree.command(name="skip", description="문서 반영을 건너뛴다", guild=guild)
    async def skip(interaction: discord.Interaction) -> None:
        await bot.run_command(interaction, CommandName.SKIP)

    @tree.command(name="fix", description="merge 전에 [권장] 지적을 1회 반영한다", guild=guild)
    async def fix(interaction: discord.Interaction) -> None:
        await bot.run_command(interaction, CommandName.FIX, "권장 반영")

    @tree.command(name="resume", description="확인 필요 상태에서 직전 단계로 재개한다", guild=guild)
    async def resume(interaction: discord.Interaction) -> None:
        await bot.run_command(interaction, CommandName.RESUME)

    @tree.command(
        name="stop", description="작업을 멈추고 실행 중인 에이전트를 종료한다", guild=guild
    )
    async def stop(interaction: discord.Interaction) -> None:
        await bot.run_command(interaction, CommandName.STOP)

    @tree.command(name="hire", description="팀장의 채용 제안에 답한다", guild=guild)
    @app_commands.rename(decision="결정", number="번호", note="메모")
    @app_commands.describe(
        decision="수락 또는 거절", number="채용 제안 번호", note="거절 이유 (다음 제안에 반영)"
    )
    @app_commands.choices(
        decision=[
            app_commands.Choice(name="수락", value="accept"),
            app_commands.Choice(name="거절", value="reject"),
        ]
    )
    async def hire(
        interaction: discord.Interaction,
        decision: app_commands.Choice[str],
        number: int,
        note: str = "",
    ) -> None:
        await bot.run_hire(interaction, accept=decision.value == "accept", number=number, note=note)

    @tree.command(name="status", description="이 작업의 상태와 진행 횟수", guild=guild)
    async def status(interaction: discord.Interaction) -> None:
        await bot.show_status(interaction)

    @tree.command(name="daily", description="오늘 개념 노트와 문제를 지금 낸다", guild=guild)
    async def daily(interaction: discord.Interaction) -> None:
        await bot.run_daily(interaction)

    @tree.command(
        name="weekly", description="이번 주 학습 보고와 주간 문제를 지금 만든다", guild=guild
    )
    async def weekly(interaction: discord.Interaction) -> None:
        await bot.run_weekly(interaction)

    @tree.command(
        name="evening", description="가장 최근 하루 정리(Daily)를 지금 만든다", guild=guild
    )
    async def evening(interaction: discord.Interaction) -> None:
        await bot.run_evening(interaction)

    @tree.command(name="hold", description="주기 작업을 홀딩하거나 푼다", guild=guild)
    @app_commands.rename(job="작업", action="동작")
    @app_commands.describe(job="주기 작업 (전체는 *)", action="홀딩 또는 해제")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="홀딩", value="hold"),
            app_commands.Choice(name="해제", value="release"),
        ]
    )
    async def hold(
        interaction: discord.Interaction, job: str, action: app_commands.Choice[str]
    ) -> None:
        await bot.run_hold(interaction, job, held=action.value == "hold")

    @hold.autocomplete("job")
    async def hold_jobs(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=name, value=key)
            for key, name in bot.hold_choices()
            if current.lower() in f"{key} {name}".lower()
        ][:25]

    @tree.command(name="ping", description="봇 생존과 실행 중인 CLI 수", guild=guild)
    async def ping(interaction: discord.Interaction) -> None:
        await bot.show_ping(interaction)


async def run_bot(config: Config) -> None:
    """DB를 열고 러너·엔진·Discord 클라이언트를 연결해 종료될 때까지 실행한다."""
    from bot.agents import ClaudeCliRunner, CodexCliRunner, ProcessRunner
    from bot.github import GitOps
    from bot.store import connect
    from bot.testrun import TestRunner

    conn = await connect(config.paths.db)
    try:
        process = ProcessRunner(config.paths.runs)
        runners: dict[EngineName, AgentRunner] = {
            "claude": ClaudeCliRunner(process, config.cli.claude, config.cli.claude_models),
            "codex": CodexCliRunner(process, config.cli.codex, config.cli.codex_models),
        }
        tests = TestRunner(
            process,
            concurrent=config.limits.concurrent_builds,
            timeout_sec=config.limits.cli_timeout_sec,
        )
        repo = Repository(conn, SystemClock())
        if renamed := await repo.rename_agents(config.org.renames):
            log.info("예전 에이전트 이름 기록 %d줄을 새 이름으로 옮겼다", renamed)
        bot = ZeroSyncBot(config, repo, runners, git=GitOps(), tests=tests)
        async with bot:
            await bot.start(config.secrets.discord_token.get_secret_value())
        if bot.startup_error is not None:
            raise bot.startup_error
    finally:
        await conn.close()
