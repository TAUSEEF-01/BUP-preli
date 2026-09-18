import copy
import json

import pytest

from app.guardrails import validate_interpretations
from app.request_validation import parse_scenario
from app.schemas import Directive
from app.validator import replay
from tests.conftest import load_public_cases

CASES = load_public_cases()


def load(case):
    scenario = parse_scenario(json.dumps(case["input"]).encode())
    truth = validate_interpretations(
        {"interpretations": case["expected_output"]["directive_interpretation"]},
        len(scenario.operator_notes),
        scenario.battery.capacity_kwh,
    )
    return scenario, truth, copy.deepcopy(case["expected_output"])


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_organizer_reference_outputs_pass(case):
    scenario, truth, expected = load(case)
    assert replay(scenario, truth, expected) == []


def violations_after(mutate, case_index=0, extra_directives=()):
    scenario, truth, response = load(CASES[case_index])
    mutate(response)
    return replay(scenario, list(truth) + list(extra_directives), response)


def has(violations, fragment):
    return any(fragment in v for v in violations)


def test_catches_energy_balance_and_total_mismatch():
    def mutate(r):
        r["hourly_plan"][0]["grid_kwh"] += 5
    v = violations_after(mutate)
    assert has(v, "energy balance") and has(v, "total_grid_kwh")


def test_catches_wrong_cost_total():
    v = violations_after(lambda r: r.update(total_cost_bdt=r["total_cost_bdt"] + 1))
    assert has(v, "total_cost_bdt")


def test_catches_wrong_peak():
    v = violations_after(lambda r: r.update(peak_grid_kwh=1))
    assert has(v, "peak_grid_kwh")


def test_catches_idle_with_nonzero_amount():
    def mutate(r):
        r["hourly_plan"][0]["battery_kwh"] = 5  # hour 0 is idle in SAMPLE-01
    assert has(violations_after(mutate), "idle requires")


def test_catches_broken_transition_and_neutrality():
    def mutate(r):
        r["hourly_plan"][23]["battery_energy_after_kwh"] -= 10
    v = violations_after(mutate)
    assert has(v, "battery transition") and has(v, "final battery energy")


def test_catches_unordered_hours():
    def mutate(r):
        r["hourly_plan"][0], r["hourly_plan"][1] = r["hourly_plan"][1], r["hourly_plan"][0]
    assert has(violations_after(mutate), "ascending")


def test_catches_negative_and_non_finite_values():
    def mutate(r):
        r["hourly_plan"][3]["solar_used_kwh"] = -1
    assert has(violations_after(mutate), "non-negative")


def test_catches_solar_above_effective_solar():
    def mutate(r):
        row = r["hourly_plan"][12]  # effective solar is 45 kWh in SAMPLE-01
        row["solar_used_kwh"] += 10
        row["grid_kwh"] -= 10
    assert has(violations_after(mutate), "effective solar")


def test_catches_directive_violations():
    # SAMPLE-01 charges in hours 2-4, discharges in hour 1, keeps energy <= 220, imports 90 kWh at hour 0.
    assert has(violations_after(lambda r: None, extra_directives=[
        Directive(9, True, "no_charge_window", {"hours": [2]}, "")]), "no_charge_window")
    assert has(violations_after(lambda r: None, extra_directives=[
        Directive(9, True, "no_discharge_window", {"hours": [1]}, "")]), "no_discharge_window")
    assert has(violations_after(lambda r: None, extra_directives=[
        Directive(9, True, "minimum_battery_reserve", {"hours": [1], "minimum_energy_kwh": 100}, "")]), "reserve")
    assert has(violations_after(lambda r: None, extra_directives=[
        Directive(9, True, "max_grid_window", {"hours": [0], "max_grid_kwh": 50}, "")]), "exceeds the cap")
    assert has(violations_after(lambda r: None, extra_directives=[
        Directive(9, True, "solar_reduction", {"hours": [9], "factor": 0.5}, "")]), "effective solar")


def test_catches_rate_limit_violation():
    def mutate(r):
        plan = r["hourly_plan"]  # hour 2 charges 50 (the limit); push it to 60
        plan[2]["battery_kwh"] = 60
        plan[2]["grid_kwh"] += 10
        for row in plan[2:]:
            row["battery_energy_after_kwh"] += 10
    assert has(violations_after(mutate), "rate limit")


def test_catches_interpretation_shape_problems():
    def mutate(r):
        r["directive_interpretation"][1]["applies"] = True
    assert has(violations_after(mutate), "no_op requires")

    def mutate_order(r):
        r["directive_interpretation"].reverse()
    assert has(violations_after(mutate_order), "note_index")


def test_catches_scenario_id_mismatch_and_missing_fields():
    assert has(violations_after(lambda r: r.update(scenario_id="other")), "scenario_id")
    assert has(violations_after(lambda r: r.pop("plan_summary")), "missing fields")
