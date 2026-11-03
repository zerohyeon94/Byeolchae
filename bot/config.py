"""봇 설정 로더: `bot/projects.yaml` + `.env` (SPEC 9.1, 9.2).

- 실제 값 파일(`bot/projects.yaml`, `.env`)은 커밋하지 않는다. 예시는 `bot/projects.example.yaml`,
  `.env.example`에 있다 (SPEC Q11)
- 오류 메시지에는 키 이름과 위치만 담고 값은 넣지 않는다. 토큰이 로그에 섞이지 않게 하기 위해서다
- `.env`보다 실제 환경변수가 우선한다 (launchd plist의 EnvironmentVariables 등)
"""

import os
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from bot.agents.base import Stage
from bot.org import Org, OrgError, load_org
from bot.pipeline import Pipeline, PipelineError, load_pipelines
from bot.scheduler import CronError, parse_cron

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PROJECTS_PATH = PACKAGE_DIR / "projects.yaml"
DEFAULT_ENV_PATH = PACKAGE_DIR.parent / ".env"

ENV_KEYS = ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "ZERO_USER_ID", "OPS_CHANNEL_ID")
OPTIONAL_ENV_KEYS = ("REPORT_CHANNEL_ID",)

_PROJECT_KEY = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class ConfigError(ValueError):
    """설정 파일이 없거나 형식이 틀렸다. 메시지에 값은 담지 않는다."""


def _expand_path(value: Path) -> Path:
    path = value.expanduser()
    if not path.is_absolute():
        raise ValueError("절대 경로이거나 ~로 시작해야 한다")
    return path


ExpandedPath = Annotated[Path, AfterValidator(_expand_path)]
DiscordId = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]


