"""CLI 러너 테스트. 실제 claude·codex·gemini는 부르지 않는다 (SPEC 12.3).

명령 인자는 build_argv로 확인하고, 출력 해석은 CLI 흉내를 내는 작은 Python 스크립트를
실제 ProcessRunner로 실행해 확인한다.
"""

import asyncio
import json
import re
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from bot.agents import (
    AgentRequest,
    ClaudeCliRunner,
    CodexCliRunner,
    GeminiCliRunner,
    ProcessRunner,
    RunStatus,
    Stage,
)
from bot.agents.claude import DENIED_TOOLS
from bot.agents.codex import git_common_dir, strict_schema
from bot.agents.gemini import (
    DENIED_RULES,
    DOC_STAGE_DIRS,
    LOCAL_GIT_COMMANDS,
    NON_GIT_COMMANDS,
    READ_GIT_COMMANDS,
    build_settings,
)
from bot.schemas import Opinion, Review, parse_agent_output
from bot.workflow.engine import is_usage_limited

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


def test_codex_rejects_write_mode_outside_write_stages(tmp_path, process):
    with pytest.raises(ValueError, match="쓰기 모드"):
        CodexCliRunner(process).build_argv(request(tmp_path, Stage.REVIEW, "write_worktree"))


@pytest.mark.parametrize(
    "stage", [Stage.PLAN, Stage.UI_DESIGN, Stage.DESIGN, Stage.IMPLEMENT, Stage.FIX]
)
def test_codex_writes_worktree_and_its_git_dir(tmp_path, process, stage):
    # 저장소/.git/worktrees/w1 을 가리키는 worktree
    common = tmp_path / "repo" / ".git"
    (common / "worktrees" / "w1").mkdir(parents=True)
    (common / "worktrees" / "w1" / "commondir").write_text("../..\n")
    worktree = tmp_path / "w1"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {common / 'worktrees' / 'w1'}\n")
    argv = CodexCliRunner(process).build_argv(
        request(worktree, stage, "write_worktree", run_id="w")
    )
    assert value_after(argv, "--sandbox") == "workspace-write"
    assert value_after(argv, "--add-dir") == str(common.resolve())


@pytest.mark.parametrize("stage", [Stage.CONCEPT_NOTE, Stage.RESEARCH])
def test_codex_web_stages_turn_on_web_search(tmp_path, process, stage):
    # --search는 최상위 옵션이라 exec 앞에 둔다. 다른 단계는 웹 검색이 없다 (Claude와 같다)
    runner = CodexCliRunner(process)
    argv = runner.build_argv(request(tmp_path, stage))
    assert argv[:3] == ["codex", "--search", "exec"]
    assert value_after(argv, "--sandbox") == "read-only"
    assert 'web_search="disabled"' not in argv
    # 다른 단계는 기본으로 켜진 웹 검색을 끈다 (codex-cli 0.161)
    other = runner.build_argv(request(tmp_path, Stage.OPINION))
    assert "--search" not in other
    assert value_after(other, "-c") == 'web_search="disabled"'


def test_codex_disables_apps_and_plugins(tmp_path, process):
    # 앱의 검색 도구로 웹 검색을 우회하지 않게 끈다 (Claude의 --strict-mcp-config와 같다)
    for stage, mode in ((Stage.OPINION, "read_only"), (Stage.IMPLEMENT, "write_worktree")):
        argv = CodexCliRunner(process).build_argv(request(tmp_path, stage, mode))
        pairs = list(zip(argv, argv[1:], strict=False))
        assert ("--disable", "apps") in pairs and ("--disable", "plugins") in pairs


def test_git_common_dir_of_plain_repo_is_none(tmp_path):
    (tmp_path / ".git").mkdir()
    assert git_common_dir(tmp_path) is None
    assert git_common_dir(tmp_path / "없음") is None


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


@pytest.mark.parametrize("stage", [Stage.CONCEPT_NOTE, Stage.RESEARCH])
def test_claude_web_stages_read_and_search_web_only(tmp_path, process, stage):
    argv = ClaudeCliRunner(process).build_argv(request(tmp_path, stage, "read_only"))
    assert value_after(argv, "--permission-mode") == "dontAsk"
    assert values_after(argv, "--allowedTools") == ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]
    assert "Read(회사/**)" in values_after(argv, "--disallowedTools")


