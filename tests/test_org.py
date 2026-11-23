"""회사 조직 설정 로더 테스트 (SPEC 9.1-1, ADR-006, ADR-007)."""

import pytest

from bot.agents.base import Stage
from bot.org import DEFAULT_ORG, OrgError, load_org

ORG_YAML = """\
teams:
  planning: {name: 기획, lead: arcturus}
  qa: {name: QA, lead: mizar}
agents:
  arcturus:
    display: 아르크투루스
    team: planning
    mbti: INFJ
    perspective: 사용 흐름
    basic_question: 써야 하는가
    persona: agents/arcturus.md
    models: {opinion: opus}
    daily_calls: 30
  mizar:
    display: 미자르
    team: qa
    engine: codex
    perspective: 확인 기준
    basic_question: 됐다고 할 수 있나
roles:
  opinions: [arcturus]
  design: arcturus
  implement: arcturus
  review: mizar
  doc_sync: arcturus
"""


def write(tmp_path, text=ORG_YAML, persona="역량: 문제 정의"):
    (tmp_path / "org.yaml").write_text(text, encoding="utf-8")
    if persona is not None:
        (tmp_path / "agents").mkdir(exist_ok=True)
        (tmp_path / "agents" / "arcturus.md").write_text(persona, encoding="utf-8")
    return tmp_path


def test_default_org_is_product_education_and_secretary_departments():
    org = DEFAULT_ORG
    assert [org.display(a) for a in org.agent_names] == [
        "아르크투루스 · 기획팀장",
        "알비레오 · 디자인팀장",
        "리겔 · 개발팀장",
        "미자르 · QA팀장",
        "알니탁 · 개발팀원",
        "샤울라 · QA팀원",
        "투반 · 교육팀장",
        "베텔게우스 · 교육팀원",
        "폴라리스 · 비서실장",
        "알데바란 · 기획팀원",
        "폴룩스 · 기획팀원",
        "시리우스 · 기획팀원",
    ]
    assert org.teams["education"].department == "education"
    assert org.teams["secretary"].department == "secretary"
    # 개념 노트 그림만 Codex가 그린다 (읽기 전용, 그림 파일은 봇이 옮긴다)
    assert [a for a in org.agent_names if org.engine(a) == "codex"] == ["betelgeuse"]
    roles = org.roles
    assert (roles.design, roles.implement, roles.review, roles.doc_sync) == (
        "rigel",
        "rigel",
        "mizar",
        "arcturus",
    )
    assert roles.opinions == ("arcturus", "rigel")


def test_default_org_uses_star_names_and_keeps_former_names():
    # 기본 조직도 별 이름을 쓰고, 예전 그리스 문자 이름 기록은 새 이름으로 보인다 (ADR-012)
    renames = DEFAULT_ORG.renames
    assert renames == {
        "beta": "arcturus",
        "gamma": "albireo",
        "alpha": "rigel",
        "delta": "mizar",
        "epsilon": "alnitak",
        "zeta": "shaula",
        "eta": "thuban",
        "iota": "betelgeuse",
        "theta": "polaris",
        "kappa": "aldebaran",
        "lambda": "pollux",
        "mu": "sirius",
    }
    assert DEFAULT_ORG.display("beta") == "아르크투루스 · 기획팀장"
    assert DEFAULT_ORG.display("theta") == "폴라리스 · 비서실장"
    assert DEFAULT_ORG.engine("iota") == "codex"


def test_missing_org_file_uses_default(tmp_path):
    assert load_org(tmp_path) is DEFAULT_ORG


def test_load_org_reads_personas_models_and_engines(tmp_path):
    org = load_org(write(tmp_path))
    assert org.display("mizar") == "미자르 · QA팀장"
    assert org.engine("mizar") == "codex"
    assert org.model("arcturus", Stage.OPINION) == "opus"
    assert org.model("arcturus", Stage.DESIGN) is None
    assert org.profile("arcturus") == "역량: 문제 정의"
    assert org.profile("mizar") == ""


