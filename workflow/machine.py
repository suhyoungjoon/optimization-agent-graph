"""워크플로우 상태와 허용 전이. 상태 변경은 transition()으로만 한다.

running → analyzed → proposed → validated → awaiting_approval → applied / rejected
중간 실패는 failed(사유 기록). 승인할 후보가 없으면 재시도(validated → proposed)하고,
재시도 상한(settings/workflow.yaml limits.max_retries)을 다 쓰면 rejected.
"""

import time

STAGES = ("1_execute", "2_analyze", "3_propose", "4_validate", "5_apply")
STAGE_FILES = {name: f"{name}.json" for name in STAGES}

TERMINAL = ("applied", "rejected", "failed")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "running": ("analyzed", "failed"),
    "analyzed": ("proposed", "failed"),
    "proposed": ("validated", "failed"),
    "validated": ("awaiting_approval", "proposed", "rejected", "failed"),   # proposed: 재시도
    "awaiting_approval": ("applied", "rejected", "failed"),
}
STATUSES = tuple(TRANSITIONS) + TERMINAL


class TransitionError(Exception):
    pass


def transition(run: dict, new: str, note: str = "") -> dict:
    """run(dict)을 제자리에서 바꾸고 이력을 남긴다. 저장은 호출한 쪽이 한다."""
    current = run["status"]
    if new not in TRANSITIONS.get(current, ()):
        raise TransitionError(f"{current} → {new} 전이는 허용되지 않음")
    run["status"] = new
    run["history"].append({"status": new, "at": time.time(), "note": note})
    return run
