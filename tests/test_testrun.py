import asyncio
import sys
from types import SimpleNamespace

import pytest

from bot.agents import ProcessRunner, RunStatus
from bot.render.work import render_nightly_outcome, render_test_report
from bot.testrun import CheckKind, TestReport, TestRunner, check_kind, summarize

XCTEST_PASS = """\
Test Suite 'All tests' started at 2026-10-11 10:00:00.000.
Test Case '-[GagaeSsiTests.BudgetTests testUnder]' passed (0.001 seconds).
Executed 3 tests, with 0 failures (0 unexpected) in 0.010 (0.012) seconds
Executed 12 tests, with 0 failures (0 unexpected) in 0.050 (0.060) seconds
** TEST SUCCEEDED **
"""

XCTEST_FAIL = """\
Test Case '-[GagaeSsiTests.BudgetTests testOver]' failed (0.002 seconds).
/x/BudgetTests.swift:20: error: -[GagaeSsiTests.BudgetTests testOver] : XCTAssertEqual failed
Test Case '-[GagaeSsiTests.TripTests testEnd]' failed (0.001 seconds).
Executed 12 tests, with 2 failures (0 unexpected) in 0.050 (0.060) seconds
** TEST FAILED **
"""

SWIFT_TESTING_FAIL = """\
✘ Test budgetOver() recorded an issue at BudgetTests.swift:10:5: Expectation failed
✘ Test budgetOver() failed after 0.001 seconds with 1 issue.
✘ Test run with 5 tests in 2 suites failed after 0.010 seconds with 1 issue.
"""

BUILD_FAIL = """\
/x/HomeView.swift:42:13: error: cannot find 'bannerView' in scope
** TEST FAILED **
"""


def test_summarize_xctest():
    assert summarize(XCTEST_PASS) == (12, 0, [], [])
    total, failures, failed, errors = summarize(XCTEST_FAIL)
    assert (total, failures) == (12, 2)
    assert failed == ["GagaeSsiTests.BudgetTests testOver", "GagaeSsiTests.TripTests testEnd"]
    assert len(errors) == 1


def test_summarize_swift_testing_and_build_errors():
    total, failures, failed, _ = summarize(SWIFT_TESTING_FAIL)
    assert (total, failures) == (5, 1)
    assert failed == ["budgetOver()"]
    total, failures, failed, errors = summarize(BUILD_FAIL)
    assert (total, failures, failed) == (None, None, [])
    assert errors == ["/x/HomeView.swift:42:13: error: cannot find 'bannerView' in scope"]


CLONE = "on 'Clone 1 of iPhone 17 - GagaeSsi (45140)'"
PARALLEL_FAIL = f"""\
Test suite 'PastOverspendConversionTests' started {CLONE}
Test case 'PastOverspendConversionTests.test_전환()' passed {CLONE} (0.065 seconds)
Test case 'PastOverspendConversionTests.test_소급()' failed {CLONE} (0.504 seconds)
Test case 'GagaeSsiTests/example()' passed {CLONE} (0.000 seconds)

Failing tests:
\tPastOverspendConversionTests.test_소급()

** TEST FAILED **
"""


def test_app_runtime_logs_are_not_errors():
    output = (
        "2026-10-11 22:14:48.68 GagaeSsi[1:2] [error] CoreData: error: +[BudgetConfig entity] x\n"
        "CoreData: error: +[DailyBudget entity] Failed\n"
        "/x/PastTests.swift:148: error: -[GagaeSsiTests.PastTests test_a] : XCTAssertEqual failed\n"
        "xcodebuild: error: Failed to build project GagaeSsi\n"
    )
    _, _, _, errors = summarize(output)
    assert errors == [
        "/x/PastTests.swift:148: error: -[GagaeSsiTests.PastTests test_a] : XCTAssertEqual failed",
        "xcodebuild: error: Failed to build project GagaeSsi",
    ]


def test_summarize_parallel_clone_output():
    """Xcode 27 병렬 테스트 출력에는 'Executed N tests' 합계가 없다."""
    total, failures, failed, _ = summarize(PARALLEL_FAIL)
    assert (total, failures) == (3, 1)
    assert failed == ["PastOverspendConversionTests.test_소급()"]


def run_tests(tmp_path, script, *, concurrent=1):
    runner = TestRunner(ProcessRunner(tmp_path / "runs"), concurrent=concurrent, timeout_sec=10)
    argv = [sys.executable, "-c", script]
    return runner, argv


def test_runner_reports_pass_and_fail(tmp_path):
    runner, ok = run_tests(tmp_path, f"print({XCTEST_PASS!r})")
    report = asyncio.run(runner.run("t1", ok, cwd=tmp_path))
    assert report.passed and report.total == 12 and report.failures == 0
    assert report.to_payload()["passed"] is True
    assert (tmp_path / "runs" / "t1.log").exists()

    _, failing = run_tests(tmp_path, f"print({XCTEST_FAIL!r}); raise SystemExit(65)")
    report = asyncio.run(runner.run("t2", failing, cwd=tmp_path))
    assert not report.passed
    assert report.status is RunStatus.ERROR and report.exit_code == 65
    assert report.failures == 2

    _, crash = run_tests(tmp_path, "raise SystemExit(70)")
    report = asyncio.run(runner.run("t3", crash, cwd=tmp_path))
    assert not report.passed
    assert "종료 코드 70" in report.errors[0]