def test_claude_dept_chat_reads_extra_folders_and_git_history(tmp_path, process):
    req = replace(
        request(tmp_path, Stage.DEPT_CHAT, "read_only"),
        read_dirs=(tmp_path / "vault", tmp_path / "repo"),
    )
    argv = ClaudeCliRunner(process).build_argv(req)
    assert value_after(argv, "--permission-mode") == "dontAsk"
    tools = values_after(argv, "--allowedTools")
    assert {"Read", "Glob", "Grep", "Bash(git log:*)"} <= set(tools)
    assert not any(tool.startswith(("Edit", "Write")) for tool in tools)
    assert values_after(argv, "--add-dir") == [str(tmp_path / "vault"), str(tmp_path / "repo")]
    assert "Read(회사/**)" in values_after(argv, "--disallowedTools")


def test_claude_weekly_quiz_is_read_only(tmp_path, process):
    argv = ClaudeCliRunner(process).build_argv(request(tmp_path, Stage.WEEKLY_QUIZ, "read_only"))
    assert value_after(argv, "--permission-mode") == "plan"
    assert "Read(회사/**)" in values_after(argv, "--disallowedTools")


# Gemini (Antigravity CLI: agy): 명령 인자와 권한 설정


def test_gemini_read_only_args(tmp_path, process):
    argv = GeminiCliRunner(process, "agy").build_argv(request(tmp_path, run_id="g1"))
    # agy의 -p는 바로 뒤 인자를 프롬프트로 받으므로 쓰지 않고 stdin(stream-json)으로 넘긴다
    assert "-p" not in argv
    assert value_after(argv, "--input-format") == "stream-json"
    assert value_after(argv, "--output-format") == "stream-json"
    assert "--disable-slash-commands" in argv
    assert value_after(argv, "--mode") == "plan"
    assert value_after(argv, "--settings") == str(tmp_path / "runs" / "g1.settings.json")
    assert "의견을 말해라" not in argv
    assert "--model" not in argv
    assert "--json-schema" not in argv
    assert "--conversation" not in argv
    # 개념 노트 외 단계는 search_web이 없는 전용 에이전트로 돌린다
    assert value_after(argv, "--agent") == "byeolchae"
    assert value_after(argv, "--add-dir") == str(tmp_path / "runs" / "antigravity")


def test_gemini_settings_denies_push_remote_env_company_and_mcp_in_every_mode(tmp_path):
    cases = [
        (Stage.OPINION, "read_only"),
        (Stage.PLAN, "write_worktree"),
        (Stage.UI_DESIGN, "write_worktree"),
        (Stage.DESIGN, "write_worktree"),
        (Stage.IMPLEMENT, "write_worktree"),
        (Stage.FIX, "write_worktree"),
        (Stage.CONCEPT_NOTE, "read_only"),
        (Stage.DEPT_CHAT, "read_only"),
    ]
    for stage, mode in cases:
        perms = build_settings(request(tmp_path, stage, mode))["permissions"]
        assert set(DENIED_RULES) <= set(perms["deny"])
        assert f"read_file({tmp_path / '.env'})" in perms["deny"]
        assert f"read_file({tmp_path / '회사'})" in perms["deny"]
        assert "read_file(*)" in perms["allow"]
        # 상대 경로나 glob이 있으면 agy 터미널 샌드박스가 모든 명령을 거부한다
        for rule in perms["allow"] + perms["deny"]:
            target = rule[rule.index("(") + 1 : -1]
            assert "**" not in target
            if rule.startswith(("read_file", "write_file")) and target != "*":
                assert Path(target).is_absolute(), rule


def test_gemini_denies_env_of_original_repo_in_worktree(tmp_path):
    main, worktree = tmp_path / "main", tmp_path / "wt"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    perms = build_settings(request(worktree, Stage.IMPLEMENT, "write_worktree"))["permissions"]
    assert f"read_file({main.resolve() / '.env'})" in perms["deny"]
    assert f"write_file({(main / '.git').resolve()})" in perms["allow"]


@pytest.mark.parametrize(("stage", "folder"), list(DOC_STAGE_DIRS.items()))
def test_gemini_doc_stages_write_only_their_folder(tmp_path, process, stage, folder):
    req = request(tmp_path, stage, "write_worktree")
    argv = GeminiCliRunner(process).build_argv(req)
    assert "--mode" not in argv
    perms = build_settings(req)["permissions"]
    assert f"write_file({tmp_path / folder})" in perms["allow"]
    assert f"write_file({tmp_path / '.git'})" in perms["allow"]
    assert "write_file(*)" not in perms["allow"]
    assert set(LOCAL_GIT_COMMANDS) <= set(perms["allow"])
    assert not any("push" in rule for rule in perms["allow"])
    assert set(NON_GIT_COMMANDS) <= set(perms["deny"])


