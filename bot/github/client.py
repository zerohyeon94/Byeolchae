"""GitHub 쓰기 (SPEC 6.2 GitHubClient, 운영 규약 1.8, 1.9, 4.5, 4.8).

`gh` CLI를 셸 없이 실행한다. 본문은 stdin(`--body-file -`)으로 넘긴다.
"""

import asyncio
import json
from dataclasses import dataclass

# 운영 규약 4.8
LABELS = {
    "zerosync": ("5865F2", "별채 봇이 만든 Issue·PR"),
    "type:feature": ("1D76DB", "기능 추가·변경"),
    "type:fix": ("D73A4A", "버그 수정"),
    "type:refactor": ("A2EEEF", "내부 개선"),
    "needs-human": ("FBCA04", "Zero의 판단이 필요하다"),
}


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class PrState:
    state: str  # OPEN, CLOSED, MERGED
    merged: bool


def _number_from_url(url: str) -> int:
    return int(url.strip().rstrip("/").rsplit("/", 1)[-1])


class GitHubClient:
    def __init__(self, repo: str, executable: str = "gh"):
        self.repo = repo
        self._gh = executable

    async def _run(self, *args: str, stdin: str | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            self._gh,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(None if stdin is None else stdin.encode())
        if proc.returncode != 0:
            raise GitHubError(
                f"gh {' '.join(args[:2])} 실패: {err.decode(errors='replace').strip()}"
            )
        return out.decode(errors="replace")

    async def ensure_labels(self) -> None:
        for name, (color, description) in LABELS.items():
            await self._run(
                "label", "create", name, "--repo", self.repo,
                "--color", color, "--description", description, "--force",
            )  # fmt: skip

    async def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        args = ["issue", "create", "--repo", self.repo, "--title", title, "--body-file", "-"]
        for label in labels:
            args += ["--label", label]
        return _number_from_url(await self._run(*args, stdin=body))

    async def comment_issue(self, number: int, body: str) -> None:
        await self._run(
            "issue", "comment", str(number), "--repo", self.repo, "--body-file", "-", stdin=body
        )

    async def close_issue(self, number: int, *, not_planned: bool, comment: str) -> None:
        await self.comment_issue(number, comment)
        reason = "not planned" if not_planned else "completed"
        await self._run("issue", "close", str(number), "--repo", self.repo, "--reason", reason)

    async def add_labels(self, number: int, labels: list[str]) -> None:
        await self._run(
            "api", "-X", "POST", f"repos/{self.repo}/issues/{number}/labels", "--input", "-",
            stdin=json.dumps({"labels": labels}),
        )  # fmt: skip

    async def remove_label(self, number: int, label: str) -> None:
        try:
            await self._run(
                "api", "-X", "DELETE", f"repos/{self.repo}/issues/{number}/labels/{label}"
            )
        except GitHubError as error:
            if "404" not in str(error) and "Not Found" not in str(error):
                raise

    async def create_pr(self, *, base: str, head: str, title: str, body: str) -> int:
        """처음부터 draft로 만든다 (운영 규약 4.5)."""
        out = await self._run(
            "pr", "create", "--repo", self.repo, "--base", base, "--head", head,
            "--title", title, "--body-file", "-", "--draft",
            stdin=body,
        )  # fmt: skip
        return _number_from_url(out)

    async def edit_pr_body(self, number: int, body: str) -> None:
        await self._run(
            "pr", "edit", str(number), "--repo", self.repo, "--body-file", "-", stdin=body
        )

    async def comment_pr(self, number: int, body: str) -> None:
        await self._run(
            "pr", "comment", str(number), "--repo", self.repo, "--body-file", "-", stdin=body
        )

    async def mark_ready(self, number: int) -> None:
        await self._run("pr", "ready", str(number), "--repo", self.repo)

    async def close_pr(self, number: int, comment: str) -> None:
        await self._run("pr", "close", str(number), "--repo", self.repo, "--comment", comment)

    async def pr_state(self, number: int) -> PrState:
        out = await self._run("pr", "view", str(number), "--repo", self.repo, "--json", "state")
        state = json.loads(out)["state"]
        return PrState(state, state == "MERGED")
