"""회사 조직 설정: `org.yaml`과 인격 파일 (SPEC 9.1-1, Q12, ADR-006, ADR-007).

- 조직(부서, 팀, 에이전트)과 단계별 담당은 비공개 회사 저장소(`paths.company`)의 `org.yaml`에 둔다
- 에이전트 이름(beta, alpha …)은 엔진(claude, codex)과 분리한다. 엔진은 에이전트마다 설정으로 정한다
- 인격·역량 전문은 `agents/<이름>.md`에 두고 프롬프트에 그대로 넣는다
- org.yaml이 없으면 ADR-007의 기본 조직(모두 Claude)을 쓴다. 이름과 직책은 공개해도 되는 값이다
- 불러올 때 규칙을 검사한다: 리뷰 담당 ≠ 설계·구현 담당(R1),
  Codex 엔진은 읽기 일과 구현·수정만(R4, ADR-012)
- 이름을 바꾼 에이전트는 예전 이름(`former`)을 남겨, 예전 기록도 새 이름으로 보이게 한다
"""

import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from bot.agents.base import CODEX_WRITE_STAGES, Stage

EngineName = Literal["claude", "codex"]

# ADR-005 기준 기록의 행위자 이름. 그때는 엔진 이름이 곧 에이전트 이름이었다
LEGACY_AGENTS: dict[str, tuple[str, EngineName]] = {
    "claude": ("Beta · Claude", "claude"),
    "codex": ("Alpha · Codex", "codex"),
}

# Codex 러너가 읽기 전용 샌드박스로 실행하는 단계. 쓰기는 CODEX_WRITE_STAGES만 (운영 규약 1.3)
READ_ONLY_STAGES = frozenset(
    {
        Stage.OPINION,
        Stage.DEBATE,
        Stage.REVIEW,
        Stage.DIAGNOSE,
        Stage.LEAD_CHECK,
        Stage.CONCEPT_NOTE,
        Stage.CONCEPT_IMAGE,
        Stage.WEEKLY_QUIZ,
        Stage.SECRETARY_DAILY,
        Stage.PROJECT_REPORT,
    }
)

_AGENT_KEY = re.compile(r"^[a-z][a-z0-9]{0,19}$")
ORG_FILE = "org.yaml"


