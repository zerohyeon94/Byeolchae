"""CLI 러너 테스트. 실제 claude·codex는 부르지 않는다 (SPEC 12.3).

명령 인자는 build_argv로 확인하고, 출력 해석은 CLI 흉내를 내는 작은 Python 스크립트를
실제 ProcessRunner로 실행해 확인한다.
"""

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest

from bot.agents import (
    AgentRequest,
    ClaudeCliRunner,
    CodexCliRunner,
    ProcessRunner,
    RunStatus,
    Stage,
)
from bot.agents.claude import DENIED_TOOLS
from bot.agents.codex import strict_schema
from bot.schemas import Opinion, Review, parse_agent_output

OPINION = {
    "stance": "agree",
    "conclusion": "좋다",
    "reasons": ["이유"],
    "risks": ["위험"],
    "proposal": "제안",
}


def run(coro):
    return asyncio.run(coro)


def request(tmp_path, stage=Stage.OPINION, mode="read_only", **kwargs):
    return AgentRequest(
        run_id=kwargs.pop("run_id", "run-1"),
        task_id=1,
        stage=stage,
        prompt=kwargs.pop("prompt", "의견을 말해라"),
        cwd=tmp_path,
        mode=mode,
        timeout_sec=kwargs.pop("timeout_sec", 10),
        **kwargs,
    )


def fake_cli(tmp_path: Path, name: str, body: str) -> str:
    """argv와 stdin을 기록하고 body를 실행하는 가짜 CLI를 만든다."""
    path = tmp_path / name
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "prompt = sys.stdin.read()\n"
        f"Path({str(tmp_path / (name + '.calls'))!r}).write_text("
        "json.dumps({'argv': sys.argv[1:], 'stdin': prompt}, ensure_ascii=False))\n"
        f"{body}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def calls(tmp_path: Path, name: str) -> dict:
    return json.loads((tmp_path / (name + ".calls")).read_text(encoding="utf-8"))


def value_after(argv, flag):
    return argv[argv.index(flag) + 1]


def values_after(argv, flag):
    # 가변 인자 옵션: 다음 --옵션 전까지
    start = argv.index(flag) + 1
    end = next((i for i in range(start, len(argv)) if argv[i].startswith("--")), len(argv))
    return argv[start:end]


@pytest.fixture
def process(tmp_path):
    return ProcessRunner(tmp_path / "runs", terminate_grace_sec=1.0)


# Claude: 명령 인자


