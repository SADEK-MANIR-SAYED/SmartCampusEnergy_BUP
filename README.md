# GridWise Smart Campus Energy Optimization API

**BUP CSE Fest 2026 Hackathon — Smart Campus Energy Optimization Challenge**

> LLM-Assisted Operator Directive Interpretation — Online Preliminary Round

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Challenge Explanation](#2-challenge-explanation)
3. [Architecture](#3-architecture)
4. [Request Flow](#4-request-flow)
5. [LLM Role](#5-llm-role)
6. [Supported Directives](#6-supported-directives)
7. [Guardrail System](#7-guardrail-system)
8. [Optimization Model](#8-optimization-model)
9. [Battery Rules](#9-battery-rules)
10. [API Endpoints](#10-api-endpoints)
11. [Request Schema](#11-request-schema)
12. [Response Schema](#12-response-schema)
13. [Environment Variables](#13-environment-variables)
14. [Gemini Setup](#14-gemini-setup)
15. [Local Installation](#15-local-installation)
16. [Local Run Command](#16-local-run-command)
17. [API Examples](#17-api-examples)
18. [Sample Case Tests](#18-sample-case-tests)
19. [Docker](#19-docker)
20. [Deployment Notes](#20-deployment-notes)
21. [Security](#21-security)
22. [Dependencies](#22-dependencies)
23. [Known Limitations](#23-known-limitations)
24. [Testing Instructions](#24-testing-instructions)
25. [Architecture Diagram](#25-architecture-diagram)
26. [Pipeline Explanation](#26-llm--guardrail--optimizer--validator-pipeline)

---

## 1. Project Overview

GridWise is a one-endpoint HTTP API that receives a 24-hour campus energy scenario plus natural-language operator notes and returns an optimal energy schedule. It uses Google Gemini to interpret operator notes into structured directives, then applies them to a Mixed-Integer Linear Programming (MILP) optimizer that minimizes grid electricity cost.

## 2. Challenge Explanation

The challenge requires building a system that:
- Accepts 1–3 natural-language operator notes
- Interprets them semantically using an LLM (Gemini)
- Converts notes to structured energy directives (or `no_op`)
- Applies those directives as constraints on a 24-hour energy scheduler
- Returns the optimal (minimum-cost) schedule that satisfies all constraints

## 3. Architecture

```
POST /optimize-energy
         │
         ▼
  ┌─────────────────┐
  │ Request Validator│  Pydantic v2 — 400/422 on invalid input
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  Gemini LLM     │  gemini-2.0-flash — interprets all notes in one call
  │  Interpreter    │  Returns structured JSON with directive types & params
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  Deterministic  │  Validates EVERY field of LLM output
  │  Guardrails     │  Raises controlled error if invalid
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  Directive      │  Converts validated directives → LP constraints
  │  Applier        │
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  MILP Optimizer │  PuLP + CBC solver
  │  (PuLP + CBC)   │  Binary vars for charge/discharge exclusivity
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  Independent    │  Replays battery state, checks every constraint
  │  Validator      │
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  Response       │  Recalculates totals from hourly_plan
  │  Builder        │
  └────────┬────────┘
           │
           ▼
      JSON Response
```

## 4. Request Flow

1. Request JSON parsed → Pydantic validation
2. Gemini API called with all operator notes + scenario context
3. LLM output validated by deterministic guardrails
4. Valid directives converted to optimization constraints
5. MILP model solved (minimizes grid cost)
6. Schedule validated independently (emulates judge)
7. Totals recalculated from hourly plan
8. JSON response returned

## 5. LLM Role

Gemini (`gemini-2.0-flash`) performs **semantic interpretation only** — it:
- Classifies each note as a specific directive type or `no_op`
- Extracts affected hours (using start-inclusive/end-exclusive semantics)
- Extracts numeric parameters (factors, kWh values, etc.)
- Does NOT solve the optimization
- Does NOT modify scenario data

The LLM is called **once per scenario** with all notes in a single batch request. All output is validated by deterministic guardrails before any constraint is applied.

## 6. Supported Directives

| Directive | Meaning | Structured Adjustment |
|---|---|---|
| `solar_reduction` | Reduce usable solar by a factor | `{"hours": [...], "factor": 0.0–1.0}` |
| `minimum_battery_reserve` | Battery must stay above a minimum | `{"hours": [...], "minimum_energy_kwh": N}` |
| `no_charge_window` | Forbid battery charging | `{"hours": [...]}` |
| `no_discharge_window` | Forbid battery discharging | `{"hours": [...]}` |
| `max_grid_window` | Cap grid import | `{"hours": [...], "max_grid_kwh": N}` |
| `no_op` | Note is irrelevant | `null` |

**Time semantics**: Start-inclusive, end-exclusive.
- "2 PM to 4 PM" → hours `[14, 15]`
- "6 PM to 9 PM" → hours `[18, 19, 20]`

**Solar factor**: `factor` is the **fraction that remains** (not the reduction amount).
- "80% reduction" → `factor = 0.2`
- "25% usable" → `factor = 0.25`

## 7. Guardrail System

All LLM output is treated as untrusted. The guardrail layer validates:

- `directive_type` — must be one of 6 allowed types
- `note_index` — 0..N-1, unique, no duplicates, no missing
- `applies` — `no_op` requires `false`; all others require `true`
- `hours` — integers 0–23, unique, sorted ascending
- `factor` — finite, 0 ≤ factor ≤ 1
- `minimum_energy_kwh` — finite, non-negative, ≤ battery capacity
- `max_grid_kwh` — finite, non-negative
- `structured_adjustment` — exact schema match

If any check fails: controlled error response, no optimizer call.

## 8. Optimization Model

**Method**: Mixed-Integer Linear Programming (MILP) using PuLP + CBC solver.

**Variables** (per hour h):
- `grid[h]` — continuous, ≥ 0 (kWh from grid)
- `solar_used[h]` — continuous, 0 ≤ solar_used ≤ effective_solar
- `charge_kwh[h]` — continuous, 0 ≤ charge ≤ max_charge
- `discharge_kwh[h]` — continuous, 0 ≤ discharge ≤ max_discharge
- `charge_b[h]` — binary (1 = charging active)
- `discharge_b[h]` — binary (1 = discharging active)
- `battery_e[h]` — continuous (battery energy after hour h)

**Objective**: Minimize `Σ grid[h] × tariff[h]`

**Key Constraints**:
- Energy balance: `grid + solar + discharge = demand + charge` (every hour)
- Battery state: `E[h] = E[h-1] + charge - discharge`
- `charge_b + discharge_b ≤ 1` (no simultaneous charge/discharge)
- Battery bounds: `min_energy ≤ E[h] ≤ capacity`
- **End-of-day neutrality**: `E[23] = initial_energy_kwh`

## 9. Battery Rules

- Energy after charging: `E_after = E_before + battery_kwh`
- Energy after discharging: `E_after = E_before − battery_kwh`
- Energy when idle: `E_after = E_before`
- Battery cannot go below `minimum_energy_kwh` (or directive-imposed reserve)
- Battery cannot exceed `capacity_kwh`
- Charge rate ≤ `max_charge_kwh_per_hour`
- Discharge rate ≤ `max_discharge_kwh_per_hour`
- **Critical**: `battery_energy_after[hour=23] = initial_energy_kwh`

## 10. API Endpoints

### GET /health

Returns service readiness:
```json
{"status": "ok"}
```

### POST /optimize-energy

Accepts a scenario and returns an optimized 24-hour schedule.

## 11. Request Schema

```json
{
  "scenario_id": "SAMPLE-01",
  "operator_notes": [
    "Panels will be cleaned from noon until 2 PM. Usable solar: 25% of forecast.",
    "The sports office moved next month's registration deadline."
  ],
  "hours": [
    {"hour": 0, "demand_kwh": 90, "solar_kwh": 0, "tariff_bdt_per_kwh": 8},
    ...
  ],
  "battery": {
    "capacity_kwh": 220,
    "initial_energy_kwh": 110,
    "minimum_energy_kwh": 40,
    "max_charge_kwh_per_hour": 50,
    "max_discharge_kwh_per_hour": 50
  }
}
```

Constraints:
- `operator_notes`: 1–3 non-empty strings
- `hours`: exactly 24 entries, hours 0–23 (unique)
- All numeric values must be non-negative

## 12. Response Schema

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
      "explanation": "Panel cleaning reduces solar to 25% at noon and 1 PM."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "Deadline change is unrelated to today's energy schedule."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 90.0,
      "solar_used_kwh": 0.0,
      "battery_action": "idle",
      "battery_kwh": 0.0,
      "battery_energy_after_kwh": 110.0
    },
    ...
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "Optimized 24-hour schedule..."
}
```

## 13. Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | **Yes** | — | Google Gemini API key |
| `GEMINI_MODEL` | No | `gemini-2.0-flash` | Gemini model name |
| `GEMINI_TIMEOUT` | No | `25` | API call timeout (seconds) |
| `PORT` | No | `8000` | Server port |
| `LOG_LEVEL` | No | `INFO` | Logging level |

## 14. Gemini Setup

1. Go to [Google AI Studio](https://aistudio.google.com/apikey)
2. Create a new API key
3. Set the environment variable:
   ```powershell
   $env:GEMINI_API_KEY = "your_api_key_here"
   ```
   Or create a `.env` file (see `.env.example`).

## 15. Local Installation

```bash
# Create virtual environment (optional but recommended)
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Copy environment template
copy .env.example .env
# Edit .env and set GEMINI_API_KEY
```

## 16. Local Run Command

```bash
# Start the server
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Or with environment variable on Windows
$env:GEMINI_API_KEY="your_key"; python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 17. API Examples

### Health Check

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### Optimize Energy

```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "TEST-01",
    "operator_notes": [
      "Battery charging unavailable from 2 AM to 5 AM for maintenance."
    ],
    "hours": [
      {"hour": 0, "demand_kwh": 100, "solar_kwh": 0, "tariff_bdt_per_kwh": 8},
      {"hour": 1, "demand_kwh": 90, "solar_kwh": 0, "tariff_bdt_per_kwh": 7},
      {"hour": 2, "demand_kwh": 85, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 3, "demand_kwh": 80, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
      {"hour": 4, "demand_kwh": 85, "solar_kwh": 0, "tariff_bdt_per_kwh": 7},
      {"hour": 5, "demand_kwh": 90, "solar_kwh": 0, "tariff_bdt_per_kwh": 8},
      {"hour": 6, "demand_kwh": 100, "solar_kwh": 5, "tariff_bdt_per_kwh": 9},
      {"hour": 7, "demand_kwh": 120, "solar_kwh": 20, "tariff_bdt_per_kwh": 12},
      {"hour": 8, "demand_kwh": 150, "solar_kwh": 60, "tariff_bdt_per_kwh": 15},
      {"hour": 9, "demand_kwh": 160, "solar_kwh": 90, "tariff_bdt_per_kwh": 18},
      {"hour": 10, "demand_kwh": 170, "solar_kwh": 120, "tariff_bdt_per_kwh": 20},
      {"hour": 11, "demand_kwh": 180, "solar_kwh": 140, "tariff_bdt_per_kwh": 22},
      {"hour": 12, "demand_kwh": 175, "solar_kwh": 150, "tariff_bdt_per_kwh": 25},
      {"hour": 13, "demand_kwh": 170, "solar_kwh": 140, "tariff_bdt_per_kwh": 24},
      {"hour": 14, "demand_kwh": 165, "solar_kwh": 120, "tariff_bdt_per_kwh": 22},
      {"hour": 15, "demand_kwh": 160, "solar_kwh": 90, "tariff_bdt_per_kwh": 20},
      {"hour": 16, "demand_kwh": 155, "solar_kwh": 50, "tariff_bdt_per_kwh": 22},
      {"hour": 17, "demand_kwh": 160, "solar_kwh": 20, "tariff_bdt_per_kwh": 25},
      {"hour": 18, "demand_kwh": 170, "solar_kwh": 5, "tariff_bdt_per_kwh": 28},
      {"hour": 19, "demand_kwh": 175, "solar_kwh": 0, "tariff_bdt_per_kwh": 30},
      {"hour": 20, "demand_kwh": 165, "solar_kwh": 0, "tariff_bdt_per_kwh": 28},
      {"hour": 21, "demand_kwh": 155, "solar_kwh": 0, "tariff_bdt_per_kwh": 24},
      {"hour": 22, "demand_kwh": 140, "solar_kwh": 0, "tariff_bdt_per_kwh": 18},
      {"hour": 23, "demand_kwh": 120, "solar_kwh": 0, "tariff_bdt_per_kwh": 12}
    ],
    "battery": {
      "capacity_kwh": 200,
      "initial_energy_kwh": 100,
      "minimum_energy_kwh": 30,
      "max_charge_kwh_per_hour": 50,
      "max_discharge_kwh_per_hour": 50
    }
  }'
```

## 18. Sample Case Tests

Run all 10 public sample cases (no Gemini needed):

```bash
# Standalone runner (shows PASS/FAIL per case)
python -X utf8 tests/test_samples.py

# Via pytest (no LLM tests)
python -m pytest tests/test_samples.py -k "not llm" -v

# Full pytest including LLM tests (requires GEMINI_API_KEY)
python -m pytest tests/test_samples.py -v
```

Run all tests:
```bash
python -m pytest tests/ -k "not llm" -v
```

## 19. Docker

### Build

```bash
docker build -t gridwise-api .
```

### Run

```bash
docker run -p 8000:8000 -e GEMINI_API_KEY=your_key_here gridwise-api
```

### Test in container

```bash
# Health check
curl http://localhost:8000/health

# Optimize
curl -X POST http://localhost:8000/optimize-energy -H "Content-Type: application/json" -d @example_request.json
```

## 20. Deployment Notes

- Service listens on `0.0.0.0:8000` (configurable via `PORT`)
- No authentication required for endpoints
- `/health` must return 200 within 60 seconds of container start
- `/optimize-energy` targets < 5 seconds p95 latency
- Secrets are never baked into the Docker image
- All secrets supplied via environment variables at runtime

## 21. Security

- **No secrets committed**: `.env` is git-ignored, only `.env.example` is committed
- **No stack traces exposed**: All errors return controlled JSON responses
- **No key logging**: API keys are never logged or exposed in responses
- **Environment-only credentials**: Gemini key loaded from environment variable only

## 22. Dependencies

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | ≥0.104 | HTTP API framework |
| `uvicorn` | ≥0.24 | ASGI server |
| `pydantic` | ≥2.0 | Request/response validation |
| `google-genai` | ≥1.0 | Gemini API client |
| `PuLP` | ≥2.7 | LP/MILP optimization |
| `python-dotenv` | ≥1.0 | .env file loading |
| `httpx` | ≥0.25 | HTTP client (for testing) |

## 23. Known Limitations

- PuLP 3.x uses `PULP_CBC_CMD` which shows a deprecation warning; functionally correct
- LLM interpretation quality depends on Gemini model version
- Very tight battery constraints (initial == minimum) may limit optimization options
- Docker build requires internet access to download Python packages

## 24. Testing Instructions

```bash
# 1. Run all non-LLM tests (fast, ~2 seconds)
python -m pytest tests/test_guardrails.py tests/test_optimizer.py tests/test_api.py -v

# 2. Run sample case optimizer tests (no LLM)
python -m pytest tests/test_samples.py -k "not llm" -v

# 3. Run sample case standalone runner
python -X utf8 tests/test_samples.py

# 4. Run LLM tests (requires GEMINI_API_KEY)
$env:GEMINI_API_KEY="your_key"
python -m pytest tests/test_samples.py -k "llm" -v

# 5. Quick LLM sanity check
python test_llm_quick.py

# 6. All tests
python -m pytest tests/ -v
```

## 25. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                      HTTP Client (Judge)                        │
└────────────────────────────┬────────────────────────────────────┘
                             │ POST /optimize-energy
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     FastAPI Application                         │
│                                                                 │
│  ┌─────────────┐   ┌──────────────┐   ┌───────────────────┐    │
│  │   Pydantic  │   │    Gemini    │   │   Deterministic   │    │
│  │  Validator  │──▶│  Interpreter │──▶│    Guardrails     │    │
│  └─────────────┘   └──────────────┘   └─────────┬─────────┘    │
│        400/422       (1 LLM call)               │              │
│                                                 ▼              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Directive Applier                          │   │
│  │  solar_factors | no_charge_hours | battery_reserve      │   │
│  │  no_discharge_hours | max_grid                          │   │
│  └──────────────────────────┬──────────────────────────────┘   │
│                             ▼                                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              PuLP MILP Optimizer (CBC)                  │   │
│  │  min Σ grid[h]×tariff[h]                                │   │
│  │  s.t. energy balance, battery bounds, end-of-day        │   │
│  │       neutrality, directive constraints                  │   │
│  └──────────────────────────┬──────────────────────────────┘   │
│                             ▼                                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │          Independent Schedule Validator                 │   │
│  │  (replays battery state, checks every constraint)       │   │
│  └──────────────────────────┬──────────────────────────────┘   │
│                             ▼                                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Response Builder                           │   │
│  │  (recalculates totals from hourly_plan)                 │   │
│  └──────────────────────────┬──────────────────────────────┘   │
└───────────────────────────  │  ──────────────────────────────────┘
                             ▼
                    JSON Response (200)
```

## 26. LLM → Guardrail → Optimizer → Validator Pipeline

### Step 1: LLM Interpretation
Gemini receives the system prompt (with all 6 directive schemas and time semantics) plus the specific notes and scenario context. It returns a JSON array of interpretations — one per note.

### Step 2: Deterministic Guardrails  
Every field of every interpretation is validated before any constraint is set:
- Type checking (string, int, float, bool)
- Range checking (hours 0-23, factor 0-1, reserve ≤ capacity)
- Semantic consistency (`no_op` must have `applies=false`)
- Completeness (one entry per note, no duplicates, no missing)

### Step 3: Directive Application  
Valid directives are aggregated into a `DirectiveConstraints` object:
- Solar factors are applied per-hour (multiple reductions take the minimum factor)
- Reserves are applied per-hour (multiple reserves take the maximum)
- Charge/discharge prohibitions are collected into hour sets
- Grid caps are applied per-hour (multiple caps take the minimum)

### Step 4: MILP Optimization  
PuLP constructs a MILP with 24×7 variables per scenario. Binary variables enforce that charging and discharging are mutually exclusive. The solver finds the global minimum-cost schedule.

### Step 5: Independent Validation  
A separate validator replays the battery state from scratch, checking:
- Energy balance every hour
- Battery bounds (min, capacity, rate limits)
- Directive constraints
- End-of-day neutrality

### Step 6: Response Building  
Totals are recalculated directly from `hourly_plan` (not from optimizer internals) to ensure the response is self-consistent.
