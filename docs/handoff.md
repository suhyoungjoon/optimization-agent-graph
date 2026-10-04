# 인수인계: 코어 재사용 가이드

새 레포(수리 최적화 솔버 기반 "최적화 엔진 모델" + 워크플로우 agent)에서 이 레포의 코어를 재사용하기 위한 문서다.
**기획서(docs/plan.md)가 아니라 2026-09-30 main의 실제 코드 기준**이다. 코드와 이 문서가 다르면 코드가 맞다.

빠른 시작:

```bash
pip install "optimization-agent-harness @ git+https://github.com/suhyoungjoon/optimization-agent-harness.git@<태그 또는 커밋>"
# 예제 스크립트는 패키지에 설치되지 않는다. 이 레포의 같은 태그·커밋에서 examples/reuse_quickstart.py 파일 하나를 받아 실행한다
python reuse_quickstart.py        # 시나리오 생성 → solve → validate → metrics → 허용 범위 검사 → 시뮬레이션
```

```python
from core import load_params, params_errors, apply_params, simulate_params, Aggregator   # 코어 공개 API
from domains.dispatch import get_pack                                                    # 예제 도메인 팩
```

---

## 1. 현재 상태

| 마일스톤 | 내용 | 동작 수준 |
|---|---|---|
| M1 설계 | 계약(`core/interfaces.py`), 하네스 레벨 설정, dispatch 설정 초안, 계약 테스트, CI | 완료 |
| M2 기준선 | 가상 데이터 생성기, 규칙 엔진 solve/validate, 지표, SQLite 저장소, API, 비교 탭 | 완료. 결정적, 테스트로 고정 |
| M3 AI agent | LLM 클라이언트(캐시), 하네스 L0~L5 러너, 검증 루프·가드레일·트레이스, 실행·비교 | 완료. **가짜 LLM으로만 검증** |
| M4 분석·개선 | 분석 agent(근거 검사), 정답표 채점, 개선안 생성, 시뮬레이션, 승인·반영 | 완료. 분석·제안 품질은 **가짜 LLM으로만 검증** |
| M5 발표 | 시연 번들·재생 모드, 리허설 번들(API 키 없이), 녹화 스크립트, 서울 강남3구 지도, Windows CI | 완료 |
| M6-a | 도메인 탭(규칙·데이터 읽기 전용), params.yaml 섹션별 `docs`, 개선안이 bounds·docs를 못 바꾸게 막음 | 완료 |
| M6-b | 사람이 만든 개선안 | **미구현** (plan.md에 계획만, 결정 4개 보류) |
| M7-a | 비교 탭 하네스 파이프라인 그림 | 완료 |
| M7-b | 레벨 추이 차트, 하네스 효과 집계, AI 입력 미리보기 | **미구현** (계획만) |

동작 수준 요약:

- **확실히 동작 (결정적, 테스트 고정)**: 데이터 생성, 규칙 엔진 solve/validate, 지표, 차원 집계, 허용 범위 검사, params 시뮬레이션, 정답표 채점, params 파일 반영·버전 올림, 저장소.
- **흐름은 동작, 품질 미검증**: AI agent(L0~L5), 분석 agent, 개선 agent. 테스트는 모두 `tests/fake_llm.py`의 가짜 LLM이다. **실제 Claude API로 돌린 실험 결과는 아직 없다.**
- 테스트: pytest 164개(이 인수인계 작업에서 공개 API 테스트 2개 추가), GitHub Actions에서 ubuntu·windows 매트릭스 + 웹 빌드.

## 2. 재사용 대상 모듈

모든 이름은 `from core import <이름>`으로 쓸 수 있다(`core/__init__.py`의 `__all__`). 원래 모듈 경로도 그대로 동작한다.

### 2.1 계약 — `core/interfaces.py`

