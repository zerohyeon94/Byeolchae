"""봇의 git 작업 (SPEC 6.2 GitOps, 운영 규약 4.3, 4.7).

셸 없이 인자 목록으로 git을 실행한다. 모든 push는 check_push를 통과해야 한다.
force push는 하지 않는다. 작업은 봇 전용 worktree에서 한다 (운영 규약 3.3).
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from bot.github.rules import check_push


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str


async def run_git(*args: str, cwd: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} 실패: {err.decode(errors='replace').strip()}")
    return out.decode(errors="replace")


class GitOps:
    async def prepare_worktree(self, repo: Path, path: Path, base_branch: str) -> None:
        """base 브랜치 최신 커밋에서 detached worktree를 만든다. 이미 있으면 그대로 쓴다."""
        if path.exists():
            return
        await run_git("fetch", "origin", base_branch, cwd=repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        await run_git("worktree", "add", "--detach", str(path), f"origin/{base_branch}", cwd=repo)

    async def remove_worktree(self, repo: Path, path: Path) -> None:
        if path.exists():
            await run_git("worktree", "remove", "--force", str(path), cwd=repo)

    async def is_clean(self, path: Path) -> bool:
        return not (await run_git("status", "--porcelain", cwd=path)).strip()

    async def commits(self, path: Path, base_branch: str) -> list[Commit]:
        """base 브랜치 이후 이 worktree에 쌓인 커밋 (오래된 것부터)."""
        out = await run_git(
            "log", "--reverse", "--format=%H%x00%B%x1e", f"origin/{base_branch}..HEAD", cwd=path
        )
        commits = []
        for record in out.split("\x1e"):
            record = record.strip("\n")
            if record:
                sha, _, message = record.partition("\x00")
                commits.append(Commit(sha, message.strip()))
        return commits

    async def diff(self, path: Path, base_branch: str) -> str:
        return await run_git("diff", "--stat", "--patch", f"origin/{base_branch}...HEAD", cwd=path)

    async def current_branch(self, path: Path) -> str | None:
        name = (await run_git("branch", "--show-current", cwd=path)).strip()
        return name or None

    async def create_branch(self, path: Path, name: str) -> None:
        await run_git("switch", "-c", name, cwd=path)

    async def rename_branch(self, path: Path, old: str, new: str) -> None:
        await run_git("branch", "-m", old, new, cwd=path)

    async def push(self, path: Path, branch: str, *, recorded: str | None) -> None:
        check_push(branch, recorded)
        await run_git("push", "--set-upstream", "origin", f"{branch}:{branch}", cwd=path)

    async def delete_remote_branch(self, path: Path, branch: str, *, recorded: str | None) -> None:
        check_push(branch, recorded)
        await run_git("push", "origin", "--delete", branch, cwd=path)


@dataclass(frozen=True)
class VaultPush:
    """하루 한 번 볼트 push 결과. 못 올렸으면 problem에 이유를 적는다."""

    pushed: int = 0  # GitHub에 올린 커밋 수
    problem: str = ""


class VaultGit:
    """볼트가 git 저장소면 봇이 쓴 파일만 커밋한다 (운영 규약 3.4).

    Zero가 볼트에서 따로 고치던 파일은 경로를 지정해 커밋하므로 섞이지 않는다.
    push는 비서가 저녁 Daily를 쓴 뒤 하루 한 번만 한다 (ADR-010). force push는 하지 않고,
    GitHub에 이 맥에 없는 커밋이 있으면 올리지 않고 알린다.
    """

    async def commit(self, root: Path, paths: Sequence[str], title: str, trailer: str) -> bool:
        try:
            await run_git("rev-parse", "--is-inside-work-tree", cwd=root)
        except GitError:
            return False  # git이 아닌 볼트는 파일만 쓴다
        await run_git("add", "--", *paths, cwd=root)
        await run_git("commit", "-q", "-m", title, "-m", trailer, "--only", "--", *paths, cwd=root)
        return True

    async def push(self, root: Path) -> VaultPush:
        """지금 브랜치를 추적 중인 원격 브랜치로 올린다. 실패는 예외 대신 결과로 돌려준다."""
        try:
            await run_git("rev-parse", "--is-inside-work-tree", cwd=root)
        except GitError:
            return VaultPush()  # git이 아닌 볼트는 올릴 것이 없다
        try:
            upstream = (
                await run_git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=root)
            ).strip()
        except GitError:
            return VaultPush(problem="볼트 브랜치에 추적 중인 GitHub 브랜치가 없다")
        remote, _, branch = upstream.partition("/")
        try:
            await run_git("fetch", "-q", remote, cwd=root)
            behind, ahead = (
                await run_git("rev-list", "--left-right", "--count", "@{u}...HEAD", cwd=root)
            ).split()
            if int(behind):
                return VaultPush(
                    problem=f"GitHub에 이 맥에 없는 커밋 {behind}개가 있어 올리지 않았다"
                )
            if not int(ahead):
                return VaultPush()
            await run_git("push", "-q", remote, f"HEAD:{branch}", cwd=root)
        except GitError as error:
            return VaultPush(problem=str(error)[:200])
        return VaultPush(pushed=int(ahead))
