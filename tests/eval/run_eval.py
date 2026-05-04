"""Eval harness: real LLM, real fixtures, real scorecard.

Run: .venv/bin/python tests/eval/run_eval.py [--model qwen2.5:14b]

Reads tests/eval/fixtures/*.txt, looks up tests/eval/ground_truth.jsonl,
runs each through parse_email() with the configured Ollama model, scores
per-field accuracy, prints a scorecard, appends a JSONL row to
tests/eval/results.jsonl for trend tracking.

Different from tests/test_parser.py: those stub the LLM (testing parser
plumbing). This script calls the real model (testing accuracy). It is
slow on purpose and is NOT part of the default pytest run.

Exit codes: 0 = threshold met, 1 = invalid setup, 2 = threshold not met.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date as _date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from parser import parse_email  # noqa: E402
from poller import _load_config, _ollama_call_factory  # noqa: E402

EVAL_DIR = ROOT / "tests" / "eval"
FIXTURES_DIR = EVAL_DIR / "fixtures"
GROUND_TRUTH = EVAL_DIR / "ground_truth.jsonl"
RESULTS = EVAL_DIR / "results.jsonl"

# Per TODOS.md TODO-4: minimum acceptable accuracy on the critical fields.
CRITICAL_FIELDS = ("amount", "merchant", "category")
THRESHOLD = 0.80


@dataclass
class FixtureCase:
    fixture: str
    from_addr: str
    subject: str
    received_at: _date
    expected: dict


@dataclass
class FixtureResult:
    fixture: str
    passed: bool
    field_results: dict[str, bool]
    actual: dict | None
    reason: str
    elapsed_s: float


def _load_cases() -> list[FixtureCase]:
    if not GROUND_TRUTH.exists():
        sys.exit(f"missing {GROUND_TRUTH}")
    cases = []
    for line in GROUND_TRUTH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        cases.append(FixtureCase(
            fixture=d["fixture"],
            from_addr=d["from_addr"],
            subject=d["subject"],
            received_at=_date.fromisoformat(d["received_at"]),
            expected=d["expected"],
        ))
    return cases


def _field_match(field: str, expected, actual) -> bool:
    if field == "amount":
        try:
            return abs(float(expected) - float(actual)) < 0.01
        except (TypeError, ValueError):
            return False
    return expected == actual


def _score_one(case: FixtureCase, ollama_call) -> FixtureResult:
    body_path = FIXTURES_DIR / case.fixture
    if not body_path.exists():
        return FixtureResult(case.fixture, False, {}, None,
                             f"fixture file missing: {body_path}", 0.0)
    body = body_path.read_text()
    t0 = time.perf_counter()
    result = parse_email(
        from_addr=case.from_addr, subject=case.subject,
        body_text=body, ollama_call=ollama_call,
        received_at=case.received_at,
    )
    elapsed = time.perf_counter() - t0

    if result.transaction is None:
        return FixtureResult(case.fixture, False, {}, None,
                             result.reason, elapsed)

    actual = result.transaction.model_dump(mode="json")
    field_results = {
        f: _field_match(f, exp, actual.get(f))
        for f, exp in case.expected.items()
    }
    return FixtureResult(case.fixture, all(field_results.values()),
                         field_results, actual, "ok", elapsed)


def _print_scorecard(results: list[FixtureResult],
                     cases_by_fixture: dict[str, FixtureCase],
                     model: str) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"EVAL RUN — model={model}  fixtures={len(results)}  ts={ts}")
    print("=" * 72)

    total_fields = total_field_pass = 0
    crit_total = crit_pass = 0
    for r in results:
        verdict = "PASS" if r.passed else "FAIL"
        n_pass = sum(1 for v in r.field_results.values() if v)
        n_total = len(r.field_results) or 1
        print(f"{r.fixture:<40} {verdict}  "
              f"({n_pass}/{n_total} fields, {r.elapsed_s:.2f}s)")
        if not r.passed:
            if r.actual is None:
                print(f"  └─ no transaction returned — reason: {r.reason}")
            else:
                expected = cases_by_fixture[r.fixture].expected
                for f, ok in r.field_results.items():
                    if not ok:
                        print(f"  └─ {f}: expected={expected[f]!r} "
                              f"got={r.actual.get(f)!r}")
        total_fields += n_total
        total_field_pass += n_pass
        for f in CRITICAL_FIELDS:
            if f in r.field_results:
                crit_total += 1
                crit_pass += 1 if r.field_results[f] else 0

    print("-" * 72)
    cases_passed = sum(1 for r in results if r.passed)
    field_acc = total_field_pass / total_fields if total_fields else 0.0
    crit_acc = crit_pass / crit_total if crit_total else 0.0
    threshold_met = crit_acc >= THRESHOLD
    print(f"OVERALL: {cases_passed}/{len(results)} cases fully pass | "
          f"{total_field_pass}/{total_fields} fields ({field_acc:.1%}) | "
          f"critical {crit_pass}/{crit_total} ({crit_acc:.1%})")
    badge = "MET" if threshold_met else "NOT MET"
    print(f"TODO-4 threshold (≥{THRESHOLD:.0%} on amount/merchant/category): {badge}")
    print("=" * 72)

    return {
        "ts": ts, "model": model,
        "fixtures": len(results), "cases_passed": cases_passed,
        "field_accuracy": round(field_acc, 4),
        "critical_accuracy": round(crit_acc, 4),
        "threshold_met": threshold_met,
        "per_fixture": [
            {"fixture": r.fixture, "passed": r.passed,
             "field_results": r.field_results,
             "elapsed_s": round(r.elapsed_s, 3),
             "reason": r.reason}
            for r in results
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run LLM eval against fixtures.")
    ap.add_argument("--model", help="Override model from config.toml")
    args = ap.parse_args()

    cfg = _load_config()
    if args.model:
        cfg["llm"]["model"] = args.model
    ollama_call = _ollama_call_factory(cfg)
    model = cfg["llm"]["model"]

    cases = _load_cases()
    if not cases:
        print("no fixtures in ground_truth.jsonl")
        return 1
    cases_by_fixture = {c.fixture: c for c in cases}

    results = [_score_one(c, ollama_call) for c in cases]
    summary = _print_scorecard(results, cases_by_fixture, model)

    with RESULTS.open("a") as f:
        f.write(json.dumps(summary) + "\n")
    print(f"\nappended to {RESULTS.relative_to(ROOT)}")

    return 0 if summary["threshold_met"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
