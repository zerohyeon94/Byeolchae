"""GitHub·git 계층 테스트.

push 보호·커밋 메시지 규칙은 단위 테스트로 고정한다 (AGENTS.md).
GitOps는 로컬 bare 저장소를 origin으로 둔 실제 git으로, GitHubClient는 인자를 기록하는
가짜 gh 실행 파일로 확인한다. 네트워크와 실제 GitHub는 쓰지 않는다.
"""

import asyncio
import json
import stat
import subprocess
import sys

import pytest

from bot.github import (
    GitHubClient,
    GitHubError,
    GitOps,
    PushRefused,
    VaultGit,
    VaultPush,
    check_push,
    commit_message_problems,
)


def run(coro):
    return asyncio.run(coro)


# push 보호 (운영 규약 4.7)


@pytest.mark.parametrize(
    ("branch", "recorded"),
    [
        ("feat/42-budget-banner", "feat/42-budget-banner"),
        ("fix/7-crash", "fix/7-crash"),
        ("refactor/3-cache-layer", "refactor/3-cache-layer"),
    ],
)
def test_push_allowed(branch, recorded):
    check_push(branch, recorded)


@pytest.mark.parametrize(
    ("branch", "recorded", "reason"),
    [
        ("develop", "develop", "형식이 아니다"),
        ("main", "main", "형식이 아니다"),
        ("feat/budget", "feat/budget", "형식이 아니다"),
        ("claude/42-x", "claude/42-x", "형식이 아니다"),
        ("docs/42-x", "docs/42-x", "형식이 아니다"),
        ("feat/42-budget-banner", None, "봇이 만든 브랜치가 아니다"),
        ("feat/42-budget-banner", "feat/42-other", "봇이 만든 브랜치가 아니다"),
    ],
)
def test_push_refused(branch, recorded, reason):
    with pytest.raises(PushRefused, match=reason):
        check_push(branch, recorded)


# 커밋 메시지 (운영 규약 4.4)


@pytest.mark.parametrize(
    "message",
    [
        "docs(design): 예산 초과 배너 설계\n\nRefs: #42",
        "feat(budget): 월 지출 합계 캐시 추가\n\n본문 설명\n\nRefs: #42",
        "fix: 리뷰 1라운드 [필수] 1·2 반영\n\nRefs: #42",
    ],
)
def test_commit_message_ok(message):
    assert commit_message_problems(message, 42) == []


@pytest.mark.parametrize(
    ("message", "problem"),
    [
        ("예산 배너 추가\n\nRefs: #42", "형식이 아니다"),
        ("feature(budget): 추가\n\nRefs: #42", "형식이 아니다"),
        ("feat(Budget): 추가\n\nRefs: #42", "형식이 아니다"),
        ("feat: wip\n\nRefs: #42", "의미 없는 말"),
        ("fix: 수정\n\nRefs: #42", "의미 없는 말"),
        ("feat: 배너 추가\n본문\n\nRefs: #42", "빈 줄"),
        ("feat: 배너 추가", "Refs: #42"),
        ("feat: 배너 추가\n\nRefs: #7", "Refs: #42"),
        ("", "비어 있다"),
    ],
)
def test_commit_message_problems(message, problem):
    problems = commit_message_problems(message, 42)
    assert any(problem in p for p in problems), problems


