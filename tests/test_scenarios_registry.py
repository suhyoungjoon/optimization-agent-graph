"""시나리오 세트와 모델 레지스트리 (결정적, 테스트 먼저)."""

import shutil
from pathlib import Path

import pytest
import yaml

from engines import get_engine
from modelreg import Registry
from workflow.scenario_sets import adhoc, load_scenario_set, primary

REPO = Path(__file__).resolve().parent.parent
BOUNDARY = {"override_rules": [{"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 4}}]}
CEI = {"params_changes": [{"path": "cei.master_threshold", "value": 75}]}


# --- 시나리오 세트 -----------------------------------------------------------------

def test_default_scenario_set_matches_decision():
    s = load_scenario_set("default", REPO / "scenarios")
    assert [x["seed"] for x in s["train"]] == [42, 43, 44]
    assert [x["seed"] for x in s["validation"]] == [101, 102, 103]
    assert all(x["faults"] == ["P1", "P2", "P3", "P4"] for x in s["train"] + s["validation"])
    assert primary(s) == {"seed": 42, "faults": ["P1", "P2", "P3", "P4"]}


def test_scenario_set_validation(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"train": [{"seed": 1, "faults": []}], "validation": [{"seed": 1, "faults": []}]}))
    with pytest.raises(ValueError, match="같은 시나리오"):
        load_scenario_set(path, tmp_path)
    path.write_text(yaml.safe_dump({"train": [], "validation": [{"seed": 2}]}))
    with pytest.raises(ValueError, match="train"):
        load_scenario_set(path, tmp_path)
    with pytest.raises(ValueError, match="없음"):
        load_scenario_set("nope", tmp_path)
    assert adhoc(7, ["P1"]) == {"name": "adhoc-7", "train": [{"seed": 7, "faults": ["P1"]}], "validation": []}


# --- 레지스트리 --------------------------------------------------------------------

@pytest.fixture
def reg(tmp_path):
    r = Registry(tmp_path / "models", "rule")
    r.bootstrap(get_engine("rule").params_path, by="test")
    return r


def test_bootstrap_registers_v1_as_champion_once(reg):
    assert reg.versions() == [1] and reg.champion() == 1
    assert reg.card(1)["parent"] is None and reg.card(1)["source"]["kind"] == "bootstrap"
    assert yaml.safe_load(reg.params_path(1).read_text(encoding="utf-8"))["version"] == 1
    assert reg.bootstrap(get_engine("rule").params_path, by="test") == 1      # 이미 있으면 그대로
    assert reg.versions() == [1]


def test_register_new_version_keeps_parent_immutable(reg):
    before = reg.params_path(1).read_text(encoding="utf-8")
    v2 = reg.register(1, BOUNDARY, {"source": {"kind": "run", "run_id": "r1"}})
    assert v2 == 2 and reg.versions() == [1, 2] and reg.champion() == 1     # 등록만으로 챔피언이 되지 않는다
    assert reg.params_path(1).read_text(encoding="utf-8") == before
    p2 = yaml.safe_load(reg.params_path(2).read_text(encoding="utf-8"))
    assert p2["version"] == 2 and p2["overrides"]["rules"] == BOUNDARY["override_rules"]
    assert "# 이 값 이상이면 명장" in reg.params_path(2).read_text(encoding="utf-8")   # 주석 보존
    card = reg.card(2)
    assert card["parent"] == 1 and card["model_version"] == "rule@v2" and card["source"]["run_id"] == "r1"
    assert reg.find_by_run("r1") == 2 and reg.find_by_run("zz") is None


def test_champion_history_and_rollback(reg):
    reg.set_champion(reg.register(1, BOUNDARY, {}), by="kim", reason="승인", action="promote")
    v3 = reg.register(2, CEI, {})
    reg.set_champion(v3, by="kim", reason="승인", action="promote")
    assert reg.champion() == 3

    frm, to = reg.rollback(by="lee", note="부작용")                           # 기본: 직전 챔피언으로
    assert (frm, to) == (3, 2) and reg.champion() == 2
    assert reg.history()[-1] == {**reg.history()[-1], "version": 2, "action": "rollback", "by": "lee"}
    frm, to = reg.rollback(to=1, by="lee", note="처음으로")
    assert (frm, to) == (2, 1) and reg.champion() == 1
    with pytest.raises(ValueError, match="없는 버전"):
        reg.rollback(to=9, by="lee")
    with pytest.raises(ValueError, match="이미 챔피언"):
        reg.rollback(to=1, by="lee")


def test_new_version_after_rollback_gets_next_number(reg):
    v2 = reg.register(1, BOUNDARY, {})
    reg.set_champion(v2, by="a", reason="", action="promote")
    reg.rollback(by="a")
    v3 = reg.register(1, CEI, {})                                            # 부모는 v1, 번호는 최대+1
    assert v3 == 3 and reg.card(3)["parent"] == 1
    assert yaml.safe_load(reg.params_path(3).read_text(encoding="utf-8"))["version"] == 3


def test_register_rejects_invalid_proposal(reg):
    with pytest.raises(ValueError, match="허용 범위"):
        reg.register(1, {"params_changes": [{"path": "matching.area_extension_km[2]", "value": 9}]}, {})
    assert reg.versions() == [1]


def test_rollback_needs_history(reg):
    with pytest.raises(ValueError, match="되돌릴"):
        reg.rollback(by="a")


def test_committed_registry_has_v1_champion():
    reg = Registry(REPO / "models", "rule")
    assert reg.champion() is not None and 1 in reg.versions()
    assert yaml.safe_load(reg.params_path(1).read_text(encoding="utf-8")) == \
        yaml.safe_load(get_engine("rule").params_path.read_text(encoding="utf-8"))
