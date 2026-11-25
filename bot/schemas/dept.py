"""#비서·#교육 대화의 답 스키마 (ADR-013).

의견(찬반·근거·위험) 대신 Zero의 질문에 바로 답하는 본문을 낸다.
다른 팀장이 답할 질문이면 handoff에 담당자와 질문을 적는다. 봇은 담당자가 조직에 있는지만
검사하고 그 팀장을 바로 부른다.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# 게시용 요약 길이 (운영 규약 2.6)
AnswerText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]


# 한 번에 넘길 수 있는 최대 인원 (ADR-013)
MAX_HANDOFF = 3


class Handoff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]{0,19}$")]  # org.yaml의 이름 키
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ResearchRequest(BaseModel):
    """웹에서 새로 찾아봐야 하는 질문이면 조사 보고로 넘긴다 (ADR-018). 봇이 담당을 부른다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    # org.yaml의 이름 키. 비우면 기본 담당(리서치팀 데네브)이 맡는다
    agent: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]{0,19}$")] | None = None


class DeptAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: AnswerText
    handoff: list[Handoff] = Field(default_factory=list, max_length=MAX_HANDOFF)
    research: ResearchRequest | None = None
    # 회의가 필요하면 스레드 제목. 봇이 Zero 메시지에 스레드를 열고 그 안에서 이어 간다
    thread_title: (
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[^\n]+$"
            ),
        ]
        | None
    ) = None


class OfficeAnswer(BaseModel):
    """#대표실 대장의 한 마디 (ADR-019). 첫 답, 서로 대화, 정리, 인사 모두 이 형식이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: AnswerText
