"""대시보드의 공개 이름."""

import re

from bot.org import Org

COMPANY_NAME = "별채"
COMPANY_NAME_EN = "Byeolchae"
FOUNDER_NAME = "라군"
FOUNDER_NAME_EN = "Lagoon"


def _subject(name: str) -> str:
    """이름 뒤에 받침에 맞는 주격 조사를 붙인다 (라군 → 라군이)."""
    last = ord(name[-1]) - 0xAC00
    has_final = 0 <= last < 11172 and last % 28 != 0
    return name + ("이" if has_final else "가")


FOUNDER_SUBJECT = _subject(FOUNDER_NAME)


def actor_name(org: Org, actor: str) -> str:
    if actor == "zero":
        return FOUNDER_NAME
    if actor == "bot":
        return f"{COMPANY_NAME} 봇"
    return org.display(actor)


def staff_description(text: str) -> str:
    """직원 설정의 소개 문구에서 예전 대표 호칭을 현재 호칭으로 읽는다."""
    return re.sub(r"(?<![A-Za-z0-9_])Zero(?![A-Za-z0-9_])", f"{FOUNDER_NAME} 대표", text)
