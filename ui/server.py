"""M3 워크플로우 화면 서버 (FastAPI). 로컬 전용 (127.0.0.1).

- 새 로직 없이 기존 기록을 읽어 보여 준다: runs/<run_id>/의 단계별 JSON, LangGraph 체크포인트(get_state_history),
  노드 시간 기록(timings/), 모델 레지스트리.
- 승인·반려는 CLI와 같은 workflow.graph.approve/reject를 부른다 (잠금·챔피언 확인·중단 복구가 그대로 적용).
- 화면에서 시작하는 실행은 리허설(가짜 LLM)만 허용한다. 실제 API 실행은 CLI에서 사람이 한다.
- 인증이 없으므로 로컬에서만 띄운다. 승인자는 화면에서 입력한 이름을 기록한다.
"""

import threading
from pathlib import Path

from core import to_jsonable
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from modelreg import Registry
from workflow import graph, multi_analysis
from workflow.machine import STAGE_FILES, TERMINAL
from workflow.records import summary
from workflow.scenario_sets import load_scenario_set
from workflow.store import RunStore

STATIC = Path(__file__).resolve().parent / "static"

# 그래프 노드 → (표시 이름, 주체, 기획의 5단계). 주체 색: ai=주황, code=파랑, human=검정
NODE_META = {
    "execute": ("실행", "code", "1 실행"),
    "analyze": ("단일 분석", "ai", "2 결과분석"),
    "perspective": ("관점 agent", "ai", "2 결과분석"),
    "synthesize": ("종합", "ai", "2 결과분석"),
    "propose": ("개선안 도출", "ai", "3 개선안 도출"),
    "validate": ("검증·판정", "code", "4 검증"),
    "retry": ("재시도", "code", "4 검증"),
    "auto_reject": ("자동 반려", "code", "5 개선적용"),
    "await_approval": ("승인 대기", "human", "승인 대기"),
    "approval": ("승인 (interrupt)", "human", "승인 대기"),
    "apply": ("레지스트리 등록", "human", "5 개선적용"),
    "reject": ("반려 기록", "human", "5 개선적용"),
}


class StartRequest(BaseModel):
    analysis: str = "single"
    scenario: str | None = None


class DecisionRequest(BaseModel):
    approver: str
    note: str = ""
    proposal_id: str | None = None


def _usage_of(store: RunStore, run_id: str, run: dict) -> dict[str, dict]:
    """노드(또는 노드:관점)별 LLM 사용량. 단계 파일의 usage를 모은다."""
    out: dict[str, dict] = {}
    if store.has(run_id, STAGE_FILES["2_analyze"]):
        report = store.read(run_id, STAGE_FILES["2_analyze"])
        if report.get("mode") == "multi":
            out["synthesize"] = report["synthesis"]["usage"]
            for pid, info in report.get("perspectives", {}).items():
                if info.get("usage"):
                    out[f"perspective:{pid}"] = info["usage"]
        else:
            out["analyze"] = report.get("usage", {})
    proposes = sorted(p.name for p in store.run_dir(run_id).glob("3_propose*.json"))
    for i, name in enumerate(proposes):
        out[f"propose#{i}"] = store.read(run_id, name).get("usage", {})
    return out


def _sum(usages) -> dict:
    total = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": None}
    for u in usages:
        total["llm_calls"] += u.get("llm_calls", 0) or 0
        total["input_tokens"] += u.get("input_tokens", 0) or 0
        total["output_tokens"] += u.get("output_tokens", 0) or 0
        if u.get("cost_usd") is not None:
            total["cost_usd"] = (total["cost_usd"] or 0) + u["cost_usd"]
    return total


