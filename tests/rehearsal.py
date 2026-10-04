"""리허설용 가짜 LLM 시나리오 (dispatch 도메인). API 키 없이 워크플로우를 한 바퀴 돌린다.

코어 scripts/rehearsal_bundle.py의 분석·개선 시나리오를 참고해 이 레포에서 새로 썼다 (복사 아님).
- 분석: 집계 도구 3회 → 도구 결과 수치만 인용한 리포트 제출 (근거 검사 통과)
- 개선: simulate_params 1회 → 개선안 3건 제출 (정상 1, 허용 범위 밖 1, spec 1)
- 멀티에이전트 분석(X2): 관점 agent 4개가 각자 도구 1회 → 발견 1건씩, 종합 agent가 4건을 합치고
  근거 없는 수치를 넣은 발견 1건은 한 번 반려된 뒤에도 고치지 않아 리포트에서 빠진다.
  단일 분석 시나리오는 활용률(P3)을 보지 않고, 멀티 시나리오는 본다. **각본대로 나오는 값이라 품질 근거가 아니다.**
"""

import json

from tests.fake_llm import FakeLLM, tool_use

BOUNDARY_RULE = {"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 4}}
ANALYST_QUERIES = [
    ("aggregate", {"group_by": ["branch", "hour"], "filters": {"branch": ["B"], "hour": ["09", "10"]}}),
    ("aggregate", {"group_by": ["branch", "difficulty"], "filters": {"branch": ["C"], "difficulty": ["pole"]}}),
    ("aggregate", {"group_by": ["area_zone"]}),
]

GOOD = {"title": "경계 지역만 3단계 지역 범위 +1km", "kind": "params", "target_findings": ["F3"],
        "rationale": "경계 지역 실패는 대부분 OUT_OF_AREA. 전역 완화 대신 경계 구간에만 적용한다",
        "expected_effect": "경계 지역 할당 증가, 다른 구간 영향 최소", "override_rules": [BOUNDARY_RULE]}
OUT_OF_BOUNDS = {"title": "전역 3단계 지역 범위 대폭 완화", "kind": "params", "target_findings": ["F3"],
                 "rationale": "허용 범위 검사를 보여주기 위한 과도한 제안",
                 "params_changes": [{"path": "matching.area_extension_km[2]", "value": 9}]}
SPEC = {"title": "예외 처리: 경계 지역은 3단계까지 시도", "kind": "spec", "target_findings": ["F3"],
        "rationale": "규칙 엔진은 명세를 읽지 않으므로 이 워크플로우의 대상이 아니다",
        "spec_edits": [{"section": "예외 처리", "text": "- 관할 경계 지역은 3단계 매칭까지 시도한다"}]}
DEFAULT_PROPOSALS = (GOOD, OUT_OF_BOUNDS, SPEC)


def _tool_calls(messages):
    return [b for m in messages if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]


def _tool_outputs(messages):
    return [json.loads(b["content"]) for m in messages if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_result" and not b.get("is_error")]


def _turn(messages) -> int:
    """이 대화에서 지금까지 LLM이 답한 횟수. FakeLLM의 n은 분석·제안 대화를 합쳐 세므로 쓰지 않는다."""
    return sum(1 for m in messages if m["role"] == "assistant")


def _analyst(n, messages):
    if n < len(ANALYST_QUERIES):
        return tool_use(*ANALYST_QUERIES[n])
    ids = [c["id"] for c in _tool_calls(messages)]
    b_am, c_pole, zones = _tool_outputs(messages)[:3]
    b_am, c_pole = b_am["rows"][0], c_pole["rows"][0]
    zone = {r["area_zone"]: r for r in zones["rows"]}
    return tool_use("submit_report", {"summary": "리허설: 가짜 분석 agent의 리포트", "findings": [
        {"title": "B지점 오전 실패 집중", "slice": {"branch": ["B"], "hour": ["09", "10"]},
         "reason_codes": ["CAPACITY"], "description": f"{b_am['items']}건 중 {b_am['failed']}건 실패",
         "cited_calls": [ids[0]]},
        {"title": "C지점 승주 작업 미할당", "slice": {"branch": ["C"], "difficulty": ["pole"]},
         "reason_codes": ["NO_CERT"], "description": f"{c_pole['items']}건 중 {c_pole['failed']}건 실패",
         "cited_calls": [ids[1]]},
        {"title": "경계 지역 실패", "slice": {"area_zone": ["boundary"]}, "reason_codes": ["OUT_OF_AREA"],
         "description": f"{zone['boundary']['items']}건 중 {zone['boundary']['failed']}건 실패",
         "cited_calls": [ids[2]]},
    ]})


PERSPECTIVE_QUERIES = {
    "time_branch": ("aggregate", {"group_by": ["branch", "hour"], "filters": {"branch": ["B"], "hour": ["09", "10"]}}),
    "cert_skill": ("aggregate", {"group_by": ["branch", "difficulty"], "filters": {"branch": ["C"], "difficulty": ["pole"]}}),
    "utilization": ("worker_stats", {"sort_by": "utilization", "limit": 5}),
    "region": ("aggregate", {"group_by": ["area_zone"]}),
}
UNGROUNDED_TITLE = "오전 수요와 자격 부족의 복합 효과"


def _perspective_finding(pid: str, out: dict, call_id: str) -> dict:
    if pid == "time_branch":
        row = out["rows"][0]
        return {"title": "B지점 오전 실패 집중", "slice": {"branch": ["B"], "hour": ["09", "10"]},
                "reason_codes": ["CAPACITY"], "description": f"{row['items']}건 중 {row['failed']}건 실패",
                "hypothesis": "오전 수요가 근무 인력을 넘는다", "cited_calls": [call_id]}
    if pid == "cert_skill":
        row = out["rows"][0]
        return {"title": "C지점 승주 작업 미할당", "slice": {"branch": ["C"], "difficulty": ["pole"]},
                "reason_codes": ["NO_CERT"], "description": f"{row['items']}건 중 {row['failed']}건 실패",
                "hypothesis": "C지점에 승주 자격자가 없다", "cited_calls": [call_id]}
    if pid == "utilization":
        low = out["rows"][0]
        return {"title": "활용률이 낮은 작업자", "metric": {"name": "worker_utilization", "direction": "low"},
                "description": f"평균 활용률 {out['mean_utilization']}, 가장 낮은 작업자 활용률 {low['utilization']}",
                "hypothesis": "가능시간이 수요 시간대와 맞지 않는다", "cited_calls": [call_id]}
    zone = {r["area_zone"]: r for r in out["rows"]}["boundary"]
    return {"title": "경계 지역 실패", "slice": {"area_zone": ["boundary"]}, "reason_codes": ["OUT_OF_AREA"],
            "description": f"{zone['items']}건 중 {zone['failed']}건 실패", "hypothesis": "관할 밖 거리 제한",
            "cited_calls": [call_id]}


def _perspective(pid: str, messages):
    if _turn(messages) == 0:
        return tool_use(*PERSPECTIVE_QUERIES[pid])
    call_id = _tool_calls(messages)[0]["id"]
    finding = _perspective_finding(pid, _tool_outputs(messages)[0], call_id)
    return tool_use("submit_report", {"summary": f"리허설: {pid} 관점", "findings": [finding]})


def _synthesizer(messages):
    listing = json.loads(messages[0]["content"].split("# 관점별 발견\n", 1)[1])
    findings = [{k: f[k] for k in ("title", "description", "slice", "reason_codes", "metric", "hypothesis")
                 if f.get(k) is not None} | {"sources": [f["id"]]} for f in listing]
    findings.append({"title": UNGROUNDED_TITLE, "description": "약 999건이 두 원인에 함께 걸린다",
                     "sources": [f["id"] for f in listing[:2]]})
    return tool_use("submit_synthesis", {"summary": "리허설: 종합 agent의 리포트", "findings": findings})


def _first_user_text(messages) -> str:
    first = messages[0]["content"]
    return first if isinstance(first, str) else ""


def build_policy(proposals=DEFAULT_PROPOSALS, *retries, failing_perspectives=()):
    """proposals: 첫 제안 시도의 개선안. retries: 재시도마다 낼 개선안 (모자라면 마지막을 반복)."""
    attempts = [list(proposals), *[list(r) for r in retries]]
    submitted = []

    def policy(item, n, messages, tools):
        names = {t["name"] for t in tools}
        text = _first_user_text(messages)
        if text.startswith("[관점: "):
            pid = text[len("[관점: "):text.index("]")]
            if pid in failing_perspectives:
                return RuntimeError(f"리허설: {pid} 관점 LLM 오류")
            return _perspective(pid, messages)
        if text.startswith("[종합]"):
            return _synthesizer(messages)
        if "submit_report" in names:
            return _analyst(_turn(messages), messages)
        if "submit_proposals" in names:
            if _turn(messages) == 0:
                return tool_use("simulate_params", {"override_rules": [BOUNDARY_RULE]})
            submitted.append(1)
            return tool_use("submit_proposals", {"proposals": attempts[min(len(submitted), len(attempts)) - 1]})
        raise AssertionError(f"리허설 시나리오에 없는 호출: {sorted(names)}")

    return policy


def rehearsal_llm(proposals=DEFAULT_PROPOSALS, *retries, failing_perspectives=()) -> FakeLLM:
    return FakeLLM(build_policy(proposals, *retries, failing_perspectives=failing_perspectives))
