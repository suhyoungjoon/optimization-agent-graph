"""LangGraph 워크플로우 (X1 + M2): 승인 대기·재개, 재시도, 판정, 레지스트리 반영, 잠금, 실패, Mermaid."""

import os
import subprocess
import sys

import pytest
import yaml

from modelreg import Registry
from tests.conftest import CRITERIA, FADING, LIMITS, REPO, SMALL, champion_params
from tests.fake_llm import FakeLLM, tool_use
from tests.rehearsal import BOUNDARY_RULE, GOOD, OUT_OF_BOUNDS, SPEC, rehearsal_llm
from workflow import graph
from workflow.machine import STAGE_FILES
from workflow.store import RunStore

def _run(store, registry, llm, llm_config, scenario_set=SMALL, limits=LIMITS, criteria=CRITERIA, **kw):
    return graph.run_workflow(store, registry, scenario_set=scenario_set, criteria=criteria, llm=llm,
                              llm_config=llm_config, limits=limits, rehearsal=True, **kw)


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs")


# --- 실행·검증·승인 대기 ------------------------------------------------------------

def test_run_stops_at_approval_with_judged_candidate(store, registry, llm_config):
    run = _run(store, registry, rehearsal_llm(), llm_config)
    run_id = run["run_id"]
    assert run["status"] == "awaiting_approval"
    assert [h["status"] for h in run["history"]] == ["running", "analyzed", "proposed", "validated",
                                                      "awaiting_approval"]
    # 재현 정보 (원칙 5)
    assert run["model_version"] == "rule@v1" and run["registry"]["champion_version"] == 1
    assert run["scenario_set"] == SMALL and run["scenario"] == SMALL["train"][0] and run["criteria"] == CRITERIA
    assert run["llm"]["model"] == "fake-model" and run["core"]["version"]
    assert registry.versions() == [1] and registry.champion() == 1       # 승인 전에는 레지스트리를 안 바꾼다

    executed = store.read(run_id, STAGE_FILES["1_execute"])
    assert [s["seed"] for s in executed["scenarios"]] == [42] and executed["violations_total"] == 0
    validated = store.read(run_id, STAGE_FILES["4_validate"])
    c1 = validated["candidates"][0]
    assert validated["eligible"] == ["C1"] and [s["id"] for s in validated["skipped"]] == ["C2", "C3"]
    assert [s["seed"] for s in c1["sets"]["train"]["scenarios"]] == [42]
    assert [s["seed"] for s in c1["sets"]["validation"]["scenarios"]] == [101]
    assert c1["judgement"]["passed"] and c1["sets"]["validation"]["summary"]["mean_gain"]["assignment_rate"] > 0.01


def test_default_scenario_set_executes_all_train_seeds(store, registry, llm_config):
    from workflow.scenario_sets import load_scenario_set
    default = load_scenario_set("default", REPO / "scenarios")
    run = _run(store, registry, rehearsal_llm(), llm_config, scenario_set=default)
    executed = store.read(run["run_id"], STAGE_FILES["1_execute"])
    assert [s["seed"] for s in executed["scenarios"]] == [42, 43, 44]
    c1 = store.read(run["run_id"], STAGE_FILES["4_validate"])["candidates"][0]
    assert [s["seed"] for s in c1["sets"]["validation"]["scenarios"]] == [101, 102, 103]
    assert c1["sets"]["validation"]["summary"]["improved"] == 3 and run["status"] == "awaiting_approval"


def test_effect_disappearing_on_validation_is_filtered_then_rejected(store, registry, llm_config):
    """M2 완료 기준: 검증용 세트에서 효과가 사라지는 개선안을 판정이 걸러낸다."""
    llm = rehearsal_llm()
    run = _run(store, registry, llm, llm_config, scenario_set=FADING)
    assert run["status"] == "rejected"
    assert [h["status"] for h in run["history"]] == ["running", "analyzed", "proposed", "validated",
                                                      "proposed", "validated", "rejected"]
    c1 = store.read(run["run_id"], STAGE_FILES["4_validate"])["candidates"][0]
    assert c1["sets"]["train"]["summary"]["mean_gain"]["assignment_rate"] > 0.05   # 학습셋에선 효과가 크다
    assert c1["sets"]["validation"]["summary"]["mean_gain"]["assignment_rate"] == pytest.approx(0.0)
    assert not c1["eligible"] and any("validation 평균" in r for r in c1["reasons"])
    assert "판정을 통과한 후보가 없음" in store.read(run["run_id"], STAGE_FILES["5_apply"])["note"]
    assert registry.versions() == [1]
    # 재시도한 개선 agent는 앞 시도의 탈락 이유를 입력으로 받는다 (코어 propose feedback)
    starts = [c["messages"][0]["content"] for c in llm.calls
              if len(c["messages"]) == 1 and any(t["name"] == "submit_proposals" for t in c["tools"])]
    assert len(starts) == 2 and "이전 시도에서 탈락한 이유" not in starts[0]
    retry_input = starts[1]
    assert "이전 시도에서 탈락한 이유" in retry_input
    assert "C1 경계 지역만 3단계 지역 범위 +1km: 판정 탈락 - validation 평균 assignment_rate 개선" in retry_input
    assert "C2 전역 3단계 지역 범위 대폭 완화: 허용 범위·대상 검사 탈락" in retry_input


