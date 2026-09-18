"""Request, response, and internal data models shared across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HOURS_PER_DAY = 24

DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)

# Exact structured_adjustment keys for every applicable directive type (Problem Statement §4.1).
ADJUSTMENT_KEYS: dict[str, tuple[str, ...]] = {
    "solar_reduction": ("hours", "factor"),
    "minimum_battery_reserve": ("hours", "minimum_energy_kwh"),
    "no_charge_window": ("hours",),
    "no_discharge_window": ("hours",),
    "max_grid_window": ("hours", "max_grid_kwh"),
}

BATTERY_ACTIONS = ("charge", "discharge", "idle")

RESPONSE_FIELDS = (
    "scenario_id",
    "directive_interpretation",
    "hourly_plan",
    "total_grid_kwh",
    "total_cost_bdt",
    "peak_grid_kwh",
    "plan_summary",
)

HOURLY_PLAN_FIELDS = (
    "hour",
    "grid_kwh",
    "solar_used_kwh",
    "battery_action",
    "battery_kwh",
    "battery_energy_after_kwh",
)


@dataclass(frozen=True)
class HourInput:
    hour: int
    demand_kwh: float
    solar_kwh: float
    tariff_bdt_per_kwh: float


@dataclass(frozen=True)
class BatteryInput:
    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    operator_notes: tuple[str, ...]
    hours: tuple[HourInput, ...]  # always sorted by hour, 0..23
    battery: BatteryInput


@dataclass(frozen=True)
class Directive:
    """One guarded interpretation entry, in the exact response shape."""

    note_index: int
    applies: bool
    directive_type: str
    structured_adjustment: dict[str, Any] | None
    explanation: str

    def to_json(self) -> dict[str, Any]:
        adjustment = None
        if self.structured_adjustment is not None:
            adjustment = {
                key: list(value) if isinstance(value, (list, tuple)) else value
                for key, value in self.structured_adjustment.items()
            }
        return {
            "note_index": self.note_index,
            "applies": self.applies,
            "directive_type": self.directive_type,
            "structured_adjustment": adjustment,
            "explanation": self.explanation,
        }

    def semantic_key(self) -> tuple:
        """Everything except the free-text explanation."""
        adjustment = self.structured_adjustment or {}
        return (
            self.note_index,
            self.applies,
            self.directive_type,
            tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in adjustment.items())),
        )
