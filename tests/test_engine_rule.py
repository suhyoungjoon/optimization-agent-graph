"""규칙 엔진 어댑터: 이 레포 params 파일 사용, pack_factory로 후보 params 평가."""

import shutil
from pathlib import Path

from core import apply_params, check_params, load_params, simulate_params

from engines import get_engine, model_version
from engines.rule import DEFAULT_PARAMS, RuleEngine, RulePack

REPO = Path(__file__).resolve().parent.parent


def test_default_params_file_is_owned_by_this_repo_and_valid():
    engine = get_engine("rule")
    assert engine.params_path == DEFAULT_PARAMS
    assert engine.params_path.resolve().is_relative_to(REPO)
    params = engine.load_params()
    assert isinstance(params["version"], int)
    assert check_params(params, engine.pack_factory(params).dimensions()) == []


def test_pack_points_to_given_params_path(tmp_path):
    path = tmp_path / "params.yaml"
    shutil.copy(DEFAULT_PARAMS, path)
    engine = RuleEngine(path)
    pack = engine.pack_factory(engine.load_params())
    assert isinstance(pack, RulePack) and pack.params_path() == str(path)
    assert load_params(pack) == engine.load_params()      # 코어 load_params도 이 레포 파일을 읽는다


def test_pack_factory_uses_candidate_params_for_validate_and_metrics():
    engine = RuleEngine()
    base = engine.load_params()
    candidate = apply_params(base, {"params_changes": [{"path": "travel.avg_speed_kmh", "value": 15}]})
    pack = engine.pack_factory(candidate)
    assert pack.params is candidate
    instance, _ = pack.generate(7, [])
    decisions = pack.solve(instance, candidate)
    assert pack.validate(instance, decisions) == []
    assert pack.metrics(instance, decisions) != engine.pack_factory(base).metrics(instance, decisions)


def test_rule_engine_reproduces_core_baseline_and_simulation():
    engine = RuleEngine()
    params = engine.load_params()
    pack = engine.pack_factory(params)
    instance, _ = pack.generate(42, ["P1", "P2", "P3", "P4"])
    decisions = pack.solve(instance, params)
    assert sum(d.status == "success" for d in decisions) == 1040     # handoff 4.3 기준값 (params v1)
    assert pack.validate(instance, decisions) == []
    rule = {"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 4}}
    sim = simulate_params(engine.pack_factory, instance, params, apply_params(params, {"override_rules": [rule]}))
    assert sim["after"]["assignment_rate"] > sim["before"]["assignment_rate"] and sim["violations_after"] == 0


def test_model_version_and_spec_text():
    engine = RuleEngine()
    assert model_version(engine, {"version": 3}) == "rule@v3"
    assert engine.spec_text() == ""


def test_workflow_code_does_not_know_engines():
    """원칙 4: 워크플로우는 특정 엔진·도메인을 몰라야 한다."""
    for path in (REPO / "workflow").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "domains" not in text and "engines.rule" not in text and "RuleEngine" not in text, path


def test_no_folders_shadowing_core_package_names():
    for name in ("core", "domains", "api", "scripts", "configs"):
        assert not (REPO / name).exists(), name
