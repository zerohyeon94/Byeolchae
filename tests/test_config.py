from pathlib import Path

import pytest

from bot.agents import Stage
from bot.config import OPTIONAL_ENV_KEYS, PACKAGE_DIR, ConfigError, load_config, parse_env

EXAMPLE = PACKAGE_DIR / "projects.example.yaml"
ENV_EXAMPLE = PACKAGE_DIR.parent / ".env.example"

ENV = {
    "DISCORD_TOKEN": "token-secret-value",
    "DISCORD_GUILD_ID": "111",
    "ZERO_USER_ID": "222",
    "OPS_CHANNEL_ID": "333",
}

TEST_LINE = (
    "    test: xcodebuild test -scheme GagaeSsi"
    " -destination 'platform=iOS Simulator,name=iPhone 16'\n"
)
PROJECT = (
    """\
    forum_channel_id: "100"
    repo: ~/Dev/GagaeSsi
    github: owner/GagaeSsi
"""
    + TEST_LINE
    + """\
    vault_dir: 일일 예산 관리 앱
    vault_hub: 가게씨.md
    vault_tag: brain/gagaessi
    vault_slug: gagaessi
"""
)


def yaml_text(project=PROJECT, extra=""):
    # company는 이 맥의 실제 회사 설정을 읽지 않게 없는 경로로 둔다
    return (
        f"projects:\n  gagaessi:\n{project}paths:\n  vault_projects: /vault/Projects\n"
        f"  company: /nonexistent/zerosync-company\n{extra}"
    )


