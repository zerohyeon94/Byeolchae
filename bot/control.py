"""로컬 제어 요청 (ADR-014).

웹 대시보드의 홀딩 버튼과 작업 만들기를 봇이 받는다.
웹은 DB를 읽기 전용으로만 열고, 바꾸는 것은 봇이 한다.
127.0.0.1에만 열고, 봇이 만든 토큰 파일을 읽을 수 있는 프로그램만 요청할 수 있다
(다른 웹페이지가 브라우저로 몰래 보내는 요청을 막는다).

- GET  /schedules  주기 작업 목록과 홀딩 상태
- POST /holds      {"job": 작업 키 또는 "*", "held": true|false}
- POST /tasks      {"project": 프로젝트 키, "command": "/task feature" 등, "title": 제목}
                   → {"task_id", "thread"}
"""

import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8770
TOKEN_HEADER = "X-Byeolchae-Token"


def ensure_token(path: Path) -> str:
    """토큰 파일이 없으면 만든다 (이 맥 사용자만 읽기, 0600)."""
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return token


@dataclass(frozen=True)
class Reply:
    status: int
    body: Mapping[str, Any]


Schedules = Callable[[], Awaitable[Mapping[str, Any]]]
SetHold = Callable[[str, bool], Awaitable[Mapping[str, Any]]]  # 모르는 키면 KeyError
StartTask = Callable[
    [str, str, str], Awaitable[Mapping[str, Any]]
]  # 모르는 프로젝트·명령이면 KeyError


async def handle(
    method: str,
    path: str,
    token: str,
    given: str | None,
    body: bytes,
    *,
    schedules: Schedules,
    set_hold: SetHold,
    start_task: StartTask,
) -> Reply:
    """요청 하나를 처리한다. aiohttp와 분리해 테스트에서 바로 부른다."""
    if given is None or not secrets.compare_digest(given, token):
        return Reply(401, {"error": "토큰이 맞지 않는다"})
    if method == "GET" and path == "/schedules":
        return Reply(200, await schedules())
    if method == "POST" and path in ("/holds", "/tasks"):
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return Reply(400, {"error": "JSON이 아니다"})
        if not isinstance(data, dict):
            data = {}
    if method == "POST" and path == "/tasks":
        project, command, title = (data.get(k) for k in ("project", "command", "title"))
        if not all(isinstance(v, str) for v in (project, command, title)) or not title.strip():
            return Reply(400, {"error": "project, command, title(비지 않은 문자열)이 필요하다"})
        try:
            return Reply(200, await start_task(project, command, title.strip()))
        except KeyError as error:
            return Reply(404, {"error": f"모르는 프로젝트나 명령이다: {error.args[0]}"})
    if method == "POST" and path == "/holds":
        job, held = data.get("job"), data.get("held")
        if not isinstance(job, str) or not isinstance(held, bool):
            return Reply(400, {"error": "job(문자열)과 held(true/false)가 필요하다"})
        try:
            return Reply(200, await set_hold(job, held))
        except KeyError:
            return Reply(404, {"error": f"모르는 주기 작업이다: {job}"})
    return Reply(404, {"error": "없는 경로다"})


async def start(
    token: str,
    *,
    schedules: Schedules,
    set_hold: SetHold,
    start_task: StartTask,
    port: int = PORT,
):
    """제어 서버를 띄우고 runner를 돌려준다. 끝낼 때 runner.cleanup()을 부른다."""

    async def route(request: web.Request) -> web.Response:
        reply = await handle(
            request.method,
            request.path,
            token,
            request.headers.get(TOKEN_HEADER),
            await request.read(),
            schedules=schedules,
            set_hold=set_hold,
            start_task=start_task,
        )
        return web.json_response(
            reply.body, status=reply.status, dumps=lambda v: json.dumps(v, ensure_ascii=False)
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", route)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, HOST, port).start()
    log.info("제어 서버: http://%s:%d", HOST, port)
    return runner