def test_validation_time_budget(store, registry, llm_config):
    run = _run(store, registry, rehearsal_llm(), llm_config, limits={**LIMITS, "validation_time_budget_s": 0,
                                                                     "max_retries": 0})
    c1 = store.read(run["run_id"], STAGE_FILES["4_validate"])["candidates"][0]
    assert run["status"] == "rejected" and any("시간 예산" in r for r in c1["reasons"])


# --- 승인 → 레지스트리 --------------------------------------------------------------

def test_approve_registers_new_champion_with_model_card(store, registry, llm_config):
    run_id = _run(store, registry, rehearsal_llm(), llm_config)["run_id"]
    run = graph.approve(store, run_id, note="ok")

    assert run["status"] == "applied" and registry.versions() == [1, 2] and registry.champion() == 2
    applied = store.read(run_id, STAGE_FILES["5_apply"])
    assert (applied["model_before"], applied["model_after"]) == ("rule@v1", "rule@v2") and not applied["recovered"]
    card = registry.card(2)
    assert card["parent"] == 1 and card["source"]["run_id"] == run_id and card["source"]["proposal_id"] == "C1"
    assert card["validation"]["judgement"]["passed"] and card["approval"]["note"] == "ok"
    assert card["validation"]["criteria"] == CRITERIA
    assert champion_params(registry)["overrides"]["rules"] == [BOUNDARY_RULE]
    assert registry.load_params(1)["overrides"]["rules"] == []              # 이전 스냅샷은 그대로

    nxt = _run(store, registry, rehearsal_llm(), llm_config)                # 다음 실행은 새 챔피언으로
    assert nxt["model_version"] == "rule@v2"
    with pytest.raises(ValueError, match="승인 대기 상태가 아님"):
        graph.approve(store, run_id)


def test_approve_refused_when_champion_changed_after_run(store, registry, llm_config):
    first = _run(store, registry, rehearsal_llm(), llm_config)["run_id"]
    second = _run(store, registry, rehearsal_llm(), llm_config)["run_id"]
    graph.approve(store, first)
    with pytest.raises(ValueError, match="챔피언이 v1에서 v2로"):
        graph.approve(store, second)
    assert store.load(second)["status"] == "awaiting_approval"


def test_rollback_then_new_approval(store, registry, llm_config):
    graph.approve(store, _run(store, registry, rehearsal_llm(), llm_config)["run_id"])
    assert registry.rollback(by="test", note="부작용") == (2, 1)
    run_id = _run(store, registry, rehearsal_llm(), llm_config)["run_id"]
    assert store.load(run_id)["model_version"] == "rule@v1"                 # 되돌린 챔피언으로 실행
    graph.approve(store, run_id)
    assert registry.champion() == 3 and registry.card(3)["parent"] == 1     # 번호는 최대+1, 부모는 v1


def test_multiple_candidates_need_explicit_choice(store, registry, llm_config):
    run_id = _run(store, registry, rehearsal_llm([GOOD, {**GOOD, "title": "같은 안 다시"}]), llm_config)["run_id"]
    with pytest.raises(ValueError, match="--proposal"):
        graph.approve(store, run_id)
    with pytest.raises(ValueError, match="후보가 아님"):
        graph.approve(store, run_id, "C9")
    graph.approve(store, run_id, "C2")
    assert registry.card(2)["source"]["proposal_id"] == "C2"


def test_reject_records_only(store, registry, llm_config):
    run_id = _run(store, registry, rehearsal_llm(), llm_config)["run_id"]
    run = graph.reject(store, run_id, note="부작용 확인 필요")
    assert run["status"] == "rejected" and registry.versions() == [1]
    assert store.read(run_id, STAGE_FILES["5_apply"])["note"] == "부작용 확인 필요"


# --- 재개·재시도·실패 ----------------------------------------------------------------

def _cli(*args):
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    # CLI는 UTF-8로 출력한다 (workflow/__main__.py). 읽는 쪽도 로캘 코덱(Windows cp1252)이 아니라 UTF-8로 읽는다
    return subprocess.run([sys.executable, "-m", "workflow", *args], cwd=REPO, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=300)


