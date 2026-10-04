"""실행 기록 도우미: run.json 생성(재현 정보), status 요약.

M2에서 M1 러너(runner.py)를 정리하면서 공용 부분만 이곳으로 옮겼다.
"""

import hashlib
import json
import time
from importlib import metadata

from core import to_jsonable

from engines import Engine, get_engine, model_version

from .machine import STAGE_FILES, TERMINAL
from .store import RunStore

CORE_DIST = "optimization-agent-harness"


def core_ref() -> dict:
    """설치된 코어의 버전과 고정 커밋 (재현성 기록용)."""
    dist = metadata.distribution(CORE_DIST)
    info = {"version": dist.version}
    direct = dist.read_text("direct_url.json")
    if direct:
        data = json.loads(direct)
        info["url"] = data.get("url")
        info["commit"] = (data.get("vcs_info") or {}).get("commit_id")
    return info


def params_digest(engine: Engine) -> str:
    return hashlib.sha256(engine.params_path.read_bytes()).hexdigest()


def engine_of(run: dict) -> Engine:
    """실행이 쓴 챔피언 엔진 (params_path는 레지스트리의 바뀌지 않는 스냅샷)."""
    return get_engine(run["engine"], run["params_path"])


def new_run(store: RunStore, engine: Engine, *, scenario_set: dict, llm_model: str, llm_config: dict, limits: dict,
            rehearsal: bool, extra: dict | None = None) -> dict:
    """run.json(재현 정보 + 상태 이력)을 만들어 저장한다."""
    params = engine.load_params()
    run = {
        "run_id": store.new_run_id(),
        "created_at": time.time(),
        "status": "running",
        "history": [{"status": "running", "at": time.time(), "note": ""}],
        "orchestrator": "langgraph",
        "engine": engine.name,
        "params_path": str(engine.params_path.resolve()),
        "params_sha256": params_digest(engine),
        "model_version": model_version(engine, params),
        "scenario": scenario_set["train"][0],          # 분석·개선안 도출에 쓰는 시나리오 (primary)
        "scenario_set": scenario_set,
        "llm": {"model": llm_model, "rehearsal": rehearsal, "cache": bool(llm_config.get("cache"))},
        "limits": limits,
        "core": core_ref(),
        "error": None,
        **(extra or {}),
    }
    store.create(run)
    return run


def summary(store: RunStore, run_id: str) -> dict:
    """status 명령용 요약: 메타 + 단계별 핵심 결과."""
    run = store.load(run_id)
    out = {k: run.get(k) for k in ("run_id", "status", "engine", "model_version", "scenario", "llm", "error")}
    out["scenario_set"] = (run.get("scenario_set") or {}).get("name")
    out["history"] = [h["status"] for h in run["history"]]
    if store.has(run_id, STAGE_FILES["1_execute"]):
        ex = store.read(run_id, STAGE_FILES["1_execute"])
        out["execute"] = {k: ex.get(k) for k in ("items", "metrics", "violations", "reason_counts", "scenarios",
                                                 "mean_metrics")}
    if store.has(run_id, STAGE_FILES["2_analyze"]):
        an = store.read(run_id, STAGE_FILES["2_analyze"])
        out["analyze"] = {"mode": an.get("mode", "single"),
                          "findings": [f"{f['id']} {f['title']}" for f in an["findings"]],
                          "dropped": len(an["dropped"]), "usage": an["usage"],
                          "perspectives": an.get("perspectives", {})}
    if store.has(run_id, STAGE_FILES["3_propose"]):
        pr = store.read(run_id, STAGE_FILES["3_propose"])
        out["propose"] = [{"id": p["id"], "title": p["proposal"].get("title"), "errors": p["errors"]}
                          for p in pr["proposals"]]
    if store.has(run_id, STAGE_FILES["4_validate"]):
        va = store.read(run_id, STAGE_FILES["4_validate"])
        out["validate"] = [{"id": c["id"], "eligible": c["eligible"], "reasons": c["reasons"],
                            "checks": c["judgement"]["checks"],
                            "sets": {name: s["summary"] for name, s in c["sets"].items()}}
                           for c in va["candidates"]]
    if store.has(run_id, STAGE_FILES["5_apply"]):
        out["apply"] = store.read(run_id, STAGE_FILES["5_apply"])
    out["terminal"] = run["status"] in TERMINAL
    return to_jsonable(out)