def create_app(*, runs_dir: Path, models_dir: Path, settings: dict, settings_dir: Path, scenarios_dir: Path,
               rehearsal_llm_factory) -> FastAPI:
    """rehearsal_llm_factory() -> (llm, llm_config). 화면에서 시작하는 실행에만 쓴다."""
    store = RunStore(runs_dir)
    app = FastAPI(title="optimization-agent-graph")
    threads: dict[str, threading.Thread] = {}

    def registry() -> Registry:
        return Registry(models_dir, settings["engine"])

    def load(run_id: str) -> dict:
        try:
            return store.load(run_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    # --- 그래프 구조 ---------------------------------------------------------------

    @app.get("/api/graph")
    def graph_structure():
        g = graph.build_graph(graph.Deps(store)).compile().get_graph()
        nodes = []
        for node_id in g.nodes:
            label, actor, stage = NODE_META.get(node_id, (node_id.strip("_"), "system", ""))
            nodes.append({"id": node_id, "label": label, "actor": actor, "stage": stage})
        edges = [{"source": e.source, "target": e.target, "conditional": bool(e.conditional)} for e in g.edges]
        return {"nodes": nodes, "edges": edges, "mermaid": g.draw_mermaid()}

    # --- 실행 목록·통계 ---------------------------------------------------------------

    @app.get("/api/runs")
    def runs():
        items, usages, node_seconds = [], [], {}
        for r in reversed(store.list_runs()):
            history = [h["status"] for h in r["history"]]
            usage = _sum(_usage_of(store, r["run_id"], r).values())
            usages.append(usage)
            for t in store.timings(r["run_id"]):
                node_seconds.setdefault(t["node"], []).append(t["seconds"])
            items.append({"run_id": r["run_id"], "status": r["status"], "model_version": r["model_version"],
                          "scenario_set": (r.get("scenario_set") or {}).get("name"),
                          "analysis": (r.get("analysis") or {}).get("mode", "single"),
                          "llm": r["llm"], "created_at": r["created_at"],
                          "retries": max(0, history.count("proposed") - 1), "usage": usage})
        decided = [i for i in items if i["status"] in ("applied", "rejected")]
        stats = {
            "runs": len(items),
            "by_status": {s: sum(1 for i in items if i["status"] == s) for s in {i["status"] for i in items}},
            "approval_rate": (sum(1 for i in decided if i["status"] == "applied") / len(decided)) if decided else None,
            "retry_rate": (sum(1 for i in items if i["retries"]) / len(items)) if items else None,
            "usage": _sum(usages),
            "node_avg_seconds": {n: sum(v) / len(v) for n, v in node_seconds.items()},
        }
        return {"runs": items, "stats": stats}

    @app.get("/api/scenarios")
    def scenarios():
        return {"names": sorted(p.stem for p in scenarios_dir.glob("*.yaml")),
                "default": settings["scenario_set"], "analysis_default": settings.get("analysis_mode", "single")}

    # --- 실행 상세·체크포인트 ---------------------------------------------------------

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        run = load(run_id)
        directory = store.run_dir(run_id)
        stages = {}
        for name in sorted(p.name for p in directory.glob("*.json")):
            if name in ("run.json", "decisions.json"):
                continue
            stages[name.removesuffix(".json")] = store.read(run_id, name)
        nxt = graph.pending(store, run_id) if run.get("orchestrator") == "langgraph" else ()
        interrupted = None
        if nxt == ("apply",):
            with graph.compiled(graph.Deps(store)) as g:
                interrupted = g.get_state(graph._config(run_id)).values.get("decision")
        return to_jsonable({"run": run, "summary": summary(store, run_id), "stages": stages,
                            "timings": store.timings(run_id), "usage": _usage_of(store, run_id, run),
                            "pending": list(nxt), "interrupted_decision": interrupted,
                            "running": run_id in threads and threads[run_id].is_alive(),
                            "terminal": run["status"] in TERMINAL})

    @app.get("/api/runs/{run_id}/checkpoints")
    def checkpoints(run_id: str):
        load(run_id)
        with graph.compiled(graph.Deps(store)) as g:
            history = list(g.get_state_history(graph._config(run_id)))
        out = []
        for snap in reversed(history):
            out.append({
                "step": snap.metadata.get("step"),
                "source": snap.metadata.get("source"),
                "created_at": snap.created_at,
                "checkpoint_id": snap.config["configurable"]["checkpoint_id"],
                "next": list(snap.next),
                "values": snap.values,
                "tasks": [{"name": t.name, "id": t.id, "error": str(t.error) if t.error else None,
                           "result": t.result, "interrupts": [i.value for i in t.interrupts]}
                          for t in snap.tasks],
            })
        return to_jsonable({"checkpoints": out, "reducers": ["perspectives"]})

    # --- 사람의 결정 (승인·반려 = interrupt 재개) ---------------------------------------

    def decide(fn, run_id: str, body: DecisionRequest, **kw):
        load(run_id)
        if not body.approver.strip():
            raise HTTPException(400, "승인자 이름을 입력해야 한다")
        try:
            run = fn(store, run_id, note=body.note, by=body.approver.strip(), **kw)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id, "status": run["status"]}

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, body: DecisionRequest):
        return decide(graph.approve, run_id, body, proposal_id=body.proposal_id)

    @app.post("/api/runs/{run_id}/reject")
    def reject(run_id: str, body: DecisionRequest):
        return decide(graph.reject, run_id, body)

    # --- 리허설 실행 시작 -------------------------------------------------------------

    @app.post("/api/runs")
    def start(body: StartRequest):
        if body.analysis not in ("single", "multi"):
            raise HTTPException(400, f"알 수 없는 분석 방식: {body.analysis}")
        try:
            scenario_set = load_scenario_set(body.scenario or settings["scenario_set"], scenarios_dir)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        perspectives = (multi_analysis.load_perspectives(settings_dir / "analysis.yaml")
                        if body.analysis == "multi" else None)
        llm, llm_config = rehearsal_llm_factory()
        run, go = graph.prepare_run(store, registry(), scenario_set=scenario_set, criteria=settings["criteria"],
                                    llm=llm, llm_config=llm_config, limits=settings["limits"], rehearsal=True,
                                    analysis_mode=body.analysis, perspectives=perspectives)
        thread = threading.Thread(target=go, name=f"run-{run['run_id']}", daemon=True)
        threads[run["run_id"]] = thread
        thread.start()
        return {"run_id": run["run_id"]}

    # --- 모델 레지스트리 --------------------------------------------------------------

    @app.get("/api/models")
    def models():
        reg = registry()
        return {"engine": reg.engine, "champion": reg.champion(),
                "versions": [reg.card(v) for v in reversed(reg.versions())], "history": reg.history()}

    # --- 정적 파일 --------------------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.state.threads = threads
    return app
