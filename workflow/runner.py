"""M1 워크플로우 러너: 1~4단계를 돌고 승인 대기에서 멈춘다. 5단계는 사람의 approve/reject로만 진행한다.

CLI는 X1부터 workflow.graph(LangGraph)를 쓴다. 이 모듈은 동등성 테스트의 기준과 공용 도우미(new_run, summary)로 남는다.
"""

import getpass
import json
import time
import traceback
from importlib import metadata

from core import to_jsonable

from engines import Engine, get_engine, model_version

from . import stages
from .machine import STAGE_FILES, TERMINAL, transition
from .stages import params_digest
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


def engine_of(run: dict) -> Engine:
    return get_engine(run["engine"], run["params_path"])


def new_run(store: RunStore, engine: Engine, *, seed: int, faults: list[str], llm_model: str, llm_config: dict,
            limits: dict, rehearsal: bool, orchestrator: str) -> dict:
    """run.json(재현 정보 + 상태 이력)을 만들어 저장한다. M1 러너와 X1 그래프가 같이 쓴다."""
    params = engine.load_params()
    run = {
        "run_id": store.new_run_id(),
        "created_at": time.time(),
        "status": "running",
        "history": [{"status": "running", "at": time.time(), "note": ""}],
        "orchestrator": orchestrator,
        "engine": engine.name,
        "params_path": str(engine.params_path.resolve()),
        "params_sha256": params_digest(engine),
        "model_version": model_version(engine, params),
        "scenario": {"seed": seed, "faults": list(faults)},
        "llm": {"model": llm_model, "rehearsal": rehearsal, "cache": bool(llm_config.get("cache"))},
        "limits": limits,
        "core": core_ref(),
        "error": None,
    }
    store.create(run)
    return run


def run_workflow(store: RunStore, engine: Engine, *, seed: int, faults: list[str], llm, llm_config: dict,
                 limits: dict, rehearsal: bool) -> dict:
    """M1 상태 머신 (X1 그래프의 동등성 비교 기준). 1~4단계 → 승인 대기 (또는 rejected/failed)."""
    params = engine.load_params()
    run = new_run(store, engine, seed=seed, faults=faults, llm_model=llm.model, llm_config=llm_config,
                  limits=limits, rehearsal=rehearsal, orchestrator="m1")
    run_id = run["run_id"]

    def advance(status: str, note: str = "") -> None:
        transition(run, status, note)
        store.save(run)

    try:
        instance, decisions, executed = stages.execute(engine, params, seed, faults)
        store.write(run_id, "decisions.json", decisions)
        store.write(run_id, STAGE_FILES["1_execute"], {**executed, "decisions_file": "decisions.json"})

        report = stages.analyze(engine, params, instance, decisions, llm, llm_config,
                                limits["analyze_max_llm_calls"])
        store.write(run_id, STAGE_FILES["2_analyze"], report)
        advance("analyzed", f"발견 {len(report['findings'])}건")

        proposed = stages.propose(engine, params, instance, report, llm, llm_config,
                                  limits["propose_max_llm_calls"])
        store.write(run_id, STAGE_FILES["3_propose"], proposed)
        valid = sum(1 for p in proposed["proposals"] if not p["errors"])
        advance("proposed", f"개선안 {len(proposed['proposals'])}건, 허용 범위 통과 {valid}건")

        validated = stages.validate(engine, params, instance, report, proposed)
        store.write(run_id, STAGE_FILES["4_validate"], validated)
        advance("validated", f"승인 후보 {len(validated['eligible'])}건")
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 사유를 남기고 failed로 끝낸다
        run["error"] = {"type": type(exc).__name__, "message": str(exc),
                        "traceback": traceback.format_exc(limit=8)}
        advance("failed", str(exc))
        return run

    if validated["eligible"]:
        advance("awaiting_approval", "사람의 approve 또는 reject를 기다림")
    else:
        store.write(run_id, STAGE_FILES["5_apply"], {"decision": "rejected", "by": "workflow",
                                                      "note": "검증을 통과한 후보가 없음", "at": time.time()})
        advance("rejected", "검증을 통과한 후보가 없음")
    return run


def _awaiting(store: RunStore, run_id: str) -> dict:
    run = store.load(run_id)
    if run["status"] != "awaiting_approval":
        raise ValueError(f"승인 대기 상태가 아님: {run_id} ({run['status']})")
    return run


def approve(store: RunStore, run_id: str, proposal_id: str | None = None, note: str = "") -> dict:
    """사람 승인: 후보를 챔피언 params 파일에 반영한다 (version +1)."""
    run = _awaiting(store, run_id)
    eligible = store.read(run_id, STAGE_FILES["4_validate"])["eligible"]
    if proposal_id is None:
        if len(eligible) != 1:
            raise ValueError(f"승인 후보가 {len(eligible)}건이므로 --proposal로 지정해야 함: {eligible}")
        proposal_id = eligible[0]
    if proposal_id not in eligible:
        raise ValueError(f"검증을 통과한 후보가 아님: {proposal_id} (후보: {eligible})")

    engine = engine_of(run)
    if params_digest(engine) != run["params_sha256"]:
        raise ValueError("실행 이후 챔피언 params 파일이 바뀌었으므로 승인할 수 없음. 다시 run 하라: "
                         + run["params_path"])
    proposal = next(p["proposal"] for p in store.read(run_id, STAGE_FILES["3_propose"])["proposals"]
                    if p["id"] == proposal_id)
    record = {"decision": "approved", "by": getpass.getuser(), "note": note, "proposal_id": proposal_id,
              "at": time.time()}
    try:
        applied = stages.apply(engine, proposal, run["params_sha256"])
    except Exception as exc:  # noqa: BLE001
        run["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(limit=8)}
        store.write(run_id, STAGE_FILES["5_apply"], {**record, "error": str(exc)})
        transition(run, "failed", str(exc))
        store.save(run)
        return run
    store.write(run_id, STAGE_FILES["5_apply"], {**record, **applied, "state": "applied"})
    transition(run, "applied", f"{applied['model_before']} → {applied['model_after']}")
    store.save(run)
    return run


def reject(store: RunStore, run_id: str, note: str = "") -> dict:
    run = _awaiting(store, run_id)
    store.write(run_id, STAGE_FILES["5_apply"], {"decision": "rejected", "by": getpass.getuser(), "note": note,
                                                  "at": time.time()})
    transition(run, "rejected", note or "사람이 반려")
    store.save(run)
    return run


def summary(store: RunStore, run_id: str) -> dict:
    """status 명령용 요약: 메타 + 단계별 핵심 결과."""
    run = store.load(run_id)
    out = {k: run[k] for k in ("run_id", "status", "engine", "model_version", "scenario", "llm", "error")}
    out["history"] = [h["status"] for h in run["history"]]
    if store.has(run_id, STAGE_FILES["1_execute"]):
        ex = store.read(run_id, STAGE_FILES["1_execute"])
        out["execute"] = {k: ex[k] for k in ("items", "metrics", "violations", "reason_counts")}
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
                            "before": c["simulation"]["before"], "after": c["simulation"]["after"],
                            "violations_after": c["simulation"]["violations_after"],
                            "slices": c["simulation"]["slices"]} for c in va["candidates"]]
    if store.has(run_id, STAGE_FILES["5_apply"]):
        out["apply"] = store.read(run_id, STAGE_FILES["5_apply"])
    out["terminal"] = run["status"] in TERMINAL
    return to_jsonable(out)
