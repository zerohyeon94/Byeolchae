"""`python -m bot.dashboard [--port 8765]`"""

import argparse
import sys

from bot.config import ConfigError, load_config
from bot.dashboard.features import scheduled_jobs
from bot.dashboard.server import DEFAULT_PORT, HOST, DashboardSettings, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.dashboard", description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        config = load_config()
    except ConfigError as error:
        print(f"설정 오류: {error}", file=sys.stderr)
        return 1
    settings = DashboardSettings(
        db=config.paths.db,
        runs_dir=config.paths.runs,
        guild_id=config.secrets.discord_guild_id,
        daily_calls={
            name: spec.daily_calls
            for name, spec in config.org.agents.items()
            if spec.daily_calls is not None
        },
        org=config.org,
        pipelines=config.pipelines,
        education_agent=config.education.agent if config.education else None,
        education_vault=config.education.vault if config.education else None,
        secretary_agent=config.secretary.agent if config.secretary else None,
        projects=tuple(config.projects),
        schedules=scheduled_jobs(
            config.pipelines,
            config.projects,
            education=(config.education.schedule, config.education.weekly)
            if config.education
            else None,
            secretary=config.secretary.schedule if config.secretary else None,
        ),
        lock=config.paths.lock,
    )
    server = serve(settings, args.port)
    print(f"대시보드: http://{HOST}:{args.port}  (종료: Ctrl-C)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