def write(tmp_path, text, name="projects.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path, text=None, environ=ENV, env_text=None):
    projects = write(tmp_path, yaml_text() if text is None else text)
    env_path = tmp_path / ".env"
    if env_text is not None:
        env_path.write_text(env_text, encoding="utf-8")
    return load_config(projects, env_path, environ)


# 정상 로드


def test_loads_project_with_defaults(tmp_path):
    config = load(tmp_path)
    project = config.projects["gagaessi"]
    assert project.repo == Path.home() / "Dev/GagaeSsi"
    assert project.base_branch == "develop"
    assert project.test == (
        "xcodebuild",
        "test",
        "-scheme",
        "GagaeSsi",
        "-destination",
        "platform=iOS Simulator,name=iPhone 16",
    )
    assert config.paths.db == Path.home() / ".zerosync/zerosync.db"
    assert config.paths.lock == Path.home() / ".zerosync/bot.lock"
    assert config.cli.claude == "claude"
    assert config.limits.max_review_rounds == 3
    assert config.limits.max_test_retries == 2
    assert config.limits.cli_timeout_sec == 3600


def test_test_command_may_be_a_list(tmp_path):
    project = PROJECT.replace(
        TEST_LINE,
        "    test: [swift, test]\n",
    )
    assert load(tmp_path, yaml_text(project)).projects["gagaessi"].test == ("swift", "test")


def test_overrides_limits_and_paths(tmp_path):
    extra = "limits:\n  max_review_rounds: 5\ncli:\n  claude: /opt/bin/claude\n"
    text = yaml_text(extra=extra).replace(
        "  vault_projects: /vault/Projects\n",
        "  vault_projects: /vault/Projects\n  db: /data/zs.db\n",
    )
    config = load(tmp_path, text)
    assert config.limits.max_review_rounds == 5
    assert config.limits.max_test_retries == 2
    assert config.cli.claude == "/opt/bin/claude"
    assert config.paths.lock == Path("/data/bot.lock")


def test_project_for_forum(tmp_path):
    config = load(tmp_path)
    assert config.project_for_forum("100") == "gagaessi"
    assert config.project_for_forum("999") is None


def test_example_file_is_valid(monkeypatch, tmp_path):
    # 예시의 company(~/.zerosync/company)가 이 맥의 실제 회사 설정을 가리키지 않게 한다
    monkeypatch.setenv("HOME", str(tmp_path))
    environ = {key: "1" for key in ENV}
    config = load_config(EXAMPLE, Path("/nonexistent/.env"), environ)
    assert list(config.projects) == ["gagaessi"]


def test_env_example_lists_every_key():
    keys = set(parse_env(ENV_EXAMPLE.read_text(encoding="utf-8")))
    assert keys == set(ENV) | set(OPTIONAL_ENV_KEYS)


def test_report_channel_is_optional(tmp_path):
    assert load(tmp_path).secrets.report_channel_id is None
    config = load(tmp_path, environ={**ENV, "REPORT_CHANNEL_ID": "77"})
    assert config.secrets.report_channel_id == "77"


# 프로젝트 설정 검증


@pytest.mark.parametrize(
    ("old", "new", "where"),
    [
        ('forum_channel_id: "100"', 'forum_channel_id: "abc"', "forum_channel_id"),
        ("repo: ~/Dev/GagaeSsi", "repo: Dev/GagaeSsi", "repo"),
        ("github: owner/GagaeSsi", "github: GagaeSsi", "github"),
        ("vault_slug: gagaessi", 'vault_slug: ""', "vault_slug"),
        ("vault_slug: gagaessi", "vault_slug: gagaessi\n    typo_key: 1", "typo_key"),
    ],
)
def test_rejects_invalid_project_field(tmp_path, old, new, where):
    with pytest.raises(ConfigError, match=f"projects.gagaessi.{where}"):
        load(tmp_path, yaml_text(PROJECT.replace(old, new)))


def test_rejects_main_as_base_branch(tmp_path):
    project = PROJECT + "    base_branch: main\n"
    with pytest.raises(ConfigError, match="main"):
        load(tmp_path, yaml_text(project))


@pytest.mark.parametrize("test_value", ['""', "[]", '"xcodebuild \'unclosed"'])
def test_rejects_bad_test_command(tmp_path, test_value):
    project = PROJECT.replace(
        TEST_LINE,
        f"    test: {test_value}\n",
    )
    with pytest.raises(ConfigError, match="projects.gagaessi.test"):
        load(tmp_path, yaml_text(project))


def test_rejects_bad_project_key(tmp_path):
    with pytest.raises(ConfigError, match="GagaeSsi"):
        load(tmp_path, yaml_text().replace("  gagaessi:", "  GagaeSsi:"))


def test_rejects_duplicate_forum_channel(tmp_path):
    text = yaml_text().replace("paths:", "  jday:\n" + PROJECT + "paths:")
    with pytest.raises(ConfigError, match="채널 ID가 겹친다"):
        load(tmp_path, text)


def test_rejects_non_positive_limit(tmp_path):
    with pytest.raises(ConfigError, match="limits.max_review_rounds"):
        load(tmp_path, yaml_text(extra="limits:\n  max_review_rounds: 0\n"))


def test_requires_at_least_one_project(tmp_path):
    with pytest.raises(ConfigError, match="projects"):
        load(tmp_path, "projects: {}\npaths:\n  vault_projects: /vault\n")


# 파일 오류


def test_missing_projects_file(tmp_path):
    with pytest.raises(ConfigError, match="projects.example.yaml"):
        load_config(tmp_path / "projects.yaml", tmp_path / ".env", ENV)


def test_broken_yaml_reports_line(tmp_path):
    with pytest.raises(ConfigError, match="2번째 줄"):
        load(tmp_path, "projects: {}\nbad: key: value\n")


def test_top_level_must_be_mapping(tmp_path):
    with pytest.raises(ConfigError, match="최상위"):
        load(tmp_path, "- a\n- b\n")


# 환경변수


def test_reads_env_file_when_environ_is_empty(tmp_path):
    env_text = "\n".join(f"{key}={value}" for key, value in ENV.items())
    config = load(tmp_path, environ={}, env_text=env_text)
    assert config.secrets.discord_token.get_secret_value() == "token-secret-value"
    assert config.secrets.ops_channel_id == "333"


def test_environ_overrides_env_file(tmp_path):
    env_text = "\n".join(f"{key}={value}" for key, value in ENV.items())
    config = load(tmp_path, environ={"ZERO_USER_ID": "999"}, env_text=env_text)
    assert config.secrets.zero_user_id == "999"
    assert config.secrets.discord_guild_id == "111"


def test_reports_missing_env_keys(tmp_path):
    with pytest.raises(ConfigError, match="DISCORD_TOKEN, OPS_CHANNEL_ID"):
        load(tmp_path, environ={"DISCORD_GUILD_ID": "1", "ZERO_USER_ID": "2"})


def test_errors_and_repr_do_not_leak_token(tmp_path):
    bad = dict(ENV, DISCORD_GUILD_ID="not-a-number")
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, environ=bad)
    assert "discord_guild_id" in str(caught.value)
    assert "token-secret-value" not in str(caught.value)
    assert "not-a-number" not in str(caught.value)

    config = load(tmp_path)
    assert "token-secret-value" not in repr(config)
    assert "token-secret-value" not in str(config.model_dump())


