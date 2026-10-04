"""워크플로우 5단계. 각 함수는 코어 함수를 조합하고, 저장할 결과 dict를 돌려준다.

엔진은 engines.Engine 계약으로만 다룬다 (pack_factory, load_params, spec_text, params_path).
5단계(개선적용)는 모델 레지스트리(modelreg)에 새 버전을 등록하고 챔피언으로 지정한다 (workflow.graph의 apply 노드).
"""

import time
from collections import Counter

from core import apply_params, finding_slices, simulate_params
from core import analyze as core_analyze
from core import propose as core_propose

from engines import Engine, model_version

from .judge import judge
from .scenario_sets import SETS, primary

TARGET_KIND = "params"   # 엔진이 개선할 수 있는 개선안 종류 (규칙 엔진: params만)


class StageError(Exception):
    """단계를 더 진행할 수 없는 실패. 워크플로우가 failed로 기록한다."""


# --- 1. 실행 (코드) ---------------------------------------------------------------

def execute(engine: Engine, params: dict, seed: int, faults: list[str]):
    """챔피언 모델로 시나리오 실행 → validate → metrics. (instance, decisions, 결과)"""
    started = time.time()
    pack = engine.pack_factory(params)
    instance, _truth = pack.generate(seed, faults)     # 정답표는 분석에 주지 않는다
    decisions = pack.solve(instance, params)
    violations = pack.validate(instance, decisions)
    result = {
        "model_version": model_version(engine, params),
        "items": len(decisions),
        "status_counts": dict(Counter(d.status for d in decisions)),
        "reason_counts": dict(Counter(d.reason_code for d in decisions if d.status != "success")),
        "metrics": pack.metrics(instance, decisions),
        "violations": len(violations),
        "violation_samples": [v.__dict__ for v in violations[:20]],
        "seconds": time.time() - started,
    }
    return instance, decisions, result


def execute_set(engine: Engine, params: dict, scenario_set: dict):
    """챔피언 모델을 학습용 세트 전체에 실행한다. 분석·개선안 도출은 첫 시나리오(primary)로 한다.

    (primary instance, primary decisions, 결과). 결과의 최상위 항목은 primary 기준이고,
    scenarios·mean_metrics에 학습용 세트 전체의 seed별·평균 지표가 있다.
    """
    started = time.time()
    rows, first = [], None
    for scenario in scenario_set["train"]:
        instance, decisions, result = execute(engine, params, scenario["seed"], scenario["faults"])
        if first is None:
            first = (instance, decisions, result)
        rows.append({**scenario, **{k: result[k] for k in ("items", "status_counts", "reason_counts", "metrics",
                                                              "violations")}})
    instance, decisions, result = first
    names = sorted(rows[0]["metrics"])
    return instance, decisions, {
        **result,
        "scenario_set": scenario_set["name"],
        "primary": primary(scenario_set),
        "scenarios": rows,
        "mean_metrics": {m: sum(r["metrics"][m] for r in rows) / len(rows) for m in names},
        "violations_total": sum(r["violations"] for r in rows),
        "seconds": time.time() - started,
    }


# --- 2. 결과분석 (AI + 코드) --------------------------------------------------------

def analyze(engine: Engine, params: dict, instance, decisions, llm, llm_config: dict, max_calls: int) -> dict:
    report = core_analyze(engine.pack_factory(params), instance, decisions, llm, llm_config, max_calls=max_calls)
    return check_report(report)


def check_report(report: dict) -> dict:
    """단일·멀티 분석 리포트 공통: 제출되지 않았거나 근거 있는 발견이 없으면 진행할 수 없다."""
    if report["stop"] != "submitted":
        raise StageError(f"분석 agent가 리포트를 제출하지 않음 (stop={report['stop']})")
    if not report["findings"]:
        raise StageError(f"근거 검사를 통과한 발견이 없음 (제외 {len(report['dropped'])}건)")
    return report


# --- 3. 개선안 도출 (AI + 코드) -----------------------------------------------------

def propose(engine: Engine, params: dict, instance, report: dict, llm, llm_config: dict, max_calls: int,
            salt: str = "") -> dict:
    """salt: 재시도 때 같은 요청이 LLM 캐시에서 같은 답으로 돌아오지 않게 시도마다 바꾼다."""
    pack = engine.pack_factory(params)
    out = core_propose(engine.pack_factory, instance, params, engine.spec_text(), pack.dimensions(), report,
                       llm, llm_config, salt=salt, max_calls=max_calls)
    if out["stop"] != "submitted":
        raise StageError(f"개선 agent가 개선안을 제출하지 않음 (stop={out['stop']})")
    proposals = []
    for i, item in enumerate(out["proposals"], start=1):
        errors = list(item["errors"])
        if item["proposal"].get("kind") != TARGET_KIND:
            errors = [f"{engine.name} 엔진의 개선 대상이 아님 (kind={item['proposal'].get('kind')}, "
                      f"대상은 {TARGET_KIND})"]
        proposals.append({"id": f"C{i}", "proposal": item["proposal"], "errors": errors})
    return {**out, "proposals": proposals}


# --- 4. 검증 (코드) ---------------------------------------------------------------
# M2: 개선안마다 학습용·검증용 세트의 모든 시나리오에서 챔피언(전) 대 도전자(후)를 비교하고 판정 기준을 적용한다.

def validate(engine: Engine, params: dict, report: dict, proposed: dict, scenario_set: dict, criteria: dict,
             time_budget_s: float | None = None) -> dict:
    started = time.time()
    slices = finding_slices(report)
    pack = engine.pack_factory(params)
    instances: dict[tuple, object] = {}

    def instance_of(scenario: dict):
        key = (scenario["seed"], tuple(scenario["faults"]))
        if key not in instances:
            instances[key] = pack.generate(scenario["seed"], scenario["faults"])[0]
        return instances[key]

    candidates = []
    for item in proposed["proposals"]:
        if item["errors"]:
            continue
        candidate = apply_params(params, item["proposal"])
        sets, exceeded = {}, False
        for name in SETS:
            rows = []
            for scenario in scenario_set[name]:
                if time_budget_s is not None and time.time() - started > time_budget_s:
                    exceeded = True
                    break
                sim = simulate_params(engine.pack_factory, instance_of(scenario), params, candidate, slices)
                rows.append({**scenario, "before": sim["before"], "after": sim["after"],
                             "violations_after": sim["violations_after"], "slices": sim["slices"],
                             "seconds": sim["seconds"]})
            sets[name] = {"scenarios": rows}
        verdict = judge({**sets, "budget_exceeded": exceeded}, criteria)
        for name in SETS:
            sets[name]["summary"] = verdict["summary"].get(name)
        candidates.append({"id": item["id"], "title": item["proposal"].get("title", ""), "sets": sets,
                           "judgement": {k: verdict[k] for k in ("passed", "checks", "reasons")},
                           "eligible": verdict["passed"], "reasons": verdict["reasons"]})
    return {
        "method": "simulate_params per scenario (train + validation)",
        "scenario_set": scenario_set["name"],
        "criteria": criteria,
        "skipped": [{"id": p["id"], "errors": p["errors"]} for p in proposed["proposals"] if p["errors"]],
        "candidates": candidates,
        "eligible": [c["id"] for c in candidates if c["eligible"]],
        "seconds": time.time() - started,
    }
