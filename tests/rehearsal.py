"""리허설용 가짜 LLM 시나리오 (dispatch 도메인). API 키 없이 워크플로우를 한 바퀴 돌린다.

코어 scripts/rehearsal_bundle.py의 분석·개선 시나리오를 참고해 이 레포에서 새로 썼다 (복사 아님).
- 분석: 집계 도구 3회 → 도구 결과 수치만 인용한 리포트 제출 (근거 검사 통과)
- 개선: simulate_params 1회 → 개선안 3건 제출 (정상 1, 허용 범위 밖 1, spec 1)
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


def build_policy(proposals=DEFAULT_PROPOSALS):
    def policy(item, n, messages, tools):
        names = {t["name"] for t in tools}
        if "submit_report" in names:
            return _analyst(n, messages)
        if "submit_proposals" in names:
            if n == 0:
                return tool_use("simulate_params", {"override_rules": [BOUNDARY_RULE]})
            return tool_use("submit_proposals", {"proposals": list(proposals)})
        raise AssertionError(f"리허설 시나리오에 없는 호출: {sorted(names)}")

    return policy


def rehearsal_llm(proposals=DEFAULT_PROPOSALS) -> FakeLLM:
    return FakeLLM(build_policy(proposals))