# .env 파싱


def test_parse_env_formats():
    text = "# 주석\n\nA=1\nexport B = two words \nC=\"quoted # not comment\"\nD='single'\nE=\n"
    assert parse_env(text) == {
        "A": "1",
        "B": "two words",
        "C": "quoted # not comment",
        "D": "single",
        "E": "",
    }


def test_parse_env_rejects_bad_line_without_value():
    with pytest.raises(ConfigError, match="2번째 줄") as caught:
        parse_env("A=1\nsecret-token-without-key\n")
    assert "secret-token" not in str(caught.value)


def test_parse_env_rejects_duplicate_key():
    with pytest.raises(ConfigError, match="A가 두 번"):
        parse_env("A=1\nA=2\n")


# 단계별 모델


def test_cli_models_by_stage(tmp_path):
    extra = (
        "cli:\n"
        "  claude_models:\n    opinion: sonnet\n    implement: opus\n"
        "  codex_models:\n    review: gpt-x\n"
    )
    config = load(tmp_path, yaml_text(extra=extra))
    assert config.cli.claude_models == {Stage.OPINION: "sonnet", Stage.IMPLEMENT: "opus"}
    assert config.cli.codex_models == {Stage.REVIEW: "gpt-x"}
    assert load(tmp_path).cli.claude_models == {}


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ("cli:\n  codex_models:\n    implement: gpt\n", "Codex가 맡지 않는 단계: implement"),
        ("cli:\n  claude_models:\n    opinon: sonnet\n", "cli.claude_models"),
        ("cli:\n  claude_models:\n    opinion: ''\n", "cli.claude_models"),
    ],
)
def test_rejects_bad_cli_models(tmp_path, extra, match):
    with pytest.raises(ConfigError, match=match):
        load(tmp_path, yaml_text(extra=extra))


# 회사 설정 (SPEC 9.1-1)


def test_org_defaults_when_company_dir_has_no_org(tmp_path):
    config = load(tmp_path)
    assert config.paths.company.as_posix() == "/nonexistent/zerosync-company"
    assert config.org.roles.review == "delta"


def test_org_is_loaded_from_company_dir(tmp_path):
    company = tmp_path / "company"
    company.mkdir()
    (company / "org.yaml").write_text(
        "teams:\n  dev: {name: 개발, lead: alpha}\n  qa: {name: QA, lead: delta}\n"
        "agents:\n"
        "  alpha: {display: Alpha, team: dev, perspective: p, basic_question: q}\n"
        "  delta: {display: Delta, team: qa, perspective: p, basic_question: q}\n"
        "roles: {opinions: [alpha], design: alpha, implement: alpha, review: delta,"
        " doc_sync: alpha}\n",
        encoding="utf-8",
    )
    (company / "pipelines").mkdir()
    (company / "pipelines" / "fix.yaml").write_text(
        "name: 수정\ncommand: /task bug\ntriggers: [discord_task]\nstages:\n"
        "  - {id: d, kind: diagnose, agent: delta}\n  - {id: a, kind: gate}\n"
        "  - {id: i, kind: implement, agent: alpha}\n  - {id: t, kind: test}\n"
        "  - {id: r, kind: review, agent: delta}\n  - {id: m, kind: merge_gate}\n"
        "budget: {max_calls: 9, max_minutes: 60}\n",
        encoding="utf-8",
    )
    text = yaml_text().replace("/nonexistent/zerosync-company", str(company))
    config = load(tmp_path, text)
    assert config.org.agent_names == ("alpha", "delta")
    assert list(config.pipelines) == ["fix"]


