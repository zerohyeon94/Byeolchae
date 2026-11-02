"""교육 문제의 형식 검사와 Discord 표시 문구 (ADR-009).

오늘 문제(개념 노트 3문제), 복습 1문제, 주간 문제가 같은 형식(QuizQuestion)을 쓴다.
내용의 사실 여부는 판단하지 않는다.
"""

from collections.abc import Sequence

from bot.education.note import PRIVATE
from bot.schemas import LETTERS, QuizQuestion


def check_questions(questions: Sequence[QuizQuestion], extra: Sequence[str] = ()) -> list[str]:
    """보기 중복, 머리표, 회사 폴더 언급. extra는 함께 볼 다른 글(한마디 등)."""
    problems = []
    marks = {f"{letter}." for letter in LETTERS}
    texts = list(extra)
    for number, question in enumerate(questions, start=1):
        if len(set(question.options)) != len(question.options):
            problems.append(f"{number}번 문제의 보기가 겹친다")
        if any(option[:2] in marks for option in question.options):
            problems.append(f"{number}번 문제의 보기에 'A.' 같은 머리표를 붙이지 않는다")
        texts += [question.prompt, question.explanation, *question.options]
    if any(PRIVATE in text for text in texts):
        problems.append("회사 폴더(회사/)의 경로나 내용을 쓰지 않는다")
    return problems


def render_question(question: QuizQuestion, title: str, number: int, total: int) -> str:
    options = [f"{letter}. {text}" for letter, text in zip(LETTERS, question.options, strict=True)]
    head = f"**{title} {number}/{total}**" if total > 1 else f"**{title}**"
    return "\n".join([head, question.prompt, "", *options])


def answer_text(question: QuizQuestion, chosen: str, first: str) -> str:
    """버튼을 누른 Zero에게만 보이는 결과. 처음 고른 답만 점수에 남는다."""
    if chosen != first:
        head = f"이미 {first}를 골랐다 (정답 {question.answer}). 처음 고른 답만 기록한다."
    elif first == question.answer:
        head = f"정답이다 ({question.answer})."
    else:
        head = f"오답이다. 고른 답 {first}, 정답 {question.answer}."
    return f"{head}\n{question.explanation}"
