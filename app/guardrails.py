"""
Deterministic guardrails for LLM output validation.

All LLM output is treated as untrusted. This module validates every field
before any directive is applied to the optimizer.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from app.schemas import ALLOWED_DIRECTIVES, DirectiveInterpretation

logger = logging.getLogger(__name__)

VALID_HOURS = set(range(24))


class GuardrailError(ValueError):
    """Raised when LLM output fails deterministic validation."""

    def __init__(self, message: str, note_index: int | None = None):
        self.note_index = note_index
        super().__init__(message)


def validate_and_normalize(
    raw_interpretations: list[dict[str, Any]],
    expected_count: int,
    battery_capacity_kwh: float,
) -> list[DirectiveInterpretation]:
    """
    Validate raw LLM output and return normalized DirectiveInterpretation list.

    Raises GuardrailError on any validation failure.
    Returns list in order 0..N-1.
    """
    if not isinstance(raw_interpretations, list):
        raise GuardrailError("LLM output must be a list of interpretations")

    if len(raw_interpretations) != expected_count:
        raise GuardrailError(
            f"Expected {expected_count} interpretations, got {len(raw_interpretations)}"
        )

    # Track note indices for deduplication
    seen_indices: set[int] = set()
    validated: list[DirectiveInterpretation] = []

    for pos, raw in enumerate(raw_interpretations):
        interp = _validate_single(raw, pos, expected_count, battery_capacity_kwh, seen_indices)
        validated.append(interp)
        seen_indices.add(interp.note_index)

    # Ensure all indices 0..N-1 present
    expected_indices = set(range(expected_count))
    if seen_indices != expected_indices:
        missing = expected_indices - seen_indices
        raise GuardrailError(f"Missing note indices: {sorted(missing)}")

    # Sort by note_index to guarantee order
    validated.sort(key=lambda d: d.note_index)

    return validated


def _validate_single(
    raw: dict[str, Any],
    position: int,
    expected_count: int,
    battery_capacity_kwh: float,
    seen_indices: set[int],
) -> DirectiveInterpretation:
    """Validate a single raw interpretation dict."""

    if not isinstance(raw, dict):
        raise GuardrailError(
            f"Interpretation at position {position} must be a dict, got {type(raw).__name__}"
        )

    # ── note_index ────────────────────────────────────────────────────────────
    note_index = raw.get("note_index")
    if note_index is None:
        raise GuardrailError(f"Missing note_index at position {position}")
    try:
        note_index = int(note_index)
    except (TypeError, ValueError):
        raise GuardrailError(f"note_index must be integer, got {note_index!r}", position)

    if not (0 <= note_index < expected_count):
        raise GuardrailError(
            f"note_index {note_index} out of range 0..{expected_count - 1}",
            note_index,
        )
    if note_index in seen_indices:
        raise GuardrailError(f"Duplicate note_index {note_index}", note_index)

    # ── directive_type ────────────────────────────────────────────────────────
    directive_type = raw.get("directive_type")
    if directive_type is None:
        raise GuardrailError("Missing directive_type", note_index)
    if not isinstance(directive_type, str):
        raise GuardrailError(f"directive_type must be string, got {type(directive_type).__name__}", note_index)
    if directive_type not in ALLOWED_DIRECTIVES:
        raise GuardrailError(
            f"Invalid directive_type '{directive_type}'. Must be one of: {sorted(ALLOWED_DIRECTIVES)}",
            note_index,
        )

    # ── applies ───────────────────────────────────────────────────────────────
    applies = raw.get("applies")
    if applies is None:
        raise GuardrailError("Missing 'applies' field", note_index)
    if not isinstance(applies, bool):
        # Try to coerce from string
        if isinstance(applies, str):
            if applies.lower() == "true":
                applies = True
            elif applies.lower() == "false":
                applies = False
            else:
                raise GuardrailError(f"'applies' must be boolean, got '{applies}'", note_index)
        else:
            raise GuardrailError(f"'applies' must be boolean, got {type(applies).__name__}", note_index)

    # ── applies/no_op consistency ──────────────────────────────────────────────
    if directive_type == "no_op" and applies is not False:
        raise GuardrailError(
            f"no_op directive must have applies=false, got applies={applies}", note_index
        )
    if directive_type != "no_op" and applies is not True:
        raise GuardrailError(
            f"Non-no_op directive '{directive_type}' must have applies=true", note_index
        )

    # ── structured_adjustment ─────────────────────────────────────────────────
    sa = raw.get("structured_adjustment")

    if directive_type == "no_op":
        if sa is not None:
            logger.debug("no_op at note_index=%d has non-null adjustment; normalizing to null", note_index)
            sa = None
        structured_adjustment = None
    else:
        if sa is None:
            raise GuardrailError(
                f"structured_adjustment required for directive '{directive_type}'", note_index
            )
        if not isinstance(sa, dict):
            raise GuardrailError(
                f"structured_adjustment must be object for '{directive_type}'", note_index
            )
        structured_adjustment = _validate_adjustment(directive_type, sa, battery_capacity_kwh, note_index)

    # ── explanation ───────────────────────────────────────────────────────────
    explanation = raw.get("explanation", "")
    if not isinstance(explanation, str):
        explanation = str(explanation)

    return DirectiveInterpretation(
        note_index=note_index,
        applies=applies,
        directive_type=directive_type,
        structured_adjustment=structured_adjustment,
        explanation=explanation,
    )


def _validate_adjustment(
    directive_type: str,
    sa: dict[str, Any],
    battery_capacity_kwh: float,
    note_index: int,
) -> dict[str, Any]:
    """Validate and normalize a structured_adjustment dict."""

    # ── hours (present in all non-no_op directives) ───────────────────────────
    if directive_type in {"solar_reduction", "minimum_battery_reserve",
                          "no_charge_window", "no_discharge_window", "max_grid_window"}:
        hours = _validate_hours_field(sa.get("hours"), directive_type, note_index)
    else:
        hours = []

    if directive_type == "solar_reduction":
        factor = sa.get("factor")
        if factor is None:
            raise GuardrailError("solar_reduction requires 'factor'", note_index)
        try:
            factor = float(factor)
        except (TypeError, ValueError):
            raise GuardrailError(f"solar_reduction factor must be numeric, got {factor!r}", note_index)
        if not math.isfinite(factor):
            raise GuardrailError(f"solar_reduction factor must be finite, got {factor}", note_index)
        if not (0.0 <= factor <= 1.0):
            raise GuardrailError(
                f"solar_reduction factor must be in [0, 1], got {factor}", note_index
            )
        return {"hours": hours, "factor": factor}

    elif directive_type == "minimum_battery_reserve":
        min_kwh = sa.get("minimum_energy_kwh")
        if min_kwh is None:
            raise GuardrailError(
                "minimum_battery_reserve requires 'minimum_energy_kwh'", note_index
            )
        try:
            min_kwh = float(min_kwh)
        except (TypeError, ValueError):
            raise GuardrailError(
                f"minimum_energy_kwh must be numeric, got {min_kwh!r}", note_index
            )
        if not math.isfinite(min_kwh):
            raise GuardrailError(f"minimum_energy_kwh must be finite, got {min_kwh}", note_index)
        if min_kwh < 0:
            raise GuardrailError(
                f"minimum_energy_kwh must be non-negative, got {min_kwh}", note_index
            )
        if min_kwh > battery_capacity_kwh:
            raise GuardrailError(
                f"minimum_energy_kwh ({min_kwh}) exceeds battery capacity ({battery_capacity_kwh})",
                note_index,
            )
        return {"hours": hours, "minimum_energy_kwh": min_kwh}

    elif directive_type in {"no_charge_window", "no_discharge_window"}:
        return {"hours": hours}

    elif directive_type == "max_grid_window":
        max_grid = sa.get("max_grid_kwh")
        if max_grid is None:
            raise GuardrailError("max_grid_window requires 'max_grid_kwh'", note_index)
        try:
            max_grid = float(max_grid)
        except (TypeError, ValueError):
            raise GuardrailError(
                f"max_grid_kwh must be numeric, got {max_grid!r}", note_index
            )
        if not math.isfinite(max_grid):
            raise GuardrailError(f"max_grid_kwh must be finite, got {max_grid}", note_index)
        if max_grid < 0:
            raise GuardrailError(
                f"max_grid_kwh must be non-negative, got {max_grid}", note_index
            )
        return {"hours": hours, "max_grid_kwh": max_grid}

    else:
        raise GuardrailError(f"Unknown directive type in adjustment: {directive_type}", note_index)


def _validate_hours_field(
    hours_raw: Any, directive_type: str, note_index: int
) -> list[int]:
    """Validate and normalize hours list: integers, 0-23, unique, ascending."""
    if hours_raw is None:
        raise GuardrailError(f"{directive_type} requires 'hours'", note_index)
    if not isinstance(hours_raw, list):
        raise GuardrailError(
            f"{directive_type} hours must be a list, got {type(hours_raw).__name__}", note_index
        )
    if len(hours_raw) == 0:
        raise GuardrailError(f"{directive_type} hours must not be empty", note_index)

    hours: list[int] = []
    for h in hours_raw:
        try:
            h_int = int(h)
        except (TypeError, ValueError):
            raise GuardrailError(
                f"{directive_type} hour must be integer, got {h!r}", note_index
            )
        if not (0 <= h_int <= 23):
            raise GuardrailError(
                f"{directive_type} hour {h_int} out of range 0-23", note_index
            )
        if h_int in hours:
            raise GuardrailError(
                f"{directive_type} has duplicate hour {h_int}", note_index
            )
        hours.append(h_int)

    hours.sort()
    return hours
