"""테스트용 조직 설정.

LEGACY_ORG는 ADR-005 기준 흐름(아르크투루스가 설계·구현, 리겔이 리뷰)을 엔진만 나눠 재현한다.
아르크투루스는 claude 러너, 리겔은 codex 러너로 실행되어
기존 시나리오의 가짜 응답 순서를 그대로 쓴다.
"""

from bot.org import AgentSpec, Org, Roles, TeamSpec

LEGACY_ORG = Org(
    teams={
        "planning": TeamSpec(name="기획", lead="arcturus"),
        "dev": TeamSpec(name="개발", lead="rigel"),
    },
    agents={
        "arcturus": AgentSpec(
            display="아르크투루스",
            team="planning",
            engine="claude",
            mbti="INFJ",
            perspective="사용 흐름과 범위",
            basic_question="이것을 써야 하는가",
        ),
        "rigel": AgentSpec(
            display="리겔",
            team="dev",
            engine="codex",
            mbti="INTJ",
            perspective="구조와 위험",
            basic_question="왜 실패할 수 있나",
        ),
    },
    roles=Roles(
        opinions=("arcturus", "rigel"),
        design="arcturus",
        implement="arcturus",
        review="rigel",
        doc_sync="arcturus",
    ),
)
