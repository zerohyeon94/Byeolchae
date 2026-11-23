"""조사 보고 → 볼트 파일과 Discord 글 (ADR-018)."""

import re
from datetime import date

from bot.schemas import ResearchReport

FOLDER = "조사"
_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]\s]+')


def report_path(day: date, title: str, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """볼트 기준 상대 경로. 같은 날 같은 제목이면 뒤에 번호를 붙인다."""
    slug = _UNSAFE.sub("-", title).strip("-.")[:60].rstrip("-.") or "조사"
    base = f"{FOLDER}/{day:%Y-%m}/{day.isoformat()}-{slug}"
    path, n = f"{base}.md", 2
    while path in taken:
        path, n = f"{base}-{n}.md", n + 1
    return path


def report_file(
    report: ResearchReport, *, day: date, topic: str, author: str, checker: str, check_note: str
) -> str:
    lines = [
        f"# {report.title}",
        "",
        f"> {day.isoformat()} · 조사 {author} · 확인 {checker}",
        f"> 주제: {topic}",
    ]
    if check_note:
        lines.append(f"> {check_note}")
    lines += ["", "## 요약", "", report.summary, "", report.body, "", "## 출처", ""]
    lines += [f"- [{source.title}]({source.url})" for source in report.sources]
    if report.unverified:
        lines += ["", "## 확인하지 못함", "", *(f"- {item}" for item in report.unverified)]
    return "\n".join(lines) + "\n"


def report_post(report: ResearchReport, *, path: str, check_note: str) -> str:
    lines = [f"**조사 보고: {report.title}**", "", report.summary, ""]
    if check_note:
        lines += [check_note, ""]
    lines.append(f"볼트: `{path}` (출처 {len(report.sources)}개)")
    return "\n".join(lines)
