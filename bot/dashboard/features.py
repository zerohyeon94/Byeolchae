"""기능 화면(/features)에 쓰는 데이터: 봇에 들어 있는 기능 목록과 일정.

파이프라인·일정·부서는 봇이 실제로 읽은 설정에서 만든다.
Discord 명령, 안전장치, 대시보드 화면은 코드에 고정된 목록이다. 명령을 더하거나 빼면
여기도 고친다 (`tests/test_dashboard.py`가 client.py의 명령 목록과 맞는지 확인한다).
"""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from bot.pipeline import Pipeline
from bot.scheduler import (
    BUILTIN_SCHEDULES,
    EDUCATION_DAILY,
    EDUCATION_WEEKLY,
    MORNING_REPORT,
    SECRETARY_EVENING,
    WEEKLY_REPORT,
    parse_cron,
)


@dataclass(frozen=True)
class ScheduledJob:
    job: str  # schedule_runs의 키
    name: str
    cron: str


def scheduled_jobs(
    pipelines: Mapping[str, Pipeline],
    projects: Iterable[str],
    *,
    without_test: frozenset[str] = frozenset(),
    education: tuple[str, str] | None = None,  # (매일, 주간) cron
    secretary: str | None = None,  # 저녁 cron
) -> list[ScheduledJob]:
    """봇의 일정 실행기(`run_due_schedules`)가 확인하는 일정과 같은 목록."""
    names = {MORNING_REPORT: "아침 보고", WEEKLY_REPORT: "주간 보고"}
    jobs = [ScheduledJob(job, names[job], cron) for job, cron in BUILTIN_SCHEDULES.items()]
    if secretary is not None:
        jobs.append(ScheduledJob(SECRETARY_EVENING, "비서 하루 정리", secretary))
    if education is not None:
        jobs.append(ScheduledJob(EDUCATION_DAILY, "교육 아침 학습", education[0]))
        jobs.append(ScheduledJob(EDUCATION_WEEKLY, "교육 주간 보고", education[1]))
    projects = list(projects)
    for key, pipeline in pipelines.items():
        if pipeline.schedule is None or "schedule" not in pipeline.triggers:
            continue
        for project in projects:
            if not pipeline.scheduled_for(has_test=project not in without_test):
                continue
            jobs.append(
                ScheduledJob(f"{project}:{key}", f"{pipeline.name} · {project}", pipeline.schedule)
            )
    return jobs


_WEEKDAYS = "일월화수목금토"


def cron_label(text: str) -> str:
    """`0 8 * * 1` → `월요일 08:00`. 요일이 전부면 `매일`."""
    cron = parse_cron(text)
    if len(cron.weekdays) == 7:
        days = "매일"
    else:
        days = ", ".join(f"{_WEEKDAYS[day]}요일" for day in sorted(cron.weekdays))
    times = ", ".join(f"{hour:02d}:{minute:02d}" for hour in cron.hours for minute in cron.minutes)
    return f"{days} {times}"


def bot_pid(lock: Path | None) -> int | None:
    """봇 락 파일에 적힌 PID가 살아 있으면 그 PID. 락은 건드리지 않고 읽기만 한다."""
    if lock is None:
        return None
    try:
        text = lock.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    pid = int(text)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # 다른 사용자 프로세스지만 살아 있다
    return pid


# Discord 슬래시 명령 (bot/discord_io/client.py)
COMMANDS: tuple[tuple[str, str], ...] = (
    ("/task", "채팅의 의견을 포럼 포스트로 옮겨 기능 개발·버그 수정을 시작한다"),
    ("/release", "출시 준비를 시작한다"),
    ("/debate", "직전에 답한 두 팀장이 서로 반박 1회"),
    ("/idea", "바로 하지 않을 아이디어를 기록한다"),
    ("/approve", "산출물을 승인하고 다음 단계로 간다"),
    ("/revise", "산출물을 고쳐 달라고 요청한다"),
    ("/skip", "문서 반영을 건너뛴다"),
    ("/fix", "merge 전에 [권장] 지적을 1회 반영한다"),
    ("/resume", "확인 필요 상태에서 직전 단계로 재개한다"),
    ("/stop", "작업을 멈추고 실행 중인 에이전트를 종료한다"),
    ("/hire", "팀장의 채용 제안에 답한다"),
    ("/status", "이 작업의 상태와 진행 횟수"),
    ("/daily", "오늘 개념 노트와 문제를 지금 낸다"),
    ("/weekly", "이번 주 학습 보고와 주간 문제를 지금 만든다"),
    ("/evening", "가장 최근 하루 정리(Daily)를 지금 만든다"),
    ("/ping", "봇 생존과 실행 중인 CLI 수"),
)

# 규칙으로 정해진 안전장치: (기능, 설명, 코드 위치)
SAFEGUARDS: tuple[tuple[str, str, str], ...] = (
    ("단일 인스턴스", "같은 맥에서 봇은 하나만 뜬다 (flock)", "bot/locks.py"),
    ("실행 대기열", "에이전트는 한 번에 한 명씩 일한다 (ADR-007)", "bot/workflow/gate.py"),
    (
        "하루 한도·작업 예산",
        "에이전트별 하루 실행 수, 파이프라인별 호출·시간 예산",
        "bot/workflow/engine.py",
    ),
    ("구독 사용량 한도", "한도에 걸리면 대기열을 잠시 멈춘다", "bot/workflow/engine.py"),
    (
        "출력 형식 검증",
        "에이전트 JSON을 pydantic으로 검증하고, 틀리면 형식 오류로 기록한다",
        "bot/schemas/",
    ),
    (
        "push 보호",
        "작업 브랜치 형식이고 이 작업에서 봇이 만든 브랜치일 때만 push",
        "bot/github/rules.py",
    ),
    ("테스트 전역 락", "앱 테스트(xcodebuild)는 한 번에 하나만 돈다", "bot/testrun.py"),
    ("Zero 대기 재멘션", "Zero 대기가 길어진 작업을 한 번 더 멘션한다", "bot/workflow/engine.py"),
    ("merge 확인", "AWAIT_MERGE 작업의 PR merge를 주기적으로 확인한다", "bot/discord_io/client.py"),
    ("대시보드 읽기 전용", "DB를 mode=ro로 열고 127.0.0.1에만 연다", "bot/dashboard/"),
)

# 대시보드 화면: (주소, 이름, 보여 주는 것)
PAGES: tuple[tuple[str, str, str], ...] = (
    ("/", "요약", "Zero가 할 일, 실행 중, 오늘 예산, 제품별 작업판, 누적 통계"),
    ("/tasks", "작업", "작업 목록과 작업별 이벤트·실행 기록"),
    ("/team", "직원", "팀별 구성원, 맡은 일, 오늘 실행, 인격 파일"),
    ("/education", "교육", "월별 목표, 개념 대기열, 학습 기록"),
    ("/runs", "실행 기록", "에이전트 실행과 로그"),
    ("/ideas", "아이디어", "/idea로 남긴 아이디어"),
    ("/features", "기능", "이 화면: 기능 목록, 구성도, 흐름도"),
)
