"""시나리오 세트: 학습용(train)·검증용(validation) seed·결함 조합. 정의는 scenarios/<이름>.yaml."""

from pathlib import Path

import yaml

SETS = ("train", "validation")


def load_scenario_set(name_or_path: str | Path, scenarios_dir: str | Path) -> dict:
    """세트 이름(scenarios_dir/<이름>.yaml) 또는 yaml 경로. {"name", "train": [...], "validation": [...]}"""
    path = Path(name_or_path)
    if path.suffix not in (".yaml", ".yml"):
        path = Path(scenarios_dir) / f"{name_or_path}.yaml"
    if not path.is_file():
        raise ValueError(f"시나리오 세트 없음: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = {"name": data.get("name") or path.stem}
    for key in SETS:
        items = data.get(key) or []
        for s in items:
            if not isinstance(s.get("seed"), int) or not isinstance(s.get("faults", []), list):
                raise ValueError(f"{path} {key}: 시나리오는 {{seed: 정수, faults: [...]}} 형식이어야 함: {s}")
        out[key] = [{"seed": s["seed"], "faults": list(s.get("faults") or [])} for s in items]
    if not out["train"]:
        raise ValueError(f"{path}: train 시나리오가 최소 하나 필요함 (첫 시나리오를 분석에 쓴다)")
    overlap = {(s["seed"], tuple(s["faults"])) for s in out["train"]} & \
              {(s["seed"], tuple(s["faults"])) for s in out["validation"]}
    if overlap:
        raise ValueError(f"{path}: 학습용과 검증용에 같은 시나리오가 있음 {sorted(overlap)}")
    return out


def primary(scenario_set: dict) -> dict:
    """분석·개선안 도출에 쓰는 시나리오 (train의 첫 번째)."""
    return scenario_set["train"][0]


def adhoc(seed: int, faults: list[str]) -> dict:
    """seed 하나짜리 세트 (분석 방식 비교처럼 검증 단계까지 가지 않는 실행용)."""
    return {"name": f"adhoc-{seed}", "train": [{"seed": seed, "faults": list(faults)}], "validation": []}
