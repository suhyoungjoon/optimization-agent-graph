# optimization-agent-graph

최적화 엔진("모델")을 **실행 → 결과분석 → 개선안 도출 → 검증(비교) → (승인 대기) → 개선적용**으로 반복 개선하는 워크플로우 agent.
코어 [optimization-agent-harness](https://github.com/suhyoungjoon/optimization-agent-harness)를 커밋으로 고정해 패키지로 재사용한다.

- 기획: [docs/plan.md](docs/plan.md) · 코어 사용법: [docs/handoff.md](docs/handoff.md) · 작업 규칙: [CLAUDE.md](CLAUDE.md)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
python -m workflow run --rehearsal       # API 키 없이 가짜 LLM으로 승인 대기까지
python -m workflow approve <run_id>      # params version +1
```

실행 결과는 `runs/<run_id>/`에 단계별 JSON(`1_execute.json` … `5_apply.json`)으로 남는다.
