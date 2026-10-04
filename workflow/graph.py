"""X1: LangGraph 오케스트레이션. 같은 5단계(workflow.stages)를 그래프로 돌린다.

- LangGraph는 실행 제어(체크포인트·interrupt·조건부 엣지)에만 쓴다. 노드 안에서는 stages(=코어 함수)를 그대로 부른다.
- 단계 결과의 기준은 지금처럼 runs/<run_id>/ 의 JSON 파일이다. 그래프 상태에는 요약과 파일 이름만 둔다.
  인스턴스는 엔진·seed·결함으로 재생성하고, 결정 레코드는 decisions.json에서 읽는다.
- 체크포인트는 runs/checkpoints.sqlite (thread_id = run_id). 승인 대기는 interrupt로 멈추고,
  approve/reject가 다른 프로세스에서 Command(resume=...)로 같은 실행을 재개한다.
- M2: 챔피언은 모델 레지스트리(modelreg)의 버전이다. 1단계는 학습용 세트 전체, 4단계는 학습용·검증용 세트 전체에서
  비교하고 판정 기준(settings/workflow.yaml criteria)을 적용한다. 승인하면 새 버전을 등록하고 챔피언으로 지정한다.
"""

import getpass
import sqlite3
import time
import traceback
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, TypedDict

from core import DecisionRecord
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from engines import Engine, get_engine
from modelreg import Registry

from . import multi_analysis, stages
from .locks import file_lock
from .machine import STAGE_FILES, transition
from .records import engine_of, new_run
from .store import RunStore

CHECKPOINT_DB = "checkpoints.sqlite"
CHAMPION_FILE = "champion_params.json"     # 실행 시작 시점의 챔피언 params (재개해도 같은 기준으로 평가)


def perspective_file(perspective_id: str) -> str:
    return f"2_analyze.{perspective_id}.json"


def _merge(left: dict | None, right: dict | None) -> dict:
    """병렬 관점 노드의 결과를 합치는 리듀서."""
    return {**(left or {}), **(right or {})}


class WorkflowState(TypedDict, total=False):
    run_id: str
    status: str
    engine: str
    model_version: str
    scenario_set: dict          # M1·X1: {seed, faults}. 학습용·검증용 세트는 M2
    execution: dict             # 지표 요약 + 결정 레코드 파일
    analysis: dict              # 발견 id·제목 + 리포트 파일
    proposals: dict             # 개선안 id·허용 범위 검사 결과 + 파일
    validation: dict            # 승인 후보 + 파일
    decision: dict              # 사람(또는 워크플로우)의 승인·반려
    analysis_mode: str          # single | multi (X2)
    perspectives: Annotated[dict, _merge]   # 관점 id → 요약 (병렬 노드가 각자 씀)
    retry_count: int
    max_retries: int
    errors: list


class Deps:
    """그래프 노드가 쓰는 실행 자원. 상태(체크포인트)에 넣지 않는다."""

    def __init__(self, store: RunStore, llm=None, llm_config: dict | None = None):
        self.store = store
        self.llm = llm
        self.llm_config = llm_config or {}
        self._cache: dict[str, tuple] = {}
        self._lock = threading.Lock()          # 관점 노드가 병렬로 instance를 찾을 때

    def run(self, run_id: str) -> dict:
        return self.store.load(run_id)

    def engine(self, run_id: str) -> Engine:
        return engine_of(self.run(run_id))

    def champion(self, run_id: str) -> dict:
        return self.store.read(run_id, CHAMPION_FILE)

    def instance(self, run_id: str):
        """시나리오 인스턴스 (결정적 재생성, 프로세스 안에서만 캐시)."""
        with self._lock:
            if run_id not in self._cache:
                run = self.run(run_id)
                pack = self.engine(run_id).pack_factory(self.champion(run_id))
                self._cache[run_id] = pack.generate(run["scenario"]["seed"], run["scenario"]["faults"])
            return self._cache[run_id][0]

    def decisions(self, run_id: str) -> list[DecisionRecord]:
        return [DecisionRecord(**d) for d in self.store.read(run_id, "decisions.json")]

    def advance(self, run_id: str, status: str, note: str = "") -> str:
        run = self.run(run_id)
        transition(run, status, note)
        self.store.save(run)
        return status


