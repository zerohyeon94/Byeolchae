"""에이전트 출력 스키마.

에이전트는 이 스키마의 JSON으로 출력하고, 봇은 검증한 원본을 저장·표시·옮겨 적기에 그대로 쓴다.
"""

from bot.schemas.design import (
    FEATURE_NAMES,
    ISSUE_NUMBER,
    ChangeSummary,
    ChangeType,
    DesignSummary,
)
from bot.schemas.education import (
    LETTERS,
    ConceptFigure,
    ConceptImage,
    ConceptNote,
    QuizQuestion,
    WeeklyReview,
)
from bot.schemas.extract import AgentOutputError, extract_json, parse_agent_output
from bot.schemas.hiring import HiringKind, HiringProposal
from bot.schemas.implementation import FindingResponse, ImplementationReport, ResponseAction
from bot.schemas.opinion import Opinion, Stance
from bot.schemas.review import Finding, Review, Severity, Verdict
from bot.schemas.secretary import DailySummary
from bot.schemas.stages import (
    CauseClass,
    CheckState,
    DiagnosisReport,
    DocSyncProposal,
    PlanSummary,
    ReleasePrep,
    UiDesignSummary,
)

__all__ = [
    "FEATURE_NAMES",
    "ISSUE_NUMBER",
    "AgentOutputError",
    "CauseClass",
    "ChangeSummary",
    "CheckState",
    "DiagnosisReport",
    "DocSyncProposal",
    "ChangeType",
    "ConceptFigure",
    "ConceptImage",
    "ConceptNote",
    "DailySummary",
    "LETTERS",
    "QuizQuestion",
    "WeeklyReview",
    "DesignSummary",
    "Finding",
    "HiringKind",
    "HiringProposal",
    "FindingResponse",
    "ImplementationReport",
    "Opinion",
    "PlanSummary",
    "ReleasePrep",
    "ResponseAction",
    "Review",
    "Severity",
    "Stance",
    "UiDesignSummary",
    "Verdict",
    "extract_json",
    "parse_agent_output",
]
