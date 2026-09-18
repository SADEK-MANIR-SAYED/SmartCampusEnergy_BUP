"""
Adversarial and Synthetic Paraphrase Tests.

Tests natural-language paraphrases, distractor notes, and complex edge cases
not present in the public sample cases to verify robust interpretation and execution.

Requires GEMINI_API_KEY.
"""
import os
import pytest
from app.schemas import OptimizeRequest, BatteryConfig
from app.llm.interpreter import interpret_notes
from app.guardrails import validate_and_normalize, GuardrailError
from app.optimizer import build_directive_constraints, optimize
from app.validator import validate_schedule

requires_gemini = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set; skipping LLM tests",
)

# Standard test battery config
DEFAULT_BATTERY = {
    "capacity_kwh": 200.0,
    "initial_energy_kwh": 100.0,
    "minimum_energy_kwh": 20.0,
    "max_charge_kwh_per_hour": 50.0,
    "max_discharge_kwh_per_hour": 50.0,
}


def _make_24h_input(notes: list[str], battery: dict = None) -> dict:
    bat = battery or DEFAULT_BATTERY
    hours = []
    for h in range(24):
        # 12 PM to 4 PM has solar, peak tariff in evening (17-21)
        solar = 80.0 if 10 <= h <= 15 else 0.0
        tariff = 15.0 if 17 <= h <= 21 else 5.0
        hours.append({
            "hour": h,
            "demand_kwh": 100.0,
            "solar_kwh": solar,
            "tariff_bdt_per_kwh": tariff,
        })
    return {
        "scenario_id": "SYNTHETIC-TEST",
        "operator_notes": notes,
        "hours": hours,
        "battery": bat,
    }


@requires_gemini
def test_paraphrase_battery_reserve_percentage():
    """Test percentage-based reserve note paraphrase."""
    notes = [
        "Please keep the battery above half its capacity during the evening emergency period from 6 PM until 9 PM.",
        "Submit monthly expense reports before the end of the week."
    ]
    inp = _make_24h_input(notes)
    bat = BatteryConfig.model_validate(inp["battery"])

    raw = interpret_notes(
        scenario_id=inp["scenario_id"],
        operator_notes=notes,
        battery_capacity_kwh=bat.capacity_kwh,
        battery_minimum_kwh=bat.minimum_energy_kwh,
    )
    validated = validate_and_normalize(raw, 2, bat.capacity_kwh)

    assert len(validated) == 2
    assert validated[0].directive_type == "minimum_battery_reserve"
    assert validated[0].applies is True
    assert validated[0].structured_adjustment["hours"] == [18, 19, 20]
    # 50% of 200 kWh capacity = 100 kWh
    assert abs(validated[0].structured_adjustment["minimum_energy_kwh"] - 100.0) < 1.0

    assert validated[1].directive_type == "no_op"
    assert validated[1].applies is False


@requires_gemini
def test_paraphrase_no_charge_window():
    """Test natural language range paraphrase for no-charge window."""
    notes = [
        "Do not charge the storage system between 2 AM and 5 AM due to transformer maintenance."
    ]
    inp = _make_24h_input(notes)
    bat = BatteryConfig.model_validate(inp["battery"])

    raw = interpret_notes(
        scenario_id=inp["scenario_id"],
        operator_notes=notes,
        battery_capacity_kwh=bat.capacity_kwh,
        battery_minimum_kwh=bat.minimum_energy_kwh,
    )
    validated = validate_and_normalize(raw, 1, bat.capacity_kwh)

    assert validated[0].directive_type == "no_charge_window"
    assert validated[0].structured_adjustment["hours"] == [2, 3, 4]


@requires_gemini
def test_paraphrase_solar_reduction_quarter():
    """Test fraction phrase 'one quarter of forecast'."""
    notes = [
        "Solar production should be treated as only one quarter of forecast between noon and 2 PM."
    ]
    inp = _make_24h_input(notes)
    bat = BatteryConfig.model_validate(inp["battery"])

    raw = interpret_notes(
        scenario_id=inp["scenario_id"],
        operator_notes=notes,
        battery_capacity_kwh=bat.capacity_kwh,
        battery_minimum_kwh=bat.minimum_energy_kwh,
    )
    validated = validate_and_normalize(raw, 1, bat.capacity_kwh)

    assert validated[0].directive_type == "solar_reduction"
    assert validated[0].structured_adjustment["hours"] == [12, 13]
    assert abs(validated[0].structured_adjustment["factor"] - 0.25) < 0.05


@requires_gemini
def test_paraphrase_max_grid_cap():
    """Test grid import limit paraphrase."""
    notes = [
        "Grid import must stay below 150 kWh from 6 PM until 9 PM."
    ]
    inp = _make_24h_input(notes)
    bat = BatteryConfig.model_validate(inp["battery"])

    raw = interpret_notes(
        scenario_id=inp["scenario_id"],
        operator_notes=notes,
        battery_capacity_kwh=bat.capacity_kwh,
        battery_minimum_kwh=bat.minimum_energy_kwh,
    )
    validated = validate_and_normalize(raw, 1, bat.capacity_kwh)

    assert validated[0].directive_type == "max_grid_window"
    assert validated[0].structured_adjustment["hours"] == [18, 19, 20]
    assert abs(validated[0].structured_adjustment["max_grid_kwh"] - 150.0) < 1.0


@requires_gemini
def test_synthetic_end_to_end_optimization():
    """Run full synthetic pipeline and verify optimizer + independent validator."""
    notes = [
        "Charging is unavailable from 02:00 until 05:00.",
        "Keep at least 60 kWh battery reserve between 5 PM and 8 PM."
    ]
    inp = _make_24h_input(notes)
    req = OptimizeRequest.model_validate(inp)

    raw = interpret_notes(
        scenario_id=req.scenario_id,
        operator_notes=req.operator_notes,
        battery_capacity_kwh=req.battery.capacity_kwh,
        battery_minimum_kwh=req.battery.minimum_energy_kwh,
    )
    validated = validate_and_normalize(raw, 2, req.battery.capacity_kwh)
    dc = build_directive_constraints(validated)

    schedule = optimize(req.hours, req.battery, dc)
    validation = validate_schedule(schedule.hourly, req.hours, req.battery, dc)

    assert validation.passed, f"Validation failed: {validation.errors}"
    assert schedule.solver_status == "Optimal"
