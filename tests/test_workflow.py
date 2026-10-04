"""워크플로우 종단 테스트 (가짜 LLM). 승인 대기 정지, approve로 버전 올림, 반려·실패 경로."""

import shutil

import pytest
import yaml
from core import load_config

from engines import get_engine
from tests.fake_llm import FakeLLM, tool_use
from tests.rehearsal import GOOD, OUT_OF_BOUNDS, SPEC, rehearsal_llm
from workflow import runner
from workflow.cli import DEFAULT_SETTINGS, main
from workflow.machine import STAGE_FILES
from workflow.store import RunStore

LIMITS = {"analyze_max_llm_calls": 30, "propose_max_llm_calls": 20}
SEED, FAULTS = 42, ["P1", "P2", "P3", "P4"]


@pytest.fixture
def params_path(tmp_path):
    path = tmp_path / "params.yaml"
    shutil.copy(get_engine("rule").params_path, path)
    return path


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs")


@pytest.fixture
def llm_config():
    return {**load_config(DEFAULT_SETTINGS / "llm.yaml"), "cache": False}


def _run(store, params_path, llm, llm_config):
    return runner.run_workflow(store, get_engine("rule", params_path), seed=SEED, faults=FAULTS, llm=llm,
                               llm_config=llm_config, limits=LIMITS, rehearsal=True)