def _guarded(deps: Deps, fn):
    """노드 실패를 failed(사유 기록)로 바꾼다. 그래프는 failed면 END로 간다."""
    def node(state: WorkflowState) -> dict:
        try:
            return fn(state)
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 사유를 남긴다
            run = deps.run(state["run_id"])
            run["error"] = {"type": type(exc).__name__, "message": str(exc),
                            "traceback": traceback.format_exc(limit=8)}
            transition(run, "failed", str(exc))
            deps.store.save(run)
            return {"status": "failed", "errors": [*state.get("errors", []), {"type": type(exc).__name__,
                                                                               "message": str(exc)}]}
    node.__name__ = fn.__name__
    return node


def build_graph(deps: Deps):
    store = deps.store

    # --- 1~4단계 ------------------------------------------------------------------

    def execute(state: WorkflowState) -> dict:
        """학습용 세트 전체 실행. 결정 레코드는 분석에 쓰는 첫 시나리오(primary)의 것만 저장한다."""
        run_id = state["run_id"]
        engine, params = deps.engine(run_id), deps.champion(run_id)
        instance, decisions, executed = stages.execute_set(engine, params, deps.run(run_id)["scenario_set"])
        with deps._lock:
            deps._cache[run_id] = (instance, None)
        store.write(run_id, "decisions.json", decisions)
        store.write(run_id, STAGE_FILES["1_execute"], {**executed, "decisions_file": "decisions.json"})
        return {"execution": {"metrics": executed["metrics"], "mean_metrics": executed["mean_metrics"],
                              "violations": executed["violations_total"],
                              "decisions_file": "decisions.json", "file": STAGE_FILES["1_execute"]}}

    def analyze(state: WorkflowState) -> dict:
        run_id = state["run_id"]
        report = stages.analyze(deps.engine(run_id), deps.champion(run_id), deps.instance(run_id),
                                deps.decisions(run_id), deps.llm, deps.llm_config,
                                deps.run(run_id)["limits"]["analyze_max_llm_calls"])
        store.write(run_id, STAGE_FILES["2_analyze"], report)
        status = deps.advance(run_id, "analyzed", f"발견 {len(report['findings'])}건")
        return {"status": status, "analysis": {"findings": [f"{f['id']} {f['title']}" for f in report["findings"]],
                                                "file": STAGE_FILES["2_analyze"]}}

    # --- 2단계 멀티에이전트 (X2): 관점 노드 N개 병렬 → 종합 ---------------------------

    def perspective(task: dict) -> dict:
        """Send로 관점마다 한 번씩 병렬 실행된다. 실패해도 실행을 failed로 만들지 않고 결과에 남긴다 (종합이 판단)."""
        run_id, p = task["run_id"], task["perspective"]
        try:
            engine, params = deps.engine(run_id), deps.champion(run_id)
            result = multi_analysis.run_perspective(engine.pack_factory(params), deps.instance(run_id),
                                                    deps.decisions(run_id), p, deps.llm, deps.llm_config,
                                                    deps.run(run_id)["limits"]["perspective_max_llm_calls"])
        except Exception as exc:  # noqa: BLE001
            result = {"perspective": p["id"], "name": p["name"], "error": f"{type(exc).__name__}: {exc}"}
        store.write(run_id, perspective_file(p["id"]), result)
        return {"perspectives": {p["id"]: {"file": perspective_file(p["id"]), "error": result.get("error"),
                                           "findings": len(result.get("findings", []))}}}

    def synthesize(state: WorkflowState) -> dict:
        run_id = state["run_id"]
        results = {pid: store.read(run_id, info["file"]) for pid, info in state["perspectives"].items()}
        engine, params = deps.engine(run_id), deps.champion(run_id)
        report = stages.check_report(multi_analysis.synthesize(
            engine.pack_factory(params), deps.instance(run_id), deps.decisions(run_id), results, deps.llm,
            deps.llm_config, deps.run(run_id)["limits"]["synthesis_max_llm_calls"]))
        store.write(run_id, STAGE_FILES["2_analyze"], report)
        failed = [pid for pid, r in results.items() if "error" in r]
        note = f"발견 {len(report['findings'])}건 (관점 {len(results) - len(failed)}/{len(results)})" + (
            f", 실패 관점 {failed}" if failed else "")
        status = deps.advance(run_id, "analyzed", note)
        return {"status": status, "analysis": {"findings": [f"{f['id']} {f['title']}" for f in report["findings"]],
                                                "file": STAGE_FILES["2_analyze"], "mode": "multi"}}

    def propose(state: WorkflowState) -> dict:
        run_id, attempt = state["run_id"], state.get("retry_count", 0)
        report = store.read(run_id, STAGE_FILES["2_analyze"])
        feedback = []
        for n in range(attempt):                 # 앞선 모든 시도의 탈락 이유
            feedback += stages.rejection_feedback(store.read(run_id, f"3_propose.attempt{n}.json"),
                                                  store.read(run_id, f"4_validate.attempt{n}.json"))
        proposed = stages.propose(deps.engine(run_id), deps.champion(run_id), deps.instance(run_id), report,
                                  deps.llm, deps.llm_config, deps.run(run_id)["limits"]["propose_max_llm_calls"],
                                  salt=f"retry-{attempt}" if attempt else "", feedback=feedback or None)
        store.write(run_id, STAGE_FILES["3_propose"], proposed)
        valid = sum(1 for p in proposed["proposals"] if not p["errors"])
        note = f"개선안 {len(proposed['proposals'])}건, 허용 범위 통과 {valid}건" + (f" (재시도 {attempt}회차)"
                                                                            if attempt else "")
        status = deps.advance(run_id, "proposed", note)
        return {"status": status, "proposals": {
            "items": [{"id": p["id"], "title": p["proposal"].get("title"), "errors": p["errors"]}
                      for p in proposed["proposals"]], "file": STAGE_FILES["3_propose"]}}

    def validate(state: WorkflowState) -> dict:
        run_id = state["run_id"]
        run = deps.run(run_id)
        validated = stages.validate(deps.engine(run_id), deps.champion(run_id),
                                    store.read(run_id, STAGE_FILES["2_analyze"]),
                                    store.read(run_id, STAGE_FILES["3_propose"]),
                                    run["scenario_set"], run["criteria"], run["limits"].get("validation_time_budget_s"))
        store.write(run_id, STAGE_FILES["4_validate"], validated)
        status = deps.advance(run_id, "validated", f"승인 후보 {len(validated['eligible'])}건")
        return {"status": status, "validation": {"eligible": validated["eligible"], "file": STAGE_FILES["4_validate"]}}

    # --- 재시도·반려·승인 대기 --------------------------------------------------------

    def retry(state: WorkflowState) -> dict:
        """탈락한 시도의 3·4단계 파일을 보관하고 3단계로 되돌린다."""
        run_id, attempt = state["run_id"], state.get("retry_count", 0)
        directory = store.run_dir(run_id)
        for name in ("3_propose", "4_validate"):
            (directory / STAGE_FILES[name]).replace(directory / f"{name}.attempt{attempt}.json")
        return {"retry_count": attempt + 1}

    def auto_reject(state: WorkflowState) -> dict:
        run_id = state["run_id"]
        retries = state.get("retry_count", 0)
        note = "판정을 통과한 후보가 없음" + (f" (재시도 {retries}회 소진)" if retries else "")
        decision = {"decision": "rejected", "by": "workflow", "note": note, "at": time.time()}
        store.write(run_id, STAGE_FILES["5_apply"], decision)
        return {"status": deps.advance(run_id, "rejected", note), "decision": decision}

    def await_approval(state: WorkflowState) -> dict:
        return {"status": deps.advance(state["run_id"], "awaiting_approval", "사람의 approve 또는 reject를 기다림")}

    def approval(state: WorkflowState) -> dict:
        # 재개 시 이 노드는 처음부터 다시 실행된다. interrupt 앞에는 부수효과를 두지 않는다.
        answer = interrupt({"run_id": state["run_id"], "eligible": state["validation"]["eligible"],
                            "model_version": state["model_version"]})
        return {"decision": answer}

    # --- 5단계 -----------------------------------------------------------------

    def apply(state: WorkflowState) -> dict:
        """승인된 도전자를 레지스트리에 새 버전으로 등록하고 챔피언으로 지정한다 (레지스트리 잠금 안에서).

        멱등: 반영 후 체크포인트 전에 프로세스가 죽어 이 노드가 다시 돌면, 이 실행이 등록한 버전을 찾아
        다시 등록하지 않고 챔피언 지정만 마무리한다.
        """
        run_id, decision = state["run_id"], state["decision"]
        if decision["proposal_id"] not in state["validation"]["eligible"]:
            raise stages.StageError(f"검증을 통과한 후보가 아님: {decision['proposal_id']}")
        run = deps.run(run_id)
        proposal = next(p["proposal"] for p in store.read(run_id, STAGE_FILES["3_propose"])["proposals"]
                        if p["id"] == decision["proposal_id"])
        candidate = next(c for c in store.read(run_id, STAGE_FILES["4_validate"])["candidates"]
                         if c["id"] == decision["proposal_id"])
        registry = registry_of(run)
        with registry.lock():
            version = registry.find_by_run(run_id)
            recovered = version is not None
            if not recovered:
                if registry.champion() != run["registry"]["champion_version"]:
                    raise stages.StageError(f"실행 이후 챔피언이 v{run['registry']['champion_version']}에서 "
                                            f"v{registry.champion()}로 바뀌었음. 다시 run 하라")
                version = registry._register(run["registry"]["champion_version"], proposal, {
                    "source": {"kind": "workflow", "run_id": run_id, "proposal_id": decision["proposal_id"],
                               "title": proposal.get("title"), "scenario_set": run["scenario_set"]["name"],
                               "analysis_mode": (run.get("analysis") or {}).get("mode", "single")},
                    "validation": {"judgement": candidate["judgement"],
                                   "sets": {k: s["summary"] for k, s in candidate["sets"].items()},
                                   "criteria": run["criteria"]},
                    "approval": {k: decision.get(k) for k in ("by", "note", "at")}})
            if registry.champion() != version:
                registry._set_champion(version, by=decision["by"], note=f"run {run_id} 승인 ({decision['proposal_id']})",
                                       action="promote")
        before = run["model_version"]
        after = registry.card(version)["model_version"]
        applied = {"model_before": before, "model_after": after, "version": version,
                   "params_path": str(registry.params_path(version)), "recovered": recovered}
        store.write(run_id, STAGE_FILES["5_apply"], {**decision, **applied, "state": "applied"})
        return {"status": deps.advance(run_id, "applied", f"{before} → {after}")}

    def reject(state: WorkflowState) -> dict:
        run_id, decision = state["run_id"], state["decision"]
        store.write(run_id, STAGE_FILES["5_apply"], decision)
        return {"status": deps.advance(run_id, "rejected", decision.get("note") or "사람이 반려")}

    # --- 그래프 -------------------------------------------------------------------

    def route_analysis(s: WorkflowState):
        if s.get("status") == "failed":
            return END
        if s.get("analysis_mode") != "multi":
            return "analyze"
        run_id = s["run_id"]
        return [Send("perspective", {"run_id": run_id, "perspective": p})
                for p in deps.run(run_id)["analysis"]["perspectives"]]

    def ok(next_node: str):
        return lambda s: END if s.get("status") == "failed" else next_node

    def after_validate(s: WorkflowState) -> str:
        if s.get("status") == "failed":
            return END
        if s["validation"]["eligible"]:
            return "await_approval"
        return "retry" if s.get("retry_count", 0) < s.get("max_retries", 0) else "auto_reject"

    def after_approval(s: WorkflowState) -> str:
        return "apply" if s["decision"]["decision"] == "approved" else "reject"

    g = StateGraph(WorkflowState)
    for fn in (execute, analyze, synthesize, propose, validate, retry, auto_reject, await_approval, apply, reject):
        g.add_node(fn.__name__, _guarded(deps, fn))
    g.add_node("perspective", perspective)      # 실패를 스스로 기록한다 (병렬이라 상태 전이는 종합 노드만)
    g.add_node("approval", approval)            # interrupt만 한다
    g.add_edge(START, "execute")
    g.add_conditional_edges("execute", route_analysis, ["analyze", "perspective", END])
    g.add_edge("perspective", "synthesize")     # 모든 관점이 끝난 뒤 한 번 실행된다
    g.add_conditional_edges("analyze", ok("propose"), ["propose", END])
    g.add_conditional_edges("synthesize", ok("propose"), ["propose", END])
    g.add_conditional_edges("propose", ok("validate"), ["validate", END])
    g.add_conditional_edges("validate", after_validate, ["await_approval", "retry", "auto_reject", END])
    g.add_conditional_edges("retry", ok("propose"), ["propose", END])
    g.add_edge("auto_reject", END)
    g.add_conditional_edges("await_approval", ok("approval"), ["approval", END])
    g.add_conditional_edges("approval", after_approval, ["apply", "reject"])
    g.add_edge("apply", END)
    g.add_edge("reject", END)
    return g


