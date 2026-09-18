"""
Tests for the LP optimizer and energy model.
These tests do NOT require Gemini API.
"""
import pytest
from app.schemas import BatteryConfig, HourRecord
from app.optimizer import (
    DirectiveConstraints,
    build_directive_constraints,
    optimize,
)
from app.validator import validate_schedule, recalculate_totals
from app.schemas import DirectiveInterpretation


def make_hours(demands, solars=None, tariffs=None):
    """Helper to create 24 HourRecord objects."""
    if solars is None:
        solars = [0.0] * 24
    if tariffs is None:
        tariffs = [10.0] * 24
    return [
        HourRecord(hour=h, demand_kwh=demands[h], solar_kwh=solars[h], tariff_bdt_per_kwh=tariffs[h])
        for h in range(24)
    ]


def make_battery(capacity=200, initial=100, minimum=30, max_ch=50, max_disch=50):
    return BatteryConfig(
        capacity_kwh=capacity,
        initial_energy_kwh=initial,
        minimum_energy_kwh=minimum,
        max_charge_kwh_per_hour=max_ch,
        max_discharge_kwh_per_hour=max_disch,
    )


def run_and_validate(hours, battery, dc=None):
    """Run optimizer and validate result, return (schedule, validation)."""
    if dc is None:
        dc = DirectiveConstraints()
    schedule = optimize(hours, battery, dc)
    result = validate_schedule(schedule.hourly, hours, battery, dc)
    return schedule, result


# ─── Basic energy model tests ─────────────────────────────────────────────────

def test_basic_demand_met():
    """All demand must be satisfied every hour."""
    demands = [100.0] * 24
    hours = make_hours(demands)
    battery = make_battery()
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed, f"Validation errors: {validation.errors}"


def test_end_of_day_neutrality():
    """Battery energy after hour 23 must equal initial_energy_kwh."""
    hours = make_hours([80] * 24)
    battery = make_battery(capacity=200, initial=100, minimum=30)
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed, f"Validation errors: {validation.errors}"
    # Check end-of-day
    last_hour = next(e for e in schedule.hourly if e["hour"] == 23)
    assert abs(last_hour["battery_energy_after_kwh"] - battery.initial_energy_kwh) <= 0.01


def test_solar_reduces_grid():
    """When solar is available, grid import should decrease."""
    demands = [100.0] * 24
    # Lots of solar during daytime
    solars = [0] * 8 + [100] * 8 + [0] * 8
    hours_no_solar = make_hours(demands, [0] * 24)
    hours_with_solar = make_hours(demands, solars)
    battery = make_battery()
    dc = DirectiveConstraints()

    s_no, _ = run_and_validate(hours_no_solar, battery, dc)
    s_with, _ = run_and_validate(hours_with_solar, battery, dc)

    assert s_with.total_grid_kwh < s_no.total_grid_kwh


def test_battery_discharges_during_expensive_hours():
    """Battery should discharge during expensive hours to minimize cost."""
    demands = [100.0] * 24
    # Cheap at night (0-7), expensive during day (8-17), medium evening (18-23)
    tariffs = [5.0] * 8 + [30.0] * 10 + [15.0] * 6
    hours = make_hours(demands, tariffs=tariffs)
    battery = make_battery(capacity=200, initial=100, minimum=30, max_ch=50, max_disch=50)
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed, f"Validation errors: {validation.errors}"

    # With this tariff structure, cost should be minimized
    # Verify schedule is actually trying to discharge during expensive hours
    expensive_hours = [e for e in schedule.hourly if e["hour"] in range(8, 18)]
    # Some should have discharge action
    discharge_hours = [e for e in expensive_hours if e["battery_action"] == "discharge"]
    assert len(discharge_hours) > 0, "Expected some discharge during expensive hours"


def test_no_negative_grid():
    """Grid import must never be negative."""
    demands = [50.0] * 24
    solars = [200.0] * 24  # More solar than demand
    hours = make_hours(demands, solars)
    battery = make_battery()
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed, f"Validation errors: {validation.errors}"
    for entry in schedule.hourly:
        assert entry["grid_kwh"] >= -0.01, f"Hour {entry['hour']}: negative grid {entry['grid_kwh']}"


def test_solar_not_exceeds_available():
    """Solar used must not exceed available solar."""
    demands = [100.0] * 24
    solars = [60.0] * 24
    hours = make_hours(demands, solars)
    battery = make_battery()
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed, f"Validation errors: {validation.errors}"
    for entry in schedule.hourly:
        assert entry["solar_used_kwh"] <= 60.0 + 0.01


