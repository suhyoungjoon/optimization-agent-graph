"""판정 기준 (결정적, 테스트 먼저): 위반 0건 절대 기준, 목표 지표 평균 개선, 검증셋 seed 과반, 부작용 한도."""

import pytest

from workflow.judge import judge, set_summary

CRITERIA = {"target": {"metric": "assignment_rate", "direction": "higher", "min_delta": 0.01,
                       "sets": ["train", "validation"], "seed_majority": ["validation"]},
            "side_effects": []}


def _scenario(seed, before, after, violations=0, **other):
    b = {"assignment_rate": before, "desired_time_match_rate": 0.7, "avg_travel_min": 8.0}
    a = {"assignment_rate": after, "desired_time_match_rate": other.get("dtm", 0.7),
         "avg_travel_min": other.get("travel", 8.0)}
    return {"seed": seed, "faults": ["P1"], "before": b, "after": a, "violations_after": violations}


def _sets(train, validation):
    return {"train": {"scenarios": train}, "validation": {"scenarios": validation}}


GOOD_TRAIN = [_scenario(42, 0.70, 0.78), _scenario(43, 0.70, 0.77), _scenario(44, 0.70, 0.79)]


def test_passes_when_both_sets_improve_enough():
    out = judge(_sets(GOOD_TRAIN, [_scenario(101, 0.7, 0.76), _scenario(102, 0.7, 0.75), _scenario(103, 0.7, 0.77)]),
                CRITERIA)
    assert out["passed"] and out["reasons"] == []
    assert out["summary"]["validation"]["mean_gain"]["assignment_rate"] == pytest.approx(0.06)
    assert out["summary"]["validation"]["improved"] == 3


def test_effect_disappearing_on_validation_is_rejected():
    out = judge(_sets(GOOD_TRAIN, [_scenario(101, 0.7, 0.7), _scenario(102, 0.7, 0.7), _scenario(103, 0.7, 0.7)]),
                CRITERIA)
    assert not out["passed"]
    assert any("validation" in r and "assignment_rate" in r for r in out["reasons"])
    assert any("과반" in r for r in out["reasons"])


def test_mean_improvement_without_seed_majority_is_rejected():
    # 평균은 +1%p를 넘지만 한 seed에서만 크게 좋아짐
    out = judge(_sets(GOOD_TRAIN, [_scenario(101, 0.7, 0.8), _scenario(102, 0.7, 0.69), _scenario(103, 0.7, 0.7)]),
                CRITERIA)
    assert not out["passed"] and [r for r in out["reasons"] if "과반" in r]
    assert not [r for r in out["reasons"] if "평균" in r]


def test_any_violation_is_absolute_rejection():
    out = judge(_sets(GOOD_TRAIN[:2] + [_scenario(44, 0.7, 0.9, violations=2)],
                      [_scenario(101, 0.7, 0.8), _scenario(102, 0.7, 0.8), _scenario(103, 0.7, 0.8)]), CRITERIA)
    assert not out["passed"] and "필수조건 위반" in out["reasons"][0] and "seed 44" in out["reasons"][0]


def test_lower_is_better_metric_and_side_effect_limit():
    criteria = {**CRITERIA, "side_effects": [
        {"metric": "desired_time_match_rate", "direction": "higher", "max_worsening": 0.05, "sets": ["validation"]},
        {"metric": "avg_travel_min", "direction": "lower", "max_worsening": 1.0, "sets": ["validation"]}]}
    val = [_scenario(s, 0.7, 0.8, dtm=0.6, travel=8.5) for s in (101, 102, 103)]   # 일치율 -0.1, 이동 +0.5분
    out = judge(_sets(GOOD_TRAIN, val), criteria)
    assert not out["passed"]
    assert [r for r in out["reasons"] if "desired_time_match_rate" in r]
    assert not [r for r in out["reasons"] if "avg_travel_min" in r]                  # 0.5분 악화는 한도 안
    assert out["summary"]["validation"]["mean_gain"]["avg_travel_min"] == pytest.approx(-0.5)


def test_side_effects_are_recorded_even_without_limits():
    val = [_scenario(s, 0.7, 0.8, dtm=0.5) for s in (101, 102, 103)]
    out = judge(_sets(GOOD_TRAIN, val), CRITERIA)
    assert out["passed"]                                                             # 한도 없음: 판정에 안 씀
    assert out["summary"]["validation"]["mean_gain"]["desired_time_match_rate"] == pytest.approx(-0.2)


def test_empty_required_set_fails_and_time_budget_flag():
    out = judge(_sets(GOOD_TRAIN, []), CRITERIA)
    assert not out["passed"] and any("시나리오가 없음" in r for r in out["reasons"])
    out = judge({**_sets(GOOD_TRAIN, GOOD_TRAIN), "budget_exceeded": True}, CRITERIA)
    assert not out["passed"] and any("시간 예산" in r for r in out["reasons"])


def test_set_summary_means():
    s = set_summary([_scenario(1, 0.6, 0.7), _scenario(2, 0.8, 0.8)], "assignment_rate", "higher")
    assert s["mean_before"]["assignment_rate"] == pytest.approx(0.7)
    assert s["mean_after"]["assignment_rate"] == pytest.approx(0.75)
    assert s["improved"] == 1 and s["n"] == 2
