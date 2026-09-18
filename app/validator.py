"""
Independent schedule validator.

This module is completely separate from the optimizer. It replays the
battery state and verifies every constraint independently to catch any
bugs in the optimizer or post-processing.

The judge will also replay independently, so this validator emulates
the judge's behavior as closely as possible.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from app.schemas import BatteryConfig, HourRecord
from app.optimizer import DirectiveConstraints

logger = logging.getLogger(__name__)

TOLERANCE = 0.01  # Official tolerance: 0.01 kWh


@dataclass
class ValidationResult:
    """Result of schedule validation."""
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_schedule(
    hourly_plan: list[dict],
    hours: list[HourRecord],
    battery: BatteryConfig,
    directive_constraints: DirectiveConstraints,
) -> ValidationResult:
    """
    Independently replay and validate the complete 24-hour schedule.

    Returns ValidationResult with passed=True if all checks pass.
    """
    result = ValidationResult(passed=True)
    errors = result.errors

    # ── 1. Structure checks ───────────────────────────────────────────────────
    if len(hourly_plan) != 24:
        errors.append(f"hourly_plan has {len(hourly_plan)} entries, expected 24")
        result.passed = False
        return result  # Cannot continue without correct count

    hours_in_plan = [entry["hour"] for entry in hourly_plan]
    if sorted(hours_in_plan) != list(range(24)):
        errors.append(f"hourly_plan hours are not 0..23: {sorted(hours_in_plan)}")
        result.passed = False
        return result

    # Sort plan and input by hour
    plan = sorted(hourly_plan, key=lambda x: x["hour"])
    hrs = sorted(hours, key=lambda h: h.hour)

    # Build hour lookup
    hour_data = {hr.hour: hr for hr in hrs}

    # Effective solar
    effective_solar = {
        h: hour_data[h].solar_kwh * directive_constraints.solar_factors.get(h, 1.0)
        for h in range(24)
    }

    # ── 2. Replay battery state ───────────────────────────────────────────────
    battery_energy = battery.initial_energy_kwh

    for entry in plan:
        h = entry["hour"]
        hr = hour_data[h]

        grid = entry["grid_kwh"]
        solar_used = entry["solar_used_kwh"]
        action = entry["battery_action"]
        bat_kwh = entry["battery_kwh"]
        e_after = entry["battery_energy_after_kwh"]
        eff_solar = effective_solar[h]

        # ── Numeric validity ──────────────────────────────────────────────────
        for name, val in [
            ("grid_kwh", grid),
            ("solar_used_kwh", solar_used),
            ("battery_kwh", bat_kwh),
            ("battery_energy_after_kwh", e_after),
        ]:
            if not math.isfinite(val):
                errors.append(f"Hour {h}: {name}={val} is not finite")
                result.passed = False

        # ── Grid >= 0 ─────────────────────────────────────────────────────────
        if grid < -TOLERANCE:
            errors.append(f"Hour {h}: grid_kwh={grid:.4f} is negative")
            result.passed = False

        # ── Solar bounds ──────────────────────────────────────────────────────
        if solar_used < -TOLERANCE:
            errors.append(f"Hour {h}: solar_used_kwh={solar_used:.4f} is negative")
            result.passed = False

        if solar_used > eff_solar + TOLERANCE:
            errors.append(
                f"Hour {h}: solar_used_kwh={solar_used:.4f} exceeds effective solar "
                f"{eff_solar:.4f} (original={hr.solar_kwh:.4f}, factor={directive_constraints.solar_factors.get(h, 1.0)})"
            )
            result.passed = False

        # ── Battery action ────────────────────────────────────────────────────
        if action not in ("charge", "discharge", "idle"):
            errors.append(f"Hour {h}: invalid battery_action='{action}'")
            result.passed = False
            continue

        if bat_kwh < -TOLERANCE:
            errors.append(f"Hour {h}: battery_kwh={bat_kwh:.4f} is negative")
            result.passed = False

        if action == "idle" and bat_kwh > TOLERANCE:
            errors.append(f"Hour {h}: idle action but battery_kwh={bat_kwh:.4f} != 0")
            result.passed = False

        if action == "charge":
            if bat_kwh > battery.max_charge_kwh_per_hour + TOLERANCE:
                errors.append(
                    f"Hour {h}: charge {bat_kwh:.4f} exceeds max_charge "
                    f"{battery.max_charge_kwh_per_hour}"
                )
                result.passed = False
            expected_e_after = battery_energy + bat_kwh
        elif action == "discharge":
            if bat_kwh > battery.max_discharge_kwh_per_hour + TOLERANCE:
                errors.append(
                    f"Hour {h}: discharge {bat_kwh:.4f} exceeds max_discharge "
                    f"{battery.max_discharge_kwh_per_hour}"
                )
                result.passed = False
            expected_e_after = battery_energy - bat_kwh
        else:  # idle
            expected_e_after = battery_energy

        # ── Battery state transition ──────────────────────────────────────────
        if abs(e_after - expected_e_after) > TOLERANCE:
            errors.append(
                f"Hour {h}: battery_energy_after={e_after:.4f} != "
                f"expected {expected_e_after:.4f} (before={battery_energy:.4f}, "
                f"action={action}, kwh={bat_kwh:.4f})"
            )
            result.passed = False

        # ── Battery bounds ────────────────────────────────────────────────────
        # Active minimum: base or directive-based reserve
        active_min = battery.minimum_energy_kwh
        if h in directive_constraints.battery_reserve:
            active_min = max(active_min, directive_constraints.battery_reserve[h])

        if e_after < active_min - TOLERANCE:
            errors.append(
                f"Hour {h}: battery_energy_after={e_after:.4f} < active_minimum={active_min:.4f}"
            )
            result.passed = False

        if e_after > battery.capacity_kwh + TOLERANCE:
            errors.append(
                f"Hour {h}: battery_energy_after={e_after:.4f} > capacity={battery.capacity_kwh}"
            )
            result.passed = False

        # ── Directive constraints ─────────────────────────────────────────────
        if h in directive_constraints.no_charge_hours and action == "charge" and bat_kwh > TOLERANCE:
            errors.append(f"Hour {h}: charging forbidden by no_charge_window directive")
            result.passed = False

        if h in directive_constraints.no_discharge_hours and action == "discharge" and bat_kwh > TOLERANCE:
            errors.append(f"Hour {h}: discharging forbidden by no_discharge_window directive")
            result.passed = False

        if h in directive_constraints.max_grid:
            max_g = directive_constraints.max_grid[h]
            if grid > max_g + TOLERANCE:
                errors.append(
                    f"Hour {h}: grid={grid:.4f} exceeds max_grid_kwh={max_g:.4f}"
                )
                result.passed = False

        # ── Energy balance ────────────────────────────────────────────────────
        charge_amt = bat_kwh if action == "charge" else 0.0
        discharge_amt = bat_kwh if action == "discharge" else 0.0

        lhs = grid + solar_used + discharge_amt
        rhs = hr.demand_kwh + charge_amt
        if abs(lhs - rhs) > TOLERANCE:
            errors.append(
                f"Hour {h}: energy balance violation: "
                f"grid({grid:.4f})+solar({solar_used:.4f})+discharge({discharge_amt:.4f}) = {lhs:.4f} "
                f"!= demand({hr.demand_kwh:.4f})+charge({charge_amt:.4f}) = {rhs:.4f}"
            )
            result.passed = False

        # Update battery energy for next hour
        battery_energy = e_after

    # ── 3. End-of-day neutrality ──────────────────────────────────────────────
    if abs(battery_energy - battery.initial_energy_kwh) > TOLERANCE:
        errors.append(
            f"End-of-day battery {battery_energy:.4f} != initial {battery.initial_energy_kwh:.4f}"
        )
        result.passed = False

    if result.passed:
        logger.info("Schedule validation PASSED (24 hours, all constraints satisfied)")
    else:
        logger.warning("Schedule validation FAILED: %d error(s): %s", len(errors), errors[:3])

    return result


def recalculate_totals(
    hourly_plan: list[dict],
    hours: list[HourRecord],
) -> tuple[float, float, float]:
    """
    Recalculate total_grid_kwh, total_cost_bdt, peak_grid_kwh
    directly from the hourly plan (source of truth).

    Returns (total_grid_kwh, total_cost_bdt, peak_grid_kwh).
    """
    hour_tariff = {hr.hour: hr.tariff_bdt_per_kwh for hr in hours}
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    for entry in hourly_plan:
        h = entry["hour"]
        g = entry["grid_kwh"]
        tariff = hour_tariff[h]
        total_grid += g
        total_cost += g * tariff
        if g > peak_grid:
            peak_grid = g

    return total_grid, total_cost, peak_grid