@pytest.mark.parametrize(
    ("args", "kind"),
    [
        (["xcodebuild", "build", "-scheme", "test"], CheckKind.BUILD),
        (["xcodebuild", "-scheme", "build", "test"], CheckKind.TEST),
        (["/usr/bin/xcodebuild", "-project", "test", "build"], CheckKind.BUILD),
        (["xcrun", "--sdk", "macosx", "xcodebuild", "build"], CheckKind.BUILD),
        (["xcodebuild", "build-for-testing"], CheckKind.BUILD),
        (["xcodebuild", "test-without-building"], CheckKind.TEST),
        (["xcodebuild", "clean", "build", "test"], CheckKind.TEST),
        (["python", "build"], CheckKind.UNKNOWN),
    ],
)
def test_check_kind_uses_actions_not_option_values(args, kind):
    assert check_kind(args) is kind


def test_runner_preserves_build_kind_even_before_project_loading(tmp_path):
    class Process:
        async def run(self, *args, **kwargs):
            return SimpleNamespace(
                status=RunStatus.ERROR,
                exit_code=74,
                stdout="",
                stderr="xcodebuild: error: Unable to read project 'Jday.xcodeproj'.",
                duration_sec=1.0,
                log_path=tmp_path / "build.log",
            )

    runner = TestRunner(Process())
    report = asyncio.run(
        runner.run("build", ["xcodebuild", "build", "-scheme", "Jday_macOS"], cwd=tmp_path)
    )
    assert report.kind is CheckKind.BUILD and report.total is None
    assert render_test_report(report).startswith("**빌드 실패**")
    assert TestReport.from_payload(report.to_payload()) == report


def test_runner_recognizes_build_output_from_wrapper_without_test_counts(tmp_path):
    runner, argv = run_tests(tmp_path, "print('** BUILD SUCCEEDED **')")
    report = asyncio.run(runner.run("build-wrapper", argv, cwd=tmp_path))
    assert report.passed and report.kind is CheckKind.BUILD and report.total is None
    assert report.result_label == "빌드 성공"


@pytest.mark.parametrize(
    ("kind", "total", "expected"),
    [
        (CheckKind.BUILD, None, "빌드 성공"),
        (CheckKind.TEST, 12, "테스트 통과"),
        (CheckKind.TEST, 0, "테스트 명령 성공 — 실행된 테스트 없음"),
        (CheckKind.TEST, None, "테스트 명령 성공 — 테스트 실행 수 미확인"),
        (CheckKind.UNKNOWN, None, "검증 명령 성공"),
        (CheckKind.UNKNOWN, 12, "테스트 통과"),
    ],
)
def test_render_does_not_claim_tests_passed_without_executed_tests(kind, total, expected):
    report = TestReport(RunStatus.OK, 0, total, 0, kind=kind)
    text = render_test_report(report)
    assert text.startswith(f"**{expected}**")
    if kind is CheckKind.BUILD:
        assert "테스트는 실행하지 않았다" in text
    if not total:
        assert "테스트 통과" not in text
    payload = report.to_payload()
    assert payload["kind"] == kind.value and payload["result_label"] == expected
    assert TestReport.from_payload(payload) == report


def test_legacy_result_without_test_counts_is_not_rendered_as_test_pass():
    report = TestReport.from_payload(
        {"status": "ok", "exit_code": 0, "total": None, "failures": 0, "passed": True}
    )
    assert render_test_report(report).startswith("**검증 명령 성공**")


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("passed", "빌드 성공"),
        ("flaky", "일시적 실패(다시 돌려 빌드 성공)"),
        ("appended", "같은 문제 계속 (빌드 실패)"),
        ("quiet", "실패, 개선 불필요 (빌드 실패)"),
    ],
)
def test_nightly_display_distinguishes_build_and_report_outcome(outcome, expected):
    verification = "빌드 성공" if outcome in ("passed", "flaky") else "빌드 실패"
    assert render_nightly_outcome({"outcome": outcome, "verification": verification}) == expected


DESTINATION_FAIL = """\
xcodebuild: error: Unable to find a device matching the provided destination specifier:
		{ platform:iOS Simulator, OS:27.0, name:iPhone 17 }
"""


def test_environment_error_only_when_nothing_ran(tmp_path):
    runner, cmd = run_tests(tmp_path, f"print({DESTINATION_FAIL!r}); raise SystemExit(70)")
    report = asyncio.run(runner.run("e1", cmd, cwd=tmp_path))
    assert report.environment_error
    assert report.errors[0].startswith("xcodebuild: error: Unable to find a device")
    assert report.to_payload()["environment_error"] is True

    # 코드 위치가 붙은 빌드 오류와 실패한 테스트는 에이전트가 고칠 문제다
    _, build = run_tests(tmp_path, f"print({BUILD_FAIL!r}); raise SystemExit(65)")
    assert not asyncio.run(runner.run("e2", build, cwd=tmp_path)).environment_error
    _, failing = run_tests(tmp_path, f"print({XCTEST_FAIL!r}); raise SystemExit(65)")
    assert not asyncio.run(runner.run("e3", failing, cwd=tmp_path)).environment_error
    _, ok = run_tests(tmp_path, f"print({XCTEST_PASS!r})")
    assert not asyncio.run(runner.run("e4", ok, cwd=tmp_path)).environment_error


def test_runner_holds_global_lock(tmp_path):
    """동시 실행 수 1이면 두 테스트가 겹치지 않는다."""
    marker = tmp_path / "running"
    script = (
        "import pathlib, sys, time\n"
        f"m = pathlib.Path({str(marker)!r})\n"
        "if m.exists(): sys.exit(9)\n"
        "m.write_text('x'); time.sleep(0.3); m.unlink()\n"
        "print('Executed 1 test, with 0 failures')\n"
    )
    runner, argv = run_tests(tmp_path, script)

    async def both():
        return await asyncio.gather(
            runner.run("a", argv, cwd=tmp_path), runner.run("b", argv, cwd=tmp_path)
        )

    first, second = asyncio.run(both())
    assert first.passed and second.passed
