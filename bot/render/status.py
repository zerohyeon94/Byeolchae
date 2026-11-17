"""/status, /ping 응답 문구 (SPEC 4)."""

from datetime import datetime

from bot.clock import from_iso
from bot.store.repo import TaskRecord


def _elapsed(since: datetime, now: datetime) -> str:
    minutes = max(0, int((now - since).total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분" if hours else f"{minutes}분"


def render_status(task: TaskRecord, now: datetime) -> str:
    lines = [
        f"작업 {task.id} · {task.title}",
        f"상태: {task.state}",
        f"질문 수: {task.opinion_rounds}",
        f"리뷰 라운드: {task.review_round}",
        f"테스트 재시도: {task.test_retry}",
        f"마지막 변경 후: {_elapsed(from_iso(task.updated_at), now)}",
    ]
    if task.prev_state is not None:
        lines.insert(2, f"직전 상태: {task.prev_state}")
    return "\n".join(lines)


def render_ping(
    *, running_cli: int, active_tasks: int, waiting: int = 0, paused: str = "", held: str = ""
) -> str:
    text = (
        f"살아 있다. 실행 중 CLI {running_cli}개, 대기 {waiting}개, 진행 중 작업 {active_tasks}개."
    )
    if paused:
        text += f" 사용량 한도로 {paused}까지 대기열을 멈췄다."
    if held:
        text += f" 홀딩 중인 주기 작업: {held}."
    return text
