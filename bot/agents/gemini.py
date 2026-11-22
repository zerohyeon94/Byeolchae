"""Gemini(Antigravity CLI, `agy`) 러너 (SPEC 5.2).

`agy --input-format stream-json --output-format stream-json`으로 비대화형 실행하고, 마지막
`{"event":"result"}` 줄에서 `structured_output`(또는 `response`)과 `conversation_id`를 꺼낸다.
프롬프트는 stdin에 `{"event":"user","message":{...}}` 한 줄로 넘긴다 (실행 로그의 argv에 프롬프트가
남지 않게). agy 1.5.14의 `-p`는 바로 뒤 인자를 프롬프트로 받고 stdin을 읽지 않으므로, stdin을 읽는
stream-json 입력을 쓴다 (2026-11-22 실제 호출로 확인). 전체 스트림은 실행 로그(`.log`)에 남는다.

웹 검색 도구(`search_web`)는 권한 규칙으로 막히지 않는다. 그래서 개념 노트 외 단계는 `tools`에서
`search_web`을 뺀 전용 에이전트(`byeolchae`)로 돌린다. 에이전트 파일은 실행 폴더의
`antigravity/.agents/agents/`에 러너가 쓰고 `--add-dir`로 넘긴다. `--agent`는 파일 경로를 받지 않고
작업 폴더나 더한 폴더의 `.agents/`에서만 찾는다. 구조화 출력은 `finish` 도구로 내므로 `tools`에
반드시 넣는다 (2026-11-22 실제 호출로 확인, ADR-017).

권한은 단계별로 좁힌다 (운영 규약 1.3). 실행마다 `<runs_dir>/<run_id>.settings.json`에
세션 한정 권한(`permissions.allow` / `permissions.deny`)을 써서 `--settings`로 넘긴다.
Antigravity 권한 평가 순서는 `Deny > Ask > Allow`다. agy 1.5.14 실제 호출에서 확인한 점
(2026-11-22, ADR-017):
- 어느 규칙에도 맞지 않는 파일 쓰기는 기본 정책에서 허용된다. `allow`에 자기 폴더만 적어도
  다른 폴더 쓰기는 막히지 않는다. `toolPermission: strict`로 하면 `allow`를 무시하고 모든 쓰기를
  거부한다. 그래서 문서 단계의 폴더 제한은 실행 뒤 검사(`scope_violations`)가 맡는다.
- `deny`는 듣는다. 터미널 샌드박스는 `write_file` 허용 경로만 쓸 수 있어 `.git`을 따로 연다.

- 읽기 전용: `--mode plan` + `read_file(*)`만 허용, 쓰기·셸·네트워크 차단
- PLAN·UI_DESIGN·DESIGN: 기본 모드 + 자기 문서 폴더와 `.git` `write_file(...)`, 로컬 git 명령
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
from bot.agents.codex import git_common_dir
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
    # 사용자 MCP 서버는 불러오지 않는다 (Claude의 --strict-mcp-config와 동일)
    "mcp(*)",
)

# git이 아닌 셸 명령. 맞는 규칙이 없는 명령은 헤드리스 실행에서 턴을 끝내 버리므로(빈 응답),
# 명시적으로 거부해 에이전트가 거부를 받고 남은 일을 이어 가게 한다. 규칙 안에 괄호를 쓰면
# `command(...)`가 잘못 읽히고, agy 정규식(RE2)은 lookahead가 없어 첫 글자로 나눈다.
# shortcut: 허용 목록 밖의 git 하위 명령(checkout 등)은 여전히 턴을 끝낸다.
# 문제가 되면 여기에 더한다
NON_GIT_COMMANDS = (
    "command(regex:[^g].*)",
    "command(regex:g[^i].*)",
    "command(regex:gi[^t].*)",
    "command(regex:git[^ ].*)",
)

RESEARCH_STAGES = frozenset({Stage.CONCEPT_NOTE, Stage.DEPT_CHAT})
# 웹 검색을 쓰는 단계. 나머지는 search_web이 없는 전용 에이전트로 돌린다
WEB_SEARCH_STAGES = frozenset({Stage.CONCEPT_NOTE})

AGENT_NAME = "byeolchae"
AGENT_FILE = f"""---
name: {AGENT_NAME}
description: 웹 검색 없이 작업 폴더 안에서 일하는 별채 직원
mainAgent: true
subagent: false
tools: [read_file, edit_file, write_file, run_command, manage_task, finish]
---
너는 별채의 직원이다. 사용자 메시지의 지시와 출력 형식을 그대로 따른다.

