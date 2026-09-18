import json

import pytest

from app.directives import compile_limits
from app.guardrails import validate_interpretations
from app.optimizer import InfeasibleError, solve
from app.pipeline import solve_and_build
from app.request_validation import parse_scenario
from app.response_builder import build_response
from app.schemas import BatteryInput, Directive, HourInput, Scenario
from app.validator import replay
from tests.conftest import load_public_cases

CASES = load_public_cases()


def scenario_and_truth(case):
    scenario = parse_scenario(json.dumps(case["input"]).encode())
    truth = validate_interpretations(
        {"interpretations": case["expected_output"]["directive_interpretation"]},
        len(scenario.operator_notes),
        scenario.battery.capacity_kwh,
    )
    return scenario, truth


NO_OP = [Directive(0, False, "no_op", None, "Unrelated note.")]


def flat_scenario(tariffs, demand=10.0, solar=0.0, battery=None, notes=1):
    hours = tuple(HourInput(h, demand, solar, tariffs[h]) for h in range(24))
    battery = battery or BatteryInput(20.0, 0.0, 0.0, 10.0, 10.0)
    return Scenario("T", tuple(f"note {i}" for i in range(notes)), hours, battery)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_public_case_is_valid_and_optimal_with_ground_truth(case):
    scenario, truth = scenario_and_truth(case)
    response = solve_and_build(scenario, truth)
    assert replay(scenario, truth, response) == []
    assert abs(response["total_cost_bdt"] - case["expected_output"]["total_cost_bdt"]) <= 0.01


def test_hand_solvable_arbitrage():
    # Cheap first half (5 BDT), expensive second half (10 BDT); the battery can move 20 kWh.
    scenario = flat_scenario([5.0] * 12 + [10.0] * 12)
    limits = compile_limits(scenario, NO_OP)
    solution = solve(scenario, limits)
    assert solution.cost == pytest.approx(12 * 10 * 5 + 12 * 10 * 10 - 20 * 5, abs=1e-6)
    response = build_response(scenario, NO_OP, limits, solution)
    assert replay(scenario, NO_OP, response) == []
    assert response["hourly_plan"][-1]["battery_energy_after_kwh"] == 0.0


def test_flat_tariff_keeps_battery_idle():
    scenario = flat_scenario([7.0] * 24)
    response = solve_and_build(scenario, NO_OP)
    assert all(row["battery_action"] == "idle" for row in response["hourly_plan"])


def test_overlapping_solar_reductions_use_smallest_factor_not_product():
    scenario = flat_scenario([5.0] * 24, solar=100.0)
    directives = [
        Directive(0, True, "solar_reduction", {"hours": [12], "factor": 0.5}, ""),
        Directive(1, True, "solar_reduction", {"hours": [12, 13], "factor": 0.2}, ""),
    ]
    limits = compile_limits(scenario, directives)
    assert limits.solar_cap[12] == pytest.approx(20.0)
    assert limits.solar_cap[13] == pytest.approx(20.0)
    assert limits.solar_cap[11] == pytest.approx(100.0)


def test_overlapping_reserves_and_grid_caps_are_combined_conservatively():
    scenario = flat_scenario([5.0] * 24, battery=BatteryInput(100.0, 50.0, 10.0, 20.0, 20.0), notes=4)
    directives = [
        Directive(0, True, "minimum_battery_reserve", {"hours": [5], "minimum_energy_kwh": 30}, ""),
        Directive(1, True, "minimum_battery_reserve", {"hours": [5], "minimum_energy_kwh": 40}, ""),
        Directive(2, True, "max_grid_window", {"hours": [6], "max_grid_kwh": 9}, ""),
        Directive(3, True, "max_grid_window", {"hours": [6], "max_grid_kwh": 8}, ""),
    ]
    limits = compile_limits(scenario, directives)
    assert limits.reserve_floor[5] == 40 and limits.reserve_floor[4] == 10
    assert limits.grid_cap[6] == 8 and limits.grid_cap[5] is None
    response = solve_and_build(scenario, directives)
    assert replay(scenario, directives, response) == []
    assert response["hourly_plan"][6]["grid_kwh"] <= 8 + 1e-9


@pytest.mark.parametrize("directive", [
    Directive(0, True, "no_charge_window", {"hours": list(range(12))}, ""),
    Directive(0, True, "no_discharge_window", {"hours": list(range(12, 24))}, ""),
])
def test_blocking_windows_are_hard_constraints(directive):
    scenario = flat_scenario([5.0] * 12 + [10.0] * 12)
    response = solve_and_build(scenario, [directive])
    assert replay(scenario, [directive], response) == []
    blocked = "charge" if directive.directive_type == "no_charge_window" else "discharge"
    for hour in directive.structured_adjustment["hours"]:
        assert response["hourly_plan"][hour]["battery_action"] != blocked


def test_infeasible_directives_raise_instead_of_relaxing():
    scenario = flat_scenario([5.0] * 24, battery=BatteryInput(100.0, 10.0, 10.0, 5.0, 5.0))
    impossible = [Directive(0, True, "minimum_battery_reserve", {"hours": [0], "minimum_energy_kwh": 90}, "")]
    with pytest.raises(InfeasibleError):
        solve(scenario, compile_limits(scenario, impossible))


def test_zero_capacity_battery_and_zero_grid_cap_with_solar():
    battery = BatteryInput(0.0, 0.0, 0.0, 0.0, 0.0)
    scenario = flat_scenario([6.0] * 24, demand=10.0, solar=15.0, battery=battery)
    directives = [Directive(0, True, "max_grid_window", {"hours": [3], "max_grid_kwh": 0}, "")]
    response = solve_and_build(scenario, directives)
    assert replay(scenario, directives, response) == []
    assert response["total_grid_kwh"] == 0.0


def test_margin_solve_is_still_valid():
    scenario, truth = scenario_and_truth(CASES[6])
    limits = compile_limits(scenario, truth)
    response = build_response(scenario, truth, limits, solve(scenario, limits, margin=1e-4))
    assert replay(scenario, truth, response) == []
