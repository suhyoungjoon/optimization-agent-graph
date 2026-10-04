"""판정 기준: 도전자가 챔피언을 대체해도 되는지 코드로 판단한다 (결정적).

- 절대 기준: 모든 시나리오에서 필수조건 위반 0건 (설정으로 끌 수 없다).
- 목표: criteria.target의 지표가 지정 세트들에서 평균 min_delta 이상 좋아지고, seed_majority 세트에서는
  시나리오 과반이 좋아져야 한다.
- 부작용: criteria.side_effects의 지표별 악화 한도. 비어 있으면 판정에 쓰지 않고 요약에만 남는다.
수치(목표·한도)는 settings/workflow.yaml에서 사람이 정한다.
"""

EPS = 1e-12


def _gain(before: float, after: float, direction: str) -> float:
    """좋아진 양. higher: after-before, lower: before-after."""
    return after - before if direction == "higher" else before - after


def set_summary(scenarios: list[dict], target_metric: str, target_direction: str,
                directions: dict[str, str] | None = None) -> dict:
    """세트 하나의 평균 전후 지표와 평균 개선량. directions: 지표별 방향 (없으면 higher)."""
    directions = {**(directions or {}), target_metric: target_direction}
    n = len(scenarios)
    metrics = sorted({k for s in scenarios for k in s["before"]})
    mean = lambda key, m: sum(s[key][m] for s in scenarios) / n  # noqa: E731
    return {
        "n": n,
        "mean_before": {m: mean("before", m) for m in metrics} if n else {},
        "mean_after": {m: mean("after", m) for m in metrics} if n else {},
        "mean_gain": {m: sum(_gain(s["before"][m], s["after"][m], directions.get(m, "higher")) for s in scenarios) / n
                      for m in metrics} if n else {},
        "improved": sum(_gain(s["before"][target_metric], s["after"][target_metric], target_direction) > EPS
                        for s in scenarios),
        "violations_after": sum(s["violations_after"] for s in scenarios),
    }


def judge(sets: dict, criteria: dict) -> dict:
    """sets: {세트 이름: {"scenarios": [{seed, faults, before, after, violations_after}]}, "budget_exceeded"?: bool}"""
    target = criteria["target"]
    metric, direction = target["metric"], target.get("direction", "higher")
    directions = {s["metric"]: s.get("direction", "higher") for s in criteria.get("side_effects") or []}
    names = [k for k, v in sets.items() if isinstance(v, dict) and "scenarios" in v]
    summary = {k: set_summary(sets[k]["scenarios"], metric, direction, directions) for k in names}
    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    if sets.get("budget_exceeded"):
        check("time_budget", False, "검증 시간 예산을 넘겨 일부 시나리오를 평가하지 못함")
    bad = [f"{k} seed {s['seed']} {s['violations_after']}건" for k in names for s in sets[k]["scenarios"]
           if s["violations_after"]]
    check("violations", not bad, "필수조건 위반: " + ", ".join(bad) if bad else "모든 시나리오 위반 0건")

    for k in target.get("sets", []):
        s = summary.get(k)
        if not s or not s["n"]:
            check(f"target:{k}", False, f"{k} 세트에 시나리오가 없음")
            continue
        g = s["mean_gain"][metric]
        check(f"target:{k}", g >= target["min_delta"] - EPS,
              f"{k} 평균 {metric} 개선 {g:+.4f} (기준 {target['min_delta']:+.4f} 이상)")
    for k in target.get("seed_majority", []):
        s = summary.get(k)
        if not s or not s["n"]:
            check(f"majority:{k}", False, f"{k} 세트에 시나리오가 없음")
            continue
        check(f"majority:{k}", s["improved"] * 2 > s["n"],
              f"{k} {metric} 개선 seed {s['improved']}/{s['n']} (과반 필요)")
    for se in criteria.get("side_effects") or []:
        for k in se.get("sets", []):
            s = summary.get(k)
            if not s or not s["n"]:
                check(f"side_effect:{se['metric']}:{k}", False, f"{k} 세트에 시나리오가 없음")
                continue
            worsening = -s["mean_gain"][se["metric"]]
            check(f"side_effect:{se['metric']}:{k}", worsening <= se["max_worsening"] + EPS,
                  f"{k} 평균 {se['metric']} 악화 {worsening:+.4f} (한도 {se['max_worsening']})")

    reasons = [c["detail"] for c in checks if not c["passed"]]
    return {"passed": not reasons, "checks": checks, "reasons": reasons, "summary": summary}
