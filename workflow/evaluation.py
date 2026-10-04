"""X2 평가: 같은 시나리오·같은 정답표(P1~P4)로 단일 분석 agent 대 멀티에이전트 분석을 비교한다.

- 두 방식 모두 실제 워크플로우 그래프를 분석 단계까지만 돌린다 (propose 앞에서 멈춤).
- 정답표는 이 평가 코드만 본다. 분석 agent에게는 주지 않는다.
- 오탐은 자동으로 정하지 않는다. 정답표와 맞지 않은 발견을 "오탐 후보"로 나열하고 사람이 판정한다.
- 가짜 LLM(리허설) 결과는 각본대로 나오므로 "리허설 예시"로 표시한다. 품질 비교는 실제 API 결과로만 한다.
"""

import time

from core import score

from engines import Engine

from . import graph
from .machine import STAGE_FILES
from .store import RunStore

MODES = ("single", "multi")
REHEARSAL_LABEL = "리허설 예시 (가짜 LLM 각본, 품질 근거 아님)"
REAL_LABEL = "실제 API"


def _tokens(usage: dict) -> dict:
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return {k: usage.get(k, 0) for k in keys}


def compare_analysis(root, engine: Engine, *, seed: int, faults: list[str], llm_factory, llm_config: dict,
                     limits: dict, perspectives: list[dict], rehearsal: bool) -> dict:
    """llm_factory(): 방식마다 새 LLM 클라이언트. 결과는 root/<eval_id>/comparison.json에도 저장한다."""
    eval_root = RunStore(root)
    eval_id = eval_root.new_run_id()
    store = RunStore(eval_root.run_dir(eval_id))
    _, truth = engine.pack_factory(engine.load_params()).generate(seed, faults)

    results, model = {}, None
    for mode in MODES:
        llm = llm_factory()
        model = llm.model
        started = time.time()
        run = graph.run_workflow(store, engine, seed=seed, faults=faults, llm=llm, llm_config=llm_config,
                                 limits=limits, rehearsal=rehearsal, analysis_mode=mode,
                                 perspectives=perspectives if mode == "multi" else None, stop_before=["propose"])
        wall = time.time() - started
        if run["status"] != "analyzed":
            results[mode] = {"run_id": run["run_id"], "status": run["status"], "error": run.get("error")}
            continue
        report = store.read(run["run_id"], STAGE_FILES["2_analyze"])
        executed = store.read(run["run_id"], STAGE_FILES["1_execute"])
        scored = score(report["findings"], truth["faults"])
        titles = {f["id"]: f["title"] for f in report["findings"]}
        usage = report["usage"]
        results[mode] = {
            "run_id": run["run_id"],
            "status": run["status"],
            "findings": len(report["findings"]),
            "dropped": len(report["dropped"]),
            "detected": scored["detected"],
            "total": scored["total"],
            "detection_rate": scored["detection_rate"],
            "faults": {fid: {"name": v["name"], "detected": v["detected"], "matched": v["matched_findings"]}
                       for fid, v in scored["faults"].items()},
            "false_positive_candidates": [{"id": fid, "title": titles[fid]} for fid in scored["unmatched_findings"]],
            "llm_calls": usage.get("llm_calls", 0),
            "tokens": _tokens(usage),
            "cost_usd": usage.get("cost_usd"),
            "analysis_seconds": max(0.0, wall - executed["seconds"]),   # 실행 단계 시간을 뺀 벽시계 시간
        }

    comparison = {"eval_id": eval_id, "label": REHEARSAL_LABEL if rehearsal else REAL_LABEL,
                  "rehearsal": rehearsal, "engine": engine.name, "scenario": {"seed": seed, "faults": list(faults)},
                  "llm_model": model, "results": results}
    eval_root.write(eval_id, "comparison.json", comparison)
    return comparison