def test_default_pipelines_need_default_agents(tmp_path):
    # org.yaml을 바꾸고 파이프라인을 두지 않으면 기본 파이프라인의 담당이 없어 막는다
    company = tmp_path / "company"
    company.mkdir()
    (company / "org.yaml").write_text(
        "teams:\n  dev: {name: 개발, lead: alpha}\n  qa: {name: QA, lead: delta}\n"
        "agents:\n"
        "  alpha: {display: Alpha, team: dev, perspective: p, basic_question: q}\n"
        "  delta: {display: Delta, team: qa, perspective: p, basic_question: q}\n"
        "roles: {opinions: [alpha], design: alpha, implement: alpha, review: delta,"
        " doc_sync: alpha}\n",
        encoding="utf-8",
    )
    text = yaml_text().replace("/nonexistent/zerosync-company", str(company))
    with pytest.raises(ConfigError, match="R4"):
        load(tmp_path, text)


def test_broken_org_is_a_config_error(tmp_path):
    company = tmp_path / "company"
    company.mkdir()
    (company / "org.yaml").write_text("teams: {}\nagents: {}\nroles: {review: x}\n")
    text = yaml_text().replace("/nonexistent/zerosync-company", str(company))
    with pytest.raises(ConfigError, match="org.yaml"):
        load(tmp_path, text)


# 교육 (ADR-009)


def test_education_section_is_optional_and_validated(tmp_path):
    assert load(tmp_path).education is None
    config = load(
        tmp_path,
        yaml_text(extra='education:\n  vault: ~/vault\n  channel_id: "555"\n'),
    )
    education = config.education
    assert education.vault == Path.home() / "vault"
    assert (education.agent, education.schedule, education.channel_id) == (
        "eta",
        "30 7 * * *",
        "555",
    )
    assert education.illustrator is None  # 그림은 설정해야 넣는다
    assert education.codex_images == Path.home() / ".codex/generated_images"
    config = load(tmp_path, yaml_text(extra="education:\n  vault: ~/vault\n  illustrator: iota\n"))
    assert config.education.illustrator == "iota"
    with pytest.raises(ConfigError, match="Codex 엔진"):
        load(tmp_path, yaml_text(extra="education:\n  vault: ~/vault\n  illustrator: eta\n"))
    with pytest.raises(ConfigError, match="omega"):
        load(tmp_path, yaml_text(extra="education:\n  vault: ~/vault\n  illustrator: omega\n"))
    with pytest.raises(ConfigError, match="omega"):
        load(tmp_path, yaml_text(extra="education:\n  vault: ~/vault\n  agent: omega\n"))
    with pytest.raises(ConfigError, match="schedule"):
        load(tmp_path, yaml_text(extra='education:\n  vault: ~/vault\n  schedule: "x"\n'))


def test_secretary_section_is_optional_and_validated(tmp_path):
    assert load(tmp_path).secretary is None
    config = load(tmp_path, yaml_text(extra='secretary:\n  vault: ~/vault\n  channel_id: "777"\n'))
    secretary = config.secretary
    assert (secretary.vault, secretary.claude_projects, secretary.codex_sessions) == (
        Path.home() / "vault",
        Path.home() / ".claude" / "projects",
        Path.home() / ".codex" / "sessions",
    )
    assert (secretary.agent, secretary.schedule, secretary.channel_id) == (
        "theta",
        "30 23 * * *",
        "777",
    )
    with pytest.raises(ConfigError, match="omega"):
        load(tmp_path, yaml_text(extra="secretary:\n  vault: ~/vault\n  agent: omega\n"))
    with pytest.raises(ConfigError, match="schedule"):
        load(tmp_path, yaml_text(extra='secretary:\n  vault: ~/vault\n  schedule: "x"\n'))