class OrgError(ValueError):
    """org.yaml이 없거나 틀렸다. 메시지에는 위치와 이유만 담는다."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentSpec(_Model):
    display: str = Field(min_length=1)
    team: str
    title: str = "팀장"
    engine: EngineName = "claude"
    mbti: str = Field(default="", pattern=r"^([EI][NS][FT][JP])?$")
    perspective: str = Field(min_length=1)
    basic_question: str = Field(min_length=1)
    persona: str = ""  # 인격 파일 경로 (company 기준 상대 경로)
    models: dict[Stage, str] = {}
    daily_calls: int | None = Field(default=None, ge=1)
    hired: str = ""
    proposed_by: str | None = None
    reason: str = ""
    former: tuple[str, ...] = ()  # 예전 에이전트 이름 (기록 표시용)

    @field_validator("hired", mode="before")
    @classmethod
    def _date_text(cls, value: object) -> object:
        # YAML은 2026-10-28을 날짜로 읽는다. 기록용 문자열로 맞춘다
        return value.isoformat() if isinstance(value, date) else value


class TeamSpec(_Model):
    name: str = Field(min_length=1)
    lead: str
    department: str = "product"


class Roles(_Model):
    """기능 개발 흐름의 단계별 담당. Phase 5에서 파이프라인 선언으로 옮긴다."""

    opinions: tuple[str, ...] = ("beta", "alpha")  # 아무도 부르지 않았을 때 답하는 팀장
    design: str = "alpha"
    implement: str = "alpha"
    review: str = "delta"
    doc_sync: str = "beta"

    @property
    def assigned(self) -> dict[str, Stage]:
        return {
            self.design: Stage.DESIGN,
            self.implement: Stage.IMPLEMENT,
            self.review: Stage.REVIEW,
            self.doc_sync: Stage.DOC_SYNC,
        }


class Org(_Model):
    teams: dict[str, TeamSpec]
    agents: dict[str, AgentSpec]
    roles: Roles = Roles()
    # 인격 파일 내용. 불러올 때 채운다 (에이전트 → 본문)
    profiles: dict[str, str] = {}

    @field_validator("agents")
    @classmethod
    def _keys(cls, value: dict[str, AgentSpec]) -> dict[str, AgentSpec]:
        for key in value:
            if not _AGENT_KEY.match(key):
                raise ValueError(f"에이전트 이름 {key!r}는 영문 소문자로 시작하는 20자 이내")
            if key in LEGACY_AGENTS or key in {"zero", "bot"}:
                raise ValueError(f"에이전트 이름 {key!r}는 예약된 이름이다")
        return value

    @model_validator(mode="after")
    def _check(self) -> "Org":
        for key, agent in self.agents.items():
            if agent.team not in self.teams:
                raise ValueError(f"{key}의 팀 {agent.team!r}이 teams에 없다")
            if agent.engine == "codex":
                writes = {s for a, s in self.roles.assigned.items() if a == key}
                writes -= READ_ONLY_STAGES | CODEX_WRITE_STAGES
                if writes:
                    names = ", ".join(sorted(s.value for s in writes))
                    raise ValueError(f"{key}는 Codex 엔진이라 쓰기 단계({names})를 맡을 수 없다")
        seen: set[str] = set()
        for key, agent in self.agents.items():
            for old in agent.former:
                if old in self.agents or old in seen or old in LEGACY_AGENTS:
                    raise ValueError(f"{key}의 예전 이름 {old!r}이 다른 이름과 겹친다")
                seen.add(old)
        for key, team in self.teams.items():
            if team.lead not in self.agents:
                raise ValueError(f"팀 {key}의 팀장 {team.lead!r}이 agents에 없다")
        roles = self.roles
        for name in (*roles.opinions, roles.design, roles.implement, roles.review, roles.doc_sync):
            if name not in self.agents:
                raise ValueError(f"roles의 {name!r}이 agents에 없다")
        if not roles.opinions:
            raise ValueError("roles.opinions가 비어 있다")
        # R1: 만든 에이전트와 검증하는 에이전트를 다르게 둔다 (운영 규약 1.1-2)
        if roles.review in {roles.design, roles.implement}:
            raise ValueError("리뷰 담당은 설계·구현 담당과 달라야 한다 (R1)")
        return self

    # 조회

    @property
    def renames(self) -> dict[str, str]:
        """예전 이름 → 지금 이름."""
        return {old: key for key, spec in self.agents.items() for old in spec.former}

    def current(self, agent: str) -> str:
        """예전 이름이면 지금 이름으로 바꾼다. 아니면 그대로."""
        for key, spec in self.agents.items():
            if agent in spec.former:
                return key
        return agent

    def has(self, agent: str) -> bool:
        return self.current(agent) in self.agents or agent in LEGACY_AGENTS

    def display(self, agent: str) -> str:
        """표시 이름 `이름 · 직책` (SPEC Q16). 예: Beta · 기획팀장."""
        agent = self.current(agent)
        if agent in self.agents:
            spec = self.agents[agent]
            return f"{spec.display} · {self.teams[spec.team].name}{spec.title}"
        if agent in LEGACY_AGENTS:
            return LEGACY_AGENTS[agent][0]
        return {"zero": "Zero", "bot": "ZeroSync"}.get(agent, agent)

    def engine(self, agent: str) -> EngineName:
        agent = self.current(agent)
        if agent in self.agents:
            return self.agents[agent].engine
        if agent in LEGACY_AGENTS:
            return LEGACY_AGENTS[agent][1]
        raise KeyError(f"알 수 없는 에이전트: {agent}")

    def model(self, agent: str, stage: Stage) -> str | None:
        spec = self.agents.get(agent)
        return spec.models.get(stage) if spec is not None else None

    def profile(self, agent: str) -> str:
        return self.profiles.get(agent, "")

    @property
    def agent_names(self) -> tuple[str, ...]:
        return tuple(self.agents)


def _default_org() -> Org:
    """ADR-007·ADR-008의 제품 부서, ADR-009의 교육 부서, ADR-010의 비서 부서 기본 구성.

    org.yaml이 없을 때 쓴다.
    """
    return Org(
        teams={
            "planning": TeamSpec(name="기획", lead="beta"),
            "design": TeamSpec(name="디자인", lead="gamma"),
            "dev": TeamSpec(name="개발", lead="alpha"),
            "qa": TeamSpec(name="QA", lead="delta"),
            "education": TeamSpec(name="교육", lead="eta", department="education"),
            "secretary": TeamSpec(name="비서", lead="theta", department="secretary"),
        },
        agents={
            "beta": AgentSpec(
                display="Beta",
                team="planning",
                mbti="INFJ",
                perspective=(
                    "누구의 어떤 문제인가, 사용 흐름, 범위와 우선순위, 기존 기능·용어와의 일관성"
                ),
                basic_question="이것을 써야 하는가, 어디까지 할 것인가",
                hired="2026-10-26",
                reason="초기 구성 (ADR-006)",
            ),
            "gamma": AgentSpec(
                display="Gamma",
                team="design",
                mbti="ISFP",
                perspective="화면 구성, 빈 화면·로딩·오류·큰 글씨 상태, 디자인 시스템과의 일관성",
                basic_question="사용자가 화면에서 헤매지 않는가",
                hired="2026-10-28",
                reason="화면 디자인 담당 (ADR-007)",
            ),
            "alpha": AgentSpec(
                display="Alpha",
                team="dev",
                mbti="INTJ",
                perspective="구조와 변경 범위, 구현 난이도, 실행 위험, 엣지 케이스, 테스트 방법",
                basic_question="만들 수 있는가, 왜 실패할 수 있나, 더 작게 할 수 있나",
                hired="2026-10-26",
                reason="초기 구성 (ADR-006)",
            ),
            "delta": AgentSpec(
                display="Delta",
                team="qa",
                mbti="ISTJ",
                perspective="확인 기준, 빠진 경우, 회귀 위험, 코드 품질(동시성·메모리·오류 처리)",
                basic_question="무엇을 보면 됐다고 할 수 있나",
                hired="2026-10-28",
                reason="검증과 테스트 진단 (ADR-007)",
            ),
            "epsilon": AgentSpec(
                display="Epsilon",
                team="dev",
                title="팀원",
                mbti="ISTP",
                perspective="승인된 설계를 그대로 작게 구현하기, 동작하는 코드, 빠진 테스트",
                basic_question="설계대로 동작하는가, 테스트로 보여 줄 수 있는가",
                hired="2026-10-28",
                reason="구현·수정 실행 (ADR-008)",
            ),
            "zeta": AgentSpec(
                display="Zeta",
                team="qa",
                title="팀원",
                mbti="INTP",
                perspective="재현 조건, 원인을 끝까지 좁히기, 근거 위치(파일:줄, 로그)",
                basic_question="왜 실패하는가, 다시 일으킬 수 있는가",
                hired="2026-10-28",
                reason="버그·정기 테스트 실패 진단 (ADR-008)",
            ),
            "eta": AgentSpec(
                display="Eta",
                team="education",
                mbti="ENFJ",
                perspective=(
                    "처음 듣는 사람도 따라오는 설명, 확인된 사실만, 하루 20~30분 분량, "
                    "끊기지 않게 이어 가기"
                ),
                basic_question="오늘 이것 하나로 무엇을 알게 되는가",
                hired="2026-10-29",
                reason="교육 부서 Daily 개념 노트 (ADR-009)",
            ),
            "iota": AgentSpec(
                display="Iota",
                team="education",
                title="팀원",
                engine="codex",
                mbti="ISFP",
                perspective=(
                    "실제 장비 모양과 한글 이름표로 한눈에 보이는 흐름, 노트의 설명과 어긋나지 않게"
                ),
                basic_question="이 그림 한 장으로 흐름이 보이는가",
                hired="2026-11-03",
                reason="개념 노트 그림 (Codex 이미지 생성, ADR-009)",
            ),
            "theta": AgentSpec(
                display="Theta",
                team="secretary",
                mbti="ESTJ",
                perspective="하루에 실제로 일어난 일, 숫자와 사실, Zero가 오늘 결정할 것, 짧게",
                basic_question="Zero가 이것만 읽고 하루를 알 수 있는가",
                hired="2026-11-02",
                reason="비서 부서 저녁 Daily와 아침 보고 (ADR-010)",
            ),
            "kappa": AgentSpec(
                display="Kappa",
                team="planning",
                title="팀원",
                mbti="ISTJ",
                perspective="첫 출시까지 남은 작업과 출시를 막는 문제",
                basic_question="첫 출시에 필요한 일 중 무엇이 남았는가",
                models={Stage.PROJECT_REPORT: "sonnet"},
                daily_calls=12,
                hired="2026-11-06",
                reason="가게씨 프로젝트 진행·막힘 보고 (Zero 직접 채용)",
            ),
            "lambda": AgentSpec(
                display="Lambda",
                team="planning",
                title="팀원",
                mbti="ISFJ",
                perspective="운영 중인 제품의 버그와 다음 업데이트 후보",
                basic_question="사용자에게 영향을 주는 문제와 다음 업데이트 후보는 무엇인가",
                models={Stage.PROJECT_REPORT: "sonnet"},
                daily_calls=12,
                hired="2026-11-06",
                reason="제이데이 프로젝트 진행·막힘 보고 (Zero 직접 채용)",
            ),
            "mu": AgentSpec(
                display="Mu",
                team="planning",
                title="팀원",
                mbti="INTP",
                perspective="개선 아이디어와 이를 검토할 근거",
                basic_question="어떤 개선을 다음에 검토할 근거가 있는가",
                models={Stage.PROJECT_REPORT: "sonnet"},
                daily_calls=12,
                hired="2026-11-06",
                reason="오롯 프로젝트 진행·막힘 보고 (Zero 직접 채용)",
            ),
        },
    )


DEFAULT_ORG = _default_org()


def load_org(company_dir: Path) -> Org:
    """`<company>/org.yaml`과 인격 파일을 읽는다. org.yaml이 없으면 기본 조직."""
    path = company_dir / ORG_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_ORG
    except yaml.YAMLError:
        raise OrgError(f"{ORG_FILE}의 YAML 형식이 틀렸다") from None
    if not isinstance(raw, dict):
        raise OrgError(f"{ORG_FILE}의 최상위는 매핑이어야 한다")
    try:
        org = Org.model_validate(raw)
    except ValidationError as error:
        lines = [
            f"- {'.'.join(str(p) for p in item['loc']) or '(최상위)'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        ]
        raise OrgError(f"{ORG_FILE} 값이 틀렸다\n" + "\n".join(lines)) from None
    profiles = {}
    for key, agent in org.agents.items():
        if not agent.persona:
            continue
        file = (company_dir / agent.persona).resolve()
        if company_dir.resolve() not in file.parents:
            raise OrgError(f"{key}의 persona 경로가 회사 설정 폴더 밖이다")
        try:
            profiles[key] = file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise OrgError(f"{key}의 인격 파일 {agent.persona}이 없다") from None
    return org.model_copy(update={"profiles": profiles})
