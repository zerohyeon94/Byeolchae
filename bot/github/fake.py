"""테스트용 GitHub·git 가짜 구현. 호출을 기록하고, push 보호 규칙은 실제와 같이 적용한다."""

from dataclasses import dataclass, field
from pathlib import Path

from bot.github.client import PrState
from bot.github.git import Commit
from bot.github.rules import check_push


@dataclass
class FakeGitHub:
    repo: str = "owner/app"
    calls: list[tuple] = field(default_factory=list)
    next_number: int = 42
    pr_states: dict[int, PrState] = field(default_factory=dict)

    def _number(self) -> int:
        number = self.next_number
        self.next_number += 1
        return number

    async def ensure_labels(self) -> None:
        self.calls.append(("ensure_labels",))

    async def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        number = self._number()
        self.calls.append(("create_issue", number, title, body, tuple(labels)))
        return number

    async def comment_issue(self, number: int, body: str) -> None:
        self.calls.append(("comment_issue", number, body))

    async def close_issue(self, number: int, *, not_planned: bool, comment: str) -> None:
        self.calls.append(("close_issue", number, not_planned, comment))

    async def add_labels(self, number: int, labels: list[str]) -> None:
        self.calls.append(("add_labels", number, tuple(labels)))

    async def remove_label(self, number: int, label: str) -> None:
        self.calls.append(("remove_label", number, label))

    async def create_pr(self, *, base: str, head: str, title: str, body: str) -> int:
        number = self._number()
        self.calls.append(("create_pr", number, base, head, title, body))
        self.pr_states.setdefault(number, PrState("OPEN", False))
        return number

    async def edit_pr_body(self, number: int, body: str) -> None:
        self.calls.append(("edit_pr_body", number, body))

    async def comment_pr(self, number: int, body: str) -> None:
        self.calls.append(("comment_pr", number, body))

    async def mark_ready(self, number: int) -> None:
        self.calls.append(("mark_ready", number))

    async def close_pr(self, number: int, comment: str) -> None:
        self.calls.append(("close_pr", number, comment))

    async def pr_state(self, number: int) -> PrState:
        return self.pr_states.get(number, PrState("OPEN", False))

    def named(self, name: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == name]


@dataclass
class FakeGit:
    """worktree는 실제 폴더로 만들어, 가짜 에이전트가 파일을 쓸 수 있게 한다.

    커밋은 add_commit으로 쌓는다. 실제 git 대신 이 목록을 돌려준다.
    """

    calls: list[tuple] = field(default_factory=list)
    commit_log: dict[Path, list[Commit]] = field(default_factory=dict)
    branches: dict[Path, str] = field(default_factory=dict)
    dirty: set[Path] = field(default_factory=set)
    commit_files: dict[str, tuple[str, ...]] = field(default_factory=dict)
    diff_text: str = "diff --git a/x b/x\n+변경"

    def add_commit(self, path: Path, message: str, files: tuple[str, ...] = ()) -> None:
        log = self.commit_log.setdefault(path, [])
        log.append(Commit(f"sha{len(log) + 1}", message))
        self.commit_files[log[-1].sha] = files

    async def changed_files(self, path: Path, since: str) -> list[str]:
        # shortcut: 되돌린 변경은 모르고 since 뒤 커밋들의 파일을 모두 센다
        log = self.commit_log.get(path, [])
        shas = [c.sha for c in log]
        start = shas.index(since) + 1 if since in shas else 0
        return sorted({f for c in log[start:] for f in self.commit_files.get(c.sha, ())})

    async def prepare_worktree(self, repo: Path, path: Path, base_branch: str) -> None:
        self.calls.append(("prepare_worktree", repo, path, base_branch))
        path.mkdir(parents=True, exist_ok=True)

    async def remove_worktree(self, repo: Path, path: Path) -> None:
        self.calls.append(("remove_worktree", repo, path))

    async def is_clean(self, path: Path) -> bool:
        return path not in self.dirty

    async def commits(self, path: Path, base_branch: str) -> list[Commit]:
        return list(self.commit_log.get(path, []))

    async def diff(self, path: Path, base_branch: str) -> str:
        return self.diff_text

    async def current_branch(self, path: Path) -> str | None:
        return self.branches.get(path)

    async def create_branch(self, path: Path, name: str) -> None:
        self.calls.append(("create_branch", path, name))
        self.branches[path] = name

    async def rename_branch(self, path: Path, old: str, new: str) -> None:
        self.calls.append(("rename_branch", path, old, new))
        self.branches[path] = new

    async def push(self, path: Path, branch: str, *, recorded: str | None) -> None:
        check_push(branch, recorded)
        self.calls.append(("push", path, branch))

    async def delete_remote_branch(self, path: Path, branch: str, *, recorded: str | None) -> None:
        check_push(branch, recorded)
        self.calls.append(("delete_remote_branch", path, branch))

    def named(self, name: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == name]
