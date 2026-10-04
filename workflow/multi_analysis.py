"""X2: 멀티에이전트 분석. 관점 agent 여러 개가 같은 실행 결과를 나눠 보고, 종합 agent가 하나의 리포트로 합친다.

- 관점 정의는 settings/analysis.yaml (도메인 지식은 코드가 아니라 설정에 둔다).
- 관점 agent는 코어 run_tool_loop + 코어 분석 agent의 제출 스키마·근거 검사를 그대로 쓴다.
- 종합 agent는 도구 없이 관점 발견을 합친다. 발견마다 출처(sources)를 적게 하고,
  출처 발견들이 인용한 도구 결과를 근거로 코드가 수치를 다시 검사한다 (근거 없는 수치가 있는 발견은 제외).
- 출력 형식은 단일 분석 agent(core analyze)의 리포트와 같다. 3단계는 어느 쪽 리포트로도 똑같이 동작한다.
"""

import json
from pathlib import Path

import yaml
from core import Aggregator, grounding_problems, report_submit_tool, run_tool_loop, usage_dict
from core.analysis.agent import SYSTEM as ANALYST_SYSTEM

SYNTH_SUBMIT = "submit_synthesis"
SYNTH_MARK = "[종합]"
PERSPECTIVE_KEYS = ("id", "prefix", "name", "focus", "tools")
FINDING_KEYS = ("title", "description", "slice", "reason_codes", "metric", "hypothesis")


