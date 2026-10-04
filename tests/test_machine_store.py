"""상태 전이 규칙과 실행 ID별 저장."""

import pytest

from workflow.machine import STATUSES, TERMINAL, TransitionError, transition
from workflow.store import RunStore


def _run(status="running"):
    return {"run_id": "r1", "status": status, "history": [{"status": status, "at": 0, "note": ""}], "created_at": 0}


def test_happy_path_transitions():
    run = _run()
    for status in ("analyzed", "proposed", "validated", "awaiting_approval", "applied"):
        transition(run, status)
    assert [h["status"] for h in run["history"]] == ["running", "analyzed", "proposed", "validated",
                                                      "awaiting_approval", "applied"]


@pytest.mark.parametrize("current,new", [
    ("running", "awaiting_approval"),      # 단계를 건너뛸 수 없다
    ("validated", "applied"),              # 승인 대기 없이 반영할 수 없다 (자동 승인 금지)
    ("proposed", "applied"),
    ("applied", "rejected"),               # 종료 상태는 바꿀 수 없다
    ("failed", "running"),
])
def test_illegal_transitions(current, new):
    with pytest.raises(TransitionError):
        transition(_run(current), new)


def test_only_awaiting_approval_leads_to_applied():
    from workflow.machine import TRANSITIONS
    assert [s for s, nexts in TRANSITIONS.items() if "applied" in nexts] == ["awaiting_approval"]
    assert set(TERMINAL) <= set(STATUSES)


def test_store_roundtrip_and_ids(tmp_path):
    store = RunStore(tmp_path / "runs")
    run_id = store.new_run_id()
    store.create({"run_id": run_id, "created_at": 1.0, "status": "running", "history": []})
    store.write(run_id, "1_execute.json", {"metrics": {"a": 0.5}})
    assert store.load(run_id)["status"] == "running"
    assert store.read(run_id, "1_execute.json") == {"metrics": {"a": 0.5}}
    assert [r["run_id"] for r in store.list_runs()] == [run_id]
    for bad in ("../x", "a/b", "", ".hidden"):
        with pytest.raises(ValueError):
            store.run_dir(bad)
    with pytest.raises(FileNotFoundError):
        store.load("nope")
