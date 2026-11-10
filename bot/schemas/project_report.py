"""기획팀 프로젝트 담당자가 팀장에게 전달하는 진행 보고."""

from pydantic import BaseModel, ConfigDict, Field

from bot.schemas._types import Item


class ProjectReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    done: list[Item] = Field(max_length=4)
    blockers: list[Item] = Field(max_length=4)
    next_step: Item

    def text(self) -> str:
        return "\n".join(
            (
                "한 일: " + (" / ".join(self.done) or "없음"),
                "막힌 점: " + (" / ".join(self.blockers) or "없음"),
                "다음 행동: " + self.next_step,
            )
        )
