"""Independent replay of a candidate response (Problem Statement §09, §11).

This module deliberately does not reuse the optimizer's constraint compilation: it re-derives
every limit from the request and the directives, the way the judge does, so optimizer or
serialization bugs are caught before a response is returned. The same function is used by
scripts/eval_public.py with the organizer ground-truth directives.
"""
from __future__ import annotations

import math
from typing import Any

from app.schemas import (
    ADJUSTMENT_KEYS,
    BATTERY_ACTIONS,
    DIRECTIVE_TYPES,
    HOURLY_PLAN_FIELDS,
    HOURS_PER_DAY,
    RESPONSE_FIELDS,
    Directive,
    Scenario,
)

# Much stricter than the published 0.01 kWh / 0.01 BDT tolerance.
DEFAULT_TOLERANCE = 1e-5


def _finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _check_interpretation(scenario: Scenario, entries: Any, violations: list[str]) -> None:
    note_count = len(scenario.operator_notes)
    if not isinstance(entries, list) or len(entries) != note_count:
        violations.append(f"directive_interpretation must have exactly {note_count} entries")
        return
    for position, entry in enumerate(entries):
        prefix = f"directive_interpretation[{position}]"
        if not isinstance(entry, dict):
            violations.append(f"{prefix} must be an object")
            continue
        missing = [k for k in ("note_index", "applies", "directive_type", "structured_adjustment", "explanation")
                   if k not in entry]
        if missing:
            violations.append(f"{prefix} missing {missing}")
            continue
        if entry["note_index"] != position or isinstance(entry["note_index"], bool):
            violations.append(f"{prefix}.note_index must be {position}")
        kind = entry["directive_type"]
        if kind not in DIRECTIVE_TYPES:
            violations.append(f"{prefix}.directive_type is not supported")
            continue
        if not isinstance(entry["explanation"], str):
            violations.append(f"{prefix}.explanation must be a string")
        adjustment = entry["structured_adjustment"]
        if kind == "no_op":
            if entry["applies"] is not False or adjustment is not None:
                violations.append(f"{prefix}: no_op requires applies=false and structured_adjustment=null")
            continue
        if entry["applies"] is not True:
            violations.append(f"{prefix}: {kind} requires applies=true")
        if not isinstance(adjustment, dict) or set(adjustment) != set(ADJUSTMENT_KEYS[kind]):
            violations.append(f"{prefix}: structured_adjustment shape is wrong for {kind}")
            continue
        hours = adjustment["hours"]
        if (not isinstance(hours, list) or not hours
                or any(isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 23 for h in hours)
                or hours != sorted(set(hours))):
            violations.append(f"{prefix}: hours must be unique ascending integers 0-23")
        for key in ADJUSTMENT_KEYS[kind][1:]:
            value = adjustment[key]
            if not _finite_number(value) or value < 0:
                violations.append(f"{prefix}.{key} must be a finite non-negative number")
            elif key == "factor" and value > 1:
                violations.append(f"{prefix}.factor must be at most 1")
            elif key == "minimum_energy_kwh" and value > scenario.battery.capacity_kwh:
                violations.append(f"{prefix}.minimum_energy_kwh exceeds capacity")