def test_claude_read_only_args(tmp_path, process):
    argv = ClaudeCliRunner(process, "claude").build_argv(request(tmp_path))
    assert argv[:2] == ["claude", "-p"]
    assert value_after(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv
    assert value_after(argv, "--permission-prompts") == "none"
    assert value_after(argv, "--permission-mode") == "plan"
    assert "--strict-mcp-config" in argv
    assert "--allowedTools" not in argv
    assert values_after(argv, "--disallowedTools") == list(DENIED_TOOLS)
    # 프롬프트는 argv가 아니라 stdin으로 넘긴다
    assert "의견을 말해라" not in argv
    assert "--model" not in argv
    assert "--json-schema" not in argv
    assert "--resume" not in argv


def test_claude_denies_push_remote_and_env_in_every_mode(tmp_path, process):
    runner = ClaudeCliRunner(process)
    for stage, mode in [
        (Stage.OPINION, "read_only"),
        (Stage.DESIGN, "write_worktree"),
        (Stage.IMPLEMENT, "write_worktree"),
        (Stage.FIX, "write_worktree"),
    ]:
        denied = values_after(
            runner.build_argv(request(tmp_path, stage, mode)), "--disallowedTools"
        )
        for rule in ("Bash(git push:*)", "Bash(git remote:*)", "Read(**/.env)", "Edit(**/.env)"):
            assert rule in denied


def test_claude_design_may_write_only_design_docs(tmp_path, process):
    argv = ClaudeCliRunner(process).build_argv(request(tmp_path, Stage.DESIGN, "write_worktree"))
    assert value_after(argv, "--permission-mode") == "dontAsk"
    allowed = values_after(argv, "--allowedTools")
    assert "Write(docs/설계/**)" in allowed
    assert "Edit(docs/설계/**)" in allowed
    assert "Bash(git commit:*)" in allowed
    assert "Write" not in allowed
    assert "Edit" not in allowed


def test_claude_implement_edits_worktree_with_local_git_only(tmp_path, process):
    argv = ClaudeCliRunner(process).build_argv(request(tmp_path, Stage.IMPLEMENT, "write_worktree"))
    assert value_after(argv, "--permission-mode") == "acceptEdits"
    allowed = values_after(argv, "--allowedTools")
    assert {"Edit", "Write", "Bash(git add:*)", "Bash(git commit:*)"} <= set(allowed)
    assert not any("push" in tool for tool in allowed)


def test_claude_rejects_write_mode_for_read_only_stage(tmp_path, process):
    with pytest.raises(ValueError, match="쓰기 모드"):
        ClaudeCliRunner(process).build_argv(request(tmp_path, Stage.OPINION, "write_worktree"))


def test_claude_model_schema_and_resume(tmp_path, process):
    runner = ClaudeCliRunner(process, models={Stage.OPINION: "sonnet", Stage.IMPLEMENT: "opus"})
    schema = Opinion.model_json_schema()
    argv = runner.build_argv(request(tmp_path, output_schema=schema, resume_session="abc-123"))
    assert value_after(argv, "--model") == "sonnet"
    assert json.loads(value_after(argv, "--json-schema")) == schema
    assert value_after(argv, "--resume") == "abc-123"
    # 설정에 없는 단계는 CLI 기본 모델
    assert "--model" not in runner.build_argv(request(tmp_path, Stage.DEBATE))


# Claude: 출력 해석


def claude_output(**fields):
    data = {"type": "result", "is_error": False, "session_id": "sess-1", "result": ""}
    data.update(fields)
    return f"print({json.dumps(json.dumps(data, ensure_ascii=False))})"


def test_claude_uses_structured_output_and_session(tmp_path, process):
    exe = fake_cli(tmp_path, "claude", claude_output(result="무시", structured_output=OPINION))
    result = run(ClaudeCliRunner(process, exe).run(request(tmp_path)))
    assert result.status is RunStatus.OK
    assert result.session_id == "sess-1"
    assert parse_agent_output(result.stdout, Opinion).conclusion == "좋다"
    assert calls(tmp_path, "claude")["stdin"] == "의견을 말해라"
    assert result.log_path.exists()


def test_claude_reads_last_result_line_of_stream(tmp_path, process):
    # 실제 스트림처럼 초기화·한도·assistant 줄 뒤에 result 줄이 온다
    lines = [
        {"type": "system", "subtype": "init", "session_id": "sess-1"},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "중간"}]}},
    ]
    body = "".join(f"print({json.dumps(json.dumps(line))})\n" for line in lines)
    exe = fake_cli(tmp_path, "claude", body + claude_output(structured_output=OPINION))
    result = run(ClaudeCliRunner(process, exe).run(request(tmp_path)))
    assert result.status is RunStatus.OK
    assert parse_agent_output(result.stdout, Opinion).conclusion == "좋다"
    # 실행 로그에는 스트림 전체가 남는다 (비서가 한도 줄을 읽는다)
    assert "rate_limit_event" in result.log_path.read_text(encoding="utf-8")


def test_claude_falls_back_to_result_text(tmp_path, process):
    text = "```json\n" + json.dumps(OPINION, ensure_ascii=False) + "\n```"
    exe = fake_cli(tmp_path, "claude", claude_output(result=text))
    result = run(ClaudeCliRunner(process, exe).run(request(tmp_path)))
    assert result.stdout == text
    assert parse_agent_output(result.stdout, Opinion).stance.value == "agree"


