"""프로젝트 담당자에게 보여 줄 작업 근거. 로그 원문과 볼트에 쓸 파일 본문은 제외한다."""

import json
from typing import Any

_EVIDENCE_FIELDS = frozenset(
    {
        "title",
        "text",
        "reason",
        "status",
        "stage",
        "summary",
        "changes_summary",
        "conclusion",
        "risks",
        "proposal",
        "fix_direction",
        "verdict",
        "findings",
        "passed",
        "total",
        "failures",
        "failed_tests",
        "errors",
        "outcome",
        "blockers",
        "name",
        "arg",
        "pr",
        "version",
    }
)


def report_facts(rows: list[dict[str, Any]]) -> str:
    lines = []
    for row in rows:
        data = row["payload"] if isinstance(row["payload"], dict) else {}
        summary = data
        for key in ("summary", "opinion", "report", "review", "proposal", "prep"):
            if isinstance(data.get(key), dict):
                summary = data[key]
                break
        facts = {k: v for k, v in summary.items() if k in _EVIDENCE_FIELDS}
        lines.append(
            f"{row['actor']} · {row['kind']} {json.dumps(facts, ensure_ascii=False)[:900]}"
        )
    return "\n".join(lines)