`DomainPack`(Protocol), `DecisionRecord`, `TraceRecord`, `Violation`, `ToolContext`. 원문은 [3장](#3-인터페이스-원문).

### 2.2 파라미터 — `core/params.py`

params.yaml 공용 처리: 경로 접근, 구간 조건(overrides) 적용, 허용 범위 검사. 도메인 무관.

```python
RESERVED = ("version", "overrides")
SECTION_META = ("bounds", "docs")
def parse_path(path: str) -> tuple[str, str, int | None]        # "섹션.키" 또는 "섹션.키[i]"
def get_path(params: dict, path: str) -> Any
def set_path(params: dict, path: str, value: Any) -> None       # 제자리 변경
def rule_matches(when: dict, dims: dict[str, str]) -> bool
def apply_overrides(params: dict, dims: dict[str, str]) -> dict # 항목의 차원 값에 맞는 구간 조건을 적용한 사본
def path_errors(path: str) -> list[str]                         # 예약 섹션·bounds·docs 경로면 오류
def numeric_leaves(value) -> list[Number]
def check_params(params: dict, dimensions: dict | None = None) -> list[str]   # 비면 통과
```

### 2.3 도메인 팩 로딩 — `core/registry.py`

```python
DOMAINS_DIR  # = <core의 부모>/domains  ← 설치된 패키지의 domains/만 본다 (9장 주의)
def list_domains() -> list[str]
def load_pack(domain: str, params: dict | None = None) -> DomainPack   # domains.<name>.pack.get_pack(params)
def load_params(pack: DomainPack) -> dict                               # pack.params_path()의 yaml
def load_faults(pack: DomainPack) -> dict                               # faults.yaml 전체 (정답 포함)
def list_faults(pack: DomainPack) -> list[dict]                         # [{id, name}] 정답 제외
def set_domain_files_root(root) -> None; def domain_file(domain, filename, default_dir) -> Path   # 시연 모드용
```

### 2.4 규칙 엔진 solve/validate — 도메인 팩 안 (`domains/dispatch/rule_engine.py`)

코어에는 솔버가 없다. `DomainPack.solve/validate`가 도메인별 구현이며, dispatch는 **결정적 탐욕(greedy) 휴리스틱**이다(수리 최적화 아님).

```python
def solve(inst: Instance, params: dict) -> list[DecisionRecord]           # 날짜 → 희망시간 → ID 순으로 하나씩 배정
def validate(inst: Instance, decisions: list[DecisionRecord], params: dict) -> list[Violation]
def duration_min(order, params) -> int; def travel_min(x1, y1, x2, y2, params) -> int
def dims_of(inst, order) -> dict[str, str]; def effective_params(inst, order, params) -> dict
```

새 레포의 수리 최적화 엔진은 이 자리(`DomainPack.solve`)에 들어간다. 계약은 [5장](#5-새-엔진이-맞춰야-할-계약).

### 2.5 지표 — 도메인 팩 안 (`domains/dispatch/metrics.py`)

```python
def compute(inst: Instance, decisions: list[DecisionRecord], params: dict) -> dict[str, float]
```

코어는 지표 이름을 모른다. `pack.metrics()`가 돌려주는 `dict[str, float]`을 그대로 저장·비교한다.

### 2.6 시뮬레이터 — `core/improvement/simulate.py`

```python
def simulate_params(pack_factory, instance, base_params: dict, candidate_params: dict,
                    slices: dict[str, dict] | None = None) -> dict
# pack_factory(params) -> DomainPack. 기준안과 후보안으로 각각 pack.solve → metrics, 후보안 validate.
# 반환: {"kind": "params", "items", "before": metrics, "after": metrics, "violations_after": int,
#        "slices": {이름: {"before": {items, failed, fail_rate}, "after": {...}}}, "seconds"}
```

**챔피언/도전자 비교의 출발점**이다(기준안 = 챔피언, 후보안 = 도전자). 단 인스턴스 하나, 1회 비교이고 통계 검정은 없다(8장).

### 2.7 집계 도구 — `core/analysis/aggregate_tools.py`

```python
class Aggregator:
    def __init__(self, decisions: list[DecisionRecord], dimensions: dict)
    def overview(self, args: dict) -> dict                  # 전체·상태별·사유별 건수, 차원 목록
    def aggregate(self, args: dict) -> dict                 # {group_by, filters?, sort_by?, min_items?, limit?}
    def list_items(self, args: dict) -> dict                # {filters?, reason_code?, status?, limit?}
    def count_by_decision_field(self, args: dict) -> dict   # {field, filters?, ascending?, limit?}
    def tools(self) -> list[dict]                           # LLM 도구 정의 + handler
```

"실패" = `status != "success"`(failed, blocked, pending_approval). 차원·사유 코드는 `dimensions.yaml`만 본다.

### 2.8 개선안 검사·적용 — `core/improvement/changes.py`, `approval.py`

```python
def apply_params(params: dict, proposal: dict) -> dict        # 메모리 사본에 params_changes·override_rules 적용
def params_errors(params: dict, proposal: dict, dimensions: dict) -> list[str]   # 경로 차단 + check_params
def spec_sections(text: str) -> dict[str, str]; def apply_spec(text, proposal) -> str; def spec_errors(text, proposal) -> list[str]
def write_params(params_path, proposal: dict) -> tuple[int, int]   # 파일에 쓰고 version +1, (이전, 새)
def write_spec(spec_path, proposal: dict) -> None
```

### 2.9 params 버전 관리

- `params.yaml`의 최상위 `version: int`. `write_params`가 승인 때 +1 하고 ruamel.yaml로 주석을 보존해 쓴다.
- 실행 기록(`runs` 테이블)에 실행 시점 params 전체(`params`, JSON)와 `params_version`이 남는다. 버전 이력은 **git 커밋(사람이 직접)** 과 저장소의 승인 기록(`proposals.decision`에 `params_version_before/after`)뿐이다. 롤백 함수는 없다.

### 2.10 트레이스 — `core/harness/tracer.py`

```python
class Tracer:
    def __init__(self, run_id: str, full: bool)    # full=False면 아무것도 기록하지 않음
    def record(self, item_id: str, kind: str, input: Any, output: Any) -> None   # step은 항목별 자동 증가
    records: list[TraceRecord]
```

AI agent 러너 전용이다. 규칙 엔진·개선 루프 단계는 트레이스를 남기지 않는다.

### 2.11 LLM 클라이언트 — `core/llm/client.py`, `core/llm/tool_loop.py`

```python
def load_config(path: Path = <configs/llm.yaml>) -> dict      # 환경변수 LLM_CACHE가 cache를 덮어씀
class LLMClient(Protocol):
    model: str
    def create(self, *, system: list[dict], messages: list[dict], tools: list[dict], salt: str = "") -> LLMResponse
class AnthropicClient:            # 실제 호출은 여기서만. thinking=adaptive, effort는 llm.yaml
    def __init__(self, config: dict | None = None, cache: ResponseCache | None = None, api: Any = None)
class ResponseCache:              # 요청+salt 해시 → 응답 (SQLite)
    def __init__(self, path=<패키지 위치>/runs/llm_cache.sqlite)
class Usage: calls, cached_calls, tokens; add(resp); merge(other); cost_usd(model, config); to_dict(model, config)
@dataclass class LLMResponse: content: list[dict]; stop_reason: str; usage: dict[str, int]; model: str; from_cache: bool = False

def run_tool_loop(llm, *, system: str, user: str, tools: list[dict], submit_tool: dict, max_calls: int = 30,
                  salt: str = "", check_submission=None, max_feedback: int = 1) -> LoopResult
# 도구를 쓰다 submit_tool을 부르면 끝나는 범용 루프. check_submission이 문제를 돌려주면 한 번 고쳐 오게 한다.
# LoopResult: submission, stop("submitted"|"max_calls"|"refusal"|"no_submit"), calls{tool_use_id: {name,input,output}}, usage, llm_calls, feedback_rounds, seconds
```

새 레포의 워크플로우 agent(분석·개선안 도출)는 `run_tool_loop`을 그대로 쓰면 된다.

### 2.12 그 밖의 재사용 가능 모듈

| 모듈 | 공개 이름 | 역할 |
|---|---|---|
| `core/analysis/agent.py` | `analyze(pack, instance, decisions, llm, llm_config, salt="", max_calls=30) -> dict` | 분석 agent. 발견의 수치가 인용한 도구 결과에 없으면 제외(`core/analysis/grounding.py`) |
| `core/improvement/proposer.py` | `propose(pack_factory, instance, params, spec_text, dimensions, report, llm, llm_config, salt="", max_calls=20) -> dict`, `finding_slices(report)` | 개선 agent. `simulate_params`를 도구로 쓰고, 제출안을 `params_errors`/`spec_errors`로 재검사 |
| `core/evaluation/fault_scorer.py` | `score(findings, faults) -> dict`, `matches(finding, answer)` | 정답표 대조 탐지율 |
| `core/evaluation/runner.py` | `run_rule_agent(store, pack, dataset, params, scope=None, group_id=None) -> run_id`, `run_ai_agent(...)`, `create_ai_run(...)` | 실행 + validate + metrics + 저장 |
| `core/evaluation/compare.py` | `compare(store, run_ids) -> dict`, `consistency(store, run_ids)` | 실행 간 비교표, 반복 실행 일관성 |
| `core/harness/*` | `HarnessRunner`, `Level`, `load_levels`, `get_level`, `CORE_REASON_CODES`, guardrail, validator_loop | LLM이 항목별로 결정하는 AI agent (L0~L5) |
| `core/storage/store.py` | `Store(path)`, `to_jsonable`, `dataset_id` | SQLite: datasets, runs, decisions, traces, reports, proposal_batches, proposals |

## 3. 인터페이스 원문

`core/interfaces.py` 현재 코드 그대로(발췌 없음, `ToolContext` 포함):

```python
"""코어와 도메인 팩 사이의 계약.

두 개발자가 병렬 작업하기 위한 계약이므로 필드 추가만 허용한다.
이름 변경·삭제·타입 변경은 먼저 제안하고 합의한다 (CLAUDE.md 참고).
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class DecisionRecord:            # 표준 결과 레코드
    item_id: str                 # 도메인 항목 ID
    decision: dict[str, Any] | None   # 도메인별 결정 내용, 미결정이면 None
    status: Literal["success", "failed", "blocked", "pending_approval"]
    reason_code: str | None      # dimensions.yaml에 선언된 사유 코드
    evidence: str                # 판단 근거 (사람이 읽는 문장)
    dims: dict[str, str]         # 분석 차원 값
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass
class TraceRecord:               # 표준 트레이스 레코드
    run_id: str
    item_id: str
    step: int
    kind: Literal["llm", "tool_call", "validate", "retry", "guardrail", "approval"]
    input: Any
    output: Any
    ts: float


@dataclass
class Violation:
    item_id: str
    rule: str                    # dimensions.yaml의 violation_rules에 선언된 규칙 ID
    message: str


@dataclass
class ToolContext:              # [M3 추가] 도구 handler가 받는 실행 맥락
    item_id: str                 # 지금 결정 중인 항목
    decisions: list[DecisionRecord]   # 지금까지 배정된 결정 (success, pending_approval)


class DomainPack(Protocol):
    name: str

    def generate(self, seed: int, faults: list[str]) -> tuple[Any, dict]: ...
    def items(self, instance: Any) -> list[str]: ...          # 처리 순서대로
    def solve(self, instance: Any, params: dict) -> list[DecisionRecord]: ...
    def validate(self, instance: Any, decisions: list[DecisionRecord]) -> list[Violation]: ...
    def metrics(self, instance: Any, decisions: list[DecisionRecord]) -> dict[str, float]: ...
    # 각 항목: {"name", "description", "input_schema", "handler": fn(args: dict, ctx: ToolContext) -> Any}
    # handler는 [M3 추가]. 코어는 LLM에 보낼 때 handler를 뺀다.
    def tools(self, instance: Any) -> list[dict]: ...
    def spec_path(self) -> str: ...
    def params_path(self) -> str: ...
    def dimensions(self) -> dict: ...

    # --- [M3 추가] AI agent 하네스용 ---
    def decision_schema(self) -> dict: ...                     # DecisionRecord.decision의 JSON Schema
    def item_dims(self, instance: Any, item_id: str) -> dict[str, str]: ...   # 항목의 분석 차원 값
    def approval_reasons(self, instance: Any, record: DecisionRecord,
                         decisions: list[DecisionRecord]) -> list[str]: ...    # 비면 승인 불필요
    def subset(self, instance: Any, item_ids: list[str]) -> Any: ...          # 항목 일부만 남긴 인스턴스

    # --- [M4 추가] 분석 agent용 ---
    # 차원 집계로 드러나지 않는 도메인 고유 통계 (예: 자원별 활용률).
    # 각 항목: {"name", "description", "input_schema", "handler": fn(args: dict) -> Any}
    def analysis_tools(self, instance: Any, decisions: list[DecisionRecord]) -> list[dict]: ...
```

새 레포에서 쓰는 코어 함수가 실제로 부르는 메서드만 구현하면 된다. 예: 개선 루프(solve·simulate·집계)만 쓰면 `generate`, `solve`, `validate`, `metrics`, `dimensions`, `params_path`로 충분하다. `tools`, `decision_schema`, `approval_reasons`, `subset`, `analysis_tools`, `spec_path`는 AI agent·분석 agent·API를 쓸 때 필요하다.

## 4. 데이터

### 4.1 가상 데이터 스키마 (`domains/dispatch/models.py`)

시각은 자정 기준 분(int), 좌표는 km 평면(위경도 변환 정보는 `Instance.geo`).

| 클래스 | 필드 |
|---|---|
| `Worker` | `id`(예 `WA01`), `branch`(A/B/C), `skills`(매체 기술 목록), `certs`(`pole`, `high_risk`), `cei`(int), `x`, `y`, `available: (시작분, 종료분)` |
| `Order` | `id`(예 `D01-O019`), `day`(1~10), `branch`, `work_type`(install/repair), `media`(HFC/FTTx/CATV), `difficulty`(none/pole/outdoor/high_risk), `building_type`(house/apartment), `desired`(희망시각 분), `x`, `y` |
| `Branch` | `name`(서초/강남/송파), `polygon`(관할 경계, km 평면 닫힌 링) |
| `Instance` | `branches`, `boundary_zone_km`, `workers`, `orders`, `days`, `geo` + 헬퍼 `worker_index`, `order_index`, `contains`, `distance_outside`, `area_zone` |

규모와 분포는 `domains/dispatch/dataset.yaml`: 지점 3개(서울 강남3구 실제 경계, `geo/gangnam3.geojson`), 지점당 작업자 10명, 하루 150건 × 10일 = 1500건, 희망시간 09~16시 가중치, 항목별 비율.

### 4.2 seed와 결함 옵션

`pack.generate(seed: int, faults: list[str]) -> (Instance, truth)`. 같은 seed·결함이면 항상 같은 인스턴스(`random.Random(seed)`, 결함별로 `Random(f"{seed}-{fid}")`).

| 결함 | 이름 | 주입 | 정답(answer) |
|---|---|---|---|
| P1 | 시간대 수요 집중 | 날마다 B지점 지시서의 50%를 골라 아파트·FTTx, 희망시간 9~10시대로 바꿈 | dims `{branch: B, hour: [09, 10], building_type: apartment, media: FTTx}`, 사유 `NO_TIME_MATCH`/`CAPACITY` |
| P2 | 자격자 편중 | 승주 자격자 90%를 A지점에, C지점 승주 자격자 0명 | dims `{branch: C, difficulty: pole}`, 사유 `NO_CERT` |
| P3 | 가능시간 불일치 | 작업자 20%의 가능시간을 13:00~18:00로 | metric `worker_utilization` low |
| P4 | 경계 지역 수요 | 지시서 15%의 좌표를 관할 밖 3~4km로 | dims `{area_zone: boundary}`, 사유 `OUT_OF_AREA` |

`truth = {"seed", "faults": {fid: {name, expected, answer, affected_items, affected_workers}}}`. 정답표는 분석 agent에게 주지 않고 채점(`score`)에만 쓴다.

### 4.3 시나리오 하나 만들기

```python
from domains.dispatch import get_pack
from core import load_params

pack = get_pack()                                   # 기본 params.yaml
instance, truth = pack.generate(42, ["P1", "P4"])   # 결함 없음은 []
decisions = pack.solve(instance, load_params(pack))
print(pack.metrics(instance, decisions), len(pack.validate(instance, decisions)))
small = pack.subset(instance, pack.items(instance)[:10])   # 1일차 앞 10건만
```

seed 42 + P1~P4 전체의 기준값: 1500건 중 성공 1040(69.3%), 실패 사유 OUT_OF_AREA 211·CAPACITY 168·NO_CERT 81, 위반 0건.

## 5. 새 엔진이 맞춰야 할 계약

### 5.1 `solve(instance, params) -> list[DecisionRecord]`

- **입력**: `generate()`가 만든 인스턴스(도메인 자유 타입)와 params dict(`params.yaml` 로드 결과, `bounds`·`docs` 메타 키 포함 그대로).
- **출력**: 항목마다 `DecisionRecord` 하나. 순서는 `items(instance)`와 같게 하는 것을 권장(분석·비교가 항목 ID로 맞추므로 필수는 아님).

| 필드 | 성공 | 실패 |
|---|---|---|
| `item_id` | 항목 ID | 항목 ID |
| `decision` | 도메인 결정 dict (dispatch: `{"worker_id": "WA01", "start_time": "09:00", "matching_stage": 1}`) | `None` |
| `status` | `"success"` | `"failed"` (가드레일은 `"blocked"`/`"pending_approval"`) |
| `reason_code` | `None` | `dimensions.yaml`의 `reason_codes` 키 |
| `evidence` | 사람이 읽는 판단 근거 | 실패 근거 |
| `dims` | `dimensions.yaml`의 `dimensions` 키 → 문자열 값 (**모든 항목에 같은 키**) | 같음 |
| `metrics` | 선택: 항목 단위 수치 | 선택 |

- 결정적이어야 한다(같은 입력 → 같은 출력). 시뮬레이션·챔피언/도전자 비교가 이 가정 위에 있다. 솔버의 시간 제한·난수 seed·다중 최적해 처리에 주의(8장).
- 구간 조건을 지원하려면 항목마다 `core.apply_overrides(params, dims)`로 유효 파라미터를 구해 써야 한다(dispatch: `effective_params`).

### 5.2 `params.yaml` 구조와 허용 범위

`core/params.py` 모듈 docstring이 규약이다.

```yaml
version: 1                       # 정수. 승인 때마다 +1
<섹션>:                           # 이름 자유 (version·overrides는 예약)
  <키>: 값                        # 숫자, 숫자 리스트, 숫자 dict, bool, 문자열
  bounds: {<키>: [min, max]}      # 숫자 값(리스트·dict면 모든 원소)에 같은 범위. 숫자 파라미터는 bounds 필수
  docs: {<키>: 설명}               # 선택. 화면과 개선 agent에 보임
overrides:                        # 구간 조건
  allowed_sections: [<섹션>, ...] # 구간 조건으로 바꿀 수 있는 섹션
  rules:
    - when: {<차원>: 값 | [값, ...]}          # dimensions.yaml에 선언된 차원·값만
      set: {"<섹션>.<키>": 값, "<섹션>.<키>[i]": 값}
```

- 고정값은 bounds 폭 0으로 둔다(예 `detour_factor: [1.3, 1.3]`).
- 개선안은 `bounds`·`docs`·`version`·`overrides` 경로를 `params_changes`로 바꿀 수 없다(`path_errors`).
- 검사: `check_params(params, dimensions)`가 빈 목록이면 통과.

### 5.3 metrics

- `metrics(instance, decisions) -> dict[str, float]`. 이름은 도메인이 정한다. 코어는 이름을 해석하지 않고 비교·표시만 한다.
- 결정 레코드만으로 계산해야 규칙 엔진·AI agent·새 엔진 결과를 같은 잣대로 비교할 수 있다.
- dispatch의 정의:

| 이름 | 계산 |
|---|---|
| `assignment_rate` | success 건수 ÷ 인스턴스 전체 지시서 수 |
| `avg_travel_min` | 작업자·날짜별 시간순 동선(첫 작업은 작업자 좌표 출발)의 이동시간 평균 |
| `desired_time_match_rate` | 시작시각 == 희망시각 비율 (배정 건 기준) |
| `worker_utilization` | 배정 작업소요 합 ÷ (작업자 가능시간 합 × 일수) |
| `stage_{n}_share` | 매칭 n단계 배정 비율 (n = 1..단계 수) |

### 5.4 validate()로 필수조건 판정

- `validate(instance, decisions) -> list[Violation]`. **위반 판정은 여기 한 곳에서만** 한다(솔버·AI agent·시뮬레이션 모두 이 함수로 채점).
- `Violation.rule`은 `dimensions.yaml`의 `violation_rules` 키. 비면 위반 0건.
- 판정 대상은 `status in {"success", "pending_approval"}`이고 `decision`이 있는 레코드(dispatch `PLACED_STATUSES`).
- dispatch 규칙: `skill_required`, `cert_required`, `schedule_overlap`(이동시간 포함), `outside_availability`, `invalid_decision`.
- **주의**: dispatch의 `pack.validate`·`pack.metrics`는 인자 params가 아니라 **팩을 만들 때 받은 params(`self.params`)** 를 쓴다(`solve`만 params를 인자로 받는다). 후보 params로 검증하려면 `get_pack(candidate)`로 팩을 새로 만든다. `simulate_params`가 `pack_factory`를 받는 이유다.
- 수리 최적화 엔진은 제약을 모델 안에 넣더라도, 결과는 반드시 `validate()`로 **독립 검증**할 것(솔버 허용 오차·모델링 실수 방지).

## 6. 개선 루프 현재 구현

상세 설명과 수치 샘플은 [improvement-loop.md](improvement-loop.md). 흐름과 코드 위치:

| 단계 | 하는 일 | 코드 |
|---|---|---|
| 0. 실행 | 데이터셋 재생성 → solve → validate → metrics → 저장 | `core/evaluation/runner.py` `run_rule_agent` |
| 1. 분석 | 집계 도구로 발견 도출, 근거 없는 수치는 제외 | `core/analysis/agent.py` `analyze`, `grounding.py` |
| 1-1. 채점 | 정답표 대조 (화면용) | `core/evaluation/fault_scorer.py` `score` |
| 2. 제안 | LLM이 `get_params`/`get_spec`/`simulate_params` 도구로 시험 후 `submit_proposals` | `core/improvement/proposer.py` `propose` |
| 3. 허용 범위 검사 | 제출안마다 `params_errors`(경로 차단 + `check_params`) 또는 `spec_errors` → 오류가 있으면 invalid로 표시 | `core/improvement/changes.py`, `core/params.py` |
| 4. 시뮬레이션 | params 안: 규칙 엔진 전후 재실행(`simulate_params`, 비용 없음). spec 안: AI agent를 전·후 명세로 2회 실행(비용, 확인 필요) | `core/improvement/simulate.py`, `api/improvement.py` `simulate`·`_simulate_spec_job` |
| 5. 승인 | 시뮬레이션을 마친 안만(강제 승인은 사유 필수). params → `write_params`(version +1), spec → `write_spec` | `api/improvement.py` `approve`, `core/improvement/approval.py` |
| 6. 후처리 | 같은 종류의 다른 미결 안을 `stale`로 표시, 이력 조회 | `core/storage/store.py` `mark_stale`, `decided_proposals`, `GET /history` |

개선안 상태: `proposed` → (`simulating`) → `simulated` → `approved` / `rejected`, 또는 `stale`.

개선안 형식:

```python
{"kind": "params", "title": ..., "rationale": ..., "target_findings": ["F3"],
 "params_changes": [{"path": "matching.area_extension_km[2]", "value": 4}],        # 전역
 "override_rules": [{"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 4}}]}  # 구간
{"kind": "spec", ..., "spec_edits": [{"section": "예외 처리", "text": "새 본문 전체"}]}
```

**주의**: 상태 전이(생성·시뮬레이션·승인·stale)는 코어가 아니라 **API 계층(`api/improvement.py`)** 에 있다. 새 레포의 워크플로우 agent는 코어 함수(`propose`, `params_errors`, `simulate_params`, `write_params`)를 조합해 상태 전이를 직접 구현해야 한다. `Store`의 proposals 테이블은 재사용할 수 있다.

## 7. 기획 대비 달라진 점

| 기획 (plan.md) | 실제 | 이유 |
|---|---|---|
| 가상 격자 지도, 격자 거리 | 서울 강남3구 실제 경계(GeoJSON) + km 평면, 도로 거리 = 직선 × 1.3 | 시연 현실감 요청 (M5, PR #12) |
| DomainPack 9개 메서드 | 14개: `decision_schema`, `item_dims`, `approval_reasons`, `subset`(M3), `analysis_tools`(M4) 추가, `tools()` 항목에 `handler`, `ToolContext` 추가 | AI 하네스·분석 agent 구현에 필요. 계약 규칙대로 추가만 |
| params `approval_required`가 리스트, 지역 확장 `[+3, +1, 0]` | dict(`matching_stage_gte`, `non_master`), `[0, 1, 3]`(단계가 올라갈수록 완화), `version`·`overrides`(구간 조건)·섹션별 `docs` 추가 | 검사·적용을 경로 기반으로 통일, 특정 구간만 바꾸는 개선안 지원, 화면·개선 agent용 설명 |
| AI 도구 4개 | 5개 (`get_order` 추가) | 도구 레벨에서 인스턴스 JSON 없이 지시서 정보를 얻기 위해 |
| (명시 없음) | 결정 제출은 모든 레벨에서 `submit_decision` 도구 | 출력 형식 통일. `tools` 플래그는 도메인 도구만 켠다 |
| 분석 agent 입력: 결과 + L5 트레이스 + 데이터 | 결정 레코드 + 집계 도구 + 도메인 분석 도구(`analysis_tools`). 트레이스는 읽지 않음 | 수치 근거를 도구 결과로 강제하기 위해 집계만 노출 |
| proposer 안에서 허용 범위 검사 | LLM 제출 후 코드가 재검사, bounds·docs 경로 변경 차단(M6-a) | 개선안이 허용 범위를 넓히는 보안 구멍을 막음 |
| spec 개선안 10일치 재실행 | 지정 범위(scope)만, 비용 추정 후 확인 필요 | API 비용 |
| 승인 시 git 커밋 | 앱은 파일만 쓰고 커밋은 사람이 | 사람이 변경을 확인하는 단계를 남김 |
| 저장소: 결과 JSON | 데이터셋은 (도메인, seed, 결함)만 저장하고 인스턴스는 재생성 | 결정적 생성기라 저장 불필요 |
| `?cached=true` | + 시연 번들(`--demo`), 리허설 번들(가짜 LLM) | 네트워크·API 키 없는 발표 |
| 탭 4개 | 5개 (도메인 탭 M6-a) + 하네스 설명(M7-a) | 추가 요청 |
| requirements.txt | pyproject.toml | 설치 가능한 패키지 |

## 8. 알려진 한계와 기술 부채

**검증 공백**

- 실제 LLM 실험 없음. AI agent·분석 agent·개선 agent의 품질(성공률, 탐지율, 제안 품질)과 `llm.yaml` 단가·모델 설정은 실측되지 않았다.
- 모든 LLM 경로 테스트는 가짜 LLM 시나리오다. 실제 모델 출력 형식의 변형(도구 인자 누락, 잘못된 JSON)에 대한 방어는 일부만 있다.
- 프런트엔드는 타입 검사·빌드만 CI에서 확인하고 단위 테스트는 없다. Playwright 녹화·캡처는 CI 밖이다.
- 설치된 패키지(일반 설치) 모드는 CI에서 검사하지 않는다(CI는 `pip install -e`). 이번에 수동으로만 확인했다.

**비결정성**

- 규칙 엔진·생성기·집계·시뮬레이션은 결정적이다.
- LLM 경로는 비결정적이다. 개발 중에는 `ResponseCache`(요청 + salt 해시)로 같은 입력을 재생해 반복 가능하게 만들지만, 캐시를 끄면(`LLM_CACHE=0`) 매번 달라진다. 반복 실행 일관성은 `compare.consistency`로 잰다.

**챔피언/도전자 관점의 공백 (새 레포에서 보강 필요)**

- `simulate_params`는 인스턴스 하나(같은 seed)에서 1회 비교한다. 여러 seed, 보류 데이터(holdout), 신뢰구간, 통계 검정이 없다.
- "좋아졌다"의 판정 기준(목표 지표, 허용 가능한 부작용)이 코드에 없고 사람이 표를 보고 판단한다.
- 승인된 버전으로 돌아가는 롤백 함수가 없다(git에 의존).

**성능**

- dispatch 규칙 엔진은 1500건 solve 약 0.1~0.4초. 수리 최적화 솔버는 훨씬 느릴 수 있으므로 개선 agent가 `simulate_params`를 여러 번 부르는 구조(최대 `max_calls=20`)에서 시간 예산을 따로 잡아야 한다.
- AI agent는 항목마다 순차 LLM 호출(항목당 최대 12회). 병렬화는 실행(run) 단위뿐(`llm.yaml` `concurrency`).

**경로와 파일 쓰기**

- `configs/`, `runs/`(LLM 캐시·DB 기본 위치), `.env`, 도메인 `params.yaml`·`domain-spec.md`의 기본 경로가 **패키지 설치 위치 기준**이다. 일반 설치에서 기본값을 쓰면 site-packages 안을 읽고 쓴다. 새 레포에서는 경로를 인자로 넘길 것: `load_levels(path)`, `load_config(path)`, `AnthropicClient(cache=ResponseCache(path))`, `Store(path)`, `write_params(자기 params 경로, ...)`.
- `.env.example`의 `LLM_MODEL`은 코드에서 읽지 않는다(모델은 `llm.yaml`의 `model`).

**구조**

- 개선안 상태 전이가 API 계층에 있다(6장).
- `Store`는 단일 SQLite 파일 + 스레드 락. 여러 프로세스 동시 쓰기는 고려하지 않았다.

## 9. 재사용하지 말아야 할 것

| 대상 | 이유 |
|---|---|
| `web/` (React UI) | dispatch 지도(Leaflet)·발표 흐름에 맞춘 화면. 패키지에도 포함되지 않는다 |
| `api/` (FastAPI 앱) | UI 전용 엔드포인트, 백그라운드 작업, 시연 재생이 섞여 있다. 개선안 상태 전이 로직만 참고용으로 읽을 것 |
| 시연·캐시 재생: `api/replay.py`, `scripts/snapshot.py`, `scripts/rehearsal_bundle.py`, `scripts/record_demo.mjs`, `--demo`, `cached=true`, `set_domain_files_root` | 발표용. 저장된 결과를 재생할 뿐 새 결과를 만들지 않는다 |
| `domains/dispatch/` 내부 (`generator`, `rule_engine`, `tools`, `analysis`, `geo/`, 지표 정의) | 출동 스케줄링 전용. 새 엔진의 **형식 참고용 예제**로만 쓴다 |
| `core/harness/` (L0~L5 AI agent) | LLM이 항목마다 직접 결정하는 구조. 솔버가 결정하는 새 엔진에는 해당하지 않는다(솔버 결과를 LLM이 검토하는 용도라면 `guardrail`·`validator_loop`만 참고) |
| `core/registry.load_pack` / `list_domains` | 설치된 이 패키지의 `domains/` 폴더만 탐색한다. 새 레포의 팩은 등록되지 않으므로 팩 객체를 직접 만들어 코어 함수에 넘긴다 |
| `faults.yaml`·정답표 채점 | 가상 데이터에 심은 결함 탐지 평가용. 실제 데이터에는 정답이 없다 |

**이름 충돌 주의**: 이 패키지는 최상위 이름 `core`, `domains`, `api`, `scripts`, `configs`를 site-packages에 설치한다. 새 레포에 같은 이름의 패키지·폴더(`core/`, `domains/` 등)를 만들면 import가 가려진다. 새 레포는 다른 최상위 이름(예: `engine/`, `workflow/`)을 쓸 것.

## 10. 실행 환경

| 구분 | 내용 |
|---|---|
| Python | 3.11+ (CI 3.11) |
| 배포 이름 / import 이름 | `optimization-agent-harness` / `core`, `domains.dispatch` |
| 런타임 의존성 | `pyyaml`, `ruamel.yaml`, `anthropic`, `fastapi`, `pydantic`, `uvicorn` (fastapi 계열은 코어만 쓸 때도 설치된다) |
| 개발 의존성 (`[dev]`) | `pytest`, `httpx` |
| 패키지 데이터 | `configs/*.yaml`, `domains/dispatch/*.yaml`·`*.md`·`geo/*.geojson` |
| 환경변수 | `ANTHROPIC_API_KEY`(LLM 사용 시, `.env` 가능), `LLM_CACHE=0`(캐시 끄기), `HARNESS_DB`(API·스크립트의 DB 경로), `HOST`·`PORT`(API 서버) |

설치:

```bash
# git URL (비공개 레포: GitHub 인증 필요 — gh auth login 또는 SSH 키). 재현을 위해 태그나 커밋으로 고정
pip install "optimization-agent-harness @ git+https://github.com/suhyoungjoon/optimization-agent-harness.git@<ref>"
# 로컬 경로
pip install /path/to/optimization-agent-harness          # 일반 설치
pip install -e "/path/to/optimization-agent-harness[dev]"  # 편집 모드 + 테스트 도구
```

테스트 (이 레포에서):

```bash
pip install -e ".[dev]"
pytest                                   # 164개, LLM·네트워크 없음 (약 50초)
python examples/reuse_quickstart.py      # 재사용 확인
(cd web && npm ci && npm run build)      # 프런트엔드 (재사용 대상 아님)
```

가짜 LLM으로 LLM 경로를 테스트하는 방법은 `tests/fake_llm.py`(`FakeLLM`, `submit`, `tool_use`)와 `tests/test_harness_runner.py`, `tests/test_improvement.py`를 참고한다. `tests/`는 패키지에 포함되지 않으므로 필요하면 새 레포로 복사한다.
