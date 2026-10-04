"""엔진 어댑터. 워크플로우는 이 모듈의 Engine 계약만 알고 특정 엔진을 모른다.

엔진 = 코어 DomainPack.solve 계약을 따르는 배정 로직 + 이 레포가 소유하는 params 파일.
"""

from pathlib import Path
from typing import Protocol

from core import DomainPack


class Engine(Protocol):
    name: str
    params_path: Path

    def load_params(self) -> dict: ...                       # params_path의 현재 내용
    def pack_factory(self, params: dict) -> DomainPack: ...   # 후보 params 평가는 항상 팩을 새로 만든다
    def spec_text(self) -> str: ...                           # 개선 agent에 보여줄 명세 (없으면 "")


def model_version(engine: Engine, params: dict) -> str:
    """모델 = 엔진 + params 버전. 예: rule@v3"""
    return f"{engine.name}@v{params['version']}"


def get_engine(name: str, params_path: str | Path | None = None) -> Engine:
    """params_path가 없으면 엔진의 기본 params 파일 (이 레포 안)."""
    if name == "rule":
        from engines.rule import DEFAULT_PARAMS, RuleEngine
        return RuleEngine(params_path or DEFAULT_PARAMS)
    raise ValueError(f"알 수 없는 엔진: {name}")
