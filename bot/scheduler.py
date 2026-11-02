"""일정 계산 (SPEC 6.6, Q15).

cron 형식(`분 시 일 월 요일`) 중 분·시·요일만 쓴다. 일·월은 `*`만 받는다.
시각은 이 맥의 시간대로 본다. 봇이 꺼져 있어 놓친 실행은 켜질 때 한 번만 따라잡는다:
가장 최근 예정 시각이 마지막 실행보다 뒤면 지금 한 번 실행한다.

계산은 순수 함수로 두고 단위 테스트로 고정한다. 실행 루프는 Discord 계층(client)에 있다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

# 봇에 내장된 일정 (ADR-007, 회사 재정의 05-운영 1장)
MORNING_REPORT = "morning_report"
WEEKLY_REPORT = "weekly_report"
BUILTIN_SCHEDULES = {
    MORNING_REPORT: "0 8 * * *",  # 매일 08:00
    WEEKLY_REPORT: "0 8 * * 1",  # 월요일 08:00
}
# 교육 Daily와 주간 보고 (ADR-009). 시각은 설정(education.schedule, weekly)이 정한다
EDUCATION_DAILY = "education_daily"
EDUCATION_WEEKLY = "education_weekly"
# 비서 저녁 하루 정리 (ADR-010). 시각은 설정(secretary.schedule)이 정한다
SECRETARY_EVENING = "secretary_evening"


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    weekdays: frozenset[int]  # 0=일 … 6=토 (cron 규약)

    def matches_day(self, day: datetime) -> bool:
        # Python weekday(): 월=0 … 일=6 → cron: 일=0 … 토=6
        return (day.weekday() + 1) % 7 in self.weekdays


def _field(text: str, low: int, high: int, name: str) -> tuple[int, ...]:
    if text == "*":
        return tuple(range(low, high + 1))
    values = set()
    for part in text.split(","):
        if not part.isdigit():
            raise CronError(f"{name} 값 {part!r}는 숫자, 쉼표 목록, *만 쓴다")
        value = int(part)
        if not low <= value <= high:
            raise CronError(f"{name} 값 {value}는 {low}~{high} 사이여야 한다")
        values.add(value)
    return tuple(sorted(values))


def parse_cron(text: str) -> Cron:
    parts = text.split()
    if len(parts) != 5:
        raise CronError("cron은 `분 시 일 월 요일` 다섯 칸이다")
    minute, hour, day, month, weekday = parts
    if day != "*" or month != "*":
        raise CronError("일·월 칸은 *만 쓴다")
    weekdays = frozenset(_field(weekday.replace("7", "0"), 0, 6, "요일"))
    return Cron(_field(minute, 0, 59, "분"), _field(hour, 0, 23, "시"), weekdays)


def latest_due(cron: Cron, now: datetime) -> datetime | None:
    """now 이하인 가장 최근 예정 시각. now는 시간대가 있는 이 맥의 현지 시각이다."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(8):  # 요일 일정은 최대 7일 전까지 본다
        if cron.matches_day(day):
            for hour in reversed(cron.hours):
                for minute in reversed(cron.minutes):
                    at = day.replace(hour=hour, minute=minute)
                    if at <= now:
                        return at
        day -= timedelta(days=1)
    return None


def is_due(cron: Cron, now: datetime, last_run: datetime | None) -> bool:
    """지금 실행해야 하는지. 놓친 예정은 몇 번이든 한 번만 따라잡는다."""
    latest = latest_due(cron, now)
    if latest is None:
        return False
    return last_run is None or last_run < latest