@pytest.mark.parametrize("stage", [Stage.IMPLEMENT, Stage.FIX])
def test_gemini_implement_and_fix_use_accept_edits_and_local_git(tmp_path, process, stage):
    req = request(tmp_path, stage, "write_worktree")
    argv = GeminiCliRunner(process).build_argv(req)
    assert value_after(argv, "--mode") == "accept-edits"
    perms = build_settings(req)["permissions"]
    assert "write_file(*)" in perms["allow"]
    assert set(LOCAL_GIT_COMMANDS) <= set(perms["allow"])
    assert not any("push" in rule for rule in perms["allow"])


@pytest.mark.parametrize("stage", [Stage.CONCEPT_NOTE, Stage.RESEARCH])
def test_gemini_web_stages_read_and_search_web_only(tmp_path, process, stage):
    req = request(tmp_path, stage, "read_only")
    argv = GeminiCliRunner(process).build_argv(req)
    assert value_after(argv, "--mode") == "plan"
    assert "--agent" not in argv
    assert "--add-dir" not in argv
    perms = build_settings(req)["permissions"]
    assert "read_url(*)" in perms["allow"]
    assert {"write_file(*)", "command(*)", "execute_url(*)"} <= set(perms["deny"])


def test_gemini_dept_chat_reads_extra_folders_and_git_history(tmp_path, process):
    req = replace(
        request(tmp_path, Stage.DEPT_CHAT, "read_only"),
        read_dirs=(tmp_path / "vault", tmp_path / "repo"),
    )
    argv = GeminiCliRunner(process).build_argv(req)
    assert value_after(argv, "--mode") == "plan"
    added = [argv[i + 1] for i, token in enumerate(argv) if token == "--add-dir"]
    agents = str(tmp_path / "runs" / "antigravity")
    assert added == [str(tmp_path / "vault"), str(tmp_path / "repo"), agents]
    perms = build_settings(req)["permissions"]
    assert set(READ_GIT_COMMANDS) <= set(perms["allow"])
    assert "write_file(*)" in perms["deny"]
    assert not any(rule.startswith("write_file") for rule in perms["allow"])


def test_gemini_rejects_write_mode_for_read_only_stage(tmp_path, process):
    with pytest.raises(ValueError, match="쓰기 모드"):
        GeminiCliRunner(process).build_argv(request(tmp_path, Stage.OPINION, "write_worktree"))


def test_gemini_model_schema_and_conversation(tmp_path, process):
    runner = GeminiCliRunner(process, models={Stage.OPINION: "gemini-3.5-flash"})
    schema = Opinion.model_json_schema()
    argv = runner.build_argv(
        request(
            tmp_path,
            Stage.OPINION,
            run_id="gm1",
            output_schema=schema,
            resume_session="conv-1",
        )
    )
    assert value_after(argv, "--model") == "gemini-3.5-flash"
    assert value_after(argv, "--json-schema") == str(tmp_path / "runs" / "gm1.schema.json")
    assert value_after(argv, "--conversation") == "conv-1"
    # request.model이 단계 기본 모델보다 우선한다
    argv_override = runner.build_argv(request(tmp_path, Stage.OPINION, model="gemini-3.1-pro"))
    assert value_after(argv_override, "--model") == "gemini-3.1-pro"
    assert "--model" not in runner.build_argv(request(tmp_path, Stage.DEBATE))


# Gemini (Antigravity CLI: agy): 출력 해석


def gemini_output(**fields):
    data = {
        "conversation_id": "conv-sess-1",
        "status": "SUCCESS",
        "response": "",
        "structured_output": None,
        "usage": {"total_tokens": 120},
    }
    data.update(fields)
    # 실제 agy stream-json 출력처럼 init, 단계, result 이벤트를 한 줄씩 낸다
    lines = [
        {"event": "init", "conversation_id": "conv-sess-1", "init": {"tools": ["search_web"]}},
        {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "x"}},
        {"event": "result", "result": data},
    ]
    return "\n".join(f"print({json.dumps(json.dumps(line, ensure_ascii=False))})" for line in lines)


