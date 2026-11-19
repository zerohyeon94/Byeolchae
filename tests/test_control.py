"""주기 작업 홀딩 규칙과 로컬 제어 요청 (ADR-014)."""

import asyncio
import json
import stat

import aiohttp

from bot import control
from bot.render.status import render_ping
from bot.scheduler import HOLD_ALL, dump_holds, is_held, parse_holds


def test_holds_round_trip_and_all():
    holds = parse_holds(dump_holds(frozenset({"education_daily", "gagaessi:nightly-test"})))
    assert holds == {"education_daily", "gagaessi:nightly-test"}
    assert is_held(holds, "education_daily") and not is_held(holds, "morning_report")
    assert is_held(frozenset({HOLD_ALL}), "morning_report")
    assert dump_holds(frozenset()) is None  # 홀딩이 없으면 bot_state에서 지운다


def test_broken_hold_value_means_no_hold():
    assert parse_holds(None) == frozenset()
    assert parse_holds("{깨짐") == frozenset()
    assert parse_holds('{"a": 1}') == frozenset()
    assert parse_holds('["a", 3, ""]') == {"a"}


def test_ping_shows_held_jobs():
    assert "홀딩 중인 주기 작업: 교육 아침 학습." in render_ping(
        running_cli=0, active_tasks=0, held="교육 아침 학습"
    )
    assert "홀딩" not in render_ping(running_cli=0, active_tasks=0)


def test_token_file_is_private_and_reused(tmp_path):
    path = tmp_path / "control.token"
    token = control.ensure_token(path)
    assert len(token) > 30 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert control.ensure_token(path) == token


class Holds:
    def __init__(self):
        self.calls = []

    async def schedules(self):
        return {"all": False, "jobs": [{"job": "education_daily"}]}

    async def set_hold(self, job, held):
        if job != "education_daily":
            raise KeyError(job)
        self.calls.append((job, held))
        return {"all": False, "jobs": [{"job": job, "held": held}]}

    async def start_task(self, project, command, title):
        if project != "jday" or command != "/task feature":
            raise KeyError(project)
        self.calls.append((project, command, title))
        return {"task_id": 7, "thread": "123"}


def call(method, path, given="tok", body=b""):
    holds = Holds()
    reply = asyncio.run(
        control.handle(
            method,
            path,
            "tok",
            given,
            body,
            schedules=holds.schedules,
            set_hold=holds.set_hold,
            start_task=holds.start_task,
        )
    )
    return reply, holds.calls


def test_requests_need_the_token():
    for given in (None, "", "wrong"):
        reply, calls = call("POST", "/holds", given, b'{"job": "education_daily", "held": true}')
        assert reply.status == 401 and calls == []


def test_hold_request_is_validated():
    reply, calls = call("POST", "/holds", body=b'{"job": "education_daily", "held": true}')
    assert reply.status == 200 and calls == [("education_daily", True)]
    assert call("GET", "/schedules")[0].body["jobs"] == [{"job": "education_daily"}]
    assert call("POST", "/holds", body=b"not json")[0].status == 400
    assert (
        call("POST", "/holds", body=b'{"job": "education_daily", "held": "yes"}')[0].status == 400
    )
    assert call("POST", "/holds", body=b'{"job": "nope", "held": true}')[0].status == 404
    assert call("DELETE", "/holds")[0].status == 404


def test_task_request_is_validated():
    ok = json.dumps({"project": "jday", "command": "/task feature", "title": " 시험 "}).encode()
    for given in (None, "wrong"):
        reply, calls = call("POST", "/tasks", given, ok)
        assert reply.status == 401 and calls == []
    reply, calls = call("POST", "/tasks", body=ok)
    assert reply.status == 200 and reply.body == {"task_id": 7, "thread": "123"}
    assert calls == [("jday", "/task feature", "시험")]
    for bad in (
        b"not json",
        b"[]",
        b'{"project": "jday", "command": "/task feature"}',
        b'{"project": "jday", "command": "/task feature", "title": "  "}',
        b'{"project": 1, "command": "/task feature", "title": "x"}',
    ):
        reply, calls = call("POST", "/tasks", body=bad)
        assert reply.status == 400 and calls == [], bad
    for unknown in (
        b'{"project": "nope", "command": "/task feature", "title": "x"}',
        b'{"project": "jday", "command": "/nope", "title": "x"}',
    ):
        assert call("POST", "/tasks", body=unknown)[0].status == 404


def test_server_answers_only_with_token():
    holds = Holds()

    async def main():
        runner = await control.start(
            "tok",
            schedules=holds.schedules,
            set_hold=holds.set_hold,
            start_task=holds.start_task,
            port=0,
        )
        try:
            host, port = runner.addresses[0][:2]
            assert host == "127.0.0.1"
            url = f"http://{host}:{port}"
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{url}/schedules") as denied:
                    status = denied.status
                async with session.post(
                    f"{url}/holds",
                    data=json.dumps({"job": "education_daily", "held": True}),
                    headers={control.TOKEN_HEADER: "tok"},
                ) as ok:
                    body = await ok.json()
            return status, body
        finally:
            await runner.cleanup()

    status, body = asyncio.run(main())
    assert status == 401
    assert body["jobs"] == [{"job": "education_daily", "held": True}]
    assert holds.calls == [("education_daily", True)]
