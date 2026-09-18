"""Measure the configured LLM's interpretation accuracy on original labelled paraphrases.

Calls the interpreter in-process (same prompt, guardrails and repair path as the API) using the
LLM settings from the environment / .env. The cache is disabled so every note hits the model.

Usage:
    python scripts/eval_paraphrases.py
    python scripts/eval_paraphrases.py --only R02 G05 --concurrency 3
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.llm.interpreter import InterpretationError, Interpreter  # noqa: E402
from app.schemas import BatteryInput, HourInput, Scenario  # noqa: E402

DATA = ROOT / "eval_data" / "paraphrases.json"
TOLERANCE = 0.01


def scenario_for(item: dict) -> Scenario:
    capacity, minimum = float(item["capacity_kwh"]), float(item["minimum_energy_kwh"])
    hours = tuple(HourInput(h, 100.0, 0.0, 10.0) for h in range(24))
    battery = BatteryInput(capacity, (capacity + minimum) / 2, minimum, capacity / 4, capacity / 4)
    return Scenario(item["id"], tuple(item["notes"]), hours, battery)


def compare(directive, expected: dict) -> dict[str, bool]:
    kind = expected["directive_type"]
    got_adj, exp_adj = directive.structured_adjustment, expected["structured_adjustment"]
    result = {"applies": directive.applies == (kind != "no_op"), "type": directive.directive_type == kind}
    if exp_adj is None:
        result["hours"] = result["value"] = got_adj is None
    elif got_adj is None or not result["type"]:
        result["hours"] = result["value"] = False
    else:
        result["hours"] = got_adj.get("hours") == exp_adj["hours"]
        keys = set(exp_adj) - {"hours"}
        result["value"] = all(abs(float(got_adj.get(k, math.inf)) - float(exp_adj[k])) <= TOLERANCE for k in keys)
    result["entry"] = all(result.values())
    return result


async def run(items: list[dict], concurrency: int) -> None:
    settings = dataclasses.replace(load_settings(), cache_size=0, warmup=False)
    interpreter = Interpreter.from_settings(settings)
    if not interpreter.configured:
        print("No LLM configured. Set LLM_PROVIDER, LLM_MODEL and LLM_API_KEY (see .env.example).")
        return
    print(f"Model: {settings.primary.label if settings.primary else '-'}  items: {len(items)}\n")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(item):
        async with semaphore:
            started = time.perf_counter()
            try:
                result = await interpreter.interpret(scenario_for(item), time.monotonic() + 25)
                return item, result.directives, (time.perf_counter() - started) * 1000, None
            except InterpretationError as exc:
                return item, None, (time.perf_counter() - started) * 1000, str(exc)

    outcomes = await asyncio.gather(*(one(item) for item in items))
    await interpreter.aclose()

    scores: dict[str, Counter] = {"core": Counter(), "assumption": Counter()}
    per_type: Counter = Counter()
    per_type_ok: Counter = Counter()
    latencies = []
    for item, directives, latency, error in outcomes:
        latencies.append(latency)
        group = "assumption" if item.get("assumption") else "core"
        for index, expected in enumerate(item["expected"]):
            scores[group]["notes"] += 1
            per_type[expected["directive_type"]] += 1
            if directives is None:
                print(f"{item['id']}[{index}] FAILED: {error}")
                continue
            result = compare(directives[index], expected)
            scores[group].update(k for k, ok in result.items() if ok)
            per_type_ok[expected["directive_type"]] += result["entry"]
            if not result["entry"]:
                got = directives[index]
                print(f"{item['id']}[{index}] MISMATCH  note: {item['notes'][index]!r}\n"
                      f"    expected {expected['directive_type']} {expected['structured_adjustment']}\n"
                      f"    got      {got.directive_type} {got.structured_adjustment}")

    for group, counter in scores.items():
        total = counter["notes"]
        if not total:
            continue
        print(f"\n{group} notes: {total}")
        for field in ("applies", "type", "hours", "value", "entry"):
            print(f"  {field:8s} {counter[field]}/{total} ({counter[field] / total:.0%})")
    print("\nComplete-entry accuracy by expected type:")
    for kind in sorted(per_type):
        print(f"  {kind:24s} {per_type_ok[kind]}/{per_type[kind]}")
    ordered = sorted(latencies)
    print(f"\nLatency per request p50/p95/max: {ordered[len(ordered) // 2]:.0f} / "
          f"{ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]:.0f} / {ordered[-1]:.0f} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", help="item ids to run")
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    items = json.loads(DATA.read_text(encoding="utf-8"))["cases"]
    if args.only:
        items = [item for item in items if item["id"] in set(args.only)]
    asyncio.run(run(items, max(1, args.concurrency)))


if __name__ == "__main__":
    main()
