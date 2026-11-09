"""밤도란 대시보드의 공개 이름."""

import re

from bot.org import Org

COMPANY_NAME = "밤도란"
FOUNDER_NAME = "노을"


def actor_name(org: Org, actor: str) -> str:
    if actor == "zero":
        return FOUNDER_NAME
    if actor == "bot":
        return f"{COMPANY_NAME} 봇"
    return org.display(actor)


def staff_description(text: str) -> str:
    """직원 설정의 소개 문구에서 예전 대표 호칭을 현재 호칭으로 읽는다."""
    return re.sub(r"(?<![A-Za-z0-9_])Zero(?![A-Za-z0-9_])", f"{FOUNDER_NAME} 대표", text)
