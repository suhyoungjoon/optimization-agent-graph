"""M3 화면 서버: 기록 읽기(그래프·체크포인트·단계·시간), 사람의 결정(interrupt 재개), 리허설 실행 시작."""


import pytest
import yaml
from fastapi.testclient import TestClient

from tests.conftest import CRITERIA, LIMITS, REPO, SETTINGS, SMALL
from tests.rehearsal import rehearsal_llm
from ui.server import NODE_META, create_app
from workflow import graph, multi_analysis
from workflow.store import RunStore

PERSPECTIVES = multi_analysis.load_perspectives(REPO / "settings" / "analysis.yaml")


@pytest.fixture
def env(tmp_path, registry, llm_config):
    scen = tmp_path / "scenarios"
    scen.mkdir()
    (scen / "small.yaml").write_text(yaml.safe_dump(SMALL), encoding="utf-8")
    settings = {**SETTINGS, "scenario_set": "small"}
    app = create_app(runs_dir=tmp_path / "runs", models_dir=registry.root, settings=settings,
                     settings_dir=REPO / "settings", scenarios_dir=scen,
                     rehearsal_llm_factory=lambda: (rehearsal_llm(), llm_config))
    return TestClient(app), RunStore(tmp_path / "runs"), registry, app


def _run(store, registry, llm_config, **kw):
    return graph.run_workflow(store, registry, scenario_set=SMALL, criteria=CRITERIA, llm=rehearsal_llm(),
                              llm_config=llm_config, limits=LIMITS, rehearsal=True, **kw)


def test_graph_structure_matches_compiled_graph(env):
    client, *_ = env
    g = client.get("/api/graph").json()
    ids = {n["id"] for n in g["nodes"]}
    assert set(NODE_META) <= ids and {"__start__", "__end__"} <= ids
    edges = {(e["source"], e["target"]): e["conditional"] for e in g["edges"]}
    assert edges[("retry", "propose")] and edges[("validate", "await_approval")]
    assert edges[("perspective", "synthesize")] is False                 # 병렬 가지는 모두 끝난 뒤 종합으로
    assert "graph TD" in g["mermaid"]
    actors = {n["id"]: n["actor"] for n in g["nodes"]}
    assert actors["analyze"] == "ai" and actors["validate"] == "code" and actors["approval"] == "human"


def test_run_detail_checkpoints_and_timings(env, llm_config):
    client, store, registry, _ = env
    run = _run(store, registry, llm_config, analysis_mode="multi", perspectives=PERSPECTIVES)
    d = client.get(f"/api/runs/{run['run_id']}").json()
    assert d["pending"] == ["approval"] and not d["terminal"]
    assert {"1_execute", "2_analyze", "3_propose", "4_validate", "2_analyze.utilization"} <= set(d["stages"])
    assert "decisions" not in d["stages"]
    nodes = [t["node"] for t in d["timings"]]
    assert nodes.count("perspective") == 4 and {"execute", "synthesize", "propose", "validate"} <= set(nodes)
    assert {t["task"] for t in d["timings"] if t["node"] == "perspective"} == {p["id"] for p in PERSPECTIVES}
    assert {"synthesize", "perspective:region", "propose#0"} <= set(d["usage"])

    cps = client.get(f"/api/runs/{run['run_id']}/checkpoints").json()["checkpoints"]
    steps = [[t["name"] for t in c["tasks"]] for c in cps]
    assert ["perspective"] * 4 in steps                                  # 병렬 Send가 한 step에 4개
    last = cps[-1]
    assert last["next"] == ["approval"]
    assert last["tasks"][0]["interrupts"][0]["eligible"] == ["C1"]       # interrupt 값
    assert all(cps[i]["step"] < cps[i + 1]["step"] for i in range(len(cps) - 1))


def test_decision_resumes_interrupt_and_records_approver(env, llm_config):
    client, store, registry, _ = env
    run_id = _run(store, registry, llm_config)["run_id"]
    assert client.post(f"/api/runs/{run_id}/approve", json={"approver": "  "}).status_code == 400
    ok = client.post(f"/api/runs/{run_id}/approve", json={"approver": "홍길동", "note": "화면", "proposal_id": "C1"})
    assert ok.status_code == 200 and ok.json()["status"] == "applied"
    assert registry.champion() == 2 and registry.card(2)["approval"]["by"] == "홍길동"
    again = client.post(f"/api/runs/{run_id}/approve", json={"approver": "홍길동"})
    assert again.status_code == 409 and "승인 대기 상태가 아님" in again.json()["detail"]
    models = client.get("/api/models").json()
    assert models["champion"] == 2 and [c["version"] for c in models["versions"]] == [2, 1]


def test_reject_and_stats(env, llm_config):
    client, store, registry, _ = env
    run_id = _run(store, registry, llm_config)["run_id"]
    assert client.post(f"/api/runs/{run_id}/reject", json={"approver": "김", "note": "부작용"}).json()["status"] == "rejected"
    data = client.get("/api/runs").json()
    assert data["stats"]["runs"] == 1 and data["stats"]["approval_rate"] == 0.0
    assert data["stats"]["usage"]["llm_calls"] > 0 and "validate" in data["stats"]["node_avg_seconds"]
    assert store.read(run_id, "5_apply.json")["by"] == "김"


def test_start_rehearsal_run_from_screen(env):
    client, store, registry, app = env
    res = client.post("/api/runs", json={"analysis": "single"})
    run_id = res.json()["run_id"]
    app.state.threads[run_id].join(timeout=120)
    d = client.get(f"/api/runs/{run_id}").json()
    assert d["run"]["llm"]["rehearsal"] is True and d["run"]["scenario_set"]["name"] == "small"
    assert d["pending"] == ["approval"] and not d["running"]
    assert client.post("/api/runs", json={"analysis": "nope"}).status_code == 400
    assert client.post("/api/runs", json={"scenario": "missing"}).status_code == 400


def test_bad_run_ids(env):
    client, *_ = env
    assert client.get("/api/runs/does-not-exist").status_code == 404
    assert client.get("/api/runs/.hidden").status_code == 400
    assert client.post("/api/runs/does-not-exist/approve", json={"approver": "a"}).status_code == 404


def test_index_and_static(env):
    client, *_ = env
    assert "LangGraph" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
