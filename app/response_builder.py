"""Turn solver output into the exact response schema (Problem Statement §10).

Battery energy is rounded first and every other hourly value is derived from the rounded
states, so transitions, energy balance, and end-of-day neutrality stay consistent in the
emitted numbers. Aggregates are computed from the emitted rows.
"""
from __future__ import annotations

from typing import Any

from app.directives import HourlyLimits
from app.optimizer import Solution
from app.schemas import HOURS_PER_DAY, Directive, Scenario

DECIMALS = 6


def _clean(value: float) -> float:
    rounded = round(float(value), DECIMALS)
    return 0.0 if rounded == 0 else rounded  # also turns -0.0 into 0.0


def _format_number(value: float) -> str:
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _format_hours(hours: list[int]) -> str:
    """[12, 13, 18] -> '12:00-14:00, 18:00-19:00'."""
    if not hours:
        return "no hours"
    ranges: list[tuple[int, int]] = []
    start = prev = hours[0]
    for hour in hours[1:]:
        if hour != prev + 1:
            ranges.append((start, prev + 1))
            start = hour
        prev = hour
    ranges.append((start, prev + 1))
    return ", ".join(f"{a:02d}:00-{b:02d}:00" for a, b in ranges)


def _describe(directive: Directive) -> str:
    adjustment = directive.structured_adjustment or {}
    window = _format_hours(adjustment.get("hours", []))
    kind = directive.directive_type
    if kind == "solar_reduction":
        return f"usable solar limited to {_format_number(float(adjustment['factor']) * 100)}% during {window}"
    if kind == "minimum_battery_reserve":
        return f"battery reserve of at least {_format_number(adjustment['minimum_energy_kwh'])} kWh during {window}"
    if kind == "no_charge_window":
        return f"no battery charging during {window}"
    if kind == "no_discharge_window":
        return f"no battery discharging during {window}"
    if kind == "max_grid_window":
        return f"grid import capped at {_format_number(adjustment['max_grid_kwh'])} kWh per hour during {window}"
    return kind


def _summary(scenario: Scenario, directives: list[Directive], rows: list[dict[str, Any]],
             total_grid: float, total_cost: float, peak: float) -> str:
    parts: list[str] = []
    applied = [d for d in directives if d.applies]
    ignored = [d.note_index for d in directives if not d.applies]
    if applied:
        parts.append("Applied operator directives: " + "; ".join(_describe(d) for d in applied) + ".")
    if ignored:
        notes = ", ".join(str(i) for i in ignored)
        parts.append(f"Note(s) {notes} do not affect today's schedule and were treated as no_op.")

    charge_hours = [r["hour"] for r in rows if r["battery_action"] == "charge"]
    discharge_hours = [r["hour"] for r in rows if r["battery_action"] == "discharge"]
    initial = _format_number(scenario.battery.initial_energy_kwh)
    if charge_hours or discharge_hours:
        parts.append(
            f"The battery charges during {_format_hours(charge_hours)} and discharges during "
            f"{_format_hours(discharge_hours)} to shift energy toward higher-tariff hours, "
            f"ending the day back at {initial} kWh."
        )
    else:
        parts.append(f"The battery stays idle at {initial} kWh because shifting energy would not lower cost.")
    parts.append(
        f"Total grid import is {_format_number(total_grid)} kWh at a cost of "
        f"{_format_number(total_cost)} BDT, with a peak of {_format_number(peak)} kWh in one hour."
    )
    return " ".join(parts)


def build_response(scenario: Scenario, directives: list[Directive], limits: HourlyLimits,
                   solution: Solution) -> dict[str, Any]:
    battery = scenario.battery
    rows: list[dict[str, Any]] = []
    previous_energy = battery.initial_energy_kwh

    for hour in range(HOURS_PER_DAY):
        if hour == HOURS_PER_DAY - 1:
            energy = battery.initial_energy_kwh  # neutrality is exact, not rounded
        else:
            energy = min(max(solution.energy[hour], limits.reserve_floor[hour]), battery.capacity_kwh)
            energy = _clean(energy)

        net = _clean(energy - previous_energy)  # > 0 charge, < 0 discharge
        if net > 0:
            action, battery_kwh = "charge", net
        elif net < 0:
            action, battery_kwh = "discharge", -net
        else:
            action, battery_kwh = "idle", 0.0

        solar_used = min(max(_clean(solution.solar[hour]), 0.0), limits.solar_cap[hour])
        grid = scenario.hours[hour].demand_kwh + net - solar_used
        if grid < 0:
            # Solver noise: curtail a sliver of solar instead of emitting negative grid import.
            solar_used = max(0.0, solar_used + grid)
            grid = 0.0

        rows.append({
            "hour": hour,
            "grid_kwh": _clean(grid),
            "solar_used_kwh": 0.0 if solar_used == 0 else solar_used,
            "battery_action": action,
            "battery_kwh": battery_kwh,
            "battery_energy_after_kwh": 0.0 if energy == 0 else energy,
        })
        previous_energy = energy

    grid_values = [r["grid_kwh"] for r in rows]
    total_grid = _clean(sum(grid_values))
    total_cost = _clean(sum(g * h.tariff_bdt_per_kwh for g, h in zip(grid_values, scenario.hours)))
    peak = max(grid_values)

    return {
        "scenario_id": scenario.scenario_id,
        "directive_interpretation": [d.to_json() for d in directives],
        "hourly_plan": rows,
        "total_grid_kwh": total_grid,
        "total_cost_bdt": total_cost,
        "peak_grid_kwh": peak,
        "plan_summary": _summary(scenario, directives, rows, total_grid, total_cost, peak),
    }