def test_gemini_uses_structured_output_and_writes_settings_and_schema(tmp_path, process):
    exe = fake_cli(tmp_path, "agy", gemini_output(response="무시", structured_output=OPINION))
    req = request(tmp_path, run_id="g2", output_schema=Opinion.model_json_schema())
    result = run(GeminiCliRunner(process, exe).run(req))
    assert result.status is RunStatus.OK
    assert result.session_id == "conv-sess-1"
    assert parse_agent_output(result.stdout, Opinion).conclusion == "좋다"
    message = json.loads(calls(tmp_path, "agy")["stdin"])
    assert message == {"event": "user", "message": {"role": "user", "content": "의견을 말해라"}}
    settings_file = tmp_path / "runs" / "g2.settings.json"
    schema_file = tmp_path / "runs" / "g2.schema.json"
    assert "permissions" in json.loads(settings_file.read_text(encoding="utf-8"))
    assert json.loads(schema_file.read_text(encoding="utf-8")) == Opinion.model_json_schema()
    assert '"usage"' in result.log_path.read_text(encoding="utf-8")
    agent = (tmp_path / "runs" / "antigravity" / ".agents" / "agents" / "byeolchae.md").read_text(
        encoding="utf-8"
    )
    tools = next(line for line in agent.splitlines() if line.startswith("tools:"))
    # search_web이 없어야 웹 검색이 막히고, finish가 있어야 구조화 출력이 나온다
    assert "search_web" not in tools
    assert "finish" in tools


def test_gemini_falls_back_to_response_text_with_surrounding_noise(tmp_path, process):
    text = "```json\n" + json.dumps(OPINION, ensure_ascii=False) + "\n```"
    payload = json.dumps({"conversation_id": "c2", "status": "SUCCESS", "response": text})
    body = f"print('Loaded cached credentials.')\nprint({payload!r})"
    exe = fake_cli(tmp_path, "agy", body)
    result = run(GeminiCliRunner(process, exe).run(request(tmp_path, run_id="g3")))
    assert result.status is RunStatus.OK
    assert result.session_id == "c2"
    assert parse_agent_output(result.stdout, Opinion).proposal == "제안"


def test_gemini_error_envelope_marks_run_as_error_and_detects_quota(tmp_path, process):
    err = {"code": 429, "message": "RESOURCE_EXHAUSTED: quota exceeded"}
    exe = fake_cli(tmp_path, "agy", gemini_output(status="ERROR", error=err))
    result = run(GeminiCliRunner(process, exe).run(request(tmp_path, run_id="g4")))
    assert result.status is RunStatus.ERROR
    assert "RESOURCE_EXHAUSTED" in result.stdout
    assert is_usage_limited(result)


def test_gemini_non_success_status_without_error_field_is_error(tmp_path, process):
    exe = fake_cli(tmp_path, "agy", gemini_output(status="FAILED", response="중단됨"))
    result = run(GeminiCliRunner(process, exe).run(request(tmp_path, run_id="g4b")))
    assert result.status is RunStatus.ERROR
    assert result.stdout == "중단됨"


def test_gemini_non_json_output_is_error_and_kept_raw(tmp_path, process):
    exe = fake_cli(tmp_path, "agy", "print('충돌')")
    result = run(GeminiCliRunner(process, exe).run(request(tmp_path, run_id="g5")))
    assert result.status is RunStatus.ERROR
    assert result.stdout == "충돌\n"
    assert result.session_id is None


def test_gemini_nonzero_exit_stays_error(tmp_path, process):
    exe = fake_cli(tmp_path, "agy", gemini_output(response="부분") + "\nsys.exit(1)")
    result = run(GeminiCliRunner(process, exe).run(request(tmp_path, run_id="g6")))
    assert result.status is RunStatus.ERROR
    assert result.exit_code == 1


def test_gemini_timeout_and_cancel(tmp_path, process):
    exe = fake_cli(tmp_path, "agy", "import time; time.sleep(30)")
    runner = GeminiCliRunner(process, exe)
    timeout = run(runner.run(request(tmp_path, run_id="g7", timeout_sec=0.5)))
    assert timeout.status is RunStatus.TIMEOUT

    async def scenario():
        task = asyncio.create_task(runner.run(request(tmp_path, run_id="g8")))
        while not process.is_running("g8"):
            await asyncio.sleep(0.02)
        await runner.cancel("g8")
        return await task

    assert run(scenario()).status is RunStatus.CANCELLED


def test_gemini_non_git_command_rules_match_only_non_git_commands():
    patterns = [
        re.compile(rule.removeprefix("command(regex:").removesuffix(")"))
        for rule in NON_GIT_COMMANDS
    ]
    for command in ("ls -la", "pwd", "cat .env", "gitk", "gh pr list", "python -c 1"):
        assert any(p.fullmatch(command) for p in patterns), command
    for command in ("git status", "git commit -m x", "git rev-parse HEAD"):
        assert not any(p.fullmatch(command) for p in patterns), command
    # 규칙 안에 괄호가 있으면 agy가 command(...)를 잘못 읽는다
    assert all("(" not in rule.removeprefix("command(") for rule in NON_GIT_COMMANDS)
