"""Gemini LLM interpreter for operator notes."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app import config
from app.llm.prompt import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

# ─── Lazy-initialized Gemini client ───────────────────────────────────────────
_client = None


def _get_client():
    """Lazily initialize the Gemini client."""
    global _client
    if _client is None:
        try:
            from google import genai  # type: ignore
            _client = genai.Client(api_key=config.GEMINI_API_KEY)
        except Exception as e:
            logger.error("Failed to initialize Gemini client: %s", e)
            raise RuntimeError(f"Gemini client initialization failed: {e}") from e
    return _client


# ─── Interpreter ──────────────────────────────────────────────────────────────

def interpret_notes(
    scenario_id: str,
    operator_notes: list[str],
    battery_capacity_kwh: float,
    battery_minimum_kwh: float,
) -> list[dict[str, Any]]:
    """
    Use Gemini to interpret operator notes into directives.

    Returns a list of raw directive interpretation dicts (pre-guardrail).
    Raises RuntimeError on unrecoverable LLM failure.
    """
    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is not set. "
            "Please configure it before starting the service."
        )

    user_prompt = build_user_prompt(
        scenario_id=scenario_id,
        operator_notes=operator_notes,
        battery_capacity_kwh=battery_capacity_kwh,
        battery_minimum_kwh=battery_minimum_kwh,
    )

    full_prompt = SYSTEM_PROMPT + "\n\n" + user_prompt

    logger.info(
        "Calling Gemini for scenario=%s notes_count=%d",
        scenario_id,
        len(operator_notes),
    )

    start = time.monotonic()
    raw_text = _call_gemini(full_prompt)
    elapsed = time.monotonic() - start

    logger.info("Gemini call completed in %.2fs for scenario=%s", elapsed, scenario_id)

    parsed = _parse_gemini_response(raw_text, len(operator_notes))
    return parsed


def _call_gemini(prompt: str) -> str:
    """Call Gemini API with retry logic. Returns raw text response."""
    client = _get_client()

    max_retries = 2
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config={
                    "temperature": 0.1,  # Low temperature for determinism
                    "response_mime_type": "application/json",
                },
            )
            return response.text
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                wait = 1.5 * (attempt + 1)
                logger.warning(
                    "Gemini attempt %d/%d failed: %s. Retrying in %.1fs...",
                    attempt + 1,
                    max_retries + 1,
                    e,
                    wait,
                )
                time.sleep(wait)
            else:
                logger.error("All Gemini attempts failed: %s", e)

    raise RuntimeError(f"Gemini API call failed after {max_retries + 1} attempts: {last_error}")


def _parse_gemini_response(raw_text: str, expected_count: int) -> list[dict[str, Any]]:
    """
    Parse and do minimal structural parsing of Gemini response.
    Returns list of raw interpretation dicts.
    Full validation is done by guardrails.
    """
    if not raw_text or not raw_text.strip():
        raise RuntimeError("Gemini returned an empty response")

    # Strip markdown code fences if present
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (``` markers)
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("Gemini response is not valid JSON: %s | Raw: %.200s", e, raw_text)
        raise RuntimeError(f"Gemini returned invalid JSON: {e}") from e

    # Extract interpretations from wrapper object or array
    if isinstance(data, dict):
        interps = data.get("interpretations", data.get("directive_interpretations", data.get("results", None)))
        if interps is None:
            # Maybe entire object is a single interpretation? Unlikely but handle
            if "note_index" in data:
                interps = [data]
            else:
                raise RuntimeError(f"Gemini response missing 'interpretations' key. Keys: {list(data.keys())}")
    elif isinstance(data, list):
        interps = data
    else:
        raise RuntimeError(f"Unexpected Gemini response type: {type(data)}")

    if not isinstance(interps, list):
        raise RuntimeError(f"interpretations must be a list, got {type(interps)}")

    logger.debug("Gemini returned %d interpretations (expected %d)", len(interps), expected_count)
    return interps
