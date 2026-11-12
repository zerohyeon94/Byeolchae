import subprocess
import sys

import bot
from bot.__main__ import main


def test_version_flag_prints_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"byeolchae {bot.__version__}"


def test_unknown_args_fail_with_usage(capsys):
    assert main(["--nope"]) == 2
    assert "--version" in capsys.readouterr().err


ENV = {
    "DISCORD_TOKEN": "token-secret-value",
    "DISCORD_GUILD_ID": "1",
    "ZERO_USER_ID": "2",
    "OPS_CHANNEL_ID": "3",
}


def write_projects(tmp_path):
    path = tmp_path / "projects.yaml"
    path.write_text(
        "projects:\n"
        "  gagaessi:\n"
        '    forum_channel_id: "100"\n'
        "    repo: /repo\n"
        "    github: owner/GagaeSsi\n"
        "    test: swift test\n"
        "    vault_dir: d\n"
        "    vault_hub: h.md\n"
        "    vault_tag: brain/g\n"
        "    vault_slug: g\n"
        "paths:\n"
        "  vault_projects: /vault\n"
        f"  db: {tmp_path / 'state' / 'zerosync.db'}\n"
        # 이 맥의 실제 회사 설정을 읽지 않게 없는 경로로 둔다
        f"  company: {tmp_path / 'company'}\n",
        encoding="utf-8",
    )
    return path


def run_main(tmp_path, environ=ENV, run=None):
    async def fake_run(config):
        # 봇이 실행되는 동안 락을 쥐고 있어야 한다
        lock = config.paths.lock
        assert lock.exists() and lock.read_text().strip()

    return main(
        [],
        projects_path=write_projects(tmp_path),
        env_path=tmp_path / ".env",
        environ=environ,
        run=run or fake_run,
    )


def test_main_loads_config_and_takes_lock(tmp_path, capsys):
    assert run_main(tmp_path) == 0
    err = capsys.readouterr().err
    assert "gagaessi" in err
    assert "token-secret-value" not in err
    # 정상 종료 후 락이 풀려 다시 실행할 수 있다
    assert run_main(tmp_path) == 0


def test_main_fails_on_config_error(tmp_path, capsys):
    assert run_main(tmp_path, environ={}) == 1
    assert "DISCORD_TOKEN" in capsys.readouterr().err


def test_main_fails_when_already_running(tmp_path, capsys):
    from bot.locks import InstanceLock

    with InstanceLock(tmp_path / "state" / "bot.lock"):
        assert run_main(tmp_path) == 1
    assert "이미 실행 중" in capsys.readouterr().err


def test_importing_bot_does_not_load_discord_io():
    # 워크플로 계층은 Discord 계층 없이 import되어야 한다 (운영 규약 1.1-5)
    code = "import sys, bot; print('bot.discord_io' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_main_reports_run_error_without_token(tmp_path, capsys):
    async def failing_run(config):
        raise RuntimeError("로그인 실패")

    assert run_main(tmp_path, run=failing_run) == 1
    err = capsys.readouterr().err
    assert "실행 오류: RuntimeError: 로그인 실패" in err
    assert "token-secret-value" not in err
    # 실패해도 락은 풀린다
    assert run_main(tmp_path) == 0
