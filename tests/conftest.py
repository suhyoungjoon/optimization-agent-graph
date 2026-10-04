"""공용 fixture: 임시 모델 레지스트리, 작은 시나리오 세트, 설정의 판정 기준."""

from pathlib import Path

import pytest
import yaml
from core import load_config

from engines import get_engine
from modelreg import Registry

REPO = Path(__file__).resolve().parent.parent
SETTINGS = yaml.safe_load((REPO / "settings" / "workflow.yaml").read_text(encoding="utf-8"))
CRITERIA = SETTINGS["criteria"]
LIMITS = {**SETTINGS["limits"]}
P = ["P1", "P2", "P3", "P4"]
# 빠른 테스트용: 학습 1개, 검증 1개
SMALL = {"name": "small", "train": [{"seed": 42, "faults": P}], "validation": [{"seed": 101, "faults": P}]}
# 검증셋에 경계 지역 결함(P4)이 없어 경계 지역 개선안의 효과가 사라진다
FADING = {"name": "fading", "train": [{"seed": 42, "faults": P}],
          "validation": [{"seed": 101, "faults": []}, {"seed": 102, "faults": []}]}


@pytest.fixture
def llm_config():
    return {**load_config(REPO / "settings" / "llm.yaml"), "cache": False}


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path / "models", "rule")
    reg.bootstrap(get_engine("rule").params_path, by="test")
    return reg


def champion_params(reg: Registry) -> dict:
    return reg.load_params(reg.champion())
