"""
Public Sample Case Test Runner.

Loads all 10 public sample cases from data/sample_cases.json
and validates our implementation against them.

Usage:
    python -m pytest tests/test_samples.py -v
    python tests/run_samples.py  (standalone runner)

These tests REQUIRE a valid GEMINI_API_KEY environment variable.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

# Allow running as standalone
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.schemas import OptimizeRequest, BatteryConfig, HourRecord
from app.llm.interpreter import interpret_notes
from app.guardrails import validate_and_normalize
from app.optimizer import build_directive_constraints, optimize
from app.validator import validate_schedule, recalculate_totals

SAMPLE_CASES_PATH = PROJECT_ROOT / "data" / "sample_cases.json"
TOLERANCE = 0.01  # Official tolerance


def load_sample_cases():
    """Load all sample cases from the JSON file."""
    with open(SAMPLE_CASES_PATH) as f:
        data = json.load(f)
    return data["cases"]


def run_case_without_llm(case: dict, mock_interps: list[dict]) -> dict:
    """
    Run a case through the pipeline with provided interpretations (skipping LLM).
    Used for testing optimizer/validator behavior.
    """
    inp = case["input"]
    req = OptimizeRequest.model_validate(inp)

    validated = validate_and_normalize(
        mock_interps, len(req.operator_notes), req.battery.capacity_kwh
    )
    dc = build_directive_constraints(validated)
    schedule = optimize(req.hours, req.battery, dc)
    validation = validate_schedule(schedule.hourly, req.hours, req.battery, dc)
    total_grid, total_cost, peak_grid = recalculate_totals(schedule.hourly, req.hours)

    return {
        "validated_interpretations": validated,
        "schedule": schedule,
        "validation": validation,
        "total_grid_kwh": total_grid,
        "total_cost_bdt": total_cost,
        "peak_grid_kwh": peak_grid,
    }


def get_expected_interps(case: dict) -> list[dict]:
    """Extract expected interpretations as raw dicts for use with run_case_without_llm."""
    return case["expected_output"]["directive_interpretation"]


# ─── Pytest markers ───────────────────────────────────────────────────────────

requires_gemini = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set; skipping LLM tests",
)


# ─── Tests using expected interpretations (no Gemini needed) ──────────────────

CASES = load_sample_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["input"]["scenario_id"] for c in CASES])
def test_sample_schedule_valid(case):
    """Given expected interpretations, optimizer must produce a valid schedule."""
    expected_interps = get_expected_interps(case)
    result = run_case_without_llm(case, expected_interps)
    assert result["validation"].passed, (
        f"Schedule validation failed for {case['input']['scenario_id']}: "
        f"{result['validation'].errors}"
    )


@pytest.mark.parametrize("case", CASES, ids=[c["input"]["scenario_id"] for c in CASES])
def test_sample_end_of_day_neutrality(case):
    """Battery must return to initial_energy_kwh at end of day."""
    expected_interps = get_expected_interps(case)
    result = run_case_without_llm(case, expected_interps)
    schedule = result["schedule"]
    battery = BatteryConfig.model_validate(case["input"]["battery"])

    last = next(e for e in schedule.hourly if e["hour"] == 23)
    assert abs(last["battery_energy_after_kwh"] - battery.initial_energy_kwh) <= TOLERANCE, (
        f"End-of-day battery {last['battery_energy_after_kwh']:.4f} != "
        f"initial {battery.initial_energy_kwh}"
    )


@pytest.mark.parametrize("case", CASES, ids=[c["input"]["scenario_id"] for c in CASES])
def test_sample_cost_within_tolerance(case):
    """
    Optimizer cost must be <= expected cost + tolerance.
    (An equivalent or better schedule is acceptable.)
    """
    expected_interps = get_expected_interps(case)
    result = run_case_without_llm(case, expected_interps)
    exp_out = case["expected_output"]

    our_cost = result["total_cost_bdt"]
    ref_cost = exp_out["total_cost_bdt"]

    # Our cost should not be significantly worse than reference
    # (it should be equal to or better since LP finds optimal)
    assert our_cost <= ref_cost + 1.0, (  # 1 BDT tolerance for different optimal paths
        f"Cost {our_cost:.2f} BDT is significantly worse than reference {ref_cost:.2f} BDT"
    )


@pytest.mark.parametrize("case", CASES, ids=[c["input"]["scenario_id"] for c in CASES])
def test_sample_totals_consistent(case):
    """total_grid_kwh must equal sum(hourly grid_kwh)."""
    expected_interps = get_expected_interps(case)
    result = run_case_without_llm(case, expected_interps)

    plan = result["schedule"].hourly
    computed_total = sum(e["grid_kwh"] for e in plan)
    assert abs(computed_total - result["total_grid_kwh"]) < TOLERANCE, (
        f"total_grid_kwh mismatch: {result['total_grid_kwh']} vs computed {computed_total}"
    )


@pytest.mark.parametrize("case", CASES, ids=[c["input"]["scenario_id"] for c in CASES])
def test_sample_directive_constraints_obeyed(case):
    """All directive constraints must be obeyed in the generated schedule."""
    expected_interps = get_expected_interps(case)
    result = run_case_without_llm(case, expected_interps)

    # validation already checks directive constraints
    assert result["validation"].passed, (
        f"Directive constraints violated for {case['input']['scenario_id']}: "
        f"{result['validation'].errors}"
    )


# ─── Full end-to-end tests with Gemini (requires API key) ────────────────────

@requires_gemini
@pytest.mark.parametrize("case", CASES, ids=[c["input"]["scenario_id"] for c in CASES])
def test_sample_llm_interpretation(case):
    """
    Full pipeline test: LLM interprets notes → guardrails → optimizer → validator.
    Requires GEMINI_API_KEY.
    """
    inp = case["input"]
    exp = case["expected_output"]
    battery = BatteryConfig.model_validate(inp["battery"])

    # LLM interpretation
    raw_interps = interpret_notes(
        scenario_id=inp["scenario_id"],
        operator_notes=inp["operator_notes"],
        battery_capacity_kwh=battery.capacity_kwh,
        battery_minimum_kwh=battery.minimum_energy_kwh,
    )

    # Guardrail validation
    validated = validate_and_normalize(raw_interps, len(inp["operator_notes"]), battery.capacity_kwh)

    # Check directive types match expected
    exp_directives = {d["note_index"]: d["directive_type"] for d in exp["directive_interpretation"]}
    our_directives = {d.note_index: d.directive_type for d in validated}

    assert len(validated) == len(inp["operator_notes"]), (
        f"Expected {len(inp['operator_notes'])} interpretations, got {len(validated)}"
    )

    for idx in range(len(inp["operator_notes"])):
        exp_type = exp_directives[idx]
        our_type = our_directives.get(idx, "MISSING")
        assert our_type == exp_type, (
            f"Note {idx}: expected directive_type='{exp_type}', got '{our_type}'\n"
            f"Note text: {inp['operator_notes'][idx]}\n"
            f"Our explanation: {next((d.explanation for d in validated if d.note_index == idx), 'N/A')}"
        )

    # Full optimizer + validator
    req = OptimizeRequest.model_validate(inp)
    dc = build_directive_constraints(validated)
    schedule = optimize(req.hours, req.battery, dc)
    validation = validate_schedule(schedule.hourly, req.hours, req.battery, dc)

    assert validation.passed, (
        f"Schedule invalid for {inp['scenario_id']}: {validation.errors}"
    )

    # Cost check
    _, our_cost, _ = recalculate_totals(schedule.hourly, req.hours)
    ref_cost = exp["total_cost_bdt"]
    assert our_cost <= ref_cost + 1.0, (
        f"Cost {our_cost:.2f} BDT > reference {ref_cost:.2f} BDT"
    )


# ─── Standalone runner ────────────────────────────────────────────────────────

def run_standalone():
    """Run all sample cases and print a summary report."""
    cases = load_sample_cases()
    passed = 0
    failed = 0
    errors = []

    print(f"\n{'='*70}")
    print("GridWise Public Sample Case Runner")
    print(f"{'='*70}")
    print(f"Loading {len(cases)} cases from {SAMPLE_CASES_PATH}")
    print()

    for case in cases:
        sid = case["input"]["scenario_id"]
        exp_interps = get_expected_interps(case)

        try:
            start = time.monotonic()
            result = run_case_without_llm(case, exp_interps)
            elapsed = time.monotonic() - start

            validation = result["validation"]
            our_cost = result["total_cost_bdt"]
            ref_cost = case["expected_output"]["total_cost_bdt"]
            cost_diff = our_cost - ref_cost

            if validation.passed and our_cost <= ref_cost + 1.0:
                print(f"  [PASS] {sid:<15} | cost={our_cost:>10.2f} BDT "
                      f"(ref={ref_cost:>10.2f}, diff={cost_diff:>+.2f}) | {elapsed:.2f}s")
                passed += 1
            else:
                reasons = []
                if not validation.passed:
                    reasons.extend(validation.errors[:2])
                if our_cost > ref_cost + 1.0:
                    reasons.append(f"Cost {our_cost:.2f} > ref {ref_cost:.2f}")
                print(f"  [FAIL] {sid:<15} | {'; '.join(reasons)}")
                failed += 1
                errors.append((sid, reasons))

        except Exception as e:
            print(f"  [ERROR] {sid:<15} | Exception: {e}")
            failed += 1
            errors.append((sid, [str(e)]))

    print()
    print(f"{'='*70}")
    print(f"Results: {passed} PASSED, {failed} FAILED out of {len(cases)} cases")

    if errors:
        print("\nFailed cases:")
        for sid, msgs in errors:
            print(f"  {sid}: {msgs}")

    print(f"{'='*70}")
    return failed == 0


if __name__ == "__main__":
    success = run_standalone()
    sys.exit(0 if success else 1)
