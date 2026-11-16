"""#비서·#교육 대화의 답 스키마 (ADR-013).

의견(찬반·근거·위험) 대신 Zero의 질문에 바로 답하는 본문을 낸다.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

# 게시용 요약 길이 (운영 규약 2.6)
AnswerText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]


class DeptAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: AnswerText