def test_claude_is_error_marks_run_as_error(tmp_path, process):
    exe = fake_cli(tmp_path, "claude", claude_output(is_error=True, result="사용량 한도"))
    result = run(ClaudeCliRunner(process, exe).run(request(tmp_path)))
    assert result.status is RunStatus.ERROR
    assert result.stdout == "사용량 한도"
    assert result.session_id == "sess-1"


def test_claude_non_json_output_is_error_and_kept_raw(tmp_path, process):
    exe = fake_cli(tmp_path, "claude", "print('충돌')")
    result = run(ClaudeCliRunner(process, exe).run(request(tmp_path)))
    assert result.status is RunStatus.ERROR
    assert result.stdout == "충돌\n"
    assert result.session_id is None


def test_claude_stream_without_result_keeps_only_plain_text(tmp_path, process):
    # 시간 초과·충돌로 result 줄이 없으면 중간 대화(한도 판정에 섞일 수 있는 말)는 빼고 평문만
    talk = {"type": "assistant", "message": {"content": [{"type": "text", "text": "rate limit"}]}}
    body = f"print({json.dumps(json.dumps(talk))})\nprint('Error: 연결 끊김')\nsys.exit(1)"
    exe = fake_cli(tmp_path, "claude", body)
    result = run(ClaudeCliRunner(process, exe).run(request(tmp_path)))
    assert result.status is RunStatus.ERROR
    assert result.stdout == "Error: 연결 끊김\n"
    assert "rate limit" in result.log_path.read_text(encoding="utf-8")


def test_claude_nonzero_exit_stays_error(tmp_path, process):
    exe = fake_cli(tmp_path, "claude", claude_output(is_error=True) + "\nsys.exit(1)")
    result = run(ClaudeCliRunner(process, exe).run(request(tmp_path)))
    assert result.status is RunStatus.ERROR
    assert result.exit_code == 1


def test_claude_timeout_keeps_status(tmp_path, process):
    exe = fake_cli(tmp_path, "claude", "import time; time.sleep(30)")
    result = run(ClaudeCliRunner(process, exe).run(request(tmp_path, timeout_sec=0.5)))
    assert result.status is RunStatus.TIMEOUT


def test_claude_cancel(tmp_path, process):
    exe = fake_cli(tmp_path, "claude", "import time; time.sleep(30)")
    runner = ClaudeCliRunner(process, exe)

    async def scenario():
        task = asyncio.create_task(runner.run(request(tmp_path, run_id="c1")))
        while not process.is_running("c1"):
            await asyncio.sleep(0.02)
        await runner.cancel("c1")
        return await task

    assert run(scenario()).status is RunStatus.CANCELLED


# Codex


def test_codex_args(tmp_path, process):
    runner = CodexCliRunner(process, "codex", models={Stage.REVIEW: "gpt-x"})
    argv = runner.build_argv(request(tmp_path, Stage.REVIEW, run_id="x1"))
    assert argv[:2] == ["codex", "exec"]
    assert value_after(argv, "--cd") == str(tmp_path)
    assert value_after(argv, "--sandbox") == "read-only"
    assert {"--ephemeral", "--ignore-user-config"} <= set(argv)
    assert value_after(argv, "--output-last-message") == str(tmp_path / "runs" / "x1.last.txt")
    assert value_after(argv, "--model") == "gpt-x"
    assert "--output-schema" not in argv
    assert argv[-1] == "-"
    assert "--model" not in runner.build_argv(request(tmp_path, Stage.OPINION))


def test_codex_rejects_write_mode(tmp_path, process):
    with pytest.raises(ValueError, match="읽기 전용"):
        CodexCliRunner(process).build_argv(request(tmp_path, Stage.REVIEW, "write_worktree"))