# GitOps (실제 git, 로컬 origin)


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    git("init", "--bare", "-q", "-b", "develop", str(origin), cwd=tmp_path)
    repo = tmp_path / "app"
    git("clone", "-q", str(origin), str(repo), cwd=tmp_path)
    for key, value in (("user.name", "t"), ("user.email", "t@localhost")):
        git("config", key, value, cwd=repo)
    (repo / "README.md").write_text("앱\n")
    git("add", ".", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    git("push", "-q", "origin", "HEAD:develop", cwd=repo)
    return repo


def test_git_ops_design_flow(repo, tmp_path):
    ops = GitOps()
    worktree = tmp_path / "worktrees" / "app-42"

    async def flow():
        await ops.prepare_worktree(repo, worktree, "develop")
        await ops.prepare_worktree(repo, worktree, "develop")  # 이미 있으면 그대로
        assert await ops.current_branch(worktree) is None  # detached
        assert await ops.commits(worktree, "develop") == []

        (worktree / "docs").mkdir()
        (worktree / "docs" / "d.md").write_text("설계")
        assert await ops.is_clean(worktree) is False
        git("add", ".", cwd=worktree)
        git("-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm",
            "docs(design): 설계\n\nRefs: #42", cwd=worktree)  # fmt: skip
        assert await ops.is_clean(worktree) is True
        (commit,) = await ops.commits(worktree, "develop")
        assert commit.message == "docs(design): 설계\n\nRefs: #42"
        assert "docs/d.md" in await ops.diff(worktree, "develop")

        await ops.create_branch(worktree, "feat/42-banner")
        await ops.push(worktree, "feat/42-banner", recorded="feat/42-banner")
        assert "feat/42-banner" in git("branch", "-r", cwd=repo) or git(
            "ls-remote", "--heads", "origin", cwd=repo
        ).count("feat/42-banner")

        await ops.rename_branch(worktree, "feat/42-banner", "fix/42-banner")
        assert await ops.current_branch(worktree) == "fix/42-banner"
        await ops.push(worktree, "fix/42-banner", recorded="fix/42-banner")
        await ops.delete_remote_branch(worktree, "feat/42-banner", recorded="feat/42-banner")
        heads = git("ls-remote", "--heads", "origin", cwd=repo)
        assert "fix/42-banner" in heads and "feat/42-banner" not in heads

        with pytest.raises(PushRefused):
            await ops.push(worktree, "develop", recorded="develop")

        await ops.remove_worktree(repo, worktree)
        assert not worktree.exists()

    run(flow())


# GitHubClient (가짜 gh)


@pytest.fixture
def fake_gh(tmp_path):
    log = tmp_path / "gh.log"
    script = tmp_path / "gh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "stdin = '' if sys.stdin.isatty() else sys.stdin.read()\n"
        f"record = json.dumps({{'args': args, 'stdin': stdin}})\n"
        f"with open({str(log)!r}, 'a') as f: f.write(record + '\\n')\n"
        "if args[:2] == ['issue', 'create']: print('https://github.com/o/r/issues/42')\n"
        "elif args[:2] == ['pr', 'create']: print('https://github.com/o/r/pull/43')\n"
        "elif args[:2] == ['pr', 'view']: print(json.dumps({'state': 'MERGED'}))\n"
        "elif args[:2] == ['issue', 'comment'] and 'fail' in stdin:\n"
        "    print('HTTP 422', file=sys.stderr); sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()]

    return str(script), calls


def test_github_client_commands(fake_gh):
    exe, calls = fake_gh
    gh = GitHubClient("o/r", exe)

    async def flow():
        assert await gh.create_issue("제목", "본문", ["zerosync"]) == 42
        await gh.comment_issue(42, "설계 요약")
        await gh.close_issue(42, not_planned=True, comment="중지")
        await gh.add_labels(42, ["type:feature"])
        assert await gh.create_pr(base="develop", head="feat/42-x", title="feat: x", body="b") == 43
        await gh.edit_pr_body(43, "새 본문")
        await gh.mark_ready(43)
        state = await gh.pr_state(43)
        assert state.merged is True
        with pytest.raises(GitHubError, match="HTTP 422"):
            await gh.comment_issue(42, "fail")

    run(flow())
    recorded = calls()
    create = recorded[0]
    assert create["args"][:2] == ["issue", "create"]
    assert ["--repo", "o/r"] == create["args"][2:4]
    assert "--label" in create["args"] and create["stdin"] == "본문"
    close = recorded[3]
    assert close["args"][:3] == ["issue", "close", "42"] and "not planned" in close["args"]
    labels = recorded[4]
    assert labels["args"][:3] == ["api", "-X", "POST"]
    assert json.loads(labels["stdin"]) == {"labels": ["type:feature"]}
    pr = recorded[5]
    assert "--draft" in pr["args"] and pr["args"][pr["args"].index("--base") + 1] == "develop"


# VaultGit (실제 git)


def test_vault_git_commits_only_written_files(repo):
    (repo / "기능").mkdir()
    (repo / "기능" / "배너.md").write_text("봇이 씀\n")
    (repo / "README.md").write_text("Zero가 고치던 중\n")
    done = run(
        VaultGit().commit(repo, ["기능/배너.md"], "docs(gagaessi): 배너", "ZeroSync-Task: x")
    )
    assert done is True
    log = git("log", "-1", "--format=%B", cwd=repo)
    assert "docs(gagaessi): 배너" in log and "ZeroSync-Task: x" in log
    assert git(
        "-c", "core.quotePath=false", "show", "--name-only", "--format=", "HEAD", cwd=repo
    ).split() == ["기능/배너.md"]
    assert "README.md" in git("status", "--porcelain", cwd=repo)


def test_vault_git_skips_non_git_folder(tmp_path):
    (tmp_path / "a.md").write_text("x")
    assert run(VaultGit().commit(tmp_path, ["a.md"], "t", "z")) is False


def test_vault_push_sends_new_commits_once_and_never_overwrites_github(repo, tmp_path):
    vault = VaultGit()
    # develop을 추적하도록 맞춘다 (fixture는 HEAD:develop으로 올렸다)
    git("branch", "-q", "--set-upstream-to=origin/develop", cwd=repo)
    for name in ("a.md", "b.md"):
        (repo / name).write_text("봇이 씀\n")
        run(vault.commit(repo, [name], f"docs: {name}", "ZeroSync-Department: secretary"))
    assert run(vault.push(repo)) == VaultPush(pushed=2)
    assert run(vault.push(repo)) == VaultPush()  # 올릴 것이 없다
    # 다른 곳에서 GitHub에 올린 커밋이 있으면 덮어쓰지 않는다
    other = tmp_path / "other"
    git("clone", "-q", "-b", "develop", str(tmp_path / "origin.git"), str(other), cwd=tmp_path)
    for key, value in (("user.name", "t"), ("user.email", "t@localhost")):
        git("config", key, value, cwd=other)
    (other / "c.md").write_text("다른 기기\n")
    git("add", ".", cwd=other)
    git("commit", "-qm", "other", cwd=other)
    git("push", "-q", "origin", "HEAD:develop", cwd=other)
    (repo / "d.md").write_text("봇이 씀\n")
    run(vault.commit(repo, ["d.md"], "docs: d", "z"))
    result = run(vault.push(repo))
    assert result.pushed == 0 and "이 맥에 없는 커밋 1개" in result.problem
    assert git("log", "-1", "--format=%s", "origin/develop", cwd=repo).strip() == "other"


def test_vault_push_without_upstream_or_git(repo, tmp_path):
    git("switch", "-q", "-c", "local-only", cwd=repo)
    assert "추적 중인 GitHub 브랜치가 없다" in run(VaultGit().push(repo)).problem
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run(VaultGit().push(plain)) == VaultPush()