def test_battery_capacity_not_exceeded():
    """Battery energy must never exceed capacity."""
    demands = [30.0] * 24
    solars = [0.0] * 24
    tariffs = [5.0] * 8 + [30.0] * 16  # Charge at night
    hours = make_hours(demands, tariffs=tariffs)
    battery = make_battery(capacity=200, initial=100, minimum=20, max_ch=50, max_disch=50)
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed, f"Validation errors: {validation.errors}"
    for entry in schedule.hourly:
        assert entry["battery_energy_after_kwh"] <= 200.0 + 0.01


def test_battery_minimum_energy_respected():
    """Battery must not go below minimum_energy_kwh."""
    demands = [150.0] * 24
    hours = make_hours(demands)
    battery = make_battery(capacity=200, initial=100, minimum=40, max_disch=50)
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed, f"Validation errors: {validation.errors}"
    for entry in schedule.hourly:
        assert entry["battery_energy_after_kwh"] >= 40.0 - 0.01


def test_max_charge_rate_respected():
    """Charging must not exceed max_charge_kwh_per_hour."""
    demands = [20.0] * 24
    hours = make_hours(demands, tariffs=[5.0] * 12 + [30.0] * 12)
    battery = make_battery(max_ch=30, max_disch=50)
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed, f"Validation errors: {validation.errors}"
    for entry in schedule.hourly:
        if entry["battery_action"] == "charge":
            assert entry["battery_kwh"] <= 30.0 + 0.01


def test_max_discharge_rate_respected():
    """Discharging must not exceed max_discharge_kwh_per_hour."""
    demands = [150.0] * 24
    hours = make_hours(demands, tariffs=[30.0] * 24)
    battery = make_battery(max_ch=50, max_disch=25)
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed, f"Validation errors: {validation.errors}"
    for entry in schedule.hourly:
        if entry["battery_action"] == "discharge":
            assert entry["battery_kwh"] <= 25.0 + 0.01


# ─── Directive constraint tests ───────────────────────────────────────────────

def test_no_charge_window_respected():
    """Battery must not charge during no_charge_window hours."""
    demands = [100.0] * 24
    hours = make_hours(demands, tariffs=[5.0] * 8 + [30.0] * 16)
    battery = make_battery()
    dc = DirectiveConstraints()
    dc.no_charge_hours = {0, 1, 2, 3, 4, 5, 6, 7}  # Forbid charging during cheap hours

    schedule, validation = run_and_validate(hours, battery, dc)
    assert validation.passed, f"Validation errors: {validation.errors}"
    for entry in schedule.hourly:
        if entry["hour"] in dc.no_charge_hours:
            assert entry["battery_action"] != "charge" or entry["battery_kwh"] < 0.01


def test_no_discharge_window_respected():
    """Battery must not discharge during no_discharge_window hours."""
    demands = [80.0] * 24
    tariffs = [5.0] * 8 + [30.0] * 16
    hours = make_hours(demands, tariffs=tariffs)
    battery = make_battery()
    dc = DirectiveConstraints()
    dc.no_discharge_hours = {8, 9, 10, 11, 12, 13, 14, 15, 16, 17}

    schedule, validation = run_and_validate(hours, battery, dc)
    assert validation.passed, f"Validation errors: {validation.errors}"
    for entry in schedule.hourly:
        if entry["hour"] in dc.no_discharge_hours:
            assert entry["battery_action"] != "discharge" or entry["battery_kwh"] < 0.01


def test_minimum_battery_reserve():
    """Battery must maintain minimum reserve during specified hours."""
    demands = [180.0] * 24
    hours = make_hours(demands)
    battery = make_battery(capacity=200, initial=100, minimum=20)
    dc = DirectiveConstraints()
    dc.battery_reserve = {18: 80, 19: 80, 20: 80}

    schedule, validation = run_and_validate(hours, battery, dc)
    assert validation.passed, f"Validation errors: {validation.errors}"
    for entry in schedule.hourly:
        if entry["hour"] in {18, 19, 20}:
            assert entry["battery_energy_after_kwh"] >= 80.0 - 0.01


def test_max_grid_window():
    """Grid import must not exceed max_grid_kwh during specified hours."""
    demands = [200.0] * 24
    hours = make_hours(demands)
    battery = make_battery(capacity=300, initial=200, minimum=30, max_ch=60, max_disch=60)
    dc = DirectiveConstraints()
    dc.max_grid = {18: 150, 19: 150, 20: 150}

    schedule, validation = run_and_validate(hours, battery, dc)
    assert validation.passed, f"Validation errors: {validation.errors}"
    for entry in schedule.hourly:
        if entry["hour"] in {18, 19, 20}:
            assert entry["grid_kwh"] <= 150.0 + 0.01


