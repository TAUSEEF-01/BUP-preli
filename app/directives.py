"""Compile validated directives into hard per-hour limits for the optimizer (Problem Statement §5.3)."""
from __future__ import annotations

from dataclasses import dataclass

from app.schemas import HOURS_PER_DAY, Directive, Scenario


@dataclass(frozen=True)
class HourlyLimits:
    solar_cap: tuple[float, ...]  # usable solar after reductions
    reserve_floor: tuple[float, ...]  # minimum battery_energy_after_kwh
    charge_cap: tuple[float, ...]
    discharge_cap: tuple[float, ...]
    grid_cap: tuple[float | None, ...]  # None means unbounded


def compile_limits(scenario: Scenario, directives: list[Directive]) -> HourlyLimits:
    battery = scenario.battery
    original_solar = [h.solar_kwh for h in scenario.hours]

    solar_cap = list(original_solar)
    reserve_floor = [battery.minimum_energy_kwh] * HOURS_PER_DAY
    charge_cap = [battery.max_charge_kwh_per_hour] * HOURS_PER_DAY
    discharge_cap = [battery.max_discharge_kwh_per_hour] * HOURS_PER_DAY
    grid_cap: list[float | None] = [None] * HOURS_PER_DAY

    for directive in directives:
        if not directive.applies:
            continue
        adjustment = directive.structured_adjustment or {}
        kind = directive.directive_type
        for hour in adjustment["hours"]:
            if kind == "solar_reduction":
                # Each reduction is its own upper bound; together they equal the smallest factor.
                solar_cap[hour] = min(solar_cap[hour], original_solar[hour] * float(adjustment["factor"]))
            elif kind == "minimum_battery_reserve":
                reserve_floor[hour] = max(reserve_floor[hour], float(adjustment["minimum_energy_kwh"]))
            elif kind == "no_charge_window":
                charge_cap[hour] = 0.0
            elif kind == "no_discharge_window":
                discharge_cap[hour] = 0.0
            elif kind == "max_grid_window":
                cap = float(adjustment["max_grid_kwh"])
                grid_cap[hour] = cap if grid_cap[hour] is None else min(grid_cap[hour], cap)

    return HourlyLimits(
        solar_cap=tuple(solar_cap),
        reserve_floor=tuple(reserve_floor),
        charge_cap=tuple(charge_cap),
        discharge_cap=tuple(discharge_cap),
        grid_cap=tuple(grid_cap),
    )