def replay(scenario: Scenario, directives: list[Directive], response: Any,
           tolerance: float = DEFAULT_TOLERANCE) -> list[str]:
    """Return every rule violation found in `response` when judged against `directives`."""
    violations: list[str] = []
    if not isinstance(response, dict):
        return ["response must be a JSON object"]
    missing = [field for field in RESPONSE_FIELDS if field not in response]
    if missing:
        return [f"response missing fields {missing}"]

    if response["scenario_id"] != scenario.scenario_id:
        violations.append("scenario_id does not match the request")
    if not isinstance(response["plan_summary"], str):
        violations.append("plan_summary must be a string")
    _check_interpretation(scenario, response["directive_interpretation"], violations)

    plan = response["hourly_plan"]
    if not isinstance(plan, list) or len(plan) != HOURS_PER_DAY:
        violations.append("hourly_plan must have exactly 24 entries")
        return violations
    for position, row in enumerate(plan):
        if not isinstance(row, dict) or set(row) != set(HOURLY_PLAN_FIELDS):
            violations.append(f"hourly_plan[{position}] must have exactly the fields {list(HOURLY_PLAN_FIELDS)}")
            return violations
        if row["hour"] != position or isinstance(row["hour"], bool):
            violations.append(f"hourly_plan[{position}].hour must be {position} (ascending, unique)")
            return violations
        for field in ("grid_kwh", "solar_used_kwh", "battery_kwh", "battery_energy_after_kwh"):
            if not _finite_number(row[field]) or row[field] < 0:
                violations.append(f"hour {position}: {field} must be a finite non-negative number")
        if row["battery_action"] not in BATTERY_ACTIONS:
            violations.append(f"hour {position}: battery_action must be charge, discharge, or idle")
    if violations:
        return violations

    # Organizer-style directive application, each directive checked on its own.
    battery = scenario.battery
    solar_limits = [[h.solar_kwh] for h in scenario.hours]
    reserve_limits = [[battery.minimum_energy_kwh] for _ in range(HOURS_PER_DAY)]
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    grid_limits: list[list[float]] = [[] for _ in range(HOURS_PER_DAY)]
    for directive in directives:
        if not directive.applies:
            continue
        adjustment = directive.structured_adjustment or {}
        for hour in adjustment.get("hours", []):
            if directive.directive_type == "solar_reduction":
                solar_limits[hour].append(scenario.hours[hour].solar_kwh * adjustment["factor"])
            elif directive.directive_type == "minimum_battery_reserve":
                reserve_limits[hour].append(adjustment["minimum_energy_kwh"])
            elif directive.directive_type == "no_charge_window":
                no_charge.add(hour)
            elif directive.directive_type == "no_discharge_window":
                no_discharge.add(hour)
            elif directive.directive_type == "max_grid_window":
                grid_limits[hour].append(adjustment["max_grid_kwh"])

    energy_before = battery.initial_energy_kwh
    for hour, row in enumerate(plan):
        demand = scenario.hours[hour].demand_kwh
        grid, solar, action = row["grid_kwh"], row["solar_used_kwh"], row["battery_action"]
        amount, energy_after = row["battery_kwh"], row["battery_energy_after_kwh"]
        charge = amount if action == "charge" else 0.0
        discharge = amount if action == "discharge" else 0.0

        if action == "idle" and amount != 0:
            violations.append(f"hour {hour}: idle requires battery_kwh = 0")
        expected_after = energy_before + charge - discharge
        if abs(energy_after - expected_after) > tolerance:
            violations.append(f"hour {hour}: battery transition expects {expected_after}, got {energy_after}")
        if energy_after > battery.capacity_kwh + tolerance:
            violations.append(f"hour {hour}: battery energy above capacity")
        if energy_after < max(reserve_limits[hour]) - tolerance:
            violations.append(f"hour {hour}: battery energy below the active reserve {max(reserve_limits[hour])}")
        if charge > battery.max_charge_kwh_per_hour + tolerance:
            violations.append(f"hour {hour}: charge exceeds the hourly rate limit")
        if discharge > battery.max_discharge_kwh_per_hour + tolerance:
            violations.append(f"hour {hour}: discharge exceeds the hourly rate limit")
        if hour in no_charge and charge > tolerance:
            violations.append(f"hour {hour}: charging during a no_charge_window")
        if hour in no_discharge and discharge > tolerance:
            violations.append(f"hour {hour}: discharging during a no_discharge_window")
        if solar > min(solar_limits[hour]) + tolerance:
            violations.append(f"hour {hour}: solar_used exceeds effective solar {min(solar_limits[hour])}")
        for cap in grid_limits[hour]:
            if grid > cap + tolerance:
                violations.append(f"hour {hour}: grid import {grid} exceeds the cap {cap}")
        balance = grid + solar + discharge - (demand + charge)
        if abs(balance) > tolerance:
            violations.append(f"hour {hour}: energy balance is off by {balance}")
        energy_before = energy_after

    if abs(energy_before - battery.initial_energy_kwh) > tolerance:
        violations.append("final battery energy does not equal the initial energy")

    grid_values = [row["grid_kwh"] for row in plan]
    totals = {
        "total_grid_kwh": sum(grid_values),
        "total_cost_bdt": sum(g * h.tariff_bdt_per_kwh for g, h in zip(grid_values, scenario.hours)),
        "peak_grid_kwh": max(grid_values),
    }
    for field, expected in totals.items():
        value = response[field]
        if not _finite_number(value) or abs(value - expected) > tolerance:
            violations.append(f"{field} does not match the hourly plan (expected {expected})")
    return violations
