"""규칙 엔진 어댑터: 코어 dispatch 팩의 결정적 탐욕 규칙 엔진을 감싼다.

코어 코드는 고치지 않는다. DispatchPack을 상속해 params 파일 위치만 이 레포 파일로 바꾼다
(코어 기본값은 site-packages 안의 params.yaml을 가리킨다).
"""

from pathlib import Path

import yaml

from domains.dispatch import DispatchPack

NAME = "rule"
DEFAULT_PARAMS = Path(__file__).resolve().parent / "params.yaml"


class RulePack(DispatchPack):
    """params_path만 이 레포 파일을 가리키는 dispatch 팩. validate·metrics는 생성 시 받은 params를 쓴다."""

    def __init__(self, params: dict, params_path: Path):
        self._params_path = Path(params_path)
        super().__init__(params)

    def params_path(self) -> str:
        return str(self._params_path)


class RuleEngine:
    name = NAME

    def __init__(self, params_path: str | Path = DEFAULT_PARAMS):
        self.params_path = Path(params_path)

    def load_params(self) -> dict:
        return yaml.safe_load(self.params_path.read_text(encoding="utf-8"))

    def pack_factory(self, params: dict) -> RulePack:
        return RulePack(params, self.params_path)

    def spec_text(self) -> str:
        # 규칙 엔진은 domain-spec.md를 읽지 않는다 (명세는 AI agent용). 개선 대상은 params뿐이다.
        return ""
