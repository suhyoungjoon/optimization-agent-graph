"""X1 LangGraph 오케스트레이션: M1과의 동등성, 프로세스를 넘는 재개, 재시도·소진, 실패, Mermaid."""

import itertools
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from core import load_config

import tests.fake_llm as fake_llm
from engines import get_engine
from tests.fake_llm import FakeLLM, tool_use
from tests.rehearsal import GOOD, OUT_OF_BOUNDS, SPEC, rehearsal_llm
from workflow import graph, runner
from workflow.cli import DEFAULT_SETTINGS
from workflow.machine import STAGE_FILES
from workflow.store import RunStore

REPO = Path(__file__).resolve().parent.parent
LIMITS = {"analyze_max_llm_calls": 30, "propose_max_llm_calls": 20, "max_retries": 1}
SEED, FAULTS = 42, ["P1", "P2", "P3", "P4"]
VOLATILE = {"seconds", "at"}       # 실행마다 달라지는 시각·소요 시간


@pytest.fixture
def llm_config():
    return {**load_config(DEFAULT_SETTINGS / "llm.yaml"), "cache": False}


def _params(tmp_path, name):
    path = tmp_path / name
    shutil.copy(get_engine("rule").params_path, path)
    return path


def _strip(value):
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _run(module, store, params_path, llm, llm_config, limits=LIMITS):
    fake_llm._ids = itertools.count()       # 가짜 tool_use id를 실행마다 같게
    return module.run_workflow(store, get_engine("rule", params_path), seed=SEED, faults=FAULTS, llm=llm,
                               llm_config=llm_config, limits=limits, rehearsal=True)


def test_same_stage_results_as_m1_state_machine(tmp_path, llm_config):
    m1_store, x1_store = RunStore(tmp_path / "m1"), RunStore(tmp_path / "x1")
    m1_params, x1_params = _params(tmp_path, "m1.yaml"), _params(tmp_path, "x1.yaml")
    m1 = _run(runner, m1_store, m1_params, rehearsal_llm(), llm_config)
    x1 = _run(graph, x1_store, x1_params, rehearsal_llm(), llm_config)

    assert [h["status"] for h in m1["history"]] == [h["status"] for h in x1["history"]]
    assert x1["status"] == "awaiting_approval" and x1["orchestrator"] == "langgraph"
    for name in ("decisions.json", *(STAGE_FILES[s] for s in ("1_execute", "2_analyze", "3_propose", "4_validate"))):
        assert _strip(m1_store.read(m1["run_id"], name)) == _strip(x1_store.read(x1["run_id"], name)), name

    runner.approve(m1_store, m1["run_id"], note="same")
    graph.approve(x1_store, x1["run_id"], note="same")
    m1_apply = _strip(m1_store.read(m1["run_id"], STAGE_FILES["5_apply"]))
    x1_apply = _strip(x1_store.read(x1["run_id"], STAGE_FILES["5_apply"]))
    assert {k: v for k, v in m1_apply.items() if k != "params_path"} == \
           {k: v for k, v in x1_apply.items() if k != "params_path"}
    assert m1_params.read_text(encoding="utf-8") == x1_params.read_text(encoding="utf-8")
    assert m1_store.load(m1["run_id"])["status"] == x1_store.load(x1["run_id"])["status"] == "applied"


