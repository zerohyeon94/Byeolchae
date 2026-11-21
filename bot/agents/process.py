"""자식 프로세스 실행기.

CLI 러너(claude, codex, gemini)와 테스트 실행이 함께 쓰는 공통 계층이다.
- 셸을 거치지 않고 인자 목록으로 실행한다 (SPEC 2.1).
  프롬프트의 특수문자가 셸 명령으로 해석되지 않는다
- 새 프로세스 그룹으로 띄워, 타임아웃·취소 시 자식이 띄운 프로세스까지 함께 종료한다
- 실행이 끝나면 전체 출력을 `<runs_dir>/<run_id>.log`에 남긴다
"""

import asyncio
import os
import shlex
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from bot.agents.base import RunStatus

# SIGTERM 후 SIGKILL까지 기다리는 시간
TERMINATE_GRACE_SEC = 5.0
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class ProcessResult:
    status: RunStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_sec: float
    log_path: Path


class ProcessRunner:
    def __init__(self, runs_dir: Path, *, terminate_grace_sec: float = TERMINATE_GRACE_SEC):
        self._runs_dir = runs_dir
        self._grace = terminate_grace_sec
        self._running: dict[str, asyncio.subprocess.Process] = {}
        self._cancel_requested: set[str] = set()

    @property
    def runs_dir(self) -> Path:
        return self._runs_dir

    def is_running(self, run_id: str) -> bool:
        return run_id in self._running

    async def run(
        self,
        run_id: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_sec: float,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        if run_id in self._running:
            raise ValueError(f"run_id '{run_id}'가 이미 실행 중이다")
        if not argv:
            raise ValueError("argv가 비어 있다")

        started = time.monotonic()
        stdout, stderr = bytearray(), bytearray()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=None if env is None else dict(env),
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            stderr.extend(f"프로세스를 시작하지 못했다: {e}".encode())
            return self._finish(run_id, argv, cwd, RunStatus.ERROR, None, stdout, stderr, started)

        self._running[run_id] = proc
        readers = asyncio.gather(_drain(proc.stdout, stdout), _drain(proc.stderr, stderr))
        try:
            if stdin is not None:
                await _feed(proc, stdin)
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
                timed_out = False
            except TimeoutError:
                timed_out = True
                await self._terminate(proc)
            await _wait_readers(readers, self._grace)
        except BaseException:
            # 이 실행을 감싼 태스크가 취소되거나(예: /stop) 예외가 나면 프로세스를 남기지 않는다
            await asyncio.shield(self._terminate(proc))
            readers.cancel()
            raise
        finally:
            self._running.pop(run_id, None)
            cancelled = run_id in self._cancel_requested
            self._cancel_requested.discard(run_id)

        if cancelled:
            status = RunStatus.CANCELLED
        elif timed_out:
            status = RunStatus.TIMEOUT
        elif proc.returncode == 0:
            status = RunStatus.OK
        else:
            status = RunStatus.ERROR
        return self._finish(run_id, argv, cwd, status, proc.returncode, stdout, stderr, started)

    async def cancel(self, run_id: str) -> None:
        """실행 중인 run_id를 종료한다. 없으면 아무것도 하지 않는다."""
        proc = self._running.get(run_id)
        if proc is None:
            return
        self._cancel_requested.add(run_id)
        await self._terminate(proc)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._grace)
        except TimeoutError:
            _signal_group(proc.pid, signal.SIGKILL)
            await proc.wait()
        # 그룹 리더가 먼저 끝나도 남은 자손을 정리한다
        _signal_group(proc.pid, signal.SIGKILL)

    def _finish(
        self,
        run_id: str,
        argv: Sequence[str],
        cwd: Path,
        status: RunStatus,
        exit_code: int | None,
        stdout: bytearray,
        stderr: bytearray,
        started: float,
    ) -> ProcessResult:
        duration = time.monotonic() - started
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        log_path = self._runs_dir / f"{run_id}.log"
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n".join(
                [
                    f"run_id: {run_id}",
                    f"argv: {shlex.join(argv)}",
                    f"cwd: {cwd}",
                    f"status: {status}",
                    f"exit_code: {exit_code}",
                    f"duration_sec: {duration:.3f}",
                    "=== stdout ===",
                    out,
                    "=== stderr ===",
                    err,
                ]
            ),
            encoding="utf-8",
        )
        return ProcessResult(status, exit_code, out, err, duration, log_path)


async def _drain(stream: asyncio.StreamReader | None, buffer: bytearray) -> None:
    if stream is None:
        return
    while chunk := await stream.read(_READ_CHUNK):
        buffer.extend(chunk)


async def _feed(proc: asyncio.subprocess.Process, text: str) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write(text.encode())
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # 프로세스가 입력을 다 읽기 전에 끝났다
    finally:
        proc.stdin.close()


async def _wait_readers(readers: asyncio.Future, timeout: float) -> None:
    # 종료된 프로세스의 출력을 다른 프로세스가 붙잡고 있으면 무한히 기다리지 않는다
    try:
        await asyncio.wait_for(readers, timeout=timeout)
    except TimeoutError:
        pass


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