def test_legacy_agent_names_still_resolve():
    # ADR-005 기준 기록(엔진 이름이 곧 에이전트 이름)을 읽을 수 있어야 한다
    assert DEFAULT_ORG.display("claude") == "Beta · Claude"
    assert DEFAULT_ORG.engine("codex") == "codex"
    assert DEFAULT_ORG.display("zero") == "Zero" and DEFAULT_ORG.display("bot") == "별채"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("review: mizar", "review: arcturus"), "R1"),
        (("team: qa", "team: ops"), "팀 'ops'이 teams에 없다"),
        (("opinions: [arcturus]", "opinions: [shaula]"), "'shaula'이 agents에 없다"),
        (("  mizar:\n    display", "  claude:\n    display"), "예약된 이름"),
        (("  mizar:\n    display", "  gemini:\n    display"), "예약된 이름"),
    ],
)
def test_rule_violations_are_rejected(tmp_path, change, message):
    old, new = change
    with pytest.raises(OrgError, match=message):
        load_org(write(tmp_path, ORG_YAML.replace(old, new)))


def test_codex_engine_may_take_writing_roles(tmp_path):
    # 세 엔진 모두 모든 단계를 맡는다 (ADR-017)
    org = load_org(write(tmp_path, ORG_YAML.replace("doc_sync: arcturus", "doc_sync: mizar")))
    assert org.engine("mizar") == "codex"
    assert org.roles.doc_sync == "mizar"


def test_gemini_engine_is_allowed_for_all_roles(tmp_path):
    text = ORG_YAML.replace("    team: planning\n", "    team: planning\n    engine: gemini\n")
    org = load_org(write(tmp_path, text))
    assert org.engine("arcturus") == "gemini"
    assert org.roles.design == "arcturus"
    assert org.roles.implement == "arcturus"
    assert org.roles.doc_sync == "arcturus"


def test_missing_persona_file_is_an_error(tmp_path):
    with pytest.raises(OrgError, match="인격 파일"):
        load_org(write(tmp_path, persona=None))


def test_persona_path_outside_company_dir_is_rejected(tmp_path):
    company = tmp_path / "company"
    company.mkdir()
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")
    text = ORG_YAML.replace("persona: agents/arcturus.md", "persona: ../secret.md")
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
    assert org.display("alnitak") == "알니탁 · 개발팀원"
    assert org.display("thuban") == "투반 · 교육팀장"
    assert org.display("polaris") == "폴라리스 · 비서실장"
    assert all(org.profile(agent) for agent in org.agent_names)
    assert org.roles == DEFAULT_ORG.roles


def test_former_name_shows_current_agent(tmp_path):
    text = ORG_YAML.replace(
        "    display: 아르크투루스\n", "    display: 아르크투루스\n    former: [oldbeta]\n"
    )
    org = load_org(write(tmp_path, text))
    assert org.display("oldbeta") == org.display("arcturus")
    assert org.has("oldbeta") and org.engine("oldbeta") == "claude"
    assert org.current("mizar") == "mizar"


def test_former_name_must_not_clash(tmp_path):
    text = ORG_YAML.replace(
        "    display: 아르크투루스\n", "    display: 아르크투루스\n    former: [mizar]\n"
    )
    with pytest.raises(OrgError, match="예전 이름"):
        load_org(write(tmp_path, text))


AVATAR = "https://raw.githubusercontent.com/example/repo/develop/assets/avatars/arcturus.png"


def test_avatar_follows_former_name_and_bot(tmp_path):
    text = ORG_YAML.replace(
        "    display: 아르크투루스\n",
        f"    display: 아르크투루스\n    former: [oldbeta]\n    avatar: {AVATAR}\n",
    )
    text = f"bot_avatar: {AVATAR.replace('arcturus', 'bot')}\n" + text
    org = load_org(write(tmp_path, text))
    assert org.avatar("arcturus") == AVATAR
    assert org.avatar("oldbeta") == AVATAR
    assert org.avatar("mizar") == ""  # 정하지 않으면 웹훅 기본 사진
    assert org.avatar("bot").endswith("/bot.png")
    assert org.avatar("zero") == ""


def test_default_org_has_no_avatars():
    assert all(DEFAULT_ORG.avatar(agent) == "" for agent in (*DEFAULT_ORG.agent_names, "bot"))


@pytest.mark.parametrize("field", ["agent", "bot"])
@pytest.mark.parametrize("value", ["http://example.com/a.png", "assets/avatars/arcturus.png"])
def test_avatar_must_be_https(tmp_path, field, value):
    if field == "agent":
        text = ORG_YAML.replace(
            "    display: 아르크투루스\n", f"    display: 아르크투루스\n    avatar: {value}\n"
        )
    else:
        text = f"bot_avatar: {value}\n" + ORG_YAML
    with pytest.raises(OrgError, match="avatar"):
        load_org(write(tmp_path, text))