def test_resume_from_checkpoint_in_new_process(tmp_path, registry):
    runs, scen = tmp_path / "runs", tmp_path / "small.yaml"
    scen.write_text(yaml.safe_dump(SMALL), encoding="utf-8")
    common = ["--runs-dir", str(runs), "--models-dir", str(registry.root)]
    started = _cli(*common, "run", "--rehearsal", "--scenario", str(scen))
    assert started.returncode == 0, started.stderr
    run_id = RunStore(runs).list_runs()[0]["run_id"]
    assert graph.pending(RunStore(runs), run_id) == ("approval",)

    approved = _cli(*common, "approve", run_id, "--note", "다른 프로세스")
    assert approved.returncode == 0, approved.stderr
    assert "rule@v1 → rule@v2" in approved.stdout and Registry(registry.root, "rule").champion() == 2
    assert graph.pending(RunStore(runs), run_id) == ()

    models = _cli(*common, "models")
    assert "* v2" in models.stdout and "v1(bootstrap) → v2(promote)" in models.stdout
    rolled = _cli(*common, "rollback", "--note", "확인")
    assert rolled.returncode == 0 and "v2에서 v1로" in rolled.stdout


def test_retry_recovers_when_second_attempt_passes(store, registry, llm_config):
    run = _run(store, registry, rehearsal_llm([OUT_OF_BOUNDS], [GOOD]), llm_config)
    assert run["status"] == "awaiting_approval" and "재시도 1회차" in run["history"][4]["note"]
    assert store.has(run["run_id"], "3_propose.attempt0.json")


def test_retry_exhausted_with_salt_change(store, registry, llm_config):
    llm = rehearsal_llm([OUT_OF_BOUNDS, SPEC])
    salts = []
    create = llm.create
    llm.create = lambda **kw: (salts.append((kw["tools"][-1]["name"], kw.get("salt", ""))), create(**kw))[1]
    run = _run(store, registry, llm, llm_config)
    assert run["status"] == "rejected" and "재시도 1회 소진" in store.read(run["run_id"], STAGE_FILES["5_apply"])["note"]
    assert {s for name, s in salts if name == "submit_proposals"} == {"", "retry-1"}


def test_node_failure_ends_failed(store, registry, llm_config):
    silent = FakeLLM(lambda item, n, messages, tools: tool_use("overview", {}))
    run = _run(store, registry, silent, llm_config, limits={**LIMITS, "analyze_max_llm_calls": 3})
    assert run["status"] == "failed" and "max_calls" in run["error"]["message"]
    assert graph.pending(store, run["run_id"]) == ()


# --- 반영 안전성 ---------------------------------------------------------------------

class _Crash(BaseException):
    """프로세스가 죽은 것처럼 그래프 밖으로 빠져나간다 (노드의 Exception 처리에 잡히지 않음)."""


def test_crash_after_register_recovers_without_double_registration(store, registry, llm_config, monkeypatch):
    run_id = _run(store, registry, rehearsal_llm(), llm_config)["run_id"]
    real = Registry._set_champion

    def crash(self, *a, **kw):
        raise _Crash()

    monkeypatch.setattr(Registry, "_set_champion", crash)
    with pytest.raises(_Crash):
        graph.approve(store, run_id)
    assert registry.versions() == [1, 2] and registry.champion() == 1       # 등록은 됐고 지정 전에 죽음
    assert graph.pending(store, run_id) == ("apply",)
    with pytest.raises(ValueError, match="반려할 수 없음"):
        graph.reject(store, run_id)

    monkeypatch.setattr(Registry, "_set_champion", real)
    for leftover in registry.dir.glob(".lock"):
        leftover.unlink()                                                   # 죽은 프로세스가 남긴 잠금
    run = graph.approve(store, run_id)
    assert run["status"] == "applied" and registry.versions() == [1, 2] and registry.champion() == 2
    assert store.read(run_id, STAGE_FILES["5_apply"])["recovered"] is True


def test_concurrent_decisions_are_locked(store, registry, llm_config):
    from workflow.locks import LockBusy
    run_id = _run(store, registry, rehearsal_llm(), llm_config)["run_id"]
    lock = store.run_dir(run_id) / ".decision.lock"
    lock.write_text("123")
    with pytest.raises(LockBusy):
        graph.approve(store, run_id)
    lock.unlink()

    (registry.dir / ".lock").write_text("123")                               # 다른 프로세스가 레지스트리를 바꾸는 중
    run = graph.approve(store, run_id)
    assert run["status"] == "failed" and "레지스트리" in run["error"]["message"] and registry.versions() == [1]


def test_mermaid_export(tmp_path):
    text = graph.mermaid()
    for edge in ("validate -.-> retry", "retry -.-> propose", "validate -.-> await_approval",
                 "approval -.-> apply", "approval -.-> reject", "validate -.-> auto_reject"):
        assert edge in text
    out = tmp_path / "graph.mmd"
    assert _cli("graph", "--out", str(out)).returncode == 0 and out.read_text(encoding="utf-8") == text