- 작업 폴더와 함께 주어진 폴더 안에서만 일한다.
- 파일은 read_file로 읽고 write_file·edit_file로 만들고 고친다. 셸로 파일을 읽거나 쓰지 않는다.
- 셸 명령은 허용된 것만 하나씩 실행한다. `&&`, `;`, `|`로 묶지 않는다.
- 권한이 거부된 동작은 우회하지 않는다. 그 동작 없이 남은 일을 끝까지 한다.
- 일을 마치면 반드시 사용자에게 결과를 답한다. 빈 답으로 끝내지 않는다.
"""


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

    @property
    def agent_dir(self) -> Path:
        """전용 에이전트 폴더. `--add-dir`로 넘기면 agy가 안의 `.agents/agents/`를 읽는다."""
        return self._process.runs_dir / "antigravity"

    def build_argv(self, request: AgentRequest) -> list[str]:
        mode = _execution_mode(request)
        argv = [
            self._executable,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
        ]
        if mode is not None:
            argv += ["--mode", mode]
        argv += ["--settings", str(self.settings_path(request.run_id))]
        for directory in request.read_dirs:
            argv += ["--add-dir", str(directory)]
        if request.stage not in WEB_SEARCH_STAGES:
            argv += ["--add-dir", str(self.agent_dir), "--agent", AGENT_NAME]
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
        agent_file = self.agent_dir / ".agents" / "agents" / f"{AGENT_NAME}.md"
        agent_file.parent.mkdir(parents=True, exist_ok=True)
        agent_file.write_text(AGENT_FILE, encoding="utf-8")
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
            stdin=stdin_message(request.prompt),
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
    deny: list[str] = [*DENIED_RULES, *_private_paths(request)]

    if request.stage is Stage.CONCEPT_NOTE:
        allow.append("read_url(*)")
        deny += ["write_file(*)", "command(*)", "execute_url(*)"]
    elif request.stage is Stage.DEPT_CHAT:
        allow += list(READ_GIT_COMMANDS)
        deny += ["write_file(*)", "read_url(*)", "execute_url(*)", *NON_GIT_COMMANDS]
    elif request.mode == "read_only":
        deny += ["write_file(*)", "command(*)", "read_url(*)", "execute_url(*)"]
    elif request.stage in DOC_STAGE_DIRS:
        # 파일 도구에는 듣지 않고(위 docstring) 터미널 샌드박스의 쓰기 허용 목록만 채운다
        allow.append(f"write_file({request.cwd / DOC_STAGE_DIRS[request.stage]})")
        allow += [*_git_dirs(request), *LOCAL_GIT_COMMANDS]
        deny += ["read_url(*)", "execute_url(*)", *NON_GIT_COMMANDS]
    elif request.stage in (Stage.IMPLEMENT, Stage.FIX):
        allow += ["write_file(*)", *_git_dirs(request), *LOCAL_GIT_COMMANDS]
        deny += ["read_url(*)", "execute_url(*)", *NON_GIT_COMMANDS]

    return {"permissions": {"allow": allow, "deny": deny}}


def _private_paths(request: AgentRequest) -> list[str]:
    """`.env`와 볼트 `회사/` 차단. 경로 규칙은 절대 경로만 받고 glob(`**`)은 쓸 수 없다.

    상대 경로나 glob이 하나라도 있으면 터미널 샌드박스 설정 오류로 모든 명령이 실패한다.
    `read_file` 거부는 같은 경로의 `write_file`도 막는다 (2026-11-22 실제 호출로 확인).
    """
    roots = [request.cwd, *request.read_dirs]
    if common := git_common_dir(request.cwd):
        roots.append(common.parent)
    rules: list[str] = []
    for root in dict.fromkeys(roots):
        rules.append(f"read_file({root / '.env'})")
        # 교육 볼트의 회사 폴더는 git 밖의 비공개 정보다 (볼트 AGENTS.md 회사 정보 기준)
        rules.append(f"read_file({root / '회사'})")
    return rules


def _git_dirs(request: AgentRequest) -> list[str]:
    """터미널 샌드박스가 커밋할 수 있게 `.git`(worktree면 원래 저장소의 `.git`) 쓰기를 연다."""
    return [f"write_file({git_common_dir(request.cwd) or request.cwd / '.git'})"]


def stdin_message(prompt: str) -> str:
    """stream-json 입력 한 줄. 프롬프트의 줄바꿈은 JSON 문자열 안에 들어간다."""
    message = {"event": "user", "message": {"role": "user", "content": prompt}}
    return json.dumps(message, ensure_ascii=False) + "\n"


def to_agent_result(result: ProcessResult) -> AgentResult:
    """stream-json 출력의 마지막 `result` 이벤트(또는 JSON 응답)를 AgentResult로 옮긴다."""
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
    if found is not None and found.get("event") == "result" and isinstance(found["result"], dict):
        return found["result"]
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
