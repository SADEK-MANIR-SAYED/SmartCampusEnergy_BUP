"""
GridWise Smart Campus Energy Optimization API
BUP CSE Fest 2026 Hackathon

Main FastAPI application entry point.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app import config
from app.schemas import (
    DirectiveInterpretation,
    HourlyPlanEntry,
    OptimizeRequest,
    OptimizeResponse,
)
from app.llm.interpreter import interpret_notes
from app.guardrails import GuardrailError, validate_and_normalize
from app.optimizer import (
    DirectiveConstraints,
    OptimizedSchedule,
    build_directive_constraints,
    optimize,
)
from app.validator import ValidationResult, recalculate_totals, validate_schedule

# ─── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ─── App lifecycle ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("GridWise Energy Optimization API starting up...")
    # Pre-warm: verify Gemini key is set (log warning if not)
    if not config.GEMINI_API_KEY:
        logger.warning(
            "GEMINI_API_KEY is not set! The /optimize-energy endpoint will fail "
            "until a valid API key is provided."
        )
    else:
        logger.info("Gemini API key configured (model: %s)", config.GEMINI_MODEL)
    yield
    logger.info("GridWise API shutting down.")


# ─── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="GridWise Smart Campus Energy Optimization API",
    description="LLM-Assisted Operator Directive Interpretation for energy scheduling",
    version="1.0.0",
    lifespan=lifespan,
)


# ─── Global exception handler ─────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Return controlled 500 error without exposing stack traces."""
    logger.exception("Unhandled exception for %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": "An unexpected error occurred."},
    )


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint. Returns 200 when service is ready."""
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(request: Request):
    """
    Main optimization endpoint.

    Pipeline: LLM interpretation → Guardrails → LP Optimizer → Validator → Response
    """
    start_time = time.monotonic()

    # ── Parse and validate request ────────────────────────────────────────────
    try:
        body = await request.json()
    except Exception as e:
        logger.warning("Malformed JSON request: %s", e)
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    try:
        req = OptimizeRequest.model_validate(body)
    except ValidationError as e:
        logger.warning("Request validation failed: %s", e)
        # Serialize errors safely (ctx may contain non-JSON-serializable objects)
        safe_errors = [
            {k: (str(v) if k == "ctx" else v)
             for k, v in err.items()}
            for err in e.errors()
        ]
        raise HTTPException(status_code=422, detail=safe_errors)

    scenario_id = req.scenario_id
    logger.info(
        "Request received: scenario_id=%s notes=%d",
        scenario_id,
        len(req.operator_notes),
    )

    # ── LLM Interpretation ─────────────────────────────────────────────────────
    try:
        raw_interpretations = interpret_notes(
            scenario_id=scenario_id,
            operator_notes=req.operator_notes,
            battery_capacity_kwh=req.battery.capacity_kwh,
            battery_minimum_kwh=req.battery.minimum_energy_kwh,
        )
    except RuntimeError as e:
        logger.error("LLM interpretation failed for %s: %s", scenario_id, e)
        raise HTTPException(
            status_code=503,
            detail=f"LLM service error: {e}",
        )

    # ── Guardrail validation ───────────────────────────────────────────────────
    try:
        validated_interpretations = validate_and_normalize(
            raw_interpretations=raw_interpretations,
            expected_count=len(req.operator_notes),
            battery_capacity_kwh=req.battery.capacity_kwh,
        )
    except GuardrailError as e:
        logger.error(
            "Guardrail validation failed for %s: %s (note_index=%s)",
            scenario_id,
            e,
            e.note_index,
        )
        raise HTTPException(
            status_code=500,
            detail=f"LLM output failed safety validation: {e}",
        )

    # ── Build directive constraints ────────────────────────────────────────────
    directive_constraints = build_directive_constraints(validated_interpretations)
    logger.debug(
        "Directive constraints for %s: solar_factors=%s, no_charge=%s, no_discharge=%s, "
        "reserves=%s, max_grid=%s",
        scenario_id,
        directive_constraints.solar_factors,
        directive_constraints.no_charge_hours,
        directive_constraints.no_discharge_hours,
        directive_constraints.battery_reserve,
        directive_constraints.max_grid,
    )

    # ── Optimization ───────────────────────────────────────────────────────────
    try:
        schedule: OptimizedSchedule = optimize(
            hours=req.hours,
            battery=req.battery,
            directive_constraints=directive_constraints,
        )
    except RuntimeError as e:
        logger.error("Optimization failed for %s: %s", scenario_id, e)
        raise HTTPException(
            status_code=500,
            detail=f"Optimization failed: {e}",
        )

    # ── Independent validation ─────────────────────────────────────────────────
    validation: ValidationResult = validate_schedule(
        hourly_plan=schedule.hourly,
        hours=req.hours,
        battery=req.battery,
        directive_constraints=directive_constraints,
    )

    if not validation.passed:
        logger.error(
            "Schedule validation failed for %s: %d errors: %s",
            scenario_id,
            len(validation.errors),
            validation.errors,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Generated schedule failed validation: {validation.errors[:5]}",
        )

    # ── Recalculate totals from hourly plan ───────────────────────────────────
    total_grid_kwh, total_cost_bdt, peak_grid_kwh = recalculate_totals(
        schedule.hourly, req.hours
    )

    # ── Build response ─────────────────────────────────────────────────────────
    # Round final outputs to reasonable precision
    total_grid_kwh = round(total_grid_kwh, 4)
    total_cost_bdt = round(total_cost_bdt, 4)
    peak_grid_kwh = round(peak_grid_kwh, 4)

    # Build plan summary
    directive_types = [i.directive_type for i in validated_interpretations if i.applies]
    if directive_types:
        directive_summary = ", ".join(directive_types)
        plan_summary = (
            f"Optimized 24-hour schedule for scenario {scenario_id}. "
            f"Active directives: {directive_summary}. "
            f"Total grid cost: {total_cost_bdt:.2f} BDT over {total_grid_kwh:.2f} kWh."
        )
    else:
        plan_summary = (
            f"Optimized 24-hour schedule for scenario {scenario_id} with no active directives. "
            f"Total grid cost: {total_cost_bdt:.2f} BDT over {total_grid_kwh:.2f} kWh."
        )

    # Round hourly plan values
    hourly_plan_entries = []
    for entry in schedule.hourly:
        hourly_plan_entries.append(
            HourlyPlanEntry(
                hour=entry["hour"],
                grid_kwh=round(entry["grid_kwh"], 4),
                solar_used_kwh=round(entry["solar_used_kwh"], 4),
                battery_action=entry["battery_action"],
                battery_kwh=round(entry["battery_kwh"], 4),
                battery_energy_after_kwh=round(entry["battery_energy_after_kwh"], 4),
            )
        )

    response = OptimizeResponse(
        scenario_id=scenario_id,
        directive_interpretation=validated_interpretations,
        hourly_plan=hourly_plan_entries,
        total_grid_kwh=total_grid_kwh,
        total_cost_bdt=total_cost_bdt,
        peak_grid_kwh=peak_grid_kwh,
        plan_summary=plan_summary,
    )

    elapsed = time.monotonic() - start_time
    logger.info(
        "Request completed: scenario_id=%s cost=%.2f BDT grid=%.2f kWh "
        "solver=%s elapsed=%.2fs",
        scenario_id,
        total_cost_bdt,
        total_grid_kwh,
        schedule.solver_status,
        elapsed,
    )

    return response
