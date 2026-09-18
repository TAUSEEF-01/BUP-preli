"""Deterministic guardrails for LLM interpretation output (Problem Statement §5.1, §08).

Model output is untrusted. A result is accepted only if every entry passes; otherwise the
whole result is rejected with a list of errors that can be sent back to the model for repair.
The only normalizations applied are ones that cannot change meaning: sorting/de-duplicating
an hours list and ordering entries by an already-complete set of note indices.
"""
from __future__ import annotations

import math
from typing import Any

from app.schemas import ADJUSTMENT_KEYS, DIRECTIVE_TYPES, HOURS_PER_DAY, Directive

MAX_EXPLANATION_CHARS = 300
DEFAULT_NO_OP_EXPLANATION = "This note does not affect the current 24-hour energy schedule."


class GuardrailError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _clean_explanation(value: Any, directive_type: str) -> str:
    if isinstance(value, str) and value.strip():
        text = " ".join(value.split())
        return text[:MAX_EXPLANATION_CHARS]
    if directive_type == "no_op":
        return DEFAULT_NO_OP_EXPLANATION
    return f"Interpreted as {directive_type.replace('_', ' ')}."


def _hours(value: Any, prefix: str, errors: list[str]) -> list[int] | None:
    if not isinstance(value, list) or not value:
        errors.append(f"{prefix}: hours must be a non-empty array of integers 0-23")
        return None
    hours: list[int] = []
    for item in value:
        hour = _as_int(item)
        if hour is None or not 0 <= hour < HOURS_PER_DAY:
            errors.append(f"{prefix}: every hour must be an integer from 0 to 23 (got {item!r})")
            return None
        hours.append(hour)
    return sorted(set(hours))


def _validate_entry(
    note_index: int, entry: dict[str, Any], capacity_kwh: float
) -> tuple[Directive | None, list[str]]:
    prefix = f"note_index {note_index}"
    errors: list[str] = []

    directive_type = entry.get("directive_type")
    if directive_type not in DIRECTIVE_TYPES:
        return None, [f"{prefix}: directive_type must be one of {', '.join(DIRECTIVE_TYPES)}"]

    applies = entry.get("applies")
    if not isinstance(applies, bool):
        errors.append(f"{prefix}: applies must be a boolean")
    if "structured_adjustment" not in entry:
        errors.append(f"{prefix}: structured_adjustment is required")
    adjustment = entry.get("structured_adjustment")
    explanation = _clean_explanation(entry.get("explanation"), directive_type)

    if directive_type == "no_op":
        if applies is not False:
            errors.append(f"{prefix}: no_op requires applies = false")
        if adjustment is not None:
            errors.append(f"{prefix}: no_op requires structured_adjustment = null")
        if errors:
            return None, errors
        return Directive(note_index, False, "no_op", None, explanation), []

    if applies is not True:
        errors.append(f"{prefix}: {directive_type} requires applies = true")
    if not isinstance(adjustment, dict):
        errors.append(f"{prefix}: {directive_type} requires a structured_adjustment object")
        return None, errors

    expected_keys = set(ADJUSTMENT_KEYS[directive_type])
    if set(adjustment) != expected_keys:
        errors.append(
            f"{prefix}: structured_adjustment for {directive_type} must have exactly the keys "
            f"{sorted(expected_keys)}, got {sorted(adjustment)}"
        )
        return None, errors

    hours = _hours(adjustment.get("hours"), prefix, errors)
    canonical: dict[str, Any] = {"hours": hours}

    if directive_type == "solar_reduction":
        factor = adjustment.get("factor")
        if not _is_number(factor) or not 0 <= factor <= 1:
            errors.append(f"{prefix}: factor must be a finite number between 0 and 1 inclusive")
        canonical["factor"] = factor
    elif directive_type == "minimum_battery_reserve":
        value = adjustment.get("minimum_energy_kwh")
        if not _is_number(value) or value < 0:
            errors.append(f"{prefix}: minimum_energy_kwh must be a finite non-negative number")
        elif value > capacity_kwh:
            errors.append(
                f"{prefix}: minimum_energy_kwh ({value}) must not exceed the battery capacity ({capacity_kwh})"
            )
        canonical["minimum_energy_kwh"] = value
    elif directive_type == "max_grid_window":
        value = adjustment.get("max_grid_kwh")
        if not _is_number(value) or value < 0:
            errors.append(f"{prefix}: max_grid_kwh must be a finite non-negative number")
        canonical["max_grid_kwh"] = value

    if errors:
        return None, errors
    return Directive(note_index, True, directive_type, canonical, explanation), []


def validate_interpretations(raw: Any, note_count: int, capacity_kwh: float) -> list[Directive]:
    """Validate a complete interpretation. Returns directives in note order or raises GuardrailError."""
    entries = raw.get("interpretations") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise GuardrailError(["Output must be a JSON object with an 'interpretations' array."])

    errors: list[str] = []
    if len(entries) != note_count:
        errors.append(
            f"Expected exactly {note_count} interpretation entries (one per note), got {len(entries)}."
        )

    by_index: dict[int, dict[str, Any]] = {}
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"Entry {position} must be an object.")
            continue
        index = _as_int(entry.get("note_index"))
        if index is None or not 0 <= index < note_count:
            errors.append(f"Entry {position}: note_index must be an integer from 0 to {note_count - 1}.")
            continue
        if index in by_index:
            errors.append(f"note_index {index} appears more than once.")
            continue
        by_index[index] = entry

    missing = [i for i in range(note_count) if i not in by_index]
    if missing:
        errors.append(f"Missing interpretation for note_index {', '.join(map(str, missing))}.")

    directives: list[Directive] = []
    for index in sorted(by_index):
        directive, entry_errors = _validate_entry(index, by_index[index], capacity_kwh)
        errors.extend(entry_errors)
        if directive is not None:
            directives.append(directive)

    if errors:
        raise GuardrailError(errors)
    return directives
