"""Score a running service on the ten public cases, the way the judge does.

For every case: POST the input, compare the structured interpretation with the public
expected output (explanation text ignored), replay the returned schedule against the
ORGANIZER directives (not our own), and compare cost with the public optimum.

Usage:
    python scripts/eval_public.py --base-url http://localhost:8000
    python scripts/eval_public.py --base-url https://your-deployment.example.com --repeat 3
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.guardrails import validate_interpretations  # noqa: E402
from app.request_validation import parse_scenario  # noqa: E402
from app.validator import replay  # noqa: E402

DEFAULT_CASES = ROOT / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
TOLERANCE = 0.01


def interpretation_errors(got: list, expected: list) -> list[str]:
    errors = []
    if len(got) != len(expected):
        return [f"expected {len(expected)} entries, got {len(got)}"]
    for g, e in zip(got, expected):
        i = e["note_index"]
        if g.get("note_index") != i:
            errors.append(f"note {i}: wrong note_index")
        if g.get("applies") != e["applies"] or g.get("directive_type") != e["directive_type"]:
            errors.append(f"note {i}: got {g.get('directive_type')}, expected {e['directive_type']}")
            continue
        ga, ea = g.get("structured_adjustment"), e["structured_adjustment"]
        if ea is None:
            if ga is not None:
                errors.append(f"note {i}: adjustment should be null")
            continue
        if not isinstance(ga, dict) or set(ga) != set(ea):
            errors.append(f"note {i}: adjustment shape {ga}")
            continue
        if ga["hours"] != ea["hours"]:
            errors.append(f"note {i}: hours {ga['hours']} != {ea['hours']}")
        for key in set(ea) - {"hours"}:
            if abs(float(ga[key]) - float(ea[key])) > TOLERANCE:
                errors.append(f"note {i}: {key} {ga[key]} != {ea[key]}")
    return errors


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--repeat", type=int, default=1, help="send every case this many times")
    parser.add_argument("--timeout", type=float, default=35.0)
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))["cases"]
    url = args.base_url.rstrip("/") + "/optimize-energy"
    latencies: list[float] = []
    totals = {"requests": 0, "http_ok": 0, "interpretation_ok": 0, "valid": 0, "optimal": 0}
    ratios: list[float] = []

    with httpx.Client(timeout=args.timeout) as client:
        health = client.get(args.base_url.rstrip("/") + "/health")
        print(f"GET /health -> {health.status_code} {health.text.strip()}")
        for _ in range(args.repeat):
            for case in cases:
                totals["requests"] += 1
                expected = case["expected_output"]
                started = time.perf_counter()
                try:
                    response = client.post(url, json=case["input"])
                except httpx.HTTPError as exc:
                    latencies.append(args.timeout * 1000)
                    print(f"{case['id']}: request failed ({type(exc).__name__})")
                    ratios.append(0.0)
                    continue
                latency = (time.perf_counter() - started) * 1000
                latencies.append(latency)
                if response.status_code != 200:
                    print(f"{case['id']}: HTTP {response.status_code} {response.text[:200]}")
                    ratios.append(0.0)
                    continue
                totals["http_ok"] += 1
                body = response.json()

                interp = interpretation_errors(body.get("directive_interpretation", []),
                                               expected["directive_interpretation"])
                scenario = parse_scenario(json.dumps(case["input"]).encode())
                truth = validate_interpretations({"interpretations": expected["directive_interpretation"]},
                                                 len(scenario.operator_notes), scenario.battery.capacity_kwh)
                violations = replay(scenario, truth, body, tolerance=TOLERANCE)
                cost = body.get("total_cost_bdt")
                valid = not violations
                ratio = min(1.0, expected["total_cost_bdt"] / cost) if valid and cost else (1.0 if valid else 0.0)
                optimal = valid and abs(cost - expected["total_cost_bdt"]) <= TOLERANCE
                ratios.append(ratio)
                totals["interpretation_ok"] += not interp
                totals["valid"] += valid
                totals["optimal"] += optimal

                status = "PASS" if not interp and optimal else "FAIL"
                print(f"{case['id']}: {status} {latency:7.0f} ms  interp={'ok' if not interp else interp}  "
                      f"valid={valid}  cost={cost} (ref {expected['total_cost_bdt']})")
                for v in violations[:3]:
                    print(f"    violation: {v}")

    n = totals["requests"]
    print("\nSummary")
    print(f"  HTTP 200:              {totals['http_ok']}/{n}")
    print(f"  interpretation exact:  {totals['interpretation_ok']}/{n}")
    print(f"  valid vs ground truth: {totals['valid']}/{n}")
    print(f"  optimal cost:          {totals['optimal']}/{n}")
    print(f"  mean quality ratio:    {sum(ratios) / len(ratios):.4f}")
    print(f"  latency p50/p95/max:   {percentile(latencies, 50):.0f} / {percentile(latencies, 95):.0f} / "
          f"{max(latencies):.0f} ms")
    return 0 if totals["interpretation_ok"] == totals["optimal"] == n else 1


if __name__ == "__main__":
    sys.exit(main())