def _version(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["version"]


def test_rehearsal_stops_at_awaiting_approval_and_saves_each_stage(store, params_path, llm_config):
    before = params_path.read_text(encoding="utf-8")
    run = _run(store, params_path, rehearsal_llm(), llm_config)
    run_id = run["run_id"]

    assert run["status"] == "awaiting_approval"
    assert [h["status"] for h in run["history"]] == ["running", "analyzed", "proposed", "validated",
                                                      "awaiting_approval"]
    assert params_path.read_text(encoding="utf-8") == before          # 승인 전에는 params를 건드리지 않는다
    for name in ("1_execute", "2_analyze", "3_propose", "4_validate"):
        assert store.has(run_id, STAGE_FILES[name])
    assert not store.has(run_id, STAGE_FILES["5_apply"])

    saved = store.load(run_id)                                          # 재현 정보 (원칙 5)
    assert saved["engine"] == "rule" and saved["model_version"] == "rule@v1"
    assert saved["scenario"] == {"seed": SEED, "faults": FAULTS}
    assert saved["llm"]["model"] == "fake-model" and saved["llm"]["rehearsal"] is True
    assert saved["core"]["version"]

    executed = store.read(run_id, STAGE_FILES["1_execute"])
    assert executed["violations"] == 0 and executed["status_counts"]["success"] == 1040
    assert len(store.read(run_id, "decisions.json")) == 1500

    proposals = {p["id"]: p for p in store.read(run_id, STAGE_FILES["3_propose"])["proposals"]}
    assert proposals["C1"]["errors"] == []
    assert any("허용 범위" in e for e in proposals["C2"]["errors"])
    assert any("개선 대상이 아님" in e for e in proposals["C3"]["errors"])

    validated = store.read(run_id, STAGE_FILES["4_validate"])
    assert validated["eligible"] == ["C1"] and [s["id"] for s in validated["skipped"]] == ["C2", "C3"]
    sim = validated["candidates"][0]["simulation"]
    assert sim["violations_after"] == 0 and sim["after"]["assignment_rate"] > sim["before"]["assignment_rate"]


def test_approve_bumps_params_version(store, params_path, llm_config):
    run_id = _run(store, params_path, rehearsal_llm(), llm_config)["run_id"]
    run = runner.approve(store, run_id, note="ok")

    assert run["status"] == "applied" and _version(params_path) == 2
    applied = store.read(run_id, STAGE_FILES["5_apply"])
    assert applied["decision"] == "approved" and applied["proposal_id"] == "C1"
    assert (applied["model_before"], applied["model_after"]) == ("rule@v1", "rule@v2")
    assert yaml.safe_load(params_path.read_text(encoding="utf-8"))["overrides"]["rules"] == GOOD["override_rules"]
    with pytest.raises(ValueError, match="승인 대기 상태가 아님"):
        runner.approve(store, run_id)

    # 다음 실행은 새 챔피언 버전으로 돈다
    assert _run(store, params_path, rehearsal_llm(), llm_config)["model_version"] == "rule@v2"


def test_reject_records_only(store, params_path, llm_config):
    before = params_path.read_text(encoding="utf-8")
    run_id = _run(store, params_path, rehearsal_llm(), llm_config)["run_id"]
    run = runner.reject(store, run_id, note="부작용 확인 필요")
    assert run["status"] == "rejected" and params_path.read_text(encoding="utf-8") == before
    assert store.read(run_id, STAGE_FILES["5_apply"])["note"] == "부작용 확인 필요"


def test_no_eligible_candidate_ends_rejected(store, params_path, llm_config):
    run = _run(store, params_path, rehearsal_llm([OUT_OF_BOUNDS, SPEC]), llm_config)
    assert run["status"] == "rejected"
    assert store.read(run["run_id"], STAGE_FILES["5_apply"])["by"] == "workflow"


def test_multiple_candidates_need_explicit_choice(store, params_path, llm_config):
    other = {"title": "명장 기준 완화", "kind": "params", "target_findings": ["F1"], "rationale": "x",
             "params_changes": [{"path": "cei.master_threshold", "value": 75}]}
    run_id = _run(store, params_path, rehearsal_llm([GOOD, other]), llm_config)["run_id"]
    with pytest.raises(ValueError, match="--proposal"):
        runner.approve(store, run_id)
    with pytest.raises(ValueError, match="후보가 아님"):
        runner.approve(store, run_id, "C9")
    runner.approve(store, run_id, "C2")
    assert yaml.safe_load(params_path.read_text(encoding="utf-8"))["cei"]["master_threshold"] == 75


def test_approve_refused_when_champion_params_changed(store, params_path, llm_config):
    run_id = _run(store, params_path, rehearsal_llm(), llm_config)["run_id"]
    params_path.write_text(params_path.read_text(encoding="utf-8") + "\n# 사람이 직접 고침\n", encoding="utf-8")
    with pytest.raises(ValueError, match="params 파일이 바뀌었"):
        runner.approve(store, run_id)
    assert store.load(run_id)["status"] == "awaiting_approval"


def test_analysis_without_submission_fails_with_reason(store, params_path, llm_config):
    silent = FakeLLM(lambda item, n, messages, tools: tool_use("overview", {}))
    run = runner.run_workflow(store, get_engine("rule", params_path), seed=SEED, faults=FAULTS, llm=silent,
                              llm_config=llm_config, limits={**LIMITS, "analyze_max_llm_calls": 3}, rehearsal=True)
    assert run["status"] == "failed" and "max_calls" in run["error"]["message"]
    assert store.has(run["run_id"], STAGE_FILES["1_execute"])
    assert not store.has(run["run_id"], STAGE_FILES["2_analyze"])


def test_cli_run_approve_status(tmp_path, params_path, capsys):
    runs = tmp_path / "cli-runs"
    assert main(["--runs-dir", str(runs), "run", "--rehearsal", "--params", str(params_path)]) == 0
    out = capsys.readouterr().out
    assert "awaiting_approval" in out and "다음: python -m workflow approve" in out
    run_id = RunStore(runs).list_runs()[0]["run_id"]

    assert main(["--runs-dir", str(runs), "approve", run_id, "--note", "cli"]) == 0
    assert "rule@v1 → rule@v2" in capsys.readouterr().out and _version(params_path) == 2

    assert main(["--runs-dir", str(runs), "status"]) == 0
    assert f"{run_id}  applied" in capsys.readouterr().out
    assert main(["--runs-dir", str(runs), "reject", run_id]) == 2      # 이미 종료된 실행
