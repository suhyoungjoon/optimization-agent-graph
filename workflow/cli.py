"""워크플로우 CLI.

    python -m workflow run [--engine rule] [--params PATH] [--seed N] [--faults P1,P2] [--rehearsal]
    python -m workflow approve <run_id> [--proposal C1] [--note "..."]
    python -m workflow reject <run_id> [--note "..."]
    python -m workflow status [<run_id>]

경로 기본값은 이 레포 기준이다 (runs/, settings/). 코어에는 경로를 항상 인자로 넘긴다.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

from engines import get_engine

from . import runner
from .store import RunStore

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS = REPO_ROOT / "runs"
DEFAULT_SETTINGS = REPO_ROOT / "settings"
METRIC_FMT = "{:.3f}"


def _settings(directory: Path) -> dict:
    return yaml.safe_load((directory / "workflow.yaml").read_text(encoding="utf-8"))


def _make_llm(settings_dir: Path, runs_dir: Path, rehearsal: bool):
    from core import load_config
    llm_config = load_config(settings_dir / "llm.yaml")
    if rehearsal:
        try:
            from tests.rehearsal import rehearsal_llm
        except ImportError as exc:
            raise SystemExit(f"--rehearsal은 레포 루트에서 실행해야 한다 (tests/ import 실패: {exc})")
        return rehearsal_llm(), {**llm_config, "cache": False}
    from core import AnthropicClient, ResponseCache
    from core.llm.client import load_dotenv
    load_dotenv(REPO_ROOT / ".env")     # 코어 기본값은 site-packages의 .env라서 먼저 이 레포 .env를 읽는다
    cache = ResponseCache(runs_dir / "llm_cache.sqlite") if llm_config.get("cache") else None
    return AnthropicClient(config=llm_config, cache=cache), llm_config


def _print_summary(info: dict) -> None:
    print(f"run {info['run_id']}  상태={info['status']}  모델={info['model_version']}  "
          f"시나리오=seed {info['scenario']['seed']} {','.join(info['scenario']['faults']) or '결함 없음'}  "
          f"LLM={info['llm']['model']}{' (리허설)' if info['llm']['rehearsal'] else ''}")
    print(f"  이력: {' → '.join(info['history'])}")
    if ex := info.get("execute"):
        metrics = ", ".join(f"{k}={METRIC_FMT.format(v)}" for k, v in ex["metrics"].items())
        print(f"  [1 실행] {ex['items']}건, 위반 {ex['violations']}건, 실패 사유 {ex['reason_counts']}")
        print(f"           {metrics}")
    if an := info.get("analyze"):
        print(f"  [2 결과분석] 발견 {len(an['findings'])}건 (근거 없음으로 제외 {an['dropped']}건): "
              + "; ".join(an["findings"]))
    for p in info.get("propose", []):
        state = "허용 범위 통과" if not p["errors"] else "제외: " + "; ".join(p["errors"])
        print(f"  [3 개선안] {p['id']} {p['title']} — {state}")
    for c in info.get("validate", []):
        print(f"  [4 검증] {c['id']} {'승인 후보' if c['eligible'] else '탈락: ' + '; '.join(c['reasons'])}, "
              f"변경 후 위반 {c['violations_after']}건")
        for name, before in c["before"].items():
            after = c["after"].get(name)
            print(f"           {name:24s} {METRIC_FMT.format(before)} → {METRIC_FMT.format(after)}")
        for key, s in c["slices"].items():
            print(f"           구간 {key} 실패율 {s['before']['fail_rate']:.1%} → {s['after']['fail_rate']:.1%}")
    if ap := info.get("apply"):
        extra = f" {ap['model_before']} → {ap['model_after']}" if "model_after" in ap else ""
        print(f"  [5 개선적용] {ap['decision']} by {ap['by']}{extra}{' — ' + ap['note'] if ap.get('note') else ''}")
    if info.get("error"):
        print(f"  오류: {info['error']['type']}: {info['error']['message']}")
    if info["status"] == "awaiting_approval":
        print(f"  다음: python -m workflow approve {info['run_id']}  또는  reject {info['run_id']} --note '사유'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m workflow")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--settings-dir", type=Path, default=DEFAULT_SETTINGS)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="1~4단계를 실행하고 승인 대기에서 멈춘다")
    p_run.add_argument("--engine", help="기본값: settings/workflow.yaml의 engine")
    p_run.add_argument("--params", type=Path, help="챔피언 params 파일 (기본값: 엔진 기본 파일)")
    p_run.add_argument("--seed", type=int)
    p_run.add_argument("--faults", help="쉼표로 구분. 빈 문자열이면 결함 없음")
    p_run.add_argument("--rehearsal", action="store_true", help="가짜 LLM으로 실행 (API 키 불필요)")
    p_run.add_argument("--json", action="store_true")

    p_ok = sub.add_parser("approve", help="사람 승인: 후보를 params에 반영하고 버전을 올린다")
    p_ok.add_argument("run_id")
    p_ok.add_argument("--proposal", help="후보 ID (승인 후보가 하나면 생략)")
    p_ok.add_argument("--note", default="")

    p_no = sub.add_parser("reject", help="사람 반려: 기록만 남긴다")
    p_no.add_argument("run_id")
    p_no.add_argument("--note", default="")

    p_st = sub.add_parser("status", help="실행 상태와 단계별 결과")
    p_st.add_argument("run_id", nargs="?")
    p_st.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    store = RunStore(args.runs_dir)

    try:
        if args.command == "run":
            settings = _settings(args.settings_dir)
            name = args.engine or settings["engine"]
            engine = get_engine(name, args.params)
            seed = args.seed if args.seed is not None else settings["scenario"]["seed"]
            faults = ([f for f in args.faults.split(",") if f] if args.faults is not None
                      else list(settings["scenario"]["faults"]))
            llm, llm_config = _make_llm(args.settings_dir, args.runs_dir, args.rehearsal)
            run = runner.run_workflow(store, engine, seed=seed, faults=faults, llm=llm, llm_config=llm_config,
                                      limits=settings["limits"], rehearsal=args.rehearsal)
            run_id = run["run_id"]
        elif args.command == "approve":
            run_id = runner.approve(store, args.run_id, args.proposal, args.note)["run_id"]
        elif args.command == "reject":
            run_id = runner.reject(store, args.run_id, args.note)["run_id"]
        else:
            if not args.run_id:
                for r in store.list_runs():
                    print(f"{r['run_id']}  {r['status']:18s} {r['model_version']}  "
                          f"seed {r['scenario']['seed']}  {'리허설' if r['llm']['rehearsal'] else r['llm']['model']}")
                return 0
            run_id = args.run_id
    except (ValueError, FileNotFoundError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    info = runner.summary(store, run_id)
    if getattr(args, "json", False):
        print(json.dumps(info, ensure_ascii=False, indent=1))
    else:
        _print_summary(info)
    return 1 if info["status"] == "failed" else 0

