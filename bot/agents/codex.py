"""Codex CLI 러너 (SPEC 5.2).

`codex exec`를 read-only 샌드박스로 실행한다. 구현·수정 단계만 작업 worktree에 쓰는
workspace-write 샌드박스로 실행한다 (ADR-012).
- 프롬프트는 stdin으로 넘긴다 (`-` 인자)
- 마지막 메시지는 `-o <runs_dir>/<run_id>.last.txt`로 받아 응답 본문으로 쓴다.
  진행 기록은 stderr로 나와 실행 로그에만 남는다
- 응답 스키마는 `<runs_dir>/<run_id>.schema.json`에 써서 `--output-schema`로 넘긴다.
  OpenAI 구조화 출력은 모든 속성을 required에 넣어야 하므로(strict), 선택 필드는 null을 허용하는
  필수 필드로 바꿔 넘기고, 응답에서 선택 필드의 null은 지워 pydantic 기본값이 쓰이게 한다
- 세션 파일을 남기지 않고(`--ephemeral`), 사용자 설정(`~/.codex/config.toml`)은 읽지 않는다.
  로그인 정보는 그대로 쓴다
2026-10-10 시험 호출로 확인 (SPEC 10장 V2).
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bot.agents.base import CODEX_WRITE_STAGES, AgentRequest, AgentResult, EngineName, Stage
from bot.agents.process import ProcessRunner


def git_common_dir(worktree: Path) -> Path | None:
    """git worktree이면 원래 저장소의 .git 폴더. 일반 저장소면 .git이 안에 있으니 None."""
    marker = worktree / ".git"
    if not marker.is_file():
        return None
    text = marker.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        return None
    gitdir = (worktree / text.removeprefix("gitdir:").strip()).resolve()
    commondir = gitdir / "commondir"
    if commondir.is_file():
        return (gitdir / commondir.read_text(encoding="utf-8").strip()).resolve()
    return gitdir


class CodexCliRunner:
    name: EngineName = "codex"

    def __init__(
        self,
        process: ProcessRunner,
        executable: str = "codex",
        models: Mapping[Stage, str] | None = None,
    ):
        self._process = process
        self._executable = executable
        self._models = dict(models or {})

    def last_message_path(self, run_id: str) -> Path:
        return self._process.runs_dir / f"{run_id}.last.txt"

    def schema_path(self, run_id: str) -> Path:
        return self._process.runs_dir / f"{run_id}.schema.json"

    def build_argv(self, request: AgentRequest) -> list[str]:
        if request.mode == "read_only":
            sandbox = ["--sandbox", "read-only"]
        elif request.stage in CODEX_WRITE_STAGES:
            sandbox = ["--sandbox", "workspace-write"]
            # worktree의 커밋은 원래 저장소의 .git에 쓰인다. 그곳도 쓸 수 있게 연다
            if common := git_common_dir(request.cwd):
                sandbox += ["--add-dir", str(common)]
        else:
            raise ValueError(f"Codex는 {request.stage} 단계를 쓰기 모드로 실행하지 않는다")
        argv = [
            self._executable,
            "exec",
            "--cd",
            str(request.cwd),
            *sandbox,
            "--ephemeral",
            "--ignore-user-config",
            "--color",
            "never",
            "--output-last-message",
            str(self.last_message_path(request.run_id)),
        ]
        if model := request.model or self._models.get(request.stage):
            argv += ["--model", model]
        if request.output_schema is not None:
            argv += ["--output-schema", str(self.schema_path(request.run_id))]
        argv.append("-")
        return argv

    async def run(self, request: AgentRequest) -> AgentResult:
        argv = self.build_argv(request)
        last_message = self.last_message_path(request.run_id)
        self._process.runs_dir.mkdir(parents=True, exist_ok=True)
        last_message.unlink(missing_ok=True)
        if request.output_schema is not None:
            self.schema_path(request.run_id).write_text(
                json.dumps(strict_schema(request.output_schema), ensure_ascii=False),
                encoding="utf-8",
            )

        result = await self._process.run(
            request.run_id,
            argv,
            cwd=request.cwd,
            timeout_sec=request.timeout_sec,
            stdin=request.prompt,
        )
        try:
            stdout = last_message.read_text(encoding="utf-8")
        except FileNotFoundError:
            stdout = result.stdout
        else:
            if request.output_schema is not None:
                stdout = _without_optional_nulls(stdout, request.output_schema)
        return AgentResult(
            status=result.status,
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=result.stderr,
            session_id=None,
            duration_sec=result.duration_sec,
            log_path=result.log_path,
        )

    async def cancel(self, run_id: str) -> None:
        await self._process.cancel(run_id)


# OpenAI strict 스키마 (2026-11-02 확인: 선택 필드가 있으면 400 invalid_json_schema)


def strict_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """모든 객체의 속성을 required로 두고, 원래 선택이던 속성은 null도 받게 한다."""
    return _strict(dict(schema))


def _strict(node: Any) -> Any:
    if isinstance(node, list):
        return [_strict(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "default":
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            out[key] = {name: _strict(sub) for name, sub in value.items()}
        else:
            out[key] = _strict(value)
    properties = out.get("properties")
    if isinstance(properties, dict):
        required = set(node.get("required", ()))
        for name in properties:
            if name not in required:
                properties[name] = _nullable(properties[name])
        out["required"] = list(properties)
        out["additionalProperties"] = False
    return out


def _nullable(node: dict[str, Any]) -> dict[str, Any]:
    branches = node.get("anyOf")
    if isinstance(branches, list):
        if any(branch.get("type") == "null" for branch in branches):
            return node
        return {**node, "anyOf": [*branches, {"type": "null"}]}
    return {"anyOf": [node, {"type": "null"}]}


def _without_optional_nulls(text: str, schema: Mapping[str, Any]) -> str:
    """응답 JSON에서 원래 스키마의 선택 필드에 들어온 null을 지운다. JSON이 아니면 그대로 둔다."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    cleaned = _drop_nulls(data, schema, schema.get("$defs", {}))
    return json.dumps(cleaned, ensure_ascii=False)


def _drop_nulls(data: Any, node: Mapping[str, Any], defs: Mapping[str, Any]) -> Any:
    if isinstance(data, dict):
        branch = _branch(node, defs, "properties")
        if branch is None:
            return data
        properties = branch["properties"]
        required = set(branch.get("required", ()))
        return {
            key: _drop_nulls(value, properties.get(key, {}), defs)
            for key, value in data.items()
            if value is not None or key in required
        }
    if isinstance(data, list):
        branch = _branch(node, defs, "items")
        if branch is None:
            return data
        return [_drop_nulls(item, branch["items"], defs) for item in data]
    return data


def _branch(node: Mapping[str, Any], defs: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    """$ref와 anyOf를 따라가 key(properties·items)를 가진 스키마를 찾는다."""
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        node = defs.get(ref.removeprefix("#/$defs/"), {})
    if key in node:
        return node
    for group in ("anyOf", "oneOf", "allOf"):
        for sub in node.get(group, ()):
            found = _branch(sub, defs, key)
            if found is not None:
                return found
    return None
