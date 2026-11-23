"""파이프라인 선언 테스트 (SPEC 3.5, R1~R5)."""

from pathlib import Path

import pytest

from bot.org import DEFAULT_ORG
from bot.pipeline import (
    PipelineError,
    StageKind,
    default_pipelines,
    load_pipelines,
)

FEATURE = """\
name: 기능 개발
command: /task feature
triggers: [discord_task]
stages:
  - {id: plan, kind: plan, agent: arcturus}
  - {id: approve, kind: gate}
  - {id: design, kind: design, agent: rigel}
  - {id: implement, kind: implement, agent: rigel}
  - {id: test, kind: test, retries: 2}
  - {id: review, kind: review, agent: mizar, rounds: 3}
  - {id: merge, kind: merge_gate}
budget: {max_calls: 25, max_minutes: 240}
"""


def write(tmp_path, text=FEATURE, name="feature.yaml"):
    folder = tmp_path / "pipelines"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_defaults_follow_adr_007():
    pipelines = default_pipelines()
    assert list(pipelines) == ["feature", "bugfix", "release", "nightly-test"]
    feature = pipelines["feature"]
    assert [s.kind for s in feature.stages][:4] == [
        StageKind.PLAN,
        StageKind.GATE,
        StageKind.UI_DESIGN,
        StageKind.GATE,
    ]
    assert feature.first_of(StageKind.REVIEW).agent == "mizar"
    # 팀원이 실행하고 팀장이 확인한다 (ADR-008)
    implement = feature.first_of(StageKind.IMPLEMENT)
    assert (implement.agent, implement.check) == ("alnitak", "rigel")
    diagnose = pipelines["bugfix"].first_of(StageKind.DIAGNOSE)
    assert (diagnose.agent, diagnose.check) == ("shaula", "mizar")
    assert pipelines["bugfix"].implementer == "alnitak"
    nightly = pipelines["nightly-test"].first_of(StageKind.DIAGNOSE)
    assert (nightly.agent, nightly.check) == ("shaula", "mizar")
    assert pipelines["bugfix"].priority_rank < pipelines["release"].priority_rank
    assert pipelines["nightly-test"].schedule == "0 3 * * *"


def test_missing_folder_uses_defaults_and_they_pass_org_rules(tmp_path):
    assert set(load_pipelines(tmp_path, DEFAULT_ORG)) == set(default_pipelines())


def test_load_from_company_folder(tmp_path):
    pipelines = load_pipelines(write(tmp_path), DEFAULT_ORG)
    assert list(pipelines) == ["feature"]
    assert pipelines["feature"].key == "feature"
    assert pipelines["feature"].first_of(StageKind.TEST).retries == 2


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("  - {id: approve, kind: gate}\n", "", "R2"),
        ("  - {id: merge, kind: merge_gate}\n", "", "R3"),
        ("agent: mizar, rounds: 3", "agent: rigel, rounds: 3", "R1"),
        ("agent: mizar, rounds: 3", "agent: arcturus, rounds: 3", "R1"),
        ("{id: design, kind: design, agent: rigel}", "{id: design, kind: design, agent: omega}", "R4"),  # noqa: E501
        ("{id: test, kind: test, retries: 2}", "{id: test, kind: test, agent: rigel}", "agent를 두지"),  # noqa: E501
        ("{id: plan, kind: plan, agent: arcturus}", "{id: plan, kind: plan}", "agent가 없다"),
        ("triggers: [discord_task]", "triggers: [schedule]", "schedule"),
        ("triggers: [discord_task]", "triggers: [slack]", "triggers"),
        ("  - {id: approve, kind: gate}\n", "  - {id: approve, kind: gate}\n  - {id: again, kind: gate}\n", "gate는"),  # noqa: E501
        ("id: review,", "id: test,", "겹친다"),
        # 팀장 확인 (R6, ADR-008)
        ("agent: rigel}\n  - {id: test", "agent: alnitak, check: mizar}\n  - {id: test", "R6"),
        ("agent: rigel}\n  - {id: test", "agent: rigel, check: rigel}\n  - {id: test", "R6"),
        ("agent: rigel}\n  - {id: test", "agent: alnitak, check: omega}\n  - {id: test", "R4"),
        ("{id: plan, kind: plan, agent: arcturus}", "{id: plan, kind: plan, agent: arcturus, check: rigel}", "R6"),  # noqa: E501
        ("agent: rigel}\n  - {id: test", "agent: alnitak, check_rounds: 2}\n  - {id: test", "check_rounds"),  # noqa: E501
        # 구현을 확인한 팀장은 작성 담당이므로 리뷰를 맡지 못한다
        ("agent: rigel}\n  - {id: test", "agent: shaula, check: mizar}\n  - {id: test", "R1"),
    ],
)  # fmt: skip
def test_rule_violations(tmp_path, old, new, message):
    assert old in FEATURE
    with pytest.raises(PipelineError, match=message):
        load_pipelines(write(tmp_path, FEATURE.replace(old, new)), DEFAULT_ORG)


