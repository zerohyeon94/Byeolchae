"""회사 조직 설정 로더 테스트 (SPEC 9.1-1, ADR-006, ADR-007)."""

import pytest

from bot.agents.base import Stage
from bot.org import DEFAULT_ORG, OrgError, load_org

ORG_YAML = """\
teams:
  planning: {name: 기획, lead: beta}
  qa: {name: QA, lead: delta}
agents:
  beta:
    display: Beta
    team: planning
    mbti: INFJ
    perspective: 사용 흐름
    basic_question: 써야 하는가
    persona: agents/beta.md
    models: {opinion: opus}
    daily_calls: 30
  delta:
    display: Delta
    team: qa
    engine: codex
    perspective: 확인 기준
    basic_question: 됐다고 할 수 있나
roles:
  opinions: [beta]
  design: beta
  implement: beta
  review: delta
  doc_sync: beta
"""


def write(tmp_path, text=ORG_YAML, persona="역량: 문제 정의"):
    (tmp_path / "org.yaml").write_text(text, encoding="utf-8")
    if persona is not None:
        (tmp_path / "agents").mkdir(exist_ok=True)
        (tmp_path / "agents" / "beta.md").write_text(persona, encoding="utf-8")
    return tmp_path


def test_default_org_is_product_education_and_secretary_departments():
    org = DEFAULT_ORG
    assert [org.display(a) for a in org.agent_names] == [
        "Beta · 기획팀장",
        "Gamma · 디자인팀장",
        "Alpha · 개발팀장",
        "Delta · QA팀장",
        "Epsilon · 개발팀원",
        "Zeta · QA팀원",
        "Eta · 교육팀장",
        "Iota · 교육팀원",
        "Theta · 비서팀장",
    ]
    assert org.teams["education"].department == "education"
    assert org.teams["secretary"].department == "secretary"
    # 개념 노트 그림만 Codex가 그린다 (읽기 전용, 그림 파일은 봇이 옮긴다)
    assert [a for a in org.agent_names if org.engine(a) == "codex"] == ["iota"]
    roles = org.roles
    assert (roles.design, roles.implement, roles.review, roles.doc_sync) == (
        "alpha",
        "alpha",
        "delta",
        "beta",
    )
    assert roles.opinions == ("beta", "alpha")


def test_missing_org_file_uses_default(tmp_path):
    assert load_org(tmp_path) is DEFAULT_ORG


def test_load_org_reads_personas_models_and_engines(tmp_path):
    org = load_org(write(tmp_path))
    assert org.display("delta") == "Delta · QA팀장"
    assert org.engine("delta") == "codex"
    assert org.model("beta", Stage.OPINION) == "opus"
    assert org.model("beta", Stage.DESIGN) is None
    assert org.profile("beta") == "역량: 문제 정의"
    assert org.profile("delta") == ""


def test_legacy_agent_names_still_resolve():
    # ADR-005 기준 기록(엔진 이름이 곧 에이전트 이름)을 읽을 수 있어야 한다
    assert DEFAULT_ORG.display("claude") == "Beta · Claude"
    assert DEFAULT_ORG.engine("codex") == "codex"
    assert DEFAULT_ORG.display("zero") == "Zero" and DEFAULT_ORG.display("bot") == "ZeroSync"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("review: delta", "review: beta"), "R1"),
        (("doc_sync: beta", "doc_sync: delta"), "Codex 엔진이라 쓰기 단계"),
        (("team: qa", "team: ops"), "팀 'ops'이 teams에 없다"),
        (("opinions: [beta]", "opinions: [zeta]"), "'zeta'이 agents에 없다"),
        (("  delta:\n    display", "  claude:\n    display"), "예약된 이름"),
    ],
)
def test_rule_violations_are_rejected(tmp_path, change, message):
    old, new = change
    with pytest.raises(OrgError, match=message):
        load_org(write(tmp_path, ORG_YAML.replace(old, new)))


def test_missing_persona_file_is_an_error(tmp_path):
    with pytest.raises(OrgError, match="인격 파일"):
        load_org(write(tmp_path, persona=None))


def test_persona_path_outside_company_dir_is_rejected(tmp_path):
    company = tmp_path / "company"
    company.mkdir()
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")
    text = ORG_YAML.replace("persona: agents/beta.md", "persona: ../secret.md")
    with pytest.raises(OrgError, match="폴더 밖"):
        load_org(write(company, text))


def test_broken_yaml_message_has_no_values(tmp_path):
    with pytest.raises(OrgError, match="YAML 형식"):
        load_org(write(tmp_path, "teams: [\n"))


def test_example_company_config_is_valid():
    # 공개 저장소의 예시(examples/company)가 로더 규칙을 지키는지 고정한다
    from pathlib import Path

    org = load_org(Path(__file__).resolve().parent.parent / "examples" / "company")
    assert org.agent_names == DEFAULT_ORG.agent_names
    assert org.display("epsilon") == "Epsilon · 개발팀원"
    assert org.display("eta") == "Eta · 교육팀장"
    assert org.display("theta") == "Theta · 비서팀장"
    assert all(org.profile(agent) for agent in org.agent_names)
    assert org.roles == DEFAULT_ORG.roles
