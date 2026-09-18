"""Deterministic request checks: 400 for structural problems, 422 for semantic ones."""
from __future__ import annotations

import json
import math
from typing import Any

from app.schemas import HOURS_PER_DAY, BatteryInput, HourInput, Scenario

TOP_LEVEL_FIELDS = ("scenario_id", "operator_notes", "hours", "battery")
HOUR_FIELDS = ("hour", "demand_kwh", "solar_kwh", "tariff_bdt_per_kwh")
BATTERY_FIELDS = (
    "capacity_kwh",
    "initial_energy_kwh",
    "minimum_energy_kwh",
    "max_charge_kwh_per_hour",
    "max_discharge_kwh_per_hour",
)


class RequestValidationError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _bad(message: str) -> RequestValidationError:
    return RequestValidationError(400, message)


def _unprocessable(message: str) -> RequestValidationError:
    return RequestValidationError(422, message)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(f"{path} must be a number")
    try:
        number = float(value)
    except OverflowError:
        raise _bad(f"{path} must be a finite number") from None
    if not math.isfinite(number):
        raise _bad(f"{path} must be a finite number")
    return number


def _hour(value: Any, path: str) -> int:
    if isinstance(value, bool):
        raise _bad(f"{path} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise _bad(f"{path} must be an integer")


def _object(value: Any, path: str, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _bad(f"{path} must be an object")
    missing = [field for field in fields if field not in value]
    if missing:
        raise _bad(f"{path} is missing required field(s): {', '.join(missing)}")
    return value


def parse_scenario(body: bytes) -> Scenario:
    """Parse and validate a raw request body. Raises RequestValidationError."""
    if not body or not body.strip():
        raise _bad("Request body is empty")
    try:
        data = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):
        raise _bad("Request body is not valid JSON") from None

    data = _object(data, "request body", TOP_LEVEL_FIELDS)

    scenario_id = data["scenario_id"]
    if not isinstance(scenario_id, str):
        raise _bad("scenario_id must be a string")

    notes = data["operator_notes"]
    if not isinstance(notes, list) or not 1 <= len(notes) <= 3:
        raise _bad("operator_notes must be an array of 1-3 strings")
    for index, note in enumerate(notes):
        if not isinstance(note, str) or not note.strip():
            raise _bad(f"operator_notes[{index}] must be a non-empty string")

    raw_hours = data["hours"]
    if not isinstance(raw_hours, list) or len(raw_hours) != HOURS_PER_DAY:
        raise _bad("hours must be an array of exactly 24 entries")
    hours: list[HourInput] = []
    for index, entry in enumerate(raw_hours):
        path = f"hours[{index}]"
        entry = _object(entry, path, HOUR_FIELDS)
        hours.append(
            HourInput(
                hour=_hour(entry["hour"], f"{path}.hour"),
                demand_kwh=_number(entry["demand_kwh"], f"{path}.demand_kwh"),
                solar_kwh=_number(entry["solar_kwh"], f"{path}.solar_kwh"),
                tariff_bdt_per_kwh=_number(entry["tariff_bdt_per_kwh"], f"{path}.tariff_bdt_per_kwh"),
            )
        )
    if sorted(h.hour for h in hours) != list(range(HOURS_PER_DAY)):
        raise _bad("hours must contain each hour 0-23 exactly once")
    hours.sort(key=lambda h: h.hour)

    raw_battery = _object(data["battery"], "battery", BATTERY_FIELDS)
    battery = BatteryInput(**{field: _number(raw_battery[field], f"battery.{field}") for field in BATTERY_FIELDS})

    _check_semantics(hours, battery)
    return Scenario(
        scenario_id=scenario_id,
        operator_notes=tuple(notes),
        hours=tuple(hours),
        battery=battery,
    )


def _check_semantics(hours: list[HourInput], battery: BatteryInput) -> None:
    for h in hours:
        for field in ("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh"):
            if getattr(h, field) < 0:
                raise _unprocessable(f"hour {h.hour}: {field} must not be negative")
    for field in BatteryInput.__dataclass_fields__:
        if getattr(battery, field) < 0:
            raise _unprocessable(f"battery.{field} must not be negative")
    if battery.minimum_energy_kwh > battery.capacity_kwh:
        raise _unprocessable("battery.minimum_energy_kwh must not exceed battery.capacity_kwh")
    if not battery.minimum_energy_kwh <= battery.initial_energy_kwh <= battery.capacity_kwh:
        raise _unprocessable(
            "battery.initial_energy_kwh must be between minimum_energy_kwh and capacity_kwh"
        )