class _Model(BaseModel):
    # 오타 난 키를 조용히 무시하지 않는다
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ProjectConfig(_Model):
    forum_channel_id: DiscordId
    # 제품 채팅 채널: 팀장과 의견을 나누고 /task로 일을 시작한다 (ADR-007)
    chat_channel_id: DiscordId | None = None
    # 제품 Claude 디자인 시스템 링크 (ADR-007, 화면 디자인 단계)
    design_system_url: str | None = None
    repo: ExpandedPath
    github: str = Field(pattern=_GITHUB_REPO.pattern)
    base_branch: str = "develop"
    # 셸을 거치지 않고 실행하므로 인자 목록으로 나눠 둔다 (SPEC 2.1)
    test: tuple[str, ...]
    vault_dir: str = Field(min_length=1)
    vault_hub: str = Field(min_length=1)
    vault_tag: str = Field(min_length=1)
    vault_slug: str = Field(min_length=1)

    @field_validator("base_branch")
    @classmethod
    def _not_main(cls, value: str) -> str:
        # 봇은 main에 접근하지 않는다 (SPEC 9.5)
        if value == "main":
            raise ValueError("기준 브랜치로 main을 쓸 수 없다")
        return value

    @field_validator("test", mode="before")
    @classmethod
    def _split_test(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                value = shlex.split(value)
            except ValueError as error:
                raise ValueError("test 명령의 따옴표가 닫히지 않았다") from error
        if not value:
            raise ValueError("test 명령이 비어 있다")
        return value


class PathsConfig(_Model):
    vault_projects: ExpandedPath
    worktrees: ExpandedPath = Path("~/.zerosync/worktrees")
    runs: ExpandedPath = Path("~/.zerosync/runs")
    db: ExpandedPath = Path("~/.zerosync/zerosync.db")
    # 비공개 회사 설정 저장소 (org.yaml, agents/). 없으면 기본 조직을 쓴다 (SPEC 9.1-1)
    company: ExpandedPath = Path("~/.zerosync/company")

    @property
    def lock(self) -> Path:
        # 단일 인스턴스 락은 DB와 같은 폴더에 둔다.
        # 한 DB를 두 봇이 함께 쓰지 않게 하는 것이 목적이다
        return self.db.parent / "bot.lock"


ModelName = Annotated[str, Field(min_length=1)]

# 단계별 기본 모델을 둘 수 있는 단계 (SPEC 5.2). Codex는 읽기 전용 단계만 맡는다
CLAUDE_STAGES = frozenset(Stage)
CODEX_STAGES = frozenset(
    {Stage.OPINION, Stage.DEBATE, Stage.REVIEW, Stage.DIAGNOSE, Stage.CONCEPT_IMAGE}
)


class CliConfig(_Model):
    # launchd PATH에서 못 찾으면 절대 경로를 적는다 (SPEC 5.2)
    claude: str = Field(default="claude", min_length=1)
    codex: str = Field(default="codex", min_length=1)
    # 단계별 모델. 적지 않은 단계는 각 CLI의 기본 모델을 쓴다
    claude_models: dict[Stage, ModelName] = {}
    codex_models: dict[Stage, ModelName] = {}

    @field_validator("claude_models")
    @classmethod
    def _claude_stages(cls, value: dict[Stage, str]) -> dict[Stage, str]:
        return _check_stages(value, CLAUDE_STAGES, "Claude")

    @field_validator("codex_models")
    @classmethod
    def _codex_stages(cls, value: dict[Stage, str]) -> dict[Stage, str]:
        return _check_stages(value, CODEX_STAGES, "Codex")


def _check_stages(
    value: dict[Stage, str], allowed: frozenset[Stage], agent: str
) -> dict[Stage, str]:
    extra = sorted(stage.value for stage in value if stage not in allowed)
    if extra:
        raise ValueError(f"{agent}가 맡지 않는 단계: {', '.join(extra)}")
    return value


class LimitsConfig(_Model):
    max_review_rounds: int = Field(default=3, ge=1)
    max_test_retries: int = Field(default=2, ge=0)
    cli_timeout_sec: int = Field(default=3600, ge=1)
    concurrent_builds: int = Field(default=1, ge=1)
    # 에이전트 CLI 동시 실행 수 (ADR-007: 한 번에 하나)
    concurrent_agents: int = Field(default=1, ge=1)
    # 구독 사용량 한도에 걸렸을 때 대기열을 멈추는 시간 [검증 필요] (SPEC V15)
    usage_pause_minutes: int = Field(default=60, ge=1)
    merge_poll_sec: int = Field(default=300, ge=1)
    remind_after_hours: int = Field(default=24, ge=1)


class EducationConfig(_Model):
    """교육 부서 Daily (ADR-009). 없으면 교육 일정을 돌리지 않는다."""

    # Zero의 학습 볼트 루트 (AGENTS.md, Daily/, Schedule/가 있는 폴더)
    vault: ExpandedPath
    agent: str = "eta"
    schedule: str = "30 7 * * *"  # 매일 07:30, 아침 보고(08:00) 전
    weekly: str = "0 20 * * 0"  # 주간 학습 보고와 주간 문제. 일요일 20:00
    # #교육 채널. 없으면 #대표실(없으면 운영 채널)에 알린다
    channel_id: DiscordId | None = None
    # 개념 노트 그림을 그리는 Codex 엔진 팀원. 없으면 그림 없이 낸다
    illustrator: str | None = None
    # Codex가 만든 그림이 저장되는 곳. 봇이 여기서 볼트 Assets/로 옮긴다
    codex_images: ExpandedPath = Path("~/.codex/generated_images")

    @field_validator("schedule", "weekly")
    @classmethod
    def _cron(cls, value: str) -> str:
        try:
            parse_cron(value)
        except CronError as error:
            raise ValueError(str(error)) from None
        return value


class SecretaryConfig(_Model):
    """비서 부서 저녁 Daily와 아침 보고 (ADR-010). 없으면 아침 보고는 #대표실에 그대로 간다."""

    # Daily 파일을 쓰는 볼트 루트 (Daily/YYYY-MM/YYYY-MM-DD.md)
    vault: ExpandedPath
    agent: str = "theta"
    schedule: str = "30 23 * * *"  # 매일 23:30. 놓치면 다음에 켜질 때(아침) 그날 것을 한 번 쓴다
    # #비서 채널. 없으면 #대표실(없으면 운영 채널)
    channel_id: DiscordId | None = None
    # 토큰 사용량을 읽는 로컬 기록
    claude_projects: ExpandedPath = Path("~/.claude/projects")
    codex_sessions: ExpandedPath = Path("~/.codex/sessions")

    @field_validator("schedule")
    @classmethod
    def _cron(cls, value: str) -> str:
        try:
            parse_cron(value)
        except CronError as error:
            raise ValueError(str(error)) from None
        return value


class ProjectsFile(_Model):
    projects: dict[str, ProjectConfig] = Field(min_length=1)
    paths: PathsConfig
    education: EducationConfig | None = None
    secretary: SecretaryConfig | None = None
    cli: CliConfig = CliConfig()
    limits: LimitsConfig = LimitsConfig()

    @field_validator("projects")
    @classmethod
    def _check_keys(cls, value: dict[str, ProjectConfig]) -> dict[str, ProjectConfig]:
        for key in value:
            if not _PROJECT_KEY.match(key):
                raise ValueError(f"프로젝트 키 {key!r}는 영문 소문자·숫자·-만 쓴다 (40자 이내)")
        return value

    @model_validator(mode="after")
    def _unique_forums(self) -> "ProjectsFile":
        seen: dict[str, str] = {}
        for key, project in self.projects.items():
            channels = [project.forum_channel_id]
            if project.chat_channel_id is not None:
                channels.append(project.chat_channel_id)
            for channel in channels:
                if channel in seen:
                    raise ValueError(f"프로젝트 {seen[channel]}와 {key}의 채널 ID가 겹친다")
                seen[channel] = key
        return self


class Secrets(_Model):
    discord_token: SecretStr = Field(min_length=1)
    discord_guild_id: DiscordId
    zero_user_id: DiscordId
    ops_channel_id: DiscordId
    # #대표실 (아침·주간 보고). 없으면 운영 채널로 보고한다 (ADR-007)
    report_channel_id: DiscordId | None = None


class Config(_Model):
    projects: dict[str, ProjectConfig]
    paths: PathsConfig
    cli: CliConfig
    limits: LimitsConfig
    secrets: Secrets
    org: Org
    pipelines: dict[str, Pipeline]
    education: EducationConfig | None = None
    secretary: SecretaryConfig | None = None

    def project_for_chat(self, channel_id: str) -> str | None:
        """제품 채팅 채널 ID로 프로젝트 키를 찾는다."""
        for key, project in self.projects.items():
            if project.chat_channel_id == channel_id:
                return key
        return None

    def project_for_forum(self, channel_id: str) -> str | None:
        """포럼 채널 ID로 프로젝트 키를 찾는다. 등록되지 않은 채널이면 None."""
        for key, project in self.projects.items():
            if project.forum_channel_id == channel_id:
                return key
        return None


def parse_env(text: str) -> dict[str, str]:
    """`.env` 내용을 읽는다.

    빈 줄과 `#` 주석 줄은 건너뛰고, `export ` 접두어와 값을 감싼 따옴표를 허용한다.
    """
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match is None:
            raise ConfigError(f".env {number}번째 줄이 KEY=VALUE 형식이 아니다")
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in values:
            raise ConfigError(f".env에 {key}가 두 번 있다 ({number}번째 줄)")
        values[key] = value
    return values


def load_config(
    projects_path: Path = DEFAULT_PROJECTS_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> Config:
    """설정을 읽어 검증한다. `.env` 파일은 없어도 되지만 필요한 키는 환경변수로라도 있어야 한다."""
    environ = os.environ if environ is None else environ

    try:
        raw = yaml.safe_load(projects_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
            f"{projects_path} 파일이 없다. projects.example.yaml을 복사해 값을 채운다"
        ) from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" ({mark.line + 1}번째 줄)" if mark is not None else ""
        raise ConfigError(f"{projects_path.name}의 YAML 형식이 틀렸다{where}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{projects_path.name}의 최상위는 매핑이어야 한다")

    try:
        env_file = parse_env(env_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        env_file = {}
    merged = {key: environ.get(key, env_file.get(key)) for key in ENV_KEYS}
    missing = [key for key, value in merged.items() if not value]
    if missing:
        raise ConfigError(f"환경변수가 없다: {', '.join(missing)} (.env.example 참고)")

    for key in OPTIONAL_ENV_KEYS:
        value = environ.get(key, env_file.get(key))
        if value:
            merged[key] = value
    try:
        projects = ProjectsFile.model_validate(raw)
        secrets = Secrets.model_validate({key.lower(): value for key, value in merged.items()})
    except ValidationError as error:
        raise ConfigError(_describe(error)) from None
    try:
        org = load_org(projects.paths.company)
        pipelines = load_pipelines(projects.paths.company, org)
    except (OrgError, PipelineError) as error:
        raise ConfigError(str(error)) from None
    education = projects.education
    if education is not None and education.agent not in org.agents:
        raise ConfigError(f"education.agent {education.agent!r}가 조직(org.yaml)에 없다")
    illustrator = education.illustrator if education is not None else None
    if illustrator is not None:
        if illustrator not in org.agents:
            raise ConfigError(f"education.illustrator {illustrator!r}가 조직(org.yaml)에 없다")
        if org.engine(illustrator) != "codex":
            raise ConfigError(f"education.illustrator {illustrator!r}는 Codex 엔진이어야 한다")
    secretary = projects.secretary
    if secretary is not None and secretary.agent not in org.agents:
        raise ConfigError(f"secretary.agent {secretary.agent!r}가 조직(org.yaml)에 없다")
    return Config(
        projects=projects.projects,
        paths=projects.paths,
        cli=projects.cli,
        limits=projects.limits,
        secrets=secrets,
        org=org,
        pipelines=pipelines,
        education=education,
        secretary=secretary,
    )


def _describe(error: ValidationError) -> str:
    # 입력값(토큰일 수 있음)은 빼고 위치와 이유만 남긴다
    lines = []
    for item in error.errors(include_input=False, include_url=False):
        where = ".".join(str(part) for part in item["loc"]) or "(최상위)"
        lines.append(f"- {where}: {item['msg']}")
    return "설정 값이 틀렸다\n" + "\n".join(lines)