def _cli(*args, cwd=REPO):
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.run([sys.executable, "-m", "workflow", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=300)


def test_resume_from_checkpoint_in_new_process(tmp_path):
    runs, params = tmp_path / "runs", _params(tmp_path, "params.yaml")
    started = _cli("--runs-dir", str(runs), "run", "--rehearsal", "--params", str(params))
    assert started.returncode == 0, started.stderr
    run_id = RunStore(runs).list_runs()[0]["run_id"]
    assert graph.pending(RunStore(runs), run_id) == ("approval",)      # 프로세스가 끝나도 승인 대기 지점이 남는다

    approved = _cli("--runs-dir", str(runs), "approve", run_id, "--note", "다른 프로세스")
    assert approved.returncode == 0, approved.stderr
    assert "rule@v1 → rule@v2" in approved.stdout
    assert yaml.safe_load(params.read_text(encoding="utf-8"))["version"] == 2
    assert graph.pending(RunStore(runs), run_id) == ()                 # 그래프가 끝까지 갔다


def test_retry_then_exhausted_ends_rejected(tmp_path, llm_config):
    store, params = RunStore(tmp_path / "runs"), _params(tmp_path, "params.yaml")
    llm = rehearsal_llm([OUT_OF_BOUNDS, SPEC])                          # 모든 시도가 탈락
    salts = []
    create = llm.create
    llm.create = lambda **kw: (salts.append((kw["tools"][-1]["name"], kw.get("salt", ""))), create(**kw))[1]
    run = _run(graph, store, params, llm, llm_config)

    assert run["status"] == "rejected"
    assert [h["status"] for h in run["history"]] == ["running", "analyzed", "proposed", "validated",
                                                      "proposed", "validated", "rejected"]
    assert store.has(run["run_id"], "3_propose.attempt0.json") and store.has(run["run_id"], "4_validate.attempt0.json")
    decision = store.read(run["run_id"], STAGE_FILES["5_apply"])
    assert decision["by"] == "workflow" and "재시도 1회 소진" in decision["note"]
    # 재시도는 salt를 바꿔 LLM 캐시에서 같은 답이 돌아오지 않게 한다
    assert {s for name, s in salts if name == "submit_proposals"} == {"", "retry-1"}


def test_retry_recovers_when_second_attempt_passes(tmp_path, llm_config):
    store, params = RunStore(tmp_path / "runs"), _params(tmp_path, "params.yaml")
    run = _run(graph, store, params, rehearsal_llm([OUT_OF_BOUNDS], [GOOD]), llm_config)
    assert run["status"] == "awaiting_approval"
    assert store.read(run["run_id"], STAGE_FILES["4_validate"])["eligible"] == ["C1"]
    assert "재시도 1회차" in run["history"][4]["note"]
    graph.approve(store, run["run_id"])
    assert yaml.safe_load(params.read_text(encoding="utf-8"))["version"] == 2


def test_no_retry_when_limit_is_zero(tmp_path, llm_config):
    store, params = RunStore(tmp_path / "runs"), _params(tmp_path, "params.yaml")
    run = _run(graph, store, params, rehearsal_llm([OUT_OF_BOUNDS]), llm_config, {**LIMITS, "max_retries": 0})
    assert run["status"] == "rejected" and not store.has(run["run_id"], "3_propose.attempt0.json")


def test_reject_resumes_and_records(tmp_path, llm_config):
    store, params = RunStore(tmp_path / "runs"), _params(tmp_path, "params.yaml")
    before = params.read_text(encoding="utf-8")
    run_id = _run(graph, store, params, rehearsal_llm(), llm_config)["run_id"]
    run = graph.reject(store, run_id, note="부작용 확인 필요")
    assert run["status"] == "rejected" and params.read_text(encoding="utf-8") == before
    assert store.read(run_id, STAGE_FILES["5_apply"])["note"] == "부작용 확인 필요"
    with pytest.raises(ValueError, match="승인 대기 상태가 아님"):
        graph.approve(store, run_id)


def test_approve_guards(tmp_path, llm_config):
    store, params = RunStore(tmp_path / "runs"), _params(tmp_path, "params.yaml")
    run_id = _run(graph, store, params, rehearsal_llm(), llm_config)["run_id"]
    with pytest.raises(ValueError, match="후보가 아님"):
        graph.approve(store, run_id, "C2")
    params.write_text(params.read_text(encoding="utf-8") + "\n# 사람이 직접 고침\n", encoding="utf-8")
    with pytest.raises(ValueError, match="params 파일이 바뀌었"):
        graph.approve(store, run_id)
    assert store.load(run_id)["status"] == "awaiting_approval" and graph.pending(store, run_id) == ("approval",)


def test_node_failure_ends_failed(tmp_path, llm_config):
    store, params = RunStore(tmp_path / "runs"), _params(tmp_path, "params.yaml")
    silent = FakeLLM(lambda item, n, messages, tools: tool_use("overview", {}))
    run = _run(graph, store, params, silent, llm_config, {**LIMITS, "analyze_max_llm_calls": 3})
    assert run["status"] == "failed" and "max_calls" in run["error"]["message"]
    assert graph.pending(store, run["run_id"]) == ()


def test_mermaid_export(tmp_path):
    text = graph.mermaid()
    for edge in ("validate -.-> retry", "retry -.-> propose", "validate -.-> await_approval",
                 "approval -.-> apply", "approval -.-> reject", "validate -.-> auto_reject"):
        assert edge in text
    out = tmp_path / "graph.mmd"
    assert _cli("graph", "--out", str(out)).returncode == 0 and out.read_text(encoding="utf-8") == text
