# optimization-agent-graph

최적화 엔진("모델")을 대상으로 **실행 → 결과분석 → 개선안 도출 → 검증(챔피언/도전자) → 개선적용**을 반복하는 워크플로우 agent.
기존 레포 `optimization-agent-harness`(이하 **코어 레포**)의 코어를 패키지로 설치해 재사용한다.
전체 기획은 `docs/plan.md`, 코어 사용법은 `docs/handoff.md`(코어 레포 문서의 사본)를 본다.

## 핵심 원칙 (반드시 지킬 것)

1. **AI는 제안하고, 코드는 검증하고, 사람은 결정한다.** LLM은 분석과 개선안 도출에만 쓴다. 단계 순서, 허용 범위 검사, 시뮬레이션, 비교 판정은 코드가 한다.
2. **개선적용 앞에서는 반드시 멈춘다.** 승인 대기 상태로 저장하고, 사람이 승인 명령을 내려야 새 버전이 챔피언이 된다. 자동 승인 금지.
3. **필수조건 판정은 항상 도메인 팩의 `validate()`로 한다.** 엔진(규칙·솔버·학습형)이 제약을 자체적으로 지키더라도 결과는 `validate()`로 독립 검증한다.
4. **엔진은 교체 가능해야 한다.** 워크플로우 코드는 특정 엔진을 몰라야 한다. 엔진은 `DomainPack.solve` 계약(handoff 5장)을 따르는 어댑터로만 붙인다.
5. **모든 실행은 재현 가능해야 한다.** 워크플로우 실행 ID마다 엔진 이름, 모델 버전, 시나리오 세트(seed·결함), LLM 모델명, 단계별 결과를 저장한다.
6. **현재 마일스톤에 필요한 것만 만든다.** 다음 마일스톤 기능을 미리 만들지 않는다.

## 코어 재사용 규칙

- 코어는 **태그 또는 커밋으로 고정해** 설치한다. 코어 코드를 이 레포로 복사하지 않는다.
- 코어 수정이 필요하면 이 레포에서 우회하지 말고 먼저 알린다 (코어 레포에서 따로 처리).
- 코어는 최상위 이름 `core`, `domains`, `api`, `scripts`, `configs`를 설치한다. **이 레포에서는 이 이름의 폴더·패키지를 만들지 않는다** (import가 가려짐).
- 코어 함수의 경로 기본값은 패키지 설치 위치(site-packages)를 가리킨다. **경로는 항상 인자로 넘긴다**: `load_config(path)`, `ResponseCache(path)`, `Store(path)`, `write_params(이 레포의 params 경로, ...)`.
- dispatch 팩의 `validate`·`metrics`는 팩 생성 시 받은 params를 쓴다. 후보 params로 평가할 때는 `get_pack(candidate)`로 팩을 새로 만든다 (`pack_factory` 패턴).
- 재사용하지 않는 것: 코어 레포의 `web/`, `api/`, 시연·재생 스크립트, `core/harness/`(L0~L5), `registry.load_pack`. 이유는 handoff 9장.
- 개선안 상태 전이는 코어 레포의 API 계층에 있으므로, 이 레포의 워크플로우가 직접 구현한다 (handoff 6장). 참고만 하고 복사하지 않는다.

## 디렉터리 구조

```
workflow/        워크플로우 상태 머신, 단계(stage) 구현, CLI
engines/         엔진 어댑터 (rule: 코어 규칙 엔진 래핑 + 이 레포 소유 params.yaml, solver: M4 이후)
modelreg/        모델 레지스트리: 버전별 params 스냅샷, 모델 카드, 챔피언 지정, 되돌리기
scenarios/       시나리오 세트 정의 (학습용·검증용 seed·결함 조합)
settings/        workflow.yaml(상한·판정 기준), llm.yaml
models/          레지스트리 데이터 (엔진별 버전 디렉터리)
ui/              워크플로우 화면 (M3)
tests/           pytest (fake_llm.py는 코어 레포 tests/에서 복사)
docs/            plan.md, handoff.md(사본), langgraph-multiagent.md(M1 이후 보강 기획)
runs/            실행 결과·DB·LLM 캐시 (git 제외)
```

## 워크플로우 단계와 주체

| 단계 | 주체 | 코어 재사용 |
|---|---|---|
| 1. 실행 | 코드 | `pack.solve`, `validate`, `metrics` |
| 2. 결과분석 | AI + 코드 | `analyze`, `Aggregator`, 근거 검사 |
| 3. 개선안 도출 | AI + 코드 | `propose`, `params_errors` |
| 4. 검증(비교) | 코드 | `simulate_params` (M2에서 여러 seed·검증용 세트로 확장) |
| 5. 개선적용 | 사람 + 코드 | `write_params` 대신 레지스트리에 새 버전 등록 |

## 기술 스택

Python 3.11+, 코어 패키지(`optimization-agent-harness`), PyYAML, pytest. LLM은 코어의 `AnthropicClient`·`run_tool_loop`을 쓴다. UI는 M3에서 결정 (코어 레포의 React UI는 재사용하지 않음).

## 개발 규칙

- API 키는 `.env`. `.env`, `runs/`는 커밋하지 않는다.
- LLM 경로는 가짜 LLM(`tests/fake_llm.py`) 테스트를 먼저 만든다. 실제 API 실행은 사람이 요청할 때만.
- 결정적인 부분(시나리오 생성, 비교 판정, 레지스트리)은 테스트를 먼저 작성한다.
- 반복·비용 상한은 `settings/workflow.yaml`에서 읽는다 (개선 루프 횟수, 단계별 LLM 호출 수).
- 커밋 메시지: `[M1] workflow: add run stage` 형식.

## 명령어

```bash
pip install -e ".[dev]"      # 코어 포함 설치 (pyproject.toml에 코어를 커밋으로 고정)
pytest
python -m workflow run --rehearsal                       # 가짜 LLM으로 한 바퀴 (승인 대기에서 멈춤)
python -m workflow run --seed 7 --faults P1,P4 --params <params 경로> --rehearsal
python -m workflow status [<run_id>]                     # 실행 목록 / 단계별 결과
python -m workflow approve <run_id> [--proposal C1]      # 사람 승인 → params version +1
python -m workflow reject <run_id> --note "사유"          # 사람 반려 (기록만)
```
M1에서는 시나리오를 `--seed`·`--faults`(기본값 `settings/workflow.yaml`)로 지정한다. `--scenario train`(시나리오 세트)은 M2.
기본 params 파일은 `engines/rule/params.yaml`이다. 리허설에서 이 파일을 바꾸지 않으려면 복사본을 `--params`로 넘긴다.
(명령이 바뀌면 이 섹션을 갱신한다.)

## 작업 방식

- 한 번에 하나의 마일스톤(`docs/plan.md` 6장)만 진행한다.
- 작업 전 계획을 보여주고, 끝나면 만든 파일·테스트 결과·결정할 사항을 요약한다.
- 기획과 다르게 구현해야 하면 먼저 알린다.
