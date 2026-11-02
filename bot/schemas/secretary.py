"""비서 부서 출력 스키마 (ADR-010).

비서는 저녁 하루 정리의 요약만 JSON으로 낸다. Daily 파일의 나머지(결정할 것, 학습, 제품 부서,
토큰)와 그 숫자는 봇이 센다.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class DailySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Zero가 이것만 읽고 하루를 알 수 있는 1~3줄. 숫자는 봇이 센 것을 그대로 쓴다
    summary: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    ] = Field(min_length=1, max_length=3)
