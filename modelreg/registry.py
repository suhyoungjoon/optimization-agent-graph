"""모델 레지스트리: 엔진별 params 버전 스냅샷, 모델 카드, 챔피언 지정·이력, 되돌리기.

models/<엔진>/
  v1/params.yaml, v1/card.json     # 등록 후 바꾸지 않는다
  v2/...
  champion.json                    # {"version": n, "history": [{version, action, at, by, note}]}

새 버전은 부모 스냅샷을 복사해 코어 write_params로 개선안을 반영한다 (주석 보존). 번호는 기존 최대 +1.
등록과 챔피언 지정은 별개다. 챔피언 지정은 사람이 승인했을 때(workflow approve)와 되돌리기(rollback)뿐이다.
"""

import json
import shutil
import time
from pathlib import Path

import yaml
from core import check_params, params_errors, write_params
from ruamel.yaml import YAML

from engines import get_engine
from workflow.locks import file_lock

CHAMPION = "champion.json"
CARD = "card.json"
PARAMS = "params.yaml"


class Registry:
    def __init__(self, root: str | Path, engine: str):
        self.root = Path(root)
        self.engine = engine
        self.dir = self.root / engine

    # --- 읽기 ---------------------------------------------------------------------

    def versions(self) -> list[int]:
        if not self.dir.is_dir():
            return []
        return sorted(int(p.name[1:]) for p in self.dir.glob("v*") if p.name[1:].isdigit() and (p / CARD).is_file())

    def params_path(self, version: int) -> Path:
        return self.dir / f"v{version}" / PARAMS

    def load_params(self, version: int) -> dict:
        return yaml.safe_load(self.params_path(version).read_text(encoding="utf-8"))

    def card(self, version: int) -> dict:
        return json.loads((self.dir / f"v{version}" / CARD).read_text(encoding="utf-8"))

    def _champion_doc(self) -> dict:
        path = self.dir / CHAMPION
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"version": None, "history": []}

    def champion(self) -> int | None:
        return self._champion_doc()["version"]

    def history(self) -> list[dict]:
        return self._champion_doc()["history"]

    def find_by_run(self, run_id: str) -> int | None:
        """워크플로우 실행이 등록한 버전 (승인 반영이 중단됐다가 다시 돌 때 중복 등록을 막는다)."""
        for v in self.versions():
            if (self.card(v).get("source") or {}).get("run_id") == run_id:
                return v
        return None

    def lock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        return file_lock(self.dir / ".lock", f"모델 레지스트리({self.engine}) 변경")

    # --- 쓰기 ---------------------------------------------------------------------

    def _write_json(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def _commit_version(self, version: int, staged: Path, card: dict) -> None:
        self._write_json(staged / CARD, card)
        staged.replace(self.dir / f"v{version}")       # 다 쓴 뒤 한 번에 보이게 한다

    def bootstrap(self, seed_params: str | Path, by: str) -> int:
        """비어 있으면 seed_params를 첫 버전으로 등록하고 챔피언으로 지정한다. 이미 있으면 챔피언 번호."""
        with self.lock():
            if self.versions():
                return self.champion()
            version = int(yaml.safe_load(Path(seed_params).read_text(encoding="utf-8"))["version"])
            staged = self.dir / f".staging-v{version}"
            shutil.rmtree(staged, ignore_errors=True)
            staged.mkdir(parents=True)
            shutil.copyfile(seed_params, staged / PARAMS)
            self._commit_version(version, staged, {
                "version": version, "model_version": f"{self.engine}@v{version}", "engine": self.engine,
                "parent": None, "created_at": time.time(),
                "source": {"kind": "bootstrap", "from": str(seed_params)}})
            self._set_champion(version, by=by, note="초기 버전", action="bootstrap")
            return version

    def register(self, parent: int, proposal: dict, card: dict) -> int:
        """부모 버전에 params 개선안을 반영한 새 버전을 등록한다 (챔피언은 바꾸지 않는다)."""
        with self.lock():
            return self._register(parent, proposal, card)

    def _register(self, parent: int, proposal: dict, card: dict) -> int:
        if parent not in self.versions():
            raise ValueError(f"없는 버전: v{parent}")
        base = self.load_params(parent)
        engine = get_engine(self.engine, self.params_path(parent))
        dims = engine.pack_factory(base).dimensions()
        errors = params_errors(base, proposal, dims)
        if errors:
            raise ValueError(f"v{parent}에 반영할 수 없음: " + "; ".join(errors))
        version = max(self.versions()) + 1
        staged = self.dir / f".staging-v{version}"
        shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True)
        try:
            shutil.copyfile(self.params_path(parent), staged / PARAMS)
            write_params(staged / PARAMS, proposal)
            ry = YAML()
            doc = ry.load((staged / PARAMS).read_text(encoding="utf-8"))
            doc["version"] = version                      # write_params는 부모+1로 올리므로 번호를 맞춘다
            with (staged / PARAMS).open("w", encoding="utf-8") as f:
                ry.dump(doc, f)
            problems = check_params(yaml.safe_load((staged / PARAMS).read_text(encoding="utf-8")), dims)
            if problems:
                raise ValueError("반영 후 params 검사 실패: " + "; ".join(problems))
            self._commit_version(version, staged, {
                **card, "version": version, "model_version": f"{self.engine}@v{version}", "engine": self.engine,
                "parent": parent, "created_at": time.time(), "proposal": proposal})
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        return version

    def set_champion(self, version: int, by: str, reason: str = "", action: str = "promote") -> None:
        with self.lock():
            self._set_champion(version, by=by, note=reason, action=action)

    def _set_champion(self, version: int, by: str, note: str, action: str) -> None:
        if version not in self.versions():
            raise ValueError(f"없는 버전: v{version}")
        doc = self._champion_doc()
        doc["version"] = version
        doc["history"].append({"version": version, "action": action, "at": time.time(), "by": by, "note": note})
        self._write_json(self.dir / CHAMPION, doc)

    def rollback(self, by: str, to: int | None = None, note: str = "") -> tuple[int, int]:
        """챔피언을 이전 버전으로 되돌린다. to가 없으면 직전 챔피언. (이전 챔피언, 새 챔피언)"""
        with self.lock():
            current = self.champion()
            if to is None:
                previous = [h["version"] for h in self.history() if h["version"] != current]
                if not previous:
                    raise ValueError("되돌릴 이전 챔피언이 없음")
                to = previous[-1]
            if to not in self.versions():
                raise ValueError(f"없는 버전: v{to}")
            if to == current:
                raise ValueError(f"v{to}는 이미 챔피언")
            self._set_champion(to, by=by, note=note, action="rollback")
            return current, to