def test_codex_agent_may_take_writing_stage(tmp_path):
    # 세 엔진 모두 모든 단계를 맡는다 (ADR-017)
    codex_alpha = DEFAULT_ORG.agents["rigel"].model_copy(update={"engine": "codex"})
    org = DEFAULT_ORG.model_copy(update={"agents": {**DEFAULT_ORG.agents, "rigel": codex_alpha}})
    assert load_pipelines(write(tmp_path), org)


def test_same_kind_twice_is_rejected(tmp_path):
    text = FEATURE.replace(
        "  - {id: merge, kind: merge_gate}\n",
        "  - {id: merge, kind: merge_gate}\n  - {id: test2, kind: test}\n",
    )
    with pytest.raises(PipelineError, match="같은 종류"):
        load_pipelines(write(tmp_path, text), DEFAULT_ORG)


def test_legacy_pipeline_needs_default_names():
    from orgs import LEGACY_ORG

    from bot.pipeline import legacy_pipeline

    assert legacy_pipeline(DEFAULT_ORG).first_of(StageKind.REVIEW).agent == "mizar"
    assert legacy_pipeline(LEGACY_ORG) is None


def test_bad_schedule_and_duplicate_commands(tmp_path):
    nightly = "name: n\ntriggers: [schedule]\nschedule: '*/5 * * * *'\nstages:\n  - {id: t, kind: test}\nbudget: {max_calls: 1, max_minutes: 1}\n"  # noqa: E501
    with pytest.raises(PipelineError, match="schedule"):
        load_pipelines(write(tmp_path, nightly, "nightly.yaml"), DEFAULT_ORG)
    (tmp_path / "pipelines" / "nightly.yaml").unlink()
    write(tmp_path, FEATURE, "copy.yaml")
    write(tmp_path)
    with pytest.raises(PipelineError, match="command"):
        load_pipelines(tmp_path, DEFAULT_ORG)


def test_example_company_pipelines_match_defaults():
    """examples/company/pipelines는 기본 선언을 그대로 옮긴 것이다. 한쪽만 바꾸지 않는다."""
    example = Path(__file__).resolve().parent.parent / "examples" / "company"
    assert load_pipelines(example, DEFAULT_ORG) == default_pipelines()


def test_codex_agent_may_implement(tmp_path):
    codex_epsilon = DEFAULT_ORG.agents["alnitak"].model_copy(update={"engine": "codex"})
    org = DEFAULT_ORG.model_copy(
        update={"agents": {**DEFAULT_ORG.agents, "alnitak": codex_epsilon}}
    )
    assert load_pipelines(write(tmp_path), org)


def test_gemini_agent_may_take_all_stages_subject_to_r1(tmp_path):
    gemini_agents = {
        name: spec.model_copy(update={"engine": "gemini"})
        for name, spec in DEFAULT_ORG.agents.items()
    }
    org = DEFAULT_ORG.model_copy(update={"agents": gemini_agents})
    assert load_pipelines(write(tmp_path), org)
    # R1(작성자와 리뷰어 분리)은 Gemini 엔진에도 그대로 적용된다
    bad = FEATURE.replace("agent: mizar, rounds: 3", "agent: rigel, rounds: 3")
    with pytest.raises(PipelineError, match="R1"):
        load_pipelines(write(tmp_path, bad), org)
