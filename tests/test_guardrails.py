"""
Tests for deterministic guardrails.
These tests do NOT require Gemini API - they test LLM output validation only.
"""
import pytest
from app.guardrails import GuardrailError, validate_and_normalize


BATTERY_CAPACITY = 200.0


def _make_interp(
    note_index=0,
    applies=True,
    directive_type="no_charge_window",
    structured_adjustment=None,
    explanation="test",
):
    """Helper to create a raw interpretation dict."""
    if structured_adjustment is None and directive_type == "no_charge_window":
        structured_adjustment = {"hours": [2, 3, 4]}
    return {
        "note_index": note_index,
        "applies": applies,
        "directive_type": directive_type,
        "structured_adjustment": structured_adjustment,
        "explanation": explanation,
    }


# ─── Basic valid cases ────────────────────────────────────────────────────────

def test_valid_no_op():
    raw = [{"note_index": 0, "applies": False, "directive_type": "no_op", "structured_adjustment": None, "explanation": "irrelevant"}]
    result = validate_and_normalize(raw, 1, BATTERY_CAPACITY)
    assert len(result) == 1
    assert result[0].directive_type == "no_op"
    assert result[0].applies is False
    assert result[0].structured_adjustment is None


def test_valid_solar_reduction():
    raw = [{"note_index": 0, "applies": True, "directive_type": "solar_reduction", "structured_adjustment": {"hours": [12, 13], "factor": 0.25}, "explanation": "cleaning"}]
    result = validate_and_normalize(raw, 1, BATTERY_CAPACITY)
    assert result[0].directive_type == "solar_reduction"
    assert result[0].structured_adjustment["factor"] == 0.25
    assert result[0].structured_adjustment["hours"] == [12, 13]


def test_valid_minimum_battery_reserve():
    raw = [{"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 100.0},
            "explanation": "reserve"}]
    result = validate_and_normalize(raw, 1, BATTERY_CAPACITY)
    assert result[0].structured_adjustment["minimum_energy_kwh"] == 100.0


def test_valid_no_charge_window():
    raw = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [2, 3, 4]}, "explanation": "maintenance"}]
    result = validate_and_normalize(raw, 1, BATTERY_CAPACITY)
    assert result[0].structured_adjustment["hours"] == [2, 3, 4]


def test_valid_no_discharge_window():
    raw = [{"note_index": 0, "applies": True, "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": [17, 18]}, "explanation": "testing"}]
    result = validate_and_normalize(raw, 1, BATTERY_CAPACITY)
    assert result[0].structured_adjustment["hours"] == [17, 18]


def test_valid_max_grid_window():
    raw = [{"note_index": 0, "applies": True, "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [18, 19, 20], "max_grid_kwh": 155.0},
            "explanation": "feeder limit"}]
    result = validate_and_normalize(raw, 1, BATTERY_CAPACITY)
    assert result[0].structured_adjustment["max_grid_kwh"] == 155.0


def test_multiple_notes_valid():
    raw = [
        {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [10, 11], "factor": 0.5}, "explanation": "cloud"},
        {"note_index": 1, "applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [14, 15]}, "explanation": "maintenance"},
        {"note_index": 2, "applies": False, "directive_type": "no_op",
         "structured_adjustment": None, "explanation": "irrelevant"},
    ]
    result = validate_and_normalize(raw, 3, BATTERY_CAPACITY)
    assert len(result) == 3
    assert result[0].note_index == 0
    assert result[1].note_index == 1
    assert result[2].note_index == 2


# ─── Error cases ──────────────────────────────────────────────────────────────

