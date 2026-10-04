"""X2 멀티에이전트 분석: 관점 agent, 종합 agent의 근거 검사, 그래프 병렬 실행, 부분 실패, 단일 대 멀티 비교 평가."""

import shutil
import threading

import pytest
import yaml
from core import Aggregator, load_config

from engines import get_engine
from tests.fake_llm import FakeLLM, tool_use
from tests.rehearsal import UNGROUNDED_TITLE, rehearsal_llm
from workflow import evaluation, graph, multi_analysis, stages
from workflow.cli import DEFAULT_SETTINGS, main
from workflow.machine import STAGE_FILES
from workflow.store import RunStore

LIMITS = {"analyze_max_llm_calls": 30, "propose_max_llm_calls": 20, "perspective_max_llm_calls": 15,
          "synthesis_max_llm_calls": 3, "max_retries": 1}
SEED, FAULTS = 42, ["P1", "P2", "P3", "P4"]
PERSPECTIVES = multi_analysis.load_perspectives(DEFAULT_SETTINGS / "analysis.yaml")


@pytest.fixture
def llm_config():
    return {**load_config(DEFAULT_SETTINGS / "llm.yaml"), "cache": False}


@pytest.fixture(scope="module")
def scenario():
    engine = get_engine("rule")
    params = engine.load_params()
    pack = engine.pack_factory(params)
    instance, _ = pack.generate(SEED, FAULTS)
    return pack, instance, pack.solve(instance, params)


@pytest.fixture
def params_path(tmp_path):
    path = tmp_path / "params.yaml"
    shutil.copy(get_engine("rule").params_path, path)
    return path


def _multi(store, params_path, llm, llm_config, **kwargs):
    return graph.run_workflow(store, get_engine("rule", params_path), seed=SEED, faults=FAULTS, llm=llm,
                              llm_config=llm_config, limits=LIMITS, rehearsal=True, analysis_mode="multi",
                              perspectives=PERSPECTIVES, **kwargs)


# --- 설정 -------------------------------------------------------------------------

def test_perspective_settings_use_existing_tools(scenario):
    pack, instance, decisions = scenario
    available = {t["name"] for t in Aggregator(decisions, pack.dimensions()).tools()
                 + pack.analysis_tools(instance, decisions)}
    assert [p["id"] for p in PERSPECTIVES] == ["time_branch", "cert_skill", "utilization", "region"]
    for p in PERSPECTIVES:
        assert set(p["tools"]) <= available, p["id"]


