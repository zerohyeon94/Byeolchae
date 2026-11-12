"""교육 부서 출력 스키마 (ADR-009).

교육팀장은 개념 노트 본문과 문제, 주간 한마디만 JSON으로 낸다.
파일 경로, 제목, 머리말, Daily, 주간 보고의 숫자는 봇이 정한다.
문제는 볼트에 쓰지 않고 Discord에서 버튼으로 푼다.
그림은 교육팀장이 설명과 이름표를 쓰고, Codex 팀원이 그린 파일을 봇이 확인해 볼트로 옮긴다.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Url = Annotated[str, StringConstraints(max_length=500, pattern=r"^https://\S+$")]
Letter = Literal["A", "B", "C", "D"]
LETTERS: tuple[Letter, ...] = ("A", "B", "C", "D")
NOTE_QUESTIONS = 3  # 개념 노트마다 (개념, 흐름, 실패·오류)
QUIZ_MIN, QUIZ_MAX = 5, 8  # 주간 문제
LABELS_MAX, LABEL_MAX = 10, 10  # 그림 이름표 개수, 이름표 글자 수
FIGURES_MIN, FIGURES_MAX = 2, 4  # 노트당 그림 (2026-11-12 Zero 결정: 그림이 기본)


class QuizQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=500)]
    # A~D 순서의 보기 문장. "A." 같은 머리표는 봇이 붙인다
    options: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    ] = Field(min_length=4, max_length=4)
    answer: Letter
    explanation: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)
    ]


class ConceptFigure(BaseModel):
    """노트의 한 절에 붙는 그림 설명. 그림은 교육팀원(Codex)이 그린다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 그림을 붙일 절 제목 줄. 본문의 `### …` 또는 `## …` 줄 그대로.
    # 그림은 이 제목 바로 아래, 설명 위에 들어간다
    section: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=120, pattern=r"^#{2,3} [^\n]+$")
    ]
    # 그릴 구성 요소(실제 장비 모양)와 화살표. 이름표마다 어느 요소·화살표에 붙는지 적는다
    brief: Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=1200)]
    # 그림 안에 글자 그대로 넣는 한글 이름표. 노트 본문의 용어와 같은 말을 쓴다
    labels: list[
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=LABEL_MAX),
        ]
    ] = Field(min_length=2, max_length=LABELS_MAX)
    # 그림 아래에 붙는 한 문장 설명. 노트와 #교육 안내에 쓴다
    alt: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=5, max_length=150, pattern=r"^[^\n]+$"),
    ]


class ConceptNote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # `## 한 문장으로`부터 끝까지의 마크다운. 형식은 bot.education.note.check_note가 본다
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=200, max_length=20000)]
    # 이 노트로 푸는 문제. 볼트에 쓰지 않고 Discord에 올린다
    questions: list[QuizQuestion] = Field(min_length=NOTE_QUESTIONS, max_length=NOTE_QUESTIONS)
    # 사실을 확인한 공식 문서·표준·위키백과 같은 신뢰할 수 있는 자료. #교육 알림에 붙는다
    sources: list[Url] = Field(min_length=1, max_length=8)
    # 확인하지 못해 노트에 "확인하지 못함"으로 적은 것
    unverified: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list, max_length=5
    )
    # 흐름의 작은 절마다 한 장, 어디서 깨지나에 한 장. 그리지 못한 그림은 빼고 낸다
    figures: list[ConceptFigure] = Field(min_length=FIGURES_MIN, max_length=FIGURES_MAX)


class ConceptImage(BaseModel):
    """교육팀원이 그린 그림 파일 위치. 봇이 새로 생긴 PNG인지 확인하고 볼트로 옮긴다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image_path: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]


class WeeklyReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # 다음 주를 위한 교육팀장의 한마디 1~3줄. 숫자는 봇이 센다
    comment: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]] = Field(
        min_length=1, max_length=3
    )
    # 이번 주 개념을 섞은 주간 문제
    questions: list[QuizQuestion] = Field(min_length=QUIZ_MIN, max_length=QUIZ_MAX)