def load_perspectives(path: str | Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    perspectives = data["perspectives"]
    for p in perspectives:
        missing = [k for k in PERSPECTIVE_KEYS if k not in p]
        if missing:
            raise ValueError(f"관점 정의에 {missing}가 없음: {p.get('id')}")
    ids, prefixes = [p["id"] for p in perspectives], [p["prefix"] for p in perspectives]
    if len(set(ids)) != len(ids) or len(set(prefixes)) != len(prefixes):
        raise ValueError("관점 id·prefix는 서로 달라야 한다")
    return perspectives


def perspective_mark(perspective: dict) -> str:
    return f"[관점: {perspective['id']}]"


def _sum_usage(usages: list[dict]) -> dict:
    total: dict = {}
    for u in usages:
        for k, v in u.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
            elif v is None:
                total.setdefault(k, None)
    return total


# --- 관점 agent ---------------------------------------------------------------------

def run_perspective(pack, instance, decisions, perspective: dict, llm, llm_config: dict, max_calls: int) -> dict:
    dimensions = pack.dimensions()
    available = {t["name"]: t for t in Aggregator(decisions, dimensions).tools() + pack.analysis_tools(instance, decisions)}
    unknown = [n for n in perspective["tools"] if n not in available]
    if unknown:
        raise ValueError(f"관점 {perspective['id']}: 없는 도구 {unknown} (사용 가능: {sorted(available)})")
    tools = [available[n] for n in perspective["tools"]]
    metric_names = sorted(pack.metrics(instance, decisions))

    def check(submission: dict, calls: dict) -> list[str]:
        return [f"findings[{i}] '{f.get('title', '')}': {p}"
                for i, f in enumerate(submission.get("findings") or []) for p in grounding_problems(f, calls)]

    system = (ANALYST_SYSTEM + f"\n\n너는 '{perspective['name']}' 관점만 맡는다. 다른 관점은 다른 agent가 본다.\n"
              f"관점: {perspective['focus']}")
    user = (f"{perspective_mark(perspective)} {perspective['name']}\n"
            "실행 결과에서 이 관점의 패턴과 원인 가설을 찾아라. 먼저 overview로 전체와 차원을 확인하라.\n"
            f"분석 대상 항목 수: {len(decisions)}")
    result = run_tool_loop(llm, system=system, user=user, tools=tools,
                           submit_tool=report_submit_tool(dimensions, metric_names),
                           max_calls=max_calls, salt=f"perspective-{perspective['id']}", check_submission=check)

    kept, dropped = [], []
    for f in (result.submission or {}).get("findings") or []:
        problems = grounding_problems(f, result.calls)
        if problems:
            dropped.append({"finding": f, "problems": problems})
        else:
            kept.append({**f, "id": f"{perspective['prefix']}{len(kept) + 1}"})
    return {"perspective": perspective["id"], "name": perspective["name"],
            "summary": (result.submission or {}).get("summary", ""), "findings": kept, "dropped": dropped,
            "calls": result.calls, "stop": result.stop, "feedback_rounds": result.feedback_rounds,
            "usage": usage_dict(result, llm.model, llm_config)}


# --- 종합 agent ---------------------------------------------------------------------

SYNTH_SYSTEM = """너는 분석 종합 담당이다. 여러 관점 agent가 낸 발견을 받아 하나의 분석 리포트로 합친다.

규칙:
- 같은 패턴을 가리키는 발견은 하나로 합치고, 서로 다른 패턴은 따로 둔다.
- 발견마다 sources에 근거가 된 관점 발견 id(예: T1, U2)를 적는다. sources 없는 발견은 만들지 않는다.
- 설명의 수치는 sources 발견의 설명에 있는 수치만 그대로 쓴다. 새 수치를 계산하지 않는다.
- slice·reason_codes·metric은 sources 발견에서 가져온다.
- 개선 효과가 큰 순서(영향 건수, 원인의 명확성)로 나열한다.
- 끝나면 submit_synthesis 도구로 제출한다."""


def _synth_submit_tool(base_submit: dict) -> dict:
    finding = json.loads(json.dumps(base_submit["input_schema"]["properties"]["findings"]["items"]))
    finding["properties"].pop("cited_calls", None)
    finding["properties"]["sources"] = {"type": "array", "items": {"type": "string"},
                                        "description": "근거가 된 관점 발견 id"}
    finding["required"] = ["title", "description", "sources"]
    return {"name": SYNTH_SUBMIT, "description": "종합 분석 리포트를 제출한다.",
            "input_schema": {"type": "object", "properties": {
                "summary": {"type": "string"}, "findings": {"type": "array", "items": finding}},
                "required": ["summary", "findings"]}}


def synthesize(pack, instance, decisions, results: dict[str, dict], llm, llm_config: dict, max_calls: int) -> dict:
    """results: 관점 id → run_perspective 결과 (실패한 관점은 {"error": ...})."""
    ok = {pid: r for pid, r in results.items() if "error" not in r}
    if not ok:
        raise ValueError("모든 관점 agent가 실패함: " + "; ".join(f"{k}: {v['error']}" for k, v in results.items()))
    sources = {f["id"]: f for r in ok.values() for f in r["findings"]}
    calls = {cid: c for r in ok.values() for cid, c in r["calls"].items()}

    def cited_of(finding: dict) -> list[str]:
        return sorted({c for s in finding.get("sources") or [] if s in sources for c in sources[s]["cited_calls"]})

    def problems(finding: dict) -> list[str]:
        bad = [s for s in finding.get("sources") or [] if s not in sources]
        if bad or not finding.get("sources"):
            return [f"없는 출처 {bad}" if bad else "출처(sources)가 없음"]
        return grounding_problems({**finding, "cited_calls": cited_of(finding)}, calls)

    def check(submission: dict, _calls: dict) -> list[str]:
        return [f"findings[{i}] '{f.get('title', '')}': {p}"
                for i, f in enumerate(submission.get("findings") or []) for p in problems(f)]

    dimensions = pack.dimensions()
    base = report_submit_tool(dimensions, sorted(pack.metrics(instance, decisions)))
    listing = [{"id": f["id"], "perspective": r["name"], **{k: f.get(k) for k in FINDING_KEYS}}
               for r in ok.values() for f in r["findings"]]
    user = (f"{SYNTH_MARK} 관점 agent {len(ok)}개의 발견을 하나의 리포트로 합쳐라.\n"
            + "".join(f"- {r['name']} 요약: {r['summary']}\n" for r in ok.values())
            + "# 관점별 발견\n" + json.dumps(listing, ensure_ascii=False, indent=1))
    result = run_tool_loop(llm, system=SYNTH_SYSTEM, user=user, tools=[], submit_tool=_synth_submit_tool(base),
                           max_calls=max_calls, salt="synthesis", check_submission=check)

    kept, dropped = [], []
    for f in (result.submission or {}).get("findings") or []:
        issues = problems(f)
        if issues:
            dropped.append({"finding": f, "problems": issues})
        else:
            kept.append({**f, "cited_calls": cited_of(f), "id": f"F{len(kept) + 1}"})
    synth_usage = usage_dict(result, llm.model, llm_config)
    return {
        "mode": "multi",
        "summary": (result.submission or {}).get("summary", ""),
        "findings": kept,
        "dropped": dropped,
        "calls": calls,
        "stop": result.stop,
        "feedback_rounds": result.feedback_rounds,
        "usage": _sum_usage([r["usage"] for r in ok.values()] + [synth_usage]),
        "perspectives": {pid: ({"name": r["name"], "findings": [f"{f['id']} {f['title']}" for f in r["findings"]],
                                "dropped": len(r["dropped"]), "stop": r["stop"], "usage": r["usage"]}
                               if "error" not in r else {"name": r.get("name"), "error": r["error"]})
                         for pid, r in results.items()},
        "synthesis": {"usage": synth_usage, "stop": result.stop, "feedback_rounds": result.feedback_rounds,
                      "dropped": len(dropped)},
    }
