"""Gemini(Antigravity CLI, `agy`) 러너 (SPEC 5.2).

`agy -p --output-format json --disable-slash-commands`로 비대화형 실행하고, JSON 응답에서
`structured_output`(또는 `response`)과 `conversation_id`를 꺼낸다.
프롬프트는 stdin으로 넘긴다 (실행 로그의 argv에 프롬프트가 남지 않게).
전체 출력(`usage` 포함)은 실행 로그(`.log`)에 남는다.

권한은 단계별로 좁힌다 (운영 규약 1.3). 실행마다 `<runs_dir>/<run_id>.settings.json`에
세션 한정 권한(`permissions.allow` / `permissions.deny`)을 써서 `--settings`로 넘긴다.
Antigravity 권한 평가 순서는 `Deny > Ask > Allow`이며, 헤드리스(`-p`)에서는 허용되지 않은
동작이 자동으로 거부된다.
- 읽기 전용: `--mode plan` + `read_file(*)`만 허용, 쓰기·셸·네트워크 차단
- PLAN·UI_DESIGN·DESIGN: 기본 모드 + `read_file(*)`, 자기 문서 폴더 `write_file(...)`,
  로컬 git 명령만 허용
- IMPLEMENT·FIX: `--mode accept-edits` + 작업 폴더 편집과 로컬 git 명령만 허용
- CONCEPT_NOTE: `--mode plan` + `read_file(*)`와 `read_url(*)` 허용. 볼트의 `회사/`는 읽지 않는다
- DEPT_CHAT: `--mode plan` + `read_file(*)`와 git 기록 읽기. `read_dirs`를 `--add-dir`로 더한다
- 공통 금지(`deny`): push·원격 git 명령, `.env` 접근, 볼트의 `회사/` 읽기, 사용자 MCP 서버(`mcp(*)`)
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bot.agents.base import DOC_STAGE_DIRS, AgentRequest, AgentResult, EngineName, RunStatus, Stage
from bot.agents.process import ProcessResult, ProcessRunner

READ_GIT_COMMANDS = (
    "command(git log)",
    "command(git show)",
    "command(git status)",
    "command(git diff)",
)
LOCAL_GIT_COMMANDS = (
    "command(git status)",
    "command(git diff)",
    "command(git log)",
    "command(git show)",
    "command(git add)",
    "command(git commit)",
    "command(git mv)",
    "command(git rm)",
)
DENIED_RULES = (
    "command(git push)",
    "command(git pull)",
    "command(git fetch)",
    "command(git remote)",
    "read_file(.env)",
    "read_file(**/.env)",
    "write_file(.env)",
    "write_file(**/.env)",
    # 교육 볼트의 회사 폴더는 git 밖의 비공개 정보다 (볼트 AGENTS.md 회사 정보 기준)
    "read_file(회사/)",
    # 사용자 MCP 서버는 불러오지 않는다 (Claude의 --strict-mcp-config와 동일)
    "mcp(*)",
)

RESEARCH_STAGES = frozenset({Stage.CONCEPT_NOTE, Stage.DEPT_CHAT})


class GeminiCliRunner:
    name: EngineName = "gemini"

    def __init__(
        self,
        process: ProcessRunner,
        executable: str = "agy",
        models: Mapping[Stage, str] | None = None,
    ):
        self._process = process
        self._executable = executable
        self._models = dict(models or {})

    def settings_path(self, run_id: str) -> Path:
        return self._process.runs_dir / f"{run_id}.settings.json"

    def schema_path(self, run_id: str) -> Path:
        return self._process.runs_dir / f"{run_id}.schema.json"

    def build_argv(self, request: AgentRequest) -> list[str]:
        mode = _execution_mode(request)
        argv = [
            self._executable,
            "-p",
            "--output-format",
            "json",
            "--disable-slash-commands",
        ]
        if mode is not None:
            argv += ["--mode", mode]
        argv += ["--settings", str(self.settings_path(request.run_id))]
        for directory in request.read_dirs:
            argv += ["--add-dir", str(directory)]
        if model := request.model or self._models.get(request.stage):
            argv += ["--model", model]
        if request.output_schema is not None:
            argv += ["--json-schema", str(self.schema_path(request.run_id))]
        if request.resume_session:
            argv += ["--conversation", request.resume_session]
        return argv

    async def run(self, request: AgentRequest) -> AgentResult:
        argv = self.build_argv(request)
        settings = build_settings(request)
        self._process.runs_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path(request.run_id).write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if request.output_schema is not None:
            self.schema_path(request.run_id).write_text(
                json.dumps(request.output_schema, ensure_ascii=False),
                encoding="utf-8",
            )
        result = await self._process.run(
            request.run_id,
            argv,
            cwd=request.cwd,
            timeout_sec=request.timeout_sec,
            stdin=request.prompt,
        )
        return to_agent_result(result)

    async def cancel(self, run_id: str) -> None:
        await self._process.cancel(run_id)


def _execution_mode(request: AgentRequest) -> str | None:
    if request.stage in RESEARCH_STAGES:
        return "plan"
    if request.mode == "read_only":
        return "plan"
    if request.stage in DOC_STAGE_DIRS:
        # 기본 모드에서 settings.permissions.allow로 지정 폴더 쓰기와 로컬 git만 자동 승인한다
        return None
    if request.stage in (Stage.IMPLEMENT, Stage.FIX):
        return "accept-edits"
    raise ValueError(f"{request.stage} 단계는 쓰기 모드로 실행하지 않는다")


def build_settings(request: AgentRequest) -> dict[str, Any]:
    """단계와 모드에 맞는 Antigravity CLI 세션 권한 설정(`--settings`)을 만든다."""
    _execution_mode(request)
    allow: list[str] = ["read_file(*)"]
    deny: list[str] = list(DENIED_RULES)

    if request.stage is Stage.CONCEPT_NOTE:
        allow.append("read_url(*)")
        deny += ["write_file(*)", "command(*)", "execute_url(*)"]
    elif request.stage is Stage.DEPT_CHAT:
        allow += list(READ_GIT_COMMANDS)
        deny += ["write_file(*)", "read_url(*)", "execute_url(*)"]
    elif request.mode == "read_only":
        deny += ["write_file(*)", "command(*)", "read_url(*)", "execute_url(*)"]
    elif request.stage in DOC_STAGE_DIRS:
        allow.append(f"write_file({DOC_STAGE_DIRS[request.stage]})")
        allow += list(LOCAL_GIT_COMMANDS)
        deny += ["read_url(*)", "execute_url(*)"]
    elif request.stage in (Stage.IMPLEMENT, Stage.FIX):
        allow.append("write_file(*)")
        allow += list(LOCAL_GIT_COMMANDS)
        deny += ["read_url(*)", "execute_url(*)"]

    return {"permissions": {"allow": allow, "deny": deny}}


def to_agent_result(result: ProcessResult) -> AgentResult:
    """`agy -p --output-format json`의 JSON 응답을 AgentResult로 옮긴다."""
    status, stdout, session_id = result.status, result.stdout, None
    data = _json_output(result.stdout)
    if data is not None:
        raw_sid = data.get("conversation_id") or data.get("session_id")
        session_id = raw_sid if isinstance(raw_sid, str) and raw_sid else None
        if _is_error_envelope(data):
            stdout = _error_text(data)
            if status is RunStatus.OK:
                status = RunStatus.ERROR
        else:
            structured = data.get("structured_output")
            if structured is not None:
                stdout = json.dumps(structured, ensure_ascii=False)
            elif isinstance(data.get("response"), str):
                stdout = data["response"]
            elif isinstance(data.get("result"), str):
                stdout = data["result"]
            else:
                stdout = ""
                if status is RunStatus.OK:
                    status = RunStatus.ERROR
    elif status is RunStatus.OK:
        # 종료 코드는 0인데 JSON 응답이 없으면 CLI가 예상과 다르게 동작한 것이다
        status = RunStatus.ERROR
    return AgentResult(
        status=status,
        exit_code=result.exit_code,
        stdout=stdout,
        stderr=result.stderr,
        session_id=session_id,
        duration_sec=result.duration_sec,
        log_path=result.log_path,
    )


_ENVELOPE_KEYS = frozenset(
    {"response", "result", "structured_output", "error", "status", "conversation_id"}
)


def _is_envelope(obj: Any) -> bool:
    return isinstance(obj, dict) and bool(_ENVELOPE_KEYS & obj.keys())


def _is_error_envelope(data: dict[str, Any]) -> bool:
    if data.get("error") or data.get("is_error"):
        return True
    envelope_status = data.get("status")
    return isinstance(envelope_status, str) and envelope_status.upper() not in ("SUCCESS", "OK")


def _json_output(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        data = json.loads(stripped)
        if _is_envelope(data):
            return data
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    pos = 0
    while (idx := stripped.find("{", pos)) != -1:
        try:
            obj, end = decoder.raw_decode(stripped, idx)
        except json.JSONDecodeError:
            pos = idx + 1
            continue
        if _is_envelope(obj):
            found = obj
        pos = end
    return found


def _error_text(data: dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, str) and error:
        return error
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
        return json.dumps(error, ensure_ascii=False)
    for key in ("response", "result", "message"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return json.dumps(data, ensure_ascii=False)
