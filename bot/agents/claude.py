"""Claude Code CLI 러너 (SPEC 5.2).

`claude -p --output-format stream-json --verbose`로 실행하고, 마지막 `result` 줄에서
응답 본문과 세션 ID를 꺼낸다. 스트림 형식을 쓰는 것은 실행 로그에 구독 한도 줄
(`rate_limit_event`)이 남게 하려는 것이다. 비서가 그 줄로 Claude 주간 한도 비율을 센다 (ADR-010).
프롬프트는 stdin으로 넘긴다 (실행 로그의 argv에 프롬프트가 남지 않게).

권한은 단계별로 좁힌다 (운영 규약 1.3). `--permission-prompts none`이라
허용 목록에 없어 승인이 필요한 동작은 모두 자동 거부된다.
2026-10-10 시험 호출로 확인 (SPEC 10장 V1, V3).
- 읽기 전용: `--permission-mode plan`
- DESIGN: `dontAsk` + 읽기 도구, `docs/설계/` 쓰기, 로컬 커밋만 허용
- IMPLEMENT·FIX: `acceptEdits`(작업 폴더 안 편집) + 로컬 git 명령만 허용
- CONCEPT_NOTE: `dontAsk` + 읽기 도구와 웹 검색·가져오기. 볼트의 `회사/`는 읽지 않는다
- 공통 금지: push·원격 git 명령, `.env` 접근. 사용자 MCP 서버는 불러오지 않는다
"""

import json
from collections.abc import Mapping
from typing import Any

from bot.agents.base import AgentRequest, AgentResult, EngineName, RunStatus, Stage
from bot.agents.process import ProcessResult, ProcessRunner

READ_TOOLS = ("Read", "Glob", "Grep")
LOCAL_GIT = (
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git mv:*)",
    "Bash(git rm:*)",
)
DESIGN_TOOLS = (*READ_TOOLS, "Edit(docs/설계/**)", "Write(docs/설계/**)", *LOCAL_GIT)
PLAN_TOOLS = (*READ_TOOLS, "Edit(docs/기획/**)", "Write(docs/기획/**)", *LOCAL_GIT)
UI_DESIGN_TOOLS = (*READ_TOOLS, "Edit(docs/디자인/**)", "Write(docs/디자인/**)", *LOCAL_GIT)
# 문서 단계는 자기 폴더만 쓴다 (운영 규약 1.3)
DOC_STAGE_TOOLS = {
    Stage.PLAN: PLAN_TOOLS,
    Stage.UI_DESIGN: UI_DESIGN_TOOLS,
    Stage.DESIGN: DESIGN_TOOLS,
}
IMPLEMENT_TOOLS = (*READ_TOOLS, "Edit", "Write", *LOCAL_GIT)
# 사실 확인이 필요한 읽기 단계. 파일은 쓰지 않는다 (ADR-009)
RESEARCH_STAGE_TOOLS = {Stage.CONCEPT_NOTE: (*READ_TOOLS, "WebSearch", "WebFetch")}
DENIED_TOOLS = (
    "Bash(git push:*)",
    "Bash(git pull:*)",
    "Bash(git fetch:*)",
    "Bash(git remote:*)",
    "Read(**/.env)",
    "Edit(**/.env)",
    "Write(**/.env)",
    # 교육 볼트의 회사 폴더는 git 밖의 비공개 정보다 (볼트 AGENTS.md 회사 정보 기준)
    "Read(회사/**)",
)


class ClaudeCliRunner:
    name: EngineName = "claude"

    def __init__(
        self,
        process: ProcessRunner,
        executable: str = "claude",
        models: Mapping[Stage, str] | None = None,
    ):
        self._process = process
        self._executable = executable
        self._models = dict(models or {})

    def build_argv(self, request: AgentRequest) -> list[str]:
        argv = [
            self._executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",  # -p에서 stream-json은 --verbose가 있어야 한다
            "--permission-prompts",
            "none",
            "--strict-mcp-config",
        ]
        argv += _permission_args(request)
        argv += ["--disallowedTools", *DENIED_TOOLS]
        if model := request.model or self._models.get(request.stage):
            argv += ["--model", model]
        if request.output_schema is not None:
            argv += ["--json-schema", json.dumps(request.output_schema, ensure_ascii=False)]
        if request.resume_session:
            argv += ["--resume", request.resume_session]
        return argv

    async def run(self, request: AgentRequest) -> AgentResult:
        result = await self._process.run(
            request.run_id,
            self.build_argv(request),
            cwd=request.cwd,
            timeout_sec=request.timeout_sec,
            stdin=request.prompt,
        )
        return to_agent_result(result)

    async def cancel(self, run_id: str) -> None:
        await self._process.cancel(run_id)


def _permission_args(request: AgentRequest) -> list[str]:
    if request.stage in RESEARCH_STAGE_TOOLS:
        tools = RESEARCH_STAGE_TOOLS[request.stage]
        return ["--permission-mode", "dontAsk", "--allowedTools", *tools]
    if request.mode == "read_only":
        return ["--permission-mode", "plan"]
    if request.stage in DOC_STAGE_TOOLS:
        return ["--permission-mode", "dontAsk", "--allowedTools", *DOC_STAGE_TOOLS[request.stage]]
    if request.stage in (Stage.IMPLEMENT, Stage.FIX):
        return ["--permission-mode", "acceptEdits", "--allowedTools", *IMPLEMENT_TOOLS]
    raise ValueError(f"{request.stage} 단계는 쓰기 모드로 실행하지 않는다")


def to_agent_result(result: ProcessResult) -> AgentResult:
    """CLI 스트림의 `result` 줄을 AgentResult로 옮긴다.

    그 줄이 없으면 스트림 줄(JSON)은 빼고 CLI의 평문 출력만 남긴다. 중간 대화 내용이
    사용량 한도 판정(`is_usage_limited`)에 섞이지 않게 하려는 것이다.
    스트림 전체는 실행 로그에 있다.
    """
    status, stdout, session_id = result.status, result.stdout, None
    data = _result_line(result.stdout)
    if data is not None:
        session_id = data.get("session_id") if isinstance(data.get("session_id"), str) else None
        structured = data.get("structured_output")
        if structured is not None:
            stdout = json.dumps(structured, ensure_ascii=False)
        else:
            stdout = data.get("result") if isinstance(data.get("result"), str) else ""
        if status is RunStatus.OK and data.get("is_error"):
            status = RunStatus.ERROR
    else:
        stdout = "".join(
            line for line in result.stdout.splitlines(keepends=True) if _json_object(line) is None
        )
        if status is RunStatus.OK:
            # 종료 코드는 0인데 result 줄이 없으면 CLI가 예상과 다르게 동작한 것이다
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


def _result_line(text: str) -> dict[str, Any] | None:
    """스트림(JSON 한 줄씩)에서 마지막 `result` 객체."""
    for line in reversed(text.splitlines()):
        data = _json_object(line)
        if data is not None and data.get("type") == "result":
            return data
    return None


def _json_object(line: str) -> dict[str, Any] | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