@contextmanager
def compiled(deps: Deps):
    """runs/checkpoints.sqlite 체크포인트를 붙인 그래프."""
    deps.store.root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(Path(deps.store.root) / CHECKPOINT_DB, check_same_thread=False)
    try:
        yield build_graph(deps).compile(checkpointer=SqliteSaver(conn))
    finally:
        conn.close()


def _config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


def mermaid() -> str:
    return build_graph(Deps(RunStore("."))).compile().get_graph().draw_mermaid()


# --- 공개 함수 (CLI) ------------------------------------------------------------------

def registry_of(run: dict) -> Registry:
    if not run.get("registry"):
        raise ValueError(f"레지스트리 없이 만든 실행은 승인할 수 없음: {run['run_id']}")
    return Registry(run["registry"]["root"], run["engine"])


def run_workflow(store: RunStore, registry: Registry, *, scenario_set: dict, criteria: dict, llm, llm_config: dict,
                 limits: dict, rehearsal: bool, analysis_mode: str = "single",
                 perspectives: list[dict] | None = None, stop_before: list[str] | None = None) -> dict:
    """레지스트리의 챔피언으로 1~4단계를 돌고 승인 대기(interrupt)에서 멈춘다. 판정을 통과한 후보가 없으면
    재시도 후 자동 반려(rejected), 노드 실패 시 failed로 끝난다. 최종 run(run.json)을 돌려준다.

    레지스트리가 비어 있으면 엔진 기본 params를 첫 버전으로 등록한다.
    analysis_mode="multi"면 perspectives(관점 정의)로 멀티에이전트 분석을 한다. 정의는 run.json에 복사된다.
    stop_before: 이 노드들 앞에서 멈춘다 (분석 방식 비교 평가처럼 일부 단계만 돌릴 때).
    """
    if analysis_mode not in ("single", "multi"):
        raise ValueError(f"알 수 없는 분석 방식: {analysis_mode}")
    if analysis_mode == "multi" and not perspectives:
        raise ValueError("멀티에이전트 분석에는 관점 정의가 필요하다")
    champion = registry.bootstrap(get_engine(registry.engine).params_path, by=getpass.getuser())
    engine = get_engine(registry.engine, registry.params_path(champion))
    run = new_run(store, engine, scenario_set=scenario_set, llm_model=llm.model, llm_config=llm_config,
                  limits=limits, rehearsal=rehearsal, extra={
                      "registry": {"root": str(registry.root.resolve()), "champion_version": champion},
                      "criteria": criteria,
                      "analysis": {"mode": analysis_mode,
                                   **({"perspectives": perspectives} if analysis_mode == "multi" else {})}})
    store.write(run["run_id"], CHAMPION_FILE, engine.load_params())
    deps = Deps(store, llm, llm_config)
    state: WorkflowState = {"run_id": run["run_id"], "status": "running", "engine": engine.name,
                            "model_version": run["model_version"],
                            "scenario_set": {"name": scenario_set["name"], "primary": run["scenario"]},
                            "analysis_mode": analysis_mode, "perspectives": {},
                            "retry_count": 0, "max_retries": int(limits.get("max_retries", 0)), "errors": []}
    with compiled(deps) as graph:
        graph.invoke(state, _config(run["run_id"]), interrupt_before=stop_before)
    return store.load(run["run_id"])


