"""Pydantic schemas for request and response validation."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────
# REQUEST SCHEMAS
# ─────────────────────────────────────────────

class HourRecord(BaseModel):
    hour: int = Field(..., ge=0, le=23)
    demand_kwh: float = Field(..., ge=0)
    solar_kwh: float = Field(..., ge=0)
    tariff_bdt_per_kwh: float = Field(..., ge=0)


class BatteryConfig(BaseModel):
    capacity_kwh: float = Field(..., gt=0)
    initial_energy_kwh: float = Field(..., ge=0)
    minimum_energy_kwh: float = Field(..., ge=0)
    max_charge_kwh_per_hour: float = Field(..., gt=0)
    max_discharge_kwh_per_hour: float = Field(..., gt=0)

    @model_validator(mode="after")
    def validate_battery_consistency(self) -> "BatteryConfig":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError(
                f"initial_energy_kwh ({self.initial_energy_kwh}) cannot exceed "
                f"capacity_kwh ({self.capacity_kwh})"
            )

        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError(
                f"minimum_energy_kwh ({self.minimum_energy_kwh}) cannot exceed "
                f"capacity_kwh ({self.capacity_kwh})"
            )

        if self.initial_energy_kwh < self.minimum_energy_kwh:
            raise ValueError(
                f"initial_energy_kwh ({self.initial_energy_kwh}) cannot be less than "
                f"minimum_energy_kwh ({self.minimum_energy_kwh})"
            )

        return self


class OptimizeRequest(BaseModel):
    scenario_id: str = Field(..., min_length=1)
    operator_notes: List[str] = Field(..., min_length=1, max_length=3)
    hours: List[HourRecord] = Field(..., min_length=24, max_length=24)
    battery: BatteryConfig

    @field_validator("operator_notes")
    @classmethod
    def validate_notes(cls, v: List[str]) -> List[str]:
        for i, note in enumerate(v):
            if not note or not note.strip():
                raise ValueError(
                    f"operator_notes[{i}] must be a non-empty string"
                )
        return v

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v: List[HourRecord]) -> List[HourRecord]:
        if len(v) != 24:
            raise ValueError("hours must contain exactly 24 entries")

        hours_seen = set()

        for rec in v:
            if rec.hour in hours_seen:
                raise ValueError(f"Duplicate hour: {rec.hour}")

            hours_seen.add(rec.hour)

        if hours_seen != set(range(24)):
            missing = set(range(24)) - hours_seen
            raise ValueError(f"Missing hours: {sorted(missing)}")

        # Sort by hour for consistent processing
        return sorted(v, key=lambda h: h.hour)


# ─────────────────────────────────────────────
# DIRECTIVE SCHEMAS (LLM output structure)
# ─────────────────────────────────────────────

ALLOWED_DIRECTIVES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


class SolarReductionAdjustment(BaseModel):
    hours: List[int]
    factor: float


class MinimumBatteryReserveAdjustment(BaseModel):
    hours: List[int]
    minimum_energy_kwh: float


class NoChargeWindowAdjustment(BaseModel):
    hours: List[int]


class NoDischargeWindowAdjustment(BaseModel):
    hours: List[int]


class MaxGridWindowAdjustment(BaseModel):
    hours: List[int]
    max_grid_kwh: float


class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: str
    structured_adjustment: Optional[dict] = None
    explanation: str


# ─────────────────────────────────────────────
# RESPONSE SCHEMAS
# ─────────────────────────────────────────────

class HourlyPlanEntry(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str