# optimization-agent-graph

최적화 엔진("모델")을 **실행 → 결과분석 → 개선안 도출 → 검증(비교) → (승인 대기) → 개선적용**으로 반복 개선하는 워크플로우 agent.
코어 [optimization-agent-harness](https://github.com/suhyoungjoon/optimization-agent-harness)를 커밋으로 고정해 패키지로 재사용한다.

- 기획: [docs/plan.md](docs/plan.md) · 보강 기획(LangGraph·멀티에이전트): [docs/langgraph-multiagent.md](docs/langgraph-multiagent.md) · 코어 사용법: [docs/handoff.md](docs/handoff.md) · 작업 규칙: [CLAUDE.md](CLAUDE.md)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
python -m workflow run --rehearsal       # API 키 없이 가짜 LLM으로 승인 대기까지
python -m workflow approve <run_id>      # 체크포인트에서 재개해 새 모델 버전 등록 + 챔피언 지정
python -m workflow models                # 모델 버전·챔피언 이력
python -m workflow rollback --note "사유" # 이전 챔피언으로 되돌리기
python -m workflow graph                 # 그래프를 Mermaid로 출력
python -m workflow run --analysis multi --rehearsal   # 멀티에이전트 분석
python -m workflow compare-analysis --rehearsal       # 단일 대 멀티 분석 비교
```

실행 결과는 `runs/<run_id>/`에 단계별 JSON(`1_execute.json` … `5_apply.json`)으로 남는다.
모델 버전은 `models/<엔진>/`(params 스냅샷, 모델 카드, 챔피언 이력), 시나리오 세트는 `scenarios/`, 판정 기준은 `settings/workflow.yaml`에 있다.
