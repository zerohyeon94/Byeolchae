"""기획·화면 디자인·진단·출시 준비·문서 반영 스키마 (SPEC 5.4, ADR-007).

기획 요약과 진단 보고는 변경 유형·대상 기능·slug를 정한다(ChangeSummary). 브랜치·커밋·PR 제목과
결정 노트가 이 값에서 나온다. 기획이 없는 버그 수정은 진단 보고가 이 값을 갖는다.
"""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    model_validator,
)

from bot.schemas._types import Item
from bot.schemas.design import ChangeSummary, ChangeType, FeatureName, Scope, Slug, Title
from bot.schemas.hiring import HiringProposal

Text300 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text500 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Text1000 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
PlanDoc = Annotated[
    str, StringConstraints(pattern=r"^docs/기획/\d+-[a-z0-9]+(-[a-z0-9]+){0,4}\.md$")
]
UiDoc = Annotated[
    str, StringConstraints(pattern=r"^docs/디자인/\d+-[a-z0-9]+(-[a-z0-9]+){0,4}\.md$")
]
Url = Annotated[str, StringConstraints(max_length=500, pattern=r"^https://\S+$")]


class PlanSummary(ChangeSummary):
    """기획 요약 (기획팀). 기획 승인으로 변경 유형·대상 기능이 함께 승인된다."""

    problem: Text300  # 누구의 어떤 문제인가
    summary: Text500
    in_scope: list[Item] = Field(min_length=1, max_length=6)
    out_of_scope: list[Item] = Field(min_length=1, max_length=6)
    acceptance: list[Item] = Field(min_length=1, max_length=6)  # 완료 기준. 리뷰 담당이 확인
    edge_cases: list[Item] = Field(default_factory=list, max_length=5)
    plan_doc: PlanDoc
    # 화면이 바뀌는가. 거짓이면 화면 디자인 단계를 건너뛴다 (파이프라인 when)
    has_ui_change: bool
    hiring_proposal: HiringProposal | None = None

    @model_validator(mode="after")
    def _plan_doc_matches(self, info: ValidationInfo) -> Self:
        self._check_doc(self.plan_doc, "기획", "plan_doc", info)
        return self


class ScreenSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    states: list[Item] = Field(min_length=1, max_length=6)  # 빈 화면, 로딩, 오류, 큰 글씨 등


class UiDesignSummary(BaseModel):
    """화면 디자인 요약 (디자인팀). 명세(design_doc)가 원본이고 디자인 링크는 명세에도 적는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: Text500
    screens: list[ScreenSpec] = Field(min_length=1, max_length=8)
    components: list[Item] = Field(default_factory=list, max_length=10)
    design_system_changes: list[Item] = Field(default_factory=list, max_length=3)
    out_of_plan: list[Item] = Field(default_factory=list, max_length=3)  # 기획 밖 제안
    design_url: Url | None = None
    design_doc: UiDoc
    hiring_proposal: HiringProposal | None = None


class CauseClass(StrEnum):
    CODE = "code"  # 코드 결함
    TEST = "test"  # 테스트 결함
    ENVIRONMENT = "environment"  # 실행 환경 문제
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            CauseClass.CODE: "코드 결함",
            CauseClass.TEST: "테스트 결함",
            CauseClass.ENVIRONMENT: "환경 문제",
            CauseClass.UNKNOWN: "원인 불명",
        }[self]


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def label(self) -> str:
        return {"high": "높음", "medium": "보통", "low": "낮음"}[self.value]


class ReportAction(StrEnum):
    """정기 테스트 실패를 Zero에게 보고할지 (2026-11-05 Zero 결정)."""

    REPORT = "report"  # 개선이 필요하다
    NONE = "none"  # 개선할 것이 없다

    @property
    def label(self) -> str:
        return {"report": "개선 필요", "none": "개선 불필요"}[self.value]


class DiagnosisReport(BaseModel):
    """진단 보고 (QA팀, 읽기 전용). 버그 수정에서는 수정 방향 승인으로 변경 필드가 함께 승인된다.

    정기 테스트 실패 보고에서는 변경 필드를 비워도 된다(null). 대신 개선 필요 여부(action)를 정한다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: Text500
    reproduction: list[Item] = Field(min_length=1, max_length=6)
    cause: CauseClass
    evidence: list[Item] = Field(min_length=1, max_length=5)  # 파일:줄, 로그 위치 등
    fix_direction: Text500
    confidence: Confidence
    change_type: ChangeType | None = None
    target_feature: FeatureName | None = None
    slug: Slug | None = None
    title: Title | None = None
    scope: Scope | None = None
    hiring_proposal: HiringProposal | None = None
    action: ReportAction | None = None  # 정기 테스트에서만 쓴다

    def change(self, info_context: dict | None = None) -> ChangeSummary | None:
        """변경 필드가 모두 있으면 ChangeSummary로. 하나라도 없으면 None."""
        if None in (self.change_type, self.target_feature, self.slug, self.title):
            return None
        return ChangeSummary.model_validate(
            {
                "change_type": self.change_type,
                "target_feature": self.target_feature,
                "slug": self.slug,
                "title": self.title,
                "scope": self.scope,
            },
            context=info_context,
        )


class CheckState(StrEnum):
    OK = "ok"
    TODO = "todo"
    BLOCKED = "blocked"

    @property
    def label(self) -> str:
        return {"ok": "완료", "todo": "할 일", "blocked": "막힘"}[self.value]


class CheckItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item: Item
    state: CheckState


class CertificateExpiry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Item  # 인증서 또는 프로비저닝 프로필
    expires: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$|^unknown$")]


class ReleasePrep(BaseModel):
    """출시 준비물 (배포 겸임 Beta). 외부로 나가는 글이므로 출시 승인으로 함께 승인된다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Annotated[str, StringConstraints(pattern=r"^\d+(\.\d+){1,3}$")]
    checklist: list[CheckItem] = Field(min_length=1, max_length=15)
    release_notes: Text1000
    store_description: Annotated[str, StringConstraints(max_length=4000)] | None = None
    certificate_expiry: list[CertificateExpiry] = Field(default_factory=list, max_length=5)
    blockers: list[Item] = Field(default_factory=list, max_length=5)
    hiring_proposal: HiringProposal | None = None


class DocFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    action: Annotated[str, StringConstraints(pattern=r"^(create|update)$")]
    content: Annotated[str, StringConstraints(min_length=1, max_length=20000)]


class DocSyncProposal(BaseModel):
    """merge 뒤 문서 반영안 (운영 규약 3.9-3). 봇이 허용 경로만 쓴다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: Text300
    files: list[DocFile] = Field(min_length=1, max_length=5)
    hiring_proposal: HiringProposal | None = None
