"""파이프라인 선언 (SPEC 3.5, Q13, ADR-007).

파이프라인은 회사 설정 저장소의 `pipelines/<이름>.yaml`에 단계 순서, 담당, 승인 게이트,
상한만 적는다. 단계의 동작은 엔진에 구현하고, 봇은 정해진 단계 종류를 순서대로 실행할 뿐
YAML을 해석해 판단하지 않는다.
`pipelines/`가 없으면 ADR-007의 기본 파이프라인 넷(기능 개발, 버그 수정, 출시, 정기 테스트)을 쓴다.

불러올 때 규칙을 검사한다 (하나라도 어기면 그 파이프라인을 쓰지 않는다)
- R1: 리뷰 담당은 같은 파이프라인의 기획·화면 디자인·설계·구현 담당과 다르다
- R2: 구현 앞에 승인 게이트가 최소 1개 있다
- R3: 구현 뒤에는 merge_gate가 있다 (봇은 merge하지 않는다)
- R4: 담당은 org.yaml에 있는 에이전트다. Codex 엔진은 읽기 단계와 구현만 맡는다 (ADR-012)
- R5: triggers는 정해진 값(discord_task, schedule)이다
- R6: 팀장 확인(check)은 구현·진단 단계에만 두고, 담당 팀원의 팀장이 맡는다 (ADR-008)

R1에서 작성 단계의 팀장 확인 담당도 작성 담당으로 본다. 확인하며 고칠 방향을 정했기 때문이다.
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from bot.org import Org
from bot.scheduler import CronError, parse_cron

PIPELINES_DIR = "pipelines"


class PipelineError(ValueError):
    pass


class StageKind(StrEnum):
    PLAN = "plan"
    UI_DESIGN = "ui_design"
    DESIGN = "design"
    IMPLEMENT = "implement"
    TEST = "test"
    REVIEW = "review"
    GATE = "gate"
    MERGE_GATE = "merge_gate"
    DOC_SYNC = "doc_sync"
    DIAGNOSE = "diagnose"
    RELEASE_PREP = "release_prep"
    REPORT = "report"

    @property
    def needs_agent(self) -> bool:
        return self in _AGENT_KINDS

    @property
    def writes(self) -> bool:
        """작업 worktree에 쓰는 단계. Codex 엔진은 구현만 맡을 수 있다 (R4, ADR-012)."""
        return self in _WRITING_KINDS


_AGENT_KINDS = frozenset(
    {
        StageKind.PLAN,
        StageKind.UI_DESIGN,
        StageKind.DESIGN,
        StageKind.IMPLEMENT,
        StageKind.REVIEW,
        StageKind.DOC_SYNC,
        StageKind.DIAGNOSE,
        StageKind.RELEASE_PREP,
    }
)
_WRITING_KINDS = frozenset(
    {StageKind.PLAN, StageKind.UI_DESIGN, StageKind.DESIGN, StageKind.IMPLEMENT}
)
# 리뷰 담당과 달라야 하는 단계 (R1)
_AUTHORING_KINDS = _WRITING_KINDS
# 팀장 확인(check)을 붙일 수 있는 단계: 팀원이 실행하는 일 (ADR-008)
_CHECKABLE = frozenset({StageKind.IMPLEMENT, StageKind.DIAGNOSE})
# 승인 게이트를 둘 수 있는 앞 단계
_APPROVABLE = frozenset(
    {
        StageKind.PLAN,
        StageKind.UI_DESIGN,
        StageKind.DESIGN,
        StageKind.DIAGNOSE,
        StageKind.RELEASE_PREP,
    }
)

# `when`에 쓸 수 있는 값: 앞 단계 출력의 정해진 불리언 필드나 봇이 아는 결과뿐이다
Condition = Literal["has_ui_change", "test_failed"]
Trigger = Literal["discord_task", "schedule"]
Priority = Literal["bugfix", "release", "feature"]
PRIORITY_RANK = {"bugfix": 0, "release": 1, "feature": 2}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StageDecl(_Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,29}$")
    kind: StageKind
    agent: str | None = None
    when: Condition | None = None
    retries: int | None = Field(default=None, ge=0)  # test: 실패 재시도 (없으면 limits)
    rounds: int | None = Field(default=None, ge=1)  # review: 최대 라운드 (없으면 limits)
    scope: Literal["full"] | None = None  # test: 출시 점검은 전체 테스트
    check: str | None = None  # 팀장 확인: 팀원(agent)의 결과를 확인할 팀장 (ADR-008)
    check_rounds: int | None = Field(default=None, ge=1)  # 팀장 확인 최대 횟수 (없으면 limits)

    @model_validator(mode="after")
    def _agent_matches_kind(self) -> "StageDecl":
        if self.kind.needs_agent and not self.agent:
            raise ValueError(f"{self.id}({self.kind}) 단계에 agent가 없다")
        if not self.kind.needs_agent and self.agent:
            raise ValueError(f"{self.id}({self.kind}) 단계는 agent를 두지 않는다 (봇·Zero가 한다)")
        if self.check is not None and self.kind not in _CHECKABLE:
            raise ValueError(f"{self.id}({self.kind}) 단계에는 check를 두지 않는다 (R6)")
        if self.check_rounds is not None and self.check is None:
            raise ValueError(f"{self.id}: check_rounds는 check가 있을 때만 쓴다")
        return self


class Budget(_Model):
    max_calls: int = Field(ge=1)
    max_minutes: int = Field(ge=1)


class Pipeline(_Model):
    key: str = ""  # 파일 이름 (불러올 때 채운다)
    name: str = Field(min_length=1)
    command: str | None = None  # 이 파이프라인을 시작하는 채팅 명령 (예: /task feature)
    triggers: tuple[Trigger, ...] = Field(min_length=1)
    priority: Priority = "feature"
    stages: tuple[StageDecl, ...] = Field(min_length=1)
    budget: Budget
    schedule: str | None = None

    @field_validator("schedule")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                parse_cron(value)
            except CronError as error:
                raise ValueError(f"schedule: {error}") from None
        return value

    @model_validator(mode="after")
    def _rules(self) -> "Pipeline":
        ids = [stage.id for stage in self.stages]
        if len(ids) != len(set(ids)):
            raise ValueError("단계 id가 겹친다")
        kinds = [stage.kind for stage in self.stages]
        # 같은 종류의 단계는 한 번만 둔다. 상태 하나가 단계 하나를 가리키게 하기 위해서다
        repeated = {k for k in kinds if k is not StageKind.GATE and kinds.count(k) > 1}
        if repeated:
            raise ValueError(f"같은 종류의 단계가 둘 이상이다: {', '.join(sorted(repeated))}")
        for i, kind in enumerate(kinds):
            if kind is StageKind.GATE and i > 0 and kinds[i - 1] not in _APPROVABLE:
                raise ValueError(
                    f"{self.stages[i].id}: gate는 기획·화면 디자인·설계·진단·출시 준비 뒤에만 둔다"
                )
        if StageKind.IMPLEMENT in kinds:
            implement_at = kinds.index(StageKind.IMPLEMENT)
            # R2: 구현 앞에 승인 게이트
            if StageKind.GATE not in kinds[:implement_at]:
                raise ValueError("구현 앞에 승인 게이트(gate)가 없다 (R2)")
            # R3: 구현 뒤에 merge_gate
            if StageKind.MERGE_GATE not in kinds[implement_at:]:
                raise ValueError("구현 뒤에 merge_gate가 없다 (R3)")
            if StageKind.TEST not in kinds[implement_at:]:
                raise ValueError("구현 뒤에 test 단계가 없다")
        if kinds[0] is StageKind.GATE:
            raise ValueError("첫 단계가 gate일 수 없다 (승인할 산출물이 없다)")
        if "schedule" in self.triggers and self.schedule is None:
            raise ValueError("triggers에 schedule이 있으면 schedule(cron)이 필요하다 (R5)")
        if "discord_task" in self.triggers and not self.command:
            raise ValueError("triggers에 discord_task가 있으면 command가 필요하다")
        return self

    # 조회

    def scheduled_for(self, *, has_test: bool) -> bool:
        return "schedule" in self.triggers and (has_test or self.first_of(StageKind.TEST) is None)

    def index(self, stage_id: str) -> int:
        for i, stage in enumerate(self.stages):
            if stage.id == stage_id:
                return i
        raise KeyError(stage_id)

    def first_of(self, kind: StageKind) -> StageDecl | None:
        return next((stage for stage in self.stages if stage.kind is kind), None)

    @property
    def implementer(self) -> str | None:
        stage = self.first_of(StageKind.IMPLEMENT)
        return stage.agent if stage else None

    @property
    def priority_rank(self) -> int:
        return PRIORITY_RANK[self.priority]


def check_against_org(pipeline: Pipeline, org: Org) -> None:
    """에이전트 이름과 엔진 규칙 (R1, R4, R6)."""
    for stage in pipeline.stages:
        if stage.agent is None:
            continue
        for name in (stage.agent, stage.check):
            if name is not None and name not in org.agents:
                raise PipelineError(f"{pipeline.key}: {stage.id}의 {name}이 org.yaml에 없다 (R4)")
        if stage.check is not None:
            lead = org.teams[org.agents[stage.agent].team].lead
            if stage.check == stage.agent or stage.check != lead:
                raise PipelineError(
                    f"{pipeline.key}: {stage.id}의 check는 {stage.agent}의 팀장({lead})이어야 "
                    "하고 담당과 달라야 한다 (R6)"
                )
        codex_ok = stage.kind is StageKind.IMPLEMENT  # 구현·수정만 Codex가 쓴다 (ADR-012)
        if stage.kind.writes and not codex_ok and org.engine(stage.agent) == "codex":
            raise PipelineError(
                f"{pipeline.key}: {stage.id}는 쓰기 단계라 "
                f"Codex 엔진({stage.agent})이 맡을 수 없다 (R4)"
            )
    authors = {s.agent for s in pipeline.stages if s.kind in _AUTHORING_KINDS and s.agent}
    authors |= {s.check for s in pipeline.stages if s.kind in _AUTHORING_KINDS and s.check}
    for stage in pipeline.stages:
        if stage.kind is StageKind.REVIEW and stage.agent in authors:
            raise PipelineError(
                f"{pipeline.key}: 리뷰 담당 {stage.agent}이 "
                "같은 파이프라인의 작성 단계를 맡는다 (R1)"
            )


# 기본 파이프라인 (ADR-007, SPEC 3.5의 예시와 같다)

_DEFAULTS: dict[str, dict] = {
    "feature": {
        "name": "기능 개발",
        "command": "/task feature",
        "triggers": ["discord_task"],
        "priority": "feature",
        "stages": [
            {"id": "plan", "kind": "plan", "agent": "beta"},
            {"id": "approve", "kind": "gate"},
            {"id": "ui", "kind": "ui_design", "agent": "gamma", "when": "has_ui_change"},
            {"id": "approve_ui", "kind": "gate", "when": "has_ui_change"},
            {"id": "design", "kind": "design", "agent": "alpha"},
            {"id": "implement", "kind": "implement", "agent": "epsilon", "check": "alpha"},
            {"id": "test", "kind": "test"},
            {"id": "review", "kind": "review", "agent": "delta"},
            {"id": "merge", "kind": "merge_gate"},
            {"id": "docs", "kind": "doc_sync", "agent": "beta"},
        ],
        "budget": {"max_calls": 35, "max_minutes": 300},
    },
    "bugfix": {
        "name": "버그 수정",
        "command": "/task bug",
        "triggers": ["discord_task"],
        "priority": "bugfix",
        "stages": [
            {"id": "diagnose", "kind": "diagnose", "agent": "zeta", "check": "delta"},
            {"id": "approve", "kind": "gate"},
            {"id": "implement", "kind": "implement", "agent": "epsilon", "check": "alpha"},
            {"id": "test", "kind": "test"},
            {"id": "review", "kind": "review", "agent": "delta"},
            {"id": "merge", "kind": "merge_gate"},
        ],
        "budget": {"max_calls": 18, "max_minutes": 150},
    },
    "release": {
        "name": "출시",
        "command": "/release",
        "triggers": ["discord_task"],
        "priority": "release",
        "stages": [
            {"id": "check", "kind": "test", "scope": "full"},
            {"id": "prepare", "kind": "release_prep", "agent": "beta"},
            {"id": "approve", "kind": "gate"},
        ],
        "budget": {"max_calls": 8, "max_minutes": 60},
    },
    "nightly-test": {
        "name": "정기 테스트",
        "triggers": ["schedule"],
        "schedule": "0 3 * * *",
        "priority": "bugfix",
        "stages": [
            {"id": "test", "kind": "test"},
            {
                "id": "diagnose",
                "kind": "diagnose",
                "agent": "zeta",
                "check": "delta",
                "when": "test_failed",
            },
            {"id": "report", "kind": "report"},
        ],
        "budget": {"max_calls": 6, "max_minutes": 45},
    },
}


# ADR-005 기준으로 시작한 작업(v6 이전)을 이어서 진행하는 흐름. 회사 설정에 두지 않는다
LEGACY = "legacy"
_LEGACY = {
    "name": "기능 개발 (ADR-005)",
    "triggers": ["discord_task"],
    "command": "/legacy",
    "stages": [
        {"id": "design", "kind": "design", "agent": "alpha"},
        {"id": "approve", "kind": "gate"},
        {"id": "implement", "kind": "implement", "agent": "alpha"},
        {"id": "test", "kind": "test"},
        {"id": "review", "kind": "review", "agent": "delta"},
        {"id": "merge", "kind": "merge_gate"},
    ],
    "budget": {"max_calls": 25, "max_minutes": 240},
}


def legacy_pipeline(org: Org) -> Pipeline | None:
    """legacy 흐름. 기본 조직의 이름(alpha, delta)이 없으면 쓸 수 없다."""
    pipeline = Pipeline.model_validate({**_LEGACY, "key": LEGACY})
    try:
        check_against_org(pipeline, org)
    except PipelineError:
        return None
    return pipeline


def default_pipelines() -> dict[str, Pipeline]:
    return {key: Pipeline.model_validate({**data, "key": key}) for key, data in _DEFAULTS.items()}


def load_pipelines(company_dir: Path, org: Org) -> dict[str, Pipeline]:
    """`<company>/pipelines/*.yaml`을 읽는다. 폴더가 없으면 기본 파이프라인."""
    folder = company_dir / PIPELINES_DIR
    if not folder.is_dir():
        pipelines = default_pipelines()
    else:
        pipelines = {}
        for path in sorted(folder.glob("*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                raise PipelineError(f"{path.name}의 YAML 형식이 틀렸다") from None
            if not isinstance(raw, dict):
                raise PipelineError(f"{path.name}의 최상위는 매핑이어야 한다")
            try:
                pipelines[path.stem] = Pipeline.model_validate({**raw, "key": path.stem})
            except ValidationError as error:
                lines = [
                    f"- {'.'.join(str(p) for p in item['loc']) or '(최상위)'}: {item['msg']}"
                    for item in error.errors(include_input=False, include_url=False)
                ]
                raise PipelineError(f"{path.name} 값이 틀렸다\n" + "\n".join(lines)) from None
        if not pipelines:
            raise PipelineError(f"{folder}에 파이프라인 파일이 없다")
    for pipeline in pipelines.values():
        check_against_org(pipeline, org)
    commands = [p.command for p in pipelines.values() if p.command]
    if len(commands) != len(set(commands)):
        raise PipelineError("같은 command를 쓰는 파이프라인이 둘 이상이다")
    return pipelines