CODEX_BODY = """\
args = sys.argv[1:]
out = Path(args[args.index('--output-last-message') + 1])
if '--output-schema' in args:
    schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
    assert schema['title'] in ('Opinion', 'Review')
    assert schema['required'] == list(schema['properties'])
print('진행 기록', file=sys.stderr)
out.write_text(MESSAGE, encoding='utf-8')
print(MESSAGE)
"""


def test_codex_reads_last_message_and_writes_schema(tmp_path, process):
    message = json.dumps(OPINION, ensure_ascii=False)
    exe = fake_cli(tmp_path, "codex", f"MESSAGE = {message!r}\n" + CODEX_BODY)
    req = request(tmp_path, output_schema=Opinion.model_json_schema(), run_id="x2")
    result = run(CodexCliRunner(process, exe).run(req))
    assert result.status is RunStatus.OK
    assert result.session_id is None
    assert parse_agent_output(result.stdout, Opinion).proposal == "제안"
    assert "진행 기록" in result.stderr
    assert calls(tmp_path, "codex")["stdin"] == "의견을 말해라"
    assert (tmp_path / "runs" / "x2.schema.json").exists()


def test_codex_schema_is_strict():
    # OpenAI 구조화 출력은 모든 속성이 required에 있어야 받는다 (선택 필드는 null 허용)
    schema = strict_schema(Review.model_json_schema())
    assert schema["required"] == list(schema["properties"])
    assert schema["additionalProperties"] is False
    assert {"type": "null"} in schema["properties"]["findings"]["anyOf"]
    finding = schema["$defs"]["Finding"]
    assert set(finding["required"]) == set(finding["properties"])
    assert "default" not in json.dumps(schema)
    # 원래 스키마는 바꾸지 않는다
    assert "findings" not in Review.model_json_schema()["required"]


def test_codex_drops_nulls_of_optional_fields(tmp_path, process):
    # strict 스키마로 받은 null은 지워서 pydantic 기본값을 쓴다 (findings: 기본값 [])
    message = json.dumps(
        {"verdict": "APPROVED", "summary": "문제 없음", "findings": None, "hiring_proposal": None}
    )
    exe = fake_cli(tmp_path, "codex", f"MESSAGE = {message!r}\n" + CODEX_BODY)
    req = request(tmp_path, Stage.REVIEW, output_schema=Review.model_json_schema(), run_id="x4")
    result = run(CodexCliRunner(process, exe).run(req))
    review = parse_agent_output(result.stdout, Review)
    assert review.findings == [] and review.hiring_proposal is None
    written = json.loads((tmp_path / "runs" / "x4.schema.json").read_text(encoding="utf-8"))
    assert "hiring_proposal" in written["required"]


def test_codex_without_last_message_falls_back_to_stdout(tmp_path, process):
    exe = fake_cli(tmp_path, "codex", "print('부분 출력'); sys.exit(2)")
    # 이전 실행의 마지막 메시지가 남아 있어도 읽지 않는다
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "x3.last.txt").write_text("이전 내용", encoding="utf-8")
    result = run(CodexCliRunner(process, exe).run(request(tmp_path, run_id="x3")))
    assert result.status is RunStatus.ERROR
    assert result.exit_code == 2
    assert result.stdout == "부분 출력\n"


def test_claude_concept_note_reads_and_searches_web_only(tmp_path, process):
    argv = ClaudeCliRunner(process).build_argv(request(tmp_path, Stage.CONCEPT_NOTE, "read_only"))
    assert value_after(argv, "--permission-mode") == "dontAsk"
    assert values_after(argv, "--allowedTools") == ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]
    assert "Read(회사/**)" in values_after(argv, "--disallowedTools")


def test_claude_weekly_quiz_is_read_only(tmp_path, process):
    argv = ClaudeCliRunner(process).build_argv(request(tmp_path, Stage.WEEKLY_QUIZ, "read_only"))
    assert value_after(argv, "--permission-mode") == "plan"
    assert "Read(회사/**)" in values_after(argv, "--disallowedTools")