def test_load_perspectives_rejects_bad_definitions(tmp_path):
    bad = tmp_path / "analysis.yaml"
    bad.write_text(yaml.safe_dump({"perspectives": [PERSPECTIVES[0], {**PERSPECTIVES[1], "id": "time_branch"}]}),
                   encoding="utf-8")
    with pytest.raises(ValueError, match="서로 달라야"):
        multi_analysis.load_perspectives(bad)
    bad.write_text(yaml.safe_dump({"perspectives": [{"id": "x"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="없음"):
        multi_analysis.load_perspectives(bad)


# --- 관점 agent -----------------------------------------------------------------------

def test_perspective_agent_sees_only_its_tools_and_prefixes_ids(scenario, llm_config):
    pack, instance, decisions = scenario
    utilization = next(p for p in PERSPECTIVES if p["id"] == "utilization")
    llm = rehearsal_llm()
    result = multi_analysis.run_perspective(pack, instance, decisions, utilization, llm, llm_config, 15)

    sent = {t["name"] for t in llm.calls[0]["tools"]}
    assert sent == set(utilization["tools"]) | {"submit_report"}
    assert result["stop"] == "submitted" and [f["id"] for f in result["findings"]] == ["U1"]
    assert result["findings"][0]["metric"] == {"name": "worker_utilization", "direction": "low"}
    assert "작업자 활용률" in llm.calls[0]["system"][0]["text"]

    with pytest.raises(ValueError, match="없는 도구"):
        multi_analysis.run_perspective(pack, instance, decisions, {**utilization, "tools": ["nope"]}, llm,
                                       llm_config, 15)


# --- 종합 agent -----------------------------------------------------------------------

def _perspective_results(scenario, llm_config):
    pack, instance, decisions = scenario
    llm = rehearsal_llm()
    return {p["id"]: multi_analysis.run_perspective(pack, instance, decisions, p, llm, llm_config, 15)
            for p in PERSPECTIVES}


def test_synthesis_keeps_grounded_and_drops_ungrounded(scenario, llm_config):
    pack, instance, decisions = scenario
    results = _perspective_results(scenario, llm_config)
    report = multi_analysis.synthesize(pack, instance, decisions, results, rehearsal_llm(), llm_config, 3)

    assert [f["id"] for f in report["findings"]] == ["F1", "F2", "F3", "F4"]
    assert report["feedback_rounds"] == 1                               # 근거 없는 수치로 한 번 반려됨
    assert [d["finding"]["title"] for d in report["dropped"]] == [UNGROUNDED_TITLE]
    assert "근거 없는 수치 999" in report["dropped"][0]["problems"][0]
    for f in report["findings"]:                                        # 출처 발견의 인용을 그대로 물려받는다
        source = next(s for r in results.values() for s in r["findings"] if s["id"] == f["sources"][0])
        assert f["cited_calls"] == source["cited_calls"] and f["cited_calls"][0] in report["calls"]
    # 단일 분석 리포트(core analyze)와 같은 키를 가진다 → 3단계가 그대로 동작
    assert {"summary", "findings", "dropped", "calls", "stop", "feedback_rounds", "usage"} <= set(report)
    assert report["usage"]["llm_calls"] == 4 * 2 + 2


def test_synthesis_rejects_unknown_sources(scenario, llm_config):
    pack, instance, decisions = scenario
    results = _perspective_results(scenario, llm_config)
    bad = FakeLLM(lambda item, n, messages, tools: tool_use("submit_synthesis", {"summary": "x", "findings": [
        {"title": "출처 없음", "description": "근거 없음", "sources": ["Z9"]}]}))
    report = multi_analysis.synthesize(pack, instance, decisions, results, bad, llm_config, 3)
    assert report["findings"] == [] and "없는 출처" in report["dropped"][0]["problems"][0]
    with pytest.raises(stages.StageError, match="발견이 없음"):
        stages.check_report(report)


# --- 그래프 ---------------------------------------------------------------------------

def test_multi_mode_graph_runs_perspectives_in_parallel(tmp_path, params_path, llm_config):
    store = RunStore(tmp_path / "runs")
    llm = rehearsal_llm()
    threads, barrier = set(), threading.Barrier(len(PERSPECTIVES), timeout=10)
    create = llm.create

    def tracking_create(**kw):
        text = kw["messages"][0]["content"]
        if isinstance(text, str) and text.startswith("[관점: ") and len(kw["messages"]) == 1:
            threads.add(threading.get_ident())
            barrier.wait()                         # 네 관점이 동시에 첫 호출에 도달해야 통과 (순차면 시간 초과)
        return create(**kw)

    llm.create = tracking_create
    run = _multi(store, params_path, llm, llm_config)
    run_id = run["run_id"]

    assert run["status"] == "awaiting_approval" and len(threads) == len(PERSPECTIVES)
    assert run["analysis"]["mode"] == "multi" and run["analysis"]["perspectives"] == PERSPECTIVES
    assert "관점 4/4" in run["history"][1]["note"]
    for p in PERSPECTIVES:
        assert store.read(run_id, graph.perspective_file(p["id"]))["stop"] == "submitted"
    report = store.read(run_id, STAGE_FILES["2_analyze"])
    assert report["mode"] == "multi" and len(report["findings"]) == 4
    assert store.read(run_id, STAGE_FILES["3_propose"])["proposals"]          # 3단계가 멀티 리포트로 동작
    graph.approve(store, run_id)
    assert yaml.safe_load(params_path.read_text(encoding="utf-8"))["version"] == 2


def test_one_perspective_failure_is_recorded_and_others_continue(tmp_path, params_path, llm_config):
    store = RunStore(tmp_path / "runs")
    run = _multi(store, params_path, rehearsal_llm(failing_perspectives=("utilization",)), llm_config)
    assert run["status"] == "awaiting_approval"
    assert "관점 3/4" in run["history"][1]["note"] and "utilization" in run["history"][1]["note"]
    report = store.read(run["run_id"], STAGE_FILES["2_analyze"])
    assert "리허설: utilization" in report["perspectives"]["utilization"]["error"]
    assert len(report["findings"]) == 3


def test_all_perspectives_failing_fails_run(tmp_path, params_path, llm_config):
    store = RunStore(tmp_path / "runs")
    llm = rehearsal_llm(failing_perspectives=tuple(p["id"] for p in PERSPECTIVES))
    run = _multi(store, params_path, llm, llm_config)
    assert run["status"] == "failed" and "모든 관점 agent가 실패" in run["error"]["message"]


def test_multi_mode_requires_perspectives(tmp_path, params_path, llm_config):
    with pytest.raises(ValueError, match="관점 정의"):
        graph.run_workflow(RunStore(tmp_path / "runs"), get_engine("rule", params_path), seed=SEED, faults=FAULTS,
                           llm=rehearsal_llm(), llm_config=llm_config, limits=LIMITS, rehearsal=True,
                           analysis_mode="multi")


def test_mermaid_has_parallel_analysis_branch():
    text = graph.mermaid()
    for edge in ("execute -.-> perspective", "execute -.-> analyze", "perspective --> synthesize",
                 "synthesize -.-> propose"):
        assert edge in text


# --- 비교 평가 ------------------------------------------------------------------------

def test_compare_analysis_rehearsal(tmp_path, params_path, llm_config):
    llms = []

    def factory():
        llms.append(rehearsal_llm())
        return llms[-1]

    out = evaluation.compare_analysis(tmp_path / "evals", get_engine("rule", params_path), seed=SEED, faults=FAULTS,
                                      llm_factory=factory, llm_config=llm_config, limits=LIMITS,
                                      perspectives=PERSPECTIVES, rehearsal=True)
    single, multi = out["results"]["single"], out["results"]["multi"]
    assert out["label"].startswith("리허설 예시") and out["rehearsal"] is True
    assert (single["detected"], multi["detected"]) == (3, 4)            # 리허설 각본: 단일은 P3를 안 본다
    assert not single["faults"]["P3"]["detected"] and multi["faults"]["P3"]["detected"]
    assert (single["llm_calls"], multi["llm_calls"]) == (4, 10)
    assert single["false_positive_candidates"] == [] and multi["false_positive_candidates"] == []
    assert (tmp_path / "evals" / out["eval_id"] / "comparison.json").is_file()
    # 정답표는 분석 agent에게 가지 않는다
    sent = str([c["messages"] for llm in llms for c in llm.calls])
    for name in ("시간대 수요 집중", "자격자 편중", "가능시간 불일치", "경계 지역 수요", "affected_items"):
        assert name not in sent
    # 비교는 propose 앞에서 멈춘다 (개선안 단계 비용 없음)
    store = RunStore(tmp_path / "evals" / out["eval_id"])
    assert all(not store.has(r["run_id"], STAGE_FILES["3_propose"]) for r in out["results"].values())


def test_cli_run_multi_and_compare(tmp_path, params_path, capsys):
    runs = tmp_path / "runs"
    assert main(["--runs-dir", str(runs), "run", "--rehearsal", "--analysis", "multi", "--params",
                 str(params_path)]) == 0
    out = capsys.readouterr().out
    assert "[2 결과분석·멀티에이전트] 발견 4건" in out and "관점 작업자 활용률: U1" in out
    assert main(["--runs-dir", str(runs), "compare-analysis", "--rehearsal", "--params", str(params_path)]) == 0
    out = capsys.readouterr().out
    assert "리허설 예시" in out and "single   3/4" in out and "multi    4/4" in out
