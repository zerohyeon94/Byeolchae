"""조사 보고 스키마 (ADR-018).

담당 직원이 웹으로 조사해 낸 보고. 봇은 검증한 원본으로 볼트 파일과 Discord 요약을 만든다.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from bot.schemas.education import Url


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    url: Url


class ResearchReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # 파일 이름과 제목에 쓴다
    title: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[^\n]+$"),
    ]
    # Discord에 올리는 요약. 결론을 먼저 쓴다
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)]
    # 볼트 파일 본문 마크다운. 제목(`# …`)과 출처 목록은 봇이 붙인다
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
    # 실제로 열어 확인한 자료. 없으면 조사 보고가 아니다
    sources: list[Source] = Field(min_length=1, max_length=30)
    # 확인하지 못한 것
    unverified: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list, max_length=10
    )
