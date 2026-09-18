"""
API integration tests. Tests /health and /optimize-energy.
LLM calls are mocked so these tests do NOT require Gemini API.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def make_valid_request(scenario_id="TEST-01", notes=None, demands=None, solars=None, tariffs=None):
    """Create a valid request body."""
    if notes is None:
        notes = ["Maintenance crew will work on the generator tomorrow."]
    if demands is None:
        demands = [100.0] * 24
    if solars is None:
        solars = [0.0] * 24
    if tariffs is None:
        tariffs = [10.0] * 24

    hours = [
        {"hour": h, "demand_kwh": demands[h], "solar_kwh": solars[h], "tariff_bdt_per_kwh": tariffs[h]}
        for h in range(24)
    ]
    return {
        "scenario_id": scenario_id,
        "operator_notes": notes,
        "hours": hours,
        "battery": {
            "capacity_kwh": 200.0,
            "initial_energy_kwh": 100.0,
            "minimum_energy_kwh": 30.0,
            "max_charge_kwh_per_hour": 50.0,
            "max_discharge_kwh_per_hour": 50.0,
        },
    }


def make_no_op_interp(note_index=0):
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "Not related to today's energy schedule.",
    }


# ─── Health endpoint ──────────────────────────────────────────────────────────

def test_health_returns_200():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"status": "ok"}


def test_health_json_content_type():
    resp = client.get("/health")
    assert "application/json" in resp.headers["content-type"]


# ─── /optimize-energy: happy path ────────────────────────────────────────────

def test_optimize_returns_200_with_valid_request():
    with patch("app.main.interpret_notes") as mock_llm:
        mock_llm.return_value = [make_no_op_interp(0)]
        resp = client.post("/optimize-energy", json=make_valid_request())
    assert resp.status_code == 200


def test_optimize_response_schema():
    with patch("app.main.interpret_notes") as mock_llm:
        mock_llm.return_value = [make_no_op_interp(0)]
        resp = client.post("/optimize-energy", json=make_valid_request("SC-001"))
    data = resp.json()
    assert data["scenario_id"] == "SC-001"
    assert "directive_interpretation" in data
    assert "hourly_plan" in data
    assert "total_grid_kwh" in data
    assert "total_cost_bdt" in data
    assert "peak_grid_kwh" in data
    assert "plan_summary" in data


def test_optimize_hourly_plan_24_entries():
    with patch("app.main.interpret_notes") as mock_llm:
        mock_llm.return_value = [make_no_op_interp(0)]
        resp = client.post("/optimize-energy", json=make_valid_request())
    data = resp.json()
    assert len(data["hourly_plan"]) == 24
    hours_in_plan = [e["hour"] for e in data["hourly_plan"]]
    assert sorted(hours_in_plan) == list(range(24))


def test_optimize_scenario_id_matches():
    with patch("app.main.interpret_notes") as mock_llm:
        mock_llm.return_value = [make_no_op_interp(0)]
        resp = client.post("/optimize-energy", json=make_valid_request("MY-SCENARIO-42"))
    assert resp.json()["scenario_id"] == "MY-SCENARIO-42"


def test_optimize_directive_interpretation_count_matches_notes():
    notes = [
        "Panels will be cleaned from noon to 2 PM.",
        "Staff meeting scheduled next week.",
    ]
    with patch("app.main.interpret_notes") as mock_llm:
        mock_llm.return_value = [make_no_op_interp(0), make_no_op_interp(1)]
        resp = client.post("/optimize-energy", json=make_valid_request(notes=notes))
    data = resp.json()
    assert len(data["directive_interpretation"]) == 2


def test_optimize_hourly_plan_fields():
    with patch("app.main.interpret_notes") as mock_llm:
        mock_llm.return_value = [make_no_op_interp(0)]
        resp = client.post("/optimize-energy", json=make_valid_request())
    plan = resp.json()["hourly_plan"]
    for entry in plan:
        assert "hour" in entry
        assert "grid_kwh" in entry
        assert "solar_used_kwh" in entry
        assert "battery_action" in entry
        assert "battery_kwh" in entry
        assert "battery_energy_after_kwh" in entry
        assert entry["battery_action"] in ("charge", "discharge", "idle")


def test_optimize_with_solar_reduction_directive():
    notes = ["Solar panels will be cleaned from noon until 2 PM; expect 25% usable solar."]
    with patch("app.main.interpret_notes") as mock_llm:
        mock_llm.return_value = [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
                "explanation": "Panel cleaning",
            }
        ]
        solars = [0] * 12 + [80, 80] + [0] * 10
        resp = client.post(
            "/optimize-energy",
            json=make_valid_request(solars=solars),
        )
    assert resp.status_code == 200
    plan = resp.json()["hourly_plan"]
    for entry in plan:
        if entry["hour"] in (12, 13):
            assert entry["solar_used_kwh"] <= 80 * 0.25 + 0.01


def test_optimize_with_no_charge_directive():
    notes = ["Battery charger offline from 2 AM to 5 AM."]
    with patch("app.main.interpret_notes") as mock_llm:
        mock_llm.return_value = [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": [2, 3, 4]},
                "explanation": "Charger offline",
            }
        ]
        resp = client.post("/optimize-energy", json=make_valid_request())
    assert resp.status_code == 200
    plan = resp.json()["hourly_plan"]
    for entry in plan:
        if entry["hour"] in (2, 3, 4):
            assert entry["battery_action"] != "charge" or entry["battery_kwh"] < 0.01


# ─── Error cases ──────────────────────────────────────────────────────────────

def test_malformed_json_returns_400():
    resp = client.post(
        "/optimize-energy",
        content=b"not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_missing_scenario_id_returns_422():
    body = make_valid_request()
    del body["scenario_id"]
    with patch("app.main.interpret_notes"):
        resp = client.post("/optimize-energy", json=body)
    assert resp.status_code == 422


def test_missing_hours_returns_422():
    body = make_valid_request()
    del body["hours"]
    with patch("app.main.interpret_notes"):
        resp = client.post("/optimize-energy", json=body)
    assert resp.status_code == 422


def test_wrong_hour_count_returns_422():
    body = make_valid_request()
    body["hours"] = body["hours"][:20]  # Only 20 hours
    with patch("app.main.interpret_notes"):
        resp = client.post("/optimize-energy", json=body)
    assert resp.status_code == 422


def test_too_many_notes_returns_422():
    body = make_valid_request()
    body["operator_notes"] = ["note1", "note2", "note3", "note4"]  # 4 notes
    with patch("app.main.interpret_notes"):
        resp = client.post("/optimize-energy", json=body)
    assert resp.status_code == 422


def test_empty_notes_array_returns_422():
    body = make_valid_request()
    body["operator_notes"] = []
    with patch("app.main.interpret_notes"):
        resp = client.post("/optimize-energy", json=body)
    assert resp.status_code == 422


def test_duplicate_hours_returns_422():
    body = make_valid_request()
    body["hours"][5] = {"hour": 0, "demand_kwh": 100, "solar_kwh": 0, "tariff_bdt_per_kwh": 10}
    with patch("app.main.interpret_notes"):
        resp = client.post("/optimize-energy", json=body)
    assert resp.status_code == 422


def test_invalid_battery_initial_exceeds_capacity():
    body = make_valid_request()
    body["battery"]["initial_energy_kwh"] = 300  # > capacity 200
    with patch("app.main.interpret_notes"):
        resp = client.post("/optimize-energy", json=body)
    assert resp.status_code == 422


def test_llm_failure_returns_503():
    with patch("app.main.interpret_notes") as mock_llm:
        mock_llm.side_effect = RuntimeError("Gemini unavailable")
        resp = client.post("/optimize-energy", json=make_valid_request())
    assert resp.status_code == 503


def test_empty_note_string_returns_422():
    body = make_valid_request()
    body["operator_notes"] = [""]
    with patch("app.main.interpret_notes"):
        resp = client.post("/optimize-energy", json=body)
    assert resp.status_code == 422


def test_negative_demand_returns_422():
    body = make_valid_request()
    body["hours"][0]["demand_kwh"] = -10
    with patch("app.main.interpret_notes"):
        resp = client.post("/optimize-energy", json=body)
    assert resp.status_code == 422


def test_response_totals_consistent():
    """total_grid_kwh must equal sum of hourly grid_kwh."""
    with patch("app.main.interpret_notes") as mock_llm:
        mock_llm.return_value = [make_no_op_interp(0)]
        resp = client.post("/optimize-energy", json=make_valid_request())
    data = resp.json()
    plan_total = sum(e["grid_kwh"] for e in data["hourly_plan"])
    assert abs(plan_total - data["total_grid_kwh"]) < 0.01