def pending(store: RunStore, run_id: str) -> tuple[str, ...]:
    """체크포인트 기준으로 다음에 실행될 노드 (승인 대기면 ('approval',))."""
    with compiled(Deps(store)) as graph:
        return tuple(graph.get_state(_config(run_id)).next)


def _resume(store: RunStore, run_id: str, decision: dict) -> dict:
    """승인 대기(interrupt)를 결정으로 재개한다. 반영 도중 중단돼 체크포인트가 apply 앞이면 그 노드를 이어서 돌린다
    (이때 결정은 이미 체크포인트에 있으므로 새 결정은 쓰지 않는다)."""
    nxt = pending(store, run_id)
    if nxt == ("approval",):
        command = Command(resume=decision)
    elif nxt == ("apply",) and decision["decision"] == "approved":
        with compiled(Deps(store)) as graph:
            saved = graph.get_state(_config(run_id)).values["decision"]
        if saved["proposal_id"] != decision["proposal_id"]:
            raise ValueError(f"중단된 반영은 {saved['proposal_id']} 승인이었음. 같은 후보로 approve 하라")
        command = None
    else:
        raise ValueError(f"체크포인트가 승인 대기 지점이 아님: {run_id} (다음 노드 {nxt})")
    with compiled(Deps(store)) as graph:
        graph.invoke(command, _config(run_id))
    return store.load(run_id)


