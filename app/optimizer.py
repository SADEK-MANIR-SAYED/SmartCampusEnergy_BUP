"""
Linear Programming optimizer for the GridWise energy scheduling problem.

Uses PuLP with the CBC solver (bundled with PuLP).

Model formulation:
- Binary variables: charge_b[h], discharge_b[h] (mutually exclusive)
- Continuous: grid[h], solar_used[h], charge_kwh[h], discharge_kwh[h]
- Battery state: battery_e[h] = energy AFTER hour h
- Objective: minimize sum(grid[h] * tariff[h])
- Constraints: energy balance, battery bounds, charge/discharge limits,
               directive constraints, end-of-day neutrality
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

try:
    import pulp  # type: ignore
except ImportError:
    raise ImportError("PuLP is required. Install with: pip install PuLP")

from app.schemas import BatteryConfig, HourRecord

logger = logging.getLogger(__name__)

TINY = 1e-5  # internal LP threshold for actions


@dataclass
class DirectiveConstraints:
    """Aggregated constraints from all validated directives."""

    # solar_reduction: hour -> factor (0-1, fraction remaining)
    solar_factors: dict[int, float] = field(default_factory=dict)

    # minimum_battery_reserve: hour -> min_kwh (additional reserve)
    battery_reserve: dict[int, float] = field(default_factory=dict)

    # no_charge_window: set of hours where charging forbidden
    no_charge_hours: set[int] = field(default_factory=set)

    # no_discharge_window: set of hours where discharging forbidden
    no_discharge_hours: set[int] = field(default_factory=set)

    # max_grid_window: hour -> max_grid_kwh
    max_grid: dict[int, float] = field(default_factory=dict)


@dataclass
class OptimizedSchedule:
    """Result of the optimizer."""

    hourly: list[dict]  # list of 24 dicts with hour, grid, solar_used, action, kwh, e_after
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    solver_status: str


def build_directive_constraints(
    interpretations: list,  # list of DirectiveInterpretation
) -> DirectiveConstraints:
    """Convert validated directive interpretations into optimizer constraint sets."""
    dc = DirectiveConstraints()

    for interp in interpretations:
        if not interp.applies:
            continue  # no_op

        dt = interp.directive_type
        sa = interp.structured_adjustment

        if dt == "solar_reduction":
            for h in sa["hours"]:
                # If multiple solar reductions affect same hour, use minimum factor (strongest)
                existing = dc.solar_factors.get(h, 1.0)
                dc.solar_factors[h] = min(existing, sa["factor"])

        elif dt == "minimum_battery_reserve":
            for h in sa["hours"]:
                existing = dc.battery_reserve.get(h, 0.0)
                dc.battery_reserve[h] = max(existing, sa["minimum_energy_kwh"])

        elif dt == "no_charge_window":
            dc.no_charge_hours.update(sa["hours"])

        elif dt == "no_discharge_window":
            dc.no_discharge_hours.update(sa["hours"])

        elif dt == "max_grid_window":
            for h in sa["hours"]:
                existing = dc.max_grid.get(h, math.inf)
                dc.max_grid[h] = min(existing, sa["max_grid_kwh"])

    return dc


def optimize(
    hours: list[HourRecord],
    battery: BatteryConfig,
    directive_constraints: DirectiveConstraints,
) -> OptimizedSchedule:
    """
    Solve the energy scheduling LP/MILP and return an optimized schedule.

    Raises RuntimeError if infeasible or solver fails.
    """
    N = 24
    assert len(hours) == N

    # Sort hours by index
    hrs = sorted(hours, key=lambda h: h.hour)

    # Effective solar per hour (after solar_reduction directives)
    effective_solar = []
    for hr in hrs:
        factor = directive_constraints.solar_factors.get(hr.hour, 1.0)
        effective_solar.append(hr.solar_kwh * factor)

    # ── Build LP model using standard PuLP API ──────────────────────────────
    prob = pulp.LpProblem("GridWise_Energy_Schedule", pulp.LpMinimize)

    # Decision variables using standard pulp.LpVariable
    # grid[h]: grid import at hour h (kWh)
    grid = [
        pulp.LpVariable(f"grid_{h}", lowBound=0)
        for h in range(N)
    ]

    # solar_used[h]: solar used at hour h (kWh)
    solar_used = [
        pulp.LpVariable(f"solar_used_{h}", lowBound=0, upBound=effective_solar[h])
        for h in range(N)
    ]

    # charge_kwh[h]: energy charged into battery at hour h
    charge_kwh = [
        pulp.LpVariable(f"charge_{h}", lowBound=0, upBound=battery.max_charge_kwh_per_hour)
        for h in range(N)
    ]

    # discharge_kwh[h]: energy discharged from battery at hour h
    discharge_kwh = [
        pulp.LpVariable(f"discharge_{h}", lowBound=0, upBound=battery.max_discharge_kwh_per_hour)
        for h in range(N)
    ]

    # Binary variables to enforce charge/discharge exclusivity
    charge_b = [pulp.LpVariable(f"charge_b_{h}", cat="Binary") for h in range(N)]
    discharge_b = [pulp.LpVariable(f"discharge_b_{h}", cat="Binary") for h in range(N)]

    # battery_e[h]: battery energy AFTER hour h
    battery_e = [
        pulp.LpVariable(
            f"battery_e_{h}",
            lowBound=battery.minimum_energy_kwh,
            upBound=battery.capacity_kwh,
        )
        for h in range(N)
    ]

    # ── Objective: minimize total grid cost ───────────────────────────────────
    prob += pulp.lpSum(
        grid[h] * hrs[h].tariff_bdt_per_kwh for h in range(N)
    ), "total_grid_cost"

    # ── Constraints ────────────────────────────────────────────────────────────

    for h in range(N):
        hr = hrs[h]

        # Energy balance: grid + solar_used + discharge = demand + charge
        prob += (
            grid[h] + solar_used[h] + discharge_kwh[h]
            == hr.demand_kwh + charge_kwh[h]
        ), f"energy_balance_{h}"

        # Charge exclusivity: charge_kwh[h] <= max_charge * charge_b[h]
        prob += (
            charge_kwh[h] <= battery.max_charge_kwh_per_hour * charge_b[h]
        ), f"charge_binary_{h}"

        # Discharge exclusivity: discharge_kwh[h] <= max_discharge * discharge_b[h]
        prob += (
            discharge_kwh[h] <= battery.max_discharge_kwh_per_hour * discharge_b[h]
        ), f"discharge_binary_{h}"

        # Cannot charge and discharge simultaneously
        prob += charge_b[h] + discharge_b[h] <= 1, f"no_simultaneous_{h}"

        # Battery state transition
        if h == 0:
            prob += (
                battery_e[h]
                == battery.initial_energy_kwh + charge_kwh[h] - discharge_kwh[h]
            ), f"battery_state_{h}"
        else:
            prob += (
                battery_e[h]
                == battery_e[h - 1] + charge_kwh[h] - discharge_kwh[h]
            ), f"battery_state_{h}"

        # Battery bounds are enforced by variable bounds (lowBound/upBound).
        # Additional directive-based reserve:
        if h in directive_constraints.battery_reserve:
            min_reserve = max(
                battery.minimum_energy_kwh,
                directive_constraints.battery_reserve[h],
            )
            prob += battery_e[h] >= min_reserve, f"reserve_{h}"

        # Max grid window directive
        if hr.hour in directive_constraints.max_grid:
            prob += grid[h] <= directive_constraints.max_grid[hr.hour], f"max_grid_{h}"

        # No charge window directive
        if hr.hour in directive_constraints.no_charge_hours:
            prob += charge_b[h] == 0, f"no_charge_b_{h}"
            prob += charge_kwh[h] == 0, f"no_charge_kwh_{h}"

        # No discharge window directive
        if hr.hour in directive_constraints.no_discharge_hours:
            prob += discharge_b[h] == 0, f"no_discharge_b_{h}"
            prob += discharge_kwh[h] == 0, f"no_discharge_kwh_{h}"

    # End-of-day battery neutrality (CRITICAL REQUIREMENT)
    prob += (
        battery_e[N - 1] == battery.initial_energy_kwh
    ), "end_of_day_neutrality"

    # ── Solve ──────────────────────────────────────────────────────────────────
    logger.debug("Starting CBC solver for 24-hour schedule")

    solver = pulp.PULP_CBC_CMD(
        msg=0,        # suppress CBC output
        timeLimit=20, # seconds timeout
        gapRel=0.0001, # 0.01% optimality gap
    )

    try:
        prob.solve(solver)
    except Exception as e:
        raise RuntimeError(f"Solver execution failed: {e}") from e

    status_str = pulp.LpStatus[prob.status]
    logger.info("Solver status: %s (%d)", status_str, prob.status)

    if prob.status != 1:  # 1 = Optimal
        if prob.status == -1:
            raise RuntimeError(
                "Optimization problem is INFEASIBLE. "
                "The directives or scenario constraints cannot be simultaneously satisfied."
            )
        raise RuntimeError(
            f"Solver returned non-optimal status: {status_str} ({prob.status})"
        )

    # ── Extract solution ───────────────────────────────────────────────────────
    hourly_plan = []
    current_battery_e = battery.initial_energy_kwh

    for h in range(N):
        hr = hrs[h]
        ch_raw = max(0.0, float(pulp.value(charge_kwh[h]) or 0.0))
        dc_raw = max(0.0, float(pulp.value(discharge_kwh[h]) or 0.0))
        cb_val = round(float(pulp.value(charge_b[h]) or 0.0))
        db_val = round(float(pulp.value(discharge_b[h]) or 0.0))
        s_raw = max(0.0, float(pulp.value(solar_used[h]) or 0.0))

        # Clamp solar to effective limit
        s = min(s_raw, effective_solar[h])

        # Determine battery action from binary variables and amounts
        if cb_val == 1 and ch_raw > TINY:
            action = "charge"
            bat_kwh = min(ch_raw, battery.max_charge_kwh_per_hour)
            discharge_amt = 0.0
            charge_amt = bat_kwh
        elif db_val == 1 and dc_raw > TINY:
            action = "discharge"
            bat_kwh = min(dc_raw, battery.max_discharge_kwh_per_hour)
            charge_amt = 0.0
            discharge_amt = bat_kwh
        else:
            action = "idle"
            bat_kwh = 0.0
            charge_amt = 0.0
            discharge_amt = 0.0

        # Battery transition - use exact arithmetic from action amounts (no clamping)
        e_after = current_battery_e + charge_amt - discharge_amt

        # Exact energy balance: grid = demand + charge - solar - discharge
        grid_needed = max(0.0, hr.demand_kwh + charge_amt - s - discharge_amt)

        current_battery_e = e_after

        hourly_plan.append({
            "hour": hr.hour,
            "grid_kwh": grid_needed,
            "solar_used_kwh": s,
            "battery_action": action,
            "battery_kwh": bat_kwh,
            "battery_energy_after_kwh": e_after,
        })

    # Recalculate totals directly from hourly plan
    total_grid = sum(entry["grid_kwh"] for entry in hourly_plan)
    total_cost = sum(entry["grid_kwh"] * hrs[entry["hour"]].tariff_bdt_per_kwh for entry in hourly_plan)
    peak_grid = max(entry["grid_kwh"] for entry in hourly_plan)

    return OptimizedSchedule(
        hourly=hourly_plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        solver_status=status_str,
    )
