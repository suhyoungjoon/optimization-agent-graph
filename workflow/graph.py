"""X1: LangGraph 오케스트레이션. 같은 5단계(workflow.stages)를 그래프로 돌린다.

- LangGraph는 실행 제어(체크포인트·interrupt·조건부 엣지)에만 쓴다. 노드 안에서는 stages(=코어 함수)를 그대로 부른다.
- 단계 결과의 기준은 지금처럼 runs/<run_id>/ 의 JSON 파일이다. 그래프 상태에는 요약과 파일 이름만 둔다.
  인스턴스는 엔진·seed·결함으로 재생성하고, 결정 레코드는 decisions.json에서 읽는다.
- 체크포인트는 runs/checkpoints.sqlite (thread_id = run_id). 승인 대기는 interrupt로 멈추고,
  approve/reject가 다른 프로세스에서 Command(resume=...)로 같은 실행을 재개한다.
"""

import getpass
import sqlite3
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

from core import DecisionRecord
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from engines import Engine

from . import stages
from .machine import STAGE_FILES, transition
from .locks import file_lock
from .runner import engine_of, new_run
from .stages import params_digest
from .store import RunStore

CHECKPOINT_DB = "checkpoints.sqlite"
CHAMPION_FILE = "champion_params.json"     # 실행 시작 시점의 챔피언 params (재개해도 같은 기준으로 평가)


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

    def run(self, run_id: str) -> dict:
        return self.store.load(run_id)

    def engine(self, run_id: str) -> Engine:
        return engine_of(self.run(run_id))

    def champion(self, run_id: str) -> dict:
        return self.store.read(run_id, CHAMPION_FILE)

    def instance(self, run_id: str):
        """시나리오 인스턴스 (결정적 재생성, 프로세스 안에서만 캐시)."""
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

    # --- 1~4단계: M1과 같은 stages 함수, 같은 파일 ---------------------------------

    def execute(state: WorkflowState) -> dict:
        run_id = state["run_id"]
        engine, params = deps.engine(run_id), deps.champion(run_id)
        run = deps.run(run_id)
        instance, decisions, executed = stages.execute(engine, params, run["scenario"]["seed"],
                                                       run["scenario"]["faults"])
        deps._cache[run_id] = (instance, None)
        store.write(run_id, "decisions.json", decisions)
        store.write(run_id, STAGE_FILES["1_execute"], {**executed, "decisions_file": "decisions.json"})
        return {"execution": {"metrics": executed["metrics"], "violations": executed["violations"],
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

    def propose(state: WorkflowState) -> dict:
        run_id, attempt = state["run_id"], state.get("retry_count", 0)
        report = store.read(run_id, STAGE_FILES["2_analyze"])
        proposed = stages.propose(deps.engine(run_id), deps.champion(run_id), deps.instance(run_id), report,
                                  deps.llm, deps.llm_config, deps.run(run_id)["limits"]["propose_max_llm_calls"],
                                  salt=f"retry-{attempt}" if attempt else "")
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
        validated = stages.validate(deps.engine(run_id), deps.champion(run_id), deps.instance(run_id),
                                    store.read(run_id, STAGE_FILES["2_analyze"]),
                                    store.read(run_id, STAGE_FILES["3_propose"]))
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
        note = "검증을 통과한 후보가 없음" + (f" (재시도 {retries}회 소진)" if retries else "")
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
        """반영 전에 'applying' 표시를 남긴다. 반영 후 체크포인트 전에 프로세스가 죽어 이 노드가 다시 돌면,
        파일이 이미 기대한 결과와 같은지 확인하고 다시 쓰지 않는다 (version이 두 번 오르지 않게)."""
        run_id, decision = state["run_id"], state["decision"]
        if decision["proposal_id"] not in state["validation"]["eligible"]:
            raise stages.StageError(f"검증을 통과한 후보가 아님: {decision['proposal_id']}")
        proposal = next(p["proposal"] for p in store.read(run_id, STAGE_FILES["3_propose"])["proposals"]
                        if p["id"] == decision["proposal_id"])
        engine, champion, run = deps.engine(run_id), deps.champion(run_id), deps.run(run_id)
        marker = store.read(run_id, STAGE_FILES["5_apply"]) if store.has(run_id, STAGE_FILES["5_apply"]) else {}
        if marker.get("state") == "applying" and engine.load_params() == stages.expected_after(champion, proposal):
            applied = {**stages.applied_record(engine, int(champion["version"]), engine.load_params()),
                       "recovered": True}
        else:
            store.write(run_id, STAGE_FILES["5_apply"], {**decision, "state": "applying"})
            try:
                applied = stages.apply(engine, proposal, run["params_sha256"])
            except Exception as exc:
                store.write(run_id, STAGE_FILES["5_apply"], {**decision, "state": "error", "error": str(exc)})
                raise
        store.write(run_id, STAGE_FILES["5_apply"], {**decision, **applied, "state": "applied"})
        return {"status": deps.advance(run_id, "applied", f"{applied['model_before']} → {applied['model_after']}")}

    def reject(state: WorkflowState) -> dict:
        run_id, decision = state["run_id"], state["decision"]
        store.write(run_id, STAGE_FILES["5_apply"], decision)
        return {"status": deps.advance(run_id, "rejected", decision.get("note") or "사람이 반려")}

    # --- 그래프 -------------------------------------------------------------------

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
    for fn in (execute, analyze, propose, validate, retry, auto_reject, await_approval, approval, apply, reject):
        g.add_node(fn.__name__, _guarded(deps, fn) if fn is not approval else fn)
    g.add_edge(START, "execute")
    g.add_conditional_edges("execute", ok("analyze"), ["analyze", END])
    g.add_conditional_edges("analyze", ok("propose"), ["propose", END])
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

def run_workflow(store: RunStore, engine: Engine, *, seed: int, faults: list[str], llm, llm_config: dict,
                 limits: dict, rehearsal: bool) -> dict:
    """1~4단계를 돌고 승인 대기(interrupt)에서 멈춘다. 승인 후보가 없으면 재시도 후 자동 반려(rejected),
    노드 실패 시 failed로 끝난다. 최종 run(run.json)을 돌려준다."""
    run = new_run(store, engine, seed=seed, faults=faults, llm_model=llm.model, llm_config=llm_config,
                  limits=limits, rehearsal=rehearsal, orchestrator="langgraph")
    store.write(run["run_id"], CHAMPION_FILE, engine.load_params())
    deps = Deps(store, llm, llm_config)
    state: WorkflowState = {"run_id": run["run_id"], "status": "running", "engine": engine.name,
                            "model_version": run["model_version"], "scenario_set": run["scenario"],
                            "retry_count": 0, "max_retries": int(limits.get("max_retries", 0)), "errors": []}
    with compiled(deps) as graph:
        graph.invoke(state, _config(run["run_id"]))
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
        interrupted = (store.has(run_id, STAGE_FILES["5_apply"])
                       and store.read(run_id, STAGE_FILES["5_apply"]).get("state") == "applying")
        if not interrupted and params_digest(engine_of(run)) != run["params_sha256"]:
            raise ValueError("실행 이후 챔피언 params 파일이 바뀌었으므로 승인할 수 없음. 다시 run 하라: "
                             + run["params_path"])
        # 반영 도중 중단된 실행은 다시 approve하면 apply 노드가 이어서 돌며, 이미 반영된 파일이면 다시 쓰지 않는다
        return _resume(store, run_id, {"decision": "approved", "by": getpass.getuser(), "note": note,
                                       "proposal_id": proposal_id, "at": time.time()})


def reject(store: RunStore, run_id: str, note: str = "") -> dict:
    with _decision_lock(store, run_id):
        _awaiting(store, run_id)
        if store.has(run_id, STAGE_FILES["5_apply"]) and store.read(run_id, STAGE_FILES["5_apply"]).get("state") == "applying":
            raise ValueError(f"반영 도중 중단된 실행이므로 반려할 수 없음. approve {run_id}로 마무리하라")
        return _resume(store, run_id, {"decision": "rejected", "by": getpass.getuser(), "note": note,
                                       "at": time.time()})