def _awaiting(store: RunStore, run_id: str) -> dict:
    run = store.load(run_id)
    if run["status"] != "awaiting_approval":
        raise ValueError(f"승인 대기 상태가 아님: {run_id} ({run['status']})")
    return run


def _decision_lock(store: RunStore, run_id: str):
    """같은 실행의 approve/reject를 한 번에 하나만 재개한다."""
    return file_lock(store.run_dir(run_id) / ".decision.lock", f"실행 {run_id}의 승인·반려")


def approve(store: RunStore, run_id: str, proposal_id: str | None = None, note: str = "") -> dict:
    with _decision_lock(store, run_id):
        run = _awaiting(store, run_id)
        eligible = store.read(run_id, STAGE_FILES["4_validate"])["eligible"]
        if proposal_id is None:
            if len(eligible) != 1:
                raise ValueError(f"승인 후보가 {len(eligible)}건이므로 --proposal로 지정해야 함: {eligible}")
            proposal_id = eligible[0]
        if proposal_id not in eligible:
            raise ValueError(f"검증을 통과한 후보가 아님: {proposal_id} (후보: {eligible})")
        registry = registry_of(run)
        interrupted = registry.find_by_run(run_id) is not None
        if not interrupted and registry.champion() != run["registry"]["champion_version"]:
            raise ValueError(f"실행 이후 챔피언이 v{run['registry']['champion_version']}에서 v{registry.champion()}로 "
                             "바뀌었으므로 승인할 수 없음. 다시 run 하라")
        # 반영 도중 중단된 실행은 다시 approve하면 apply 노드가 이어서 돌며, 이미 등록된 버전이면 다시 등록하지 않는다
        return _resume(store, run_id, {"decision": "approved", "by": getpass.getuser(), "note": note,
                                       "proposal_id": proposal_id, "at": time.time()})


def reject(store: RunStore, run_id: str, note: str = "") -> dict:
    with _decision_lock(store, run_id):
        run = _awaiting(store, run_id)
        if run.get("registry") and registry_of(run).find_by_run(run_id) is not None:
            raise ValueError(f"반영 도중 중단된 실행이므로 반려할 수 없음. approve {run_id}로 마무리하라")
        return _resume(store, run_id, {"decision": "rejected", "by": getpass.getuser(), "note": note,
                                       "at": time.time()})