def test_solar_reduction_applied():
    """Solar reduction factor should limit usable solar."""
    demands = [100.0] * 24
    solars = [150.0] * 24  # More than demand
    hours = make_hours(demands, solars)
    battery = make_battery()
    dc = DirectiveConstraints()
    dc.solar_factors = {12: 0.25, 13: 0.25}  # 75% reduction at noon/1PM

    schedule, validation = run_and_validate(hours, battery, dc)
    assert validation.passed, f"Validation errors: {validation.errors}"
    # At hours 12 and 13, solar used should be <= 150 * 0.25 = 37.5
    for entry in schedule.hourly:
        if entry["hour"] in {12, 13}:
            assert entry["solar_used_kwh"] <= 37.5 + 0.01, (
                f"Hour {entry['hour']}: solar_used={entry['solar_used_kwh']} > 37.5"
            )


def test_multiple_directives_simultaneously():
    """Multiple directives must be applied together without overwriting each other."""
    demands = [120.0] * 24
    solars = [50.0] * 12 + [0.0] * 12
    tariffs = [5.0] * 8 + [25.0] * 8 + [15.0] * 8
    hours = make_hours(demands, solars, tariffs)
    battery = make_battery(capacity=200, initial=100, minimum=30, max_ch=50, max_disch=50)
    dc = DirectiveConstraints()
    dc.solar_factors = {10: 0.5, 11: 0.5}
    dc.no_charge_hours = {14, 15}
    dc.battery_reserve = {18: 60, 19: 60}

    schedule, validation = run_and_validate(hours, battery, dc)
    assert validation.passed, f"Validation errors: {validation.errors}"

    for entry in schedule.hourly:
        h = entry["hour"]
        if h in {10, 11}:
            assert entry["solar_used_kwh"] <= 50.0 * 0.5 + 0.01
        if h in {14, 15}:
            assert entry["battery_action"] != "charge" or entry["battery_kwh"] < 0.01
        if h in {18, 19}:
            assert entry["battery_energy_after_kwh"] >= 60.0 - 0.01


# ─── Totals consistency tests ─────────────────────────────────────────────────

def test_totals_recalculated_correctly():
    """total_grid_kwh, total_cost_bdt, peak_grid_kwh must match hourly plan."""
    demands = [100.0] * 24
    tariffs = [10.0 + h for h in range(24)]
    hours = make_hours(demands, tariffs=tariffs)
    battery = make_battery()
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed

    total_grid, total_cost, peak_grid = recalculate_totals(schedule.hourly, hours)

    expected_grid = sum(e["grid_kwh"] for e in schedule.hourly)
    hour_tariff = {hr.hour: hr.tariff_bdt_per_kwh for hr in hours}
    expected_cost = sum(e["grid_kwh"] * hour_tariff[e["hour"]] for e in schedule.hourly)
    expected_peak = max(e["grid_kwh"] for e in schedule.hourly)

    assert abs(total_grid - expected_grid) < 0.01
    assert abs(total_cost - expected_cost) < 0.01
    assert abs(peak_grid - expected_peak) < 0.01


# ─── Edge cases ───────────────────────────────────────────────────────────────

def test_zero_solar_all_hours():
    """Works correctly with no solar available."""
    demands = [80.0] * 24
    hours = make_hours(demands, [0.0] * 24)
    battery = make_battery()
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed
    for entry in schedule.hourly:
        assert entry["solar_used_kwh"] == 0.0


def test_high_demand_no_crash():
    """Service should handle high demand gracefully."""
    demands = [500.0] * 24
    hours = make_hours(demands, tariffs=[20.0] * 24)
    battery = make_battery(capacity=1000, initial=500, minimum=100, max_ch=200, max_disch=200)
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed


def test_tight_battery_constraints():
    """Should handle tight initial vs minimum battery constraints."""
    demands = [50.0] * 24
    hours = make_hours(demands)
    battery = make_battery(capacity=100, initial=40, minimum=40, max_ch=20, max_disch=20)
    schedule, validation = run_and_validate(hours, battery)
    assert validation.passed
    for entry in schedule.hourly:
        assert entry["battery_energy_after_kwh"] >= 40.0 - 0.01