def test_wrong_count_too_few():
    raw = [{"note_index": 0, "applies": False, "directive_type": "no_op", "structured_adjustment": None, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="Expected 2"):
        validate_and_normalize(raw, 2, BATTERY_CAPACITY)


def test_wrong_count_too_many():
    raw = [
        {"note_index": 0, "applies": False, "directive_type": "no_op", "structured_adjustment": None, "explanation": "x"},
        {"note_index": 1, "applies": False, "directive_type": "no_op", "structured_adjustment": None, "explanation": "x"},
    ]
    with pytest.raises(GuardrailError, match="Expected 1"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_invalid_directive_type():
    raw = [{"note_index": 0, "applies": True, "directive_type": "demand_shift",
            "structured_adjustment": {"hours": [1]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="Invalid directive_type"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_duplicate_note_index():
    raw = [
        {"note_index": 0, "applies": False, "directive_type": "no_op", "structured_adjustment": None, "explanation": "x"},
        {"note_index": 0, "applies": False, "directive_type": "no_op", "structured_adjustment": None, "explanation": "x"},
    ]
    with pytest.raises(GuardrailError, match="[Dd]uplicate"):
        validate_and_normalize(raw, 2, BATTERY_CAPACITY)


def test_no_op_applies_true():
    raw = [{"note_index": 0, "applies": True, "directive_type": "no_op",
            "structured_adjustment": None, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="applies=false"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_non_no_op_applies_false():
    raw = [{"note_index": 0, "applies": False, "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [1]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="applies=true"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_solar_factor_negative():
    raw = [{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12], "factor": -0.1}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="factor"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_solar_factor_above_one():
    raw = [{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12], "factor": 1.5}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="factor"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_battery_reserve_exceeds_capacity():
    raw = [{"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [18], "minimum_energy_kwh": 999.0},
            "explanation": "x"}]
    with pytest.raises(GuardrailError, match="capacity"):
        validate_and_normalize(raw, 1, 200.0)


def test_battery_reserve_negative():
    raw = [{"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [18], "minimum_energy_kwh": -10.0},
            "explanation": "x"}]
    with pytest.raises(GuardrailError, match="non-negative"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_max_grid_negative():
    raw = [{"note_index": 0, "applies": True, "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [18], "max_grid_kwh": -1.0},
            "explanation": "x"}]
    with pytest.raises(GuardrailError, match="non-negative"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_invalid_hour_out_of_range():
    raw = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [24]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="out of range"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_duplicate_hours_in_directive():
    raw = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [2, 2, 3]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="duplicate"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_hours_must_be_ascending():
    """Unsorted hours must be REJECTED, not silently sorted."""
    raw = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [4, 3, 2]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="ascending"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_missing_hours_field():
    raw = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
            "structured_adjustment": {"factor": 0.5}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="hours"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_solar_missing_factor():
    raw = [{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="factor"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_note_index_out_of_range():
    raw = [{"note_index": 5, "applies": False, "directive_type": "no_op",
            "structured_adjustment": None, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="out of range"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_malformed_not_a_list():
    with pytest.raises(GuardrailError, match="list"):
        validate_and_normalize({"note_index": 0}, 1, BATTERY_CAPACITY)


def test_no_op_with_non_null_adjustment_rejected():
    """no_op with a non-null adjustment must be REJECTED per spec."""
    raw = [{"note_index": 0, "applies": False, "directive_type": "no_op",
            "structured_adjustment": {"hours": [1]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="structured_adjustment=null"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_hours_empty_list():
    raw = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": []}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="empty"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)


def test_reject_boolean_and_float_hours():
    """Hours must be strict integers, rejecting booleans, floats, strings."""
    # Boolean hour
    raw1 = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
             "structured_adjustment": {"hours": [True, 2]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="strict integer"):
        validate_and_normalize(raw1, 1, BATTERY_CAPACITY)

    # Float hour
    raw2 = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
             "structured_adjustment": {"hours": [13.5]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="strict integer"):
        validate_and_normalize(raw2, 1, BATTERY_CAPACITY)

    # String hour
    raw3 = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
             "structured_adjustment": {"hours": ["13"]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="strict integer"):
        validate_and_normalize(raw3, 1, BATTERY_CAPACITY)


def test_reject_string_applies():
    """applies must be strict boolean."""
    raw = [{"note_index": 0, "applies": "true", "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [2, 3]}, "explanation": "x"}]
    with pytest.raises(GuardrailError, match="strict boolean"):
        validate_and_normalize(raw, 1, BATTERY_CAPACITY)
