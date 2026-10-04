"""워크플로우 5단계. 각 함수는 코어 함수를 조합하고, 저장할 결과 dict를 돌려준다.

엔진은 engines.Engine 계약으로만 다룬다 (pack_factory, load_params, spec_text, params_path).
"""

import hashlib
import time
from collections import Counter

from core import (apply_params, check_params, finding_slices, params_errors, simulate_params,
                  write_params)
from core import analyze as core_analyze
from core import propose as core_propose

from engines import Engine, model_version

from .locks import file_lock

TARGET_KIND = "params"   # 엔진이 개선할 수 있는 개선안 종류 (규칙 엔진: params만)


class StageError(Exception):
    """단계를 더 진행할 수 없는 실패. 워크플로우가 failed로 기록한다."""


# --- 1. 실행 (코드) ---------------------------------------------------------------

def execute(engine: Engine, params: dict, seed: int, faults: list[str]):
    """챔피언 모델로 시나리오 실행 → validate → metrics. (instance, decisions, 결과)"""
    started = time.time()
    pack = engine.pack_factory(params)
    instance, _truth = pack.generate(seed, faults)     # 정답표는 분석에 주지 않는다
    decisions = pack.solve(instance, params)
    violations = pack.validate(instance, decisions)
    result = {
        "model_version": model_version(engine, params),
        "items": len(decisions),
        "status_counts": dict(Counter(d.status for d in decisions)),
        "reason_counts": dict(Counter(d.reason_code for d in decisions if d.status != "success")),
        "metrics": pack.metrics(instance, decisions),
        "violations": len(violations),
        "violation_samples": [v.__dict__ for v in violations[:20]],
        "seconds": time.time() - started,
    }
    return instance, decisions, result


# --- 2. 결과분석 (AI + 코드) --------------------------------------------------------

def analyze(engine: Engine, params: dict, instance, decisions, llm, llm_config: dict, max_calls: int) -> dict:
    report = core_analyze(engine.pack_factory(params), instance, decisions, llm, llm_config, max_calls=max_calls)
    return check_report(report)


def check_report(report: dict) -> dict:
    """단일·멀티 분석 리포트 공통: 제출되지 않았거나 근거 있는 발견이 없으면 진행할 수 없다."""
    if report["stop"] != "submitted":
        raise StageError(f"분석 agent가 리포트를 제출하지 않음 (stop={report['stop']})")
    if not report["findings"]:
        raise StageError(f"근거 검사를 통과한 발견이 없음 (제외 {len(report['dropped'])}건)")
    return report


# --- 3. 개선안 도출 (AI + 코드) -----------------------------------------------------

def propose(engine: Engine, params: dict, instance, report: dict, llm, llm_config: dict, max_calls: int,
            salt: str = "") -> dict:
    """salt: 재시도 때 같은 요청이 LLM 캐시에서 같은 답으로 돌아오지 않게 시도마다 바꾼다."""
    pack = engine.pack_factory(params)
    out = core_propose(engine.pack_factory, instance, params, engine.spec_text(), pack.dimensions(), report,
                       llm, llm_config, salt=salt, max_calls=max_calls)
    if out["stop"] != "submitted":
        raise StageError(f"개선 agent가 개선안을 제출하지 않음 (stop={out['stop']})")
    proposals = []
    for i, item in enumerate(out["proposals"], start=1):
        errors = list(item["errors"])
        if item["proposal"].get("kind") != TARGET_KIND:
            errors = [f"{engine.name} 엔진의 개선 대상이 아님 (kind={item['proposal'].get('kind')}, "
                      f"대상은 {TARGET_KIND})"]
        proposals.append({"id": f"C{i}", "proposal": item["proposal"], "errors": errors})
    return {**out, "proposals": proposals}


# --- 4. 검증 (코드) ---------------------------------------------------------------
# M1: 같은 시나리오에서 simulate_params 1회 비교. 여러 seed·검증용 세트·판정 기준은 M2.

def validate(engine: Engine, params: dict, instance, report: dict, proposed: dict) -> dict:
    slices = finding_slices(report)
    candidates = []
    for item in proposed["proposals"]:
        if item["errors"]:
            continue
        sim = simulate_params(engine.pack_factory, instance, params, apply_params(params, item["proposal"]), slices)
        reasons = [] if sim["violations_after"] == 0 else [f"필수조건 위반 {sim['violations_after']}건"]
        candidates.append({"id": item["id"], "title": item["proposal"].get("title", ""),
                           "simulation": sim, "eligible": not reasons, "reasons": reasons})
    return {
        "method": "simulate_params x1 (same scenario)",
        "criteria": ["violations_after == 0"],
        "skipped": [{"id": p["id"], "errors": p["errors"]} for p in proposed["proposals"] if p["errors"]],
        "candidates": candidates,
        "eligible": [c["id"] for c in candidates if c["eligible"]],
    }


# --- 5. 개선적용 (사람 + 코드) ------------------------------------------------------

def params_digest(engine: Engine) -> str:
    return hashlib.sha256(engine.params_path.read_bytes()).hexdigest()


def expected_after(champion: dict, proposal: dict) -> dict:
    """champion에 proposal을 반영한 뒤의 params (version +1). 반영이 이미 끝났는지 확인할 때 쓴다."""
    after = apply_params(champion, proposal)
    after["version"] = int(champion["version"]) + 1
    return after


def apply(engine: Engine, proposal: dict, expected_sha256: str | None = None) -> dict:
    """승인된 params 개선안을 엔진의 params 파일에 쓰고 version을 올린다.

    expected_sha256: 실행 시점 params 파일의 해시. 잠금 안에서 다시 확인해, 검사 이후 파일이 바뀌었으면 쓰지 않는다.
    """
    with file_lock(engine.params_path.with_name(engine.params_path.name + ".lock"), "params 반영"):
        if expected_sha256 is not None and params_digest(engine) != expected_sha256:
            raise StageError(f"실행 이후 챔피언 params 파일이 바뀌었음: {engine.params_path}")
        current = engine.load_params()
        dims = engine.pack_factory(current).dimensions()
        errors = params_errors(current, proposal, dims)
        if errors:
            raise StageError("현재 params에 적용할 수 없음: " + "; ".join(errors))
        original = engine.params_path.read_text(encoding="utf-8")
        before_version, after_version = write_params(engine.params_path, proposal)
        written = engine.load_params()
        problems = check_params(written, dims)
        if problems:
            engine.params_path.write_text(original, encoding="utf-8")   # 되돌리고 실패로 남긴다
            raise StageError("반영 후 params 검사 실패: " + "; ".join(problems))
    return applied_record(engine, before_version, written)


def applied_record(engine: Engine, before_version: int, written: dict) -> dict:
    return {"params_path": str(engine.params_path),
            "version_before": before_version, "version_after": int(written["version"]),
            "model_before": f"{engine.name}@v{before_version}", "model_after": model_version(engine, written)}
