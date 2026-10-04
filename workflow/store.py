"""실행 ID별 결과 저장: runs/<run_id>/run.json(메타·상태 이력)과 단계별 JSON 파일.

UI와 재현성은 이 파일들을 기준으로 한다. 인스턴스는 저장하지 않는다 (엔진·seed·결함으로 재생성).
"""

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from core import to_jsonable

RUN_FILE = "run.json"
TIMINGS_DIR = "timings"
_RUN_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]*$")


class RunStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def new_run_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)

    def run_dir(self, run_id: str) -> Path:
        if not _RUN_ID.match(run_id):
            raise ValueError(f"잘못된 실행 ID: {run_id}")
        return self.root / run_id

    def exists(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / RUN_FILE).is_file()

    def create(self, run: dict) -> None:
        directory = self.run_dir(run["run_id"])
        directory.mkdir(parents=True, exist_ok=False)
        self.save(run)

    def load(self, run_id: str) -> dict:
        if not self.exists(run_id):
            raise FileNotFoundError(f"실행 없음: {run_id}")
        return self.read(run_id, RUN_FILE)

    def save(self, run: dict) -> None:
        self.write(run["run_id"], RUN_FILE, run)

    def write(self, run_id: str, filename: str, data: Any) -> Path:
        path = self.run_dir(run_id) / filename
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(to_jsonable(data), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return path

    def read(self, run_id: str, filename: str) -> Any:
        return json.loads((self.run_dir(run_id) / filename).read_text(encoding="utf-8"))

    def record_timing(self, run_id: str, record: dict) -> None:
        """노드 실행 시간 기록. 병렬 노드가 동시에 쓰므로 노드마다 파일 하나 (timings/)."""
        directory = self.run_dir(run_id) / TIMINGS_DIR
        directory.mkdir(exist_ok=True)
        name = f"{record['started_at']:.6f}-{record['node']}-{secrets.token_hex(2)}.json"
        (directory / name).write_text(json.dumps(to_jsonable(record), ensure_ascii=False), encoding="utf-8")

    def timings(self, run_id: str) -> list[dict]:
        directory = self.run_dir(run_id) / TIMINGS_DIR
        if not directory.is_dir():
            return []
        records = [json.loads(p.read_text(encoding="utf-8")) for p in directory.glob("*.json")]
        return sorted(records, key=lambda r: r["started_at"])

    def has(self, run_id: str, filename: str) -> bool:
        return (self.run_dir(run_id) / filename).is_file()

    def list_runs(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        runs = [self.read(p.parent.name, RUN_FILE) for p in self.root.glob(f"*/{RUN_FILE}")]
        return sorted(runs, key=lambda r: r["created_at"])
