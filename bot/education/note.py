"""개념 노트 형식 검사 (볼트 Templates/개념 노트.md, AGENTS.md "개념 노트 작성 기준").

교육팀장이 쓴 본문이 템플릿 섹션 순서를 지키는지 본다. 노트에는 학습 내용만 두고,
문제는 JSON의 questions로 받아 Discord에 올린다. 내용의 사실 여부는 판단하지 않는다
(봇은 해석하지 않는다). 문제가 있으면 목록을 돌려주고, 엔진이 1회 다시 요청한다.
"""

import re

SECTIONS = (
    "## 한 문장으로",
    "## 비유",
    "## 흐름",
    "## 어디서 깨지나",
    "## 이런 시스템에서는",
    "## 단어 정리",
)
# 볼트에서 git에 올라가지 않는 회사 폴더. 노트에 경로조차 남기지 않는다
PRIVATE = "회사/"
# 마크다운 표의 구분 줄 (|---|---| 또는 --- | ---). iPad에서 좁게 읽히므로 표를 쓰지 않는다
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def check_note(body: str) -> list[str]:
    problems = []
    text = body.strip()
    if text.startswith("# "):
        problems.append("제목(# …)과 머리말은 봇이 붙인다. 본문은 '## 한 문장으로'부터 쓴다")
    headings = [line.strip() for line in text.splitlines() if line.startswith("## ")]
    expected = [h for h in headings if h in SECTIONS]
    if tuple(expected) != SECTIONS:
        missing = [h for h in SECTIONS if h not in headings]
        if missing:
            problems.append(f"빠진 섹션: {', '.join(missing)}")
        else:
            problems.append(f"섹션 순서는 {' → '.join(s[3:] for s in SECTIONS)}이다")
    extra = [h for h in headings if h not in SECTIONS]
    if extra:
        problems.append(
            f"템플릿에 없는 섹션: {', '.join(extra)} (문제는 본문이 아니라 questions에 쓴다)"
        )
    if PRIVATE in text:
        problems.append("회사 폴더(회사/)의 경로나 내용을 쓰지 않는다")
    if any(TABLE_RULE.match(line) for line in _outside_code(text)):
        problems.append("표(| … |)를 쓰지 않는다. iPad에서 읽히도록 목록으로 쓴다")
    return problems


def _outside_code(text: str) -> list[str]:
    """코드 블록(```) 밖의 줄만. 텍스트 그림 안의 선은 표로 보지 않는다."""
    lines, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            lines.append(line)
    return lines


def complete_note(header: str, body: str) -> str:
    """봇이 정한 제목·머리말을 붙인다."""
    return header + body.strip() + "\n"
