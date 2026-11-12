"""`python -m bot` 또는 `byeolchae` 명령의 진입점.

설정을 읽고 단일 인스턴스 락을 잡은 뒤 Discord 봇을 실행한다 (SPEC 2.1, 9.3).
"""

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from bot import __version__
from bot.config import DEFAULT_ENV_PATH, DEFAULT_PROJECTS_PATH, Config, ConfigError, load_config
from bot.locks import AlreadyRunningError, InstanceLock

RunBot = Callable[[Config], Awaitable[None]]


def main(
    argv: list[str] | None = None,
    *,
    projects_path: Path = DEFAULT_PROJECTS_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
    run: RunBot | None = None,
) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["--version"], ["-V"]):
        print(f"byeolchae {__version__}")
        return 0
    if args:
        print("사용법: byeolchae [--version]", file=sys.stderr)
        return 2

    try:
        config = load_config(projects_path, env_path, environ)
    except ConfigError as error:
        print(f"설정 오류: {error}", file=sys.stderr)
        return 1
    if run is None:
        # discord.py는 실제 실행할 때만 불러온다 (--version, 설정 오류를 빠르게)
        from bot.discord_io.client import run_bot

        run = run_bot
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        with InstanceLock(config.paths.lock):
            print(f"설정 확인: 프로젝트 {', '.join(config.projects)}", file=sys.stderr)
            asyncio.run(run(config))
    except AlreadyRunningError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("종료한다", file=sys.stderr)
    except Exception as error:
        # 토큰 오류 등. 토큰 값은 메시지에 들어가지 않는다
        print(f"실행 오류: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
