"""Gemini LLM interpreter for operator notes using google-genai SDK."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from google import genai
from google.genai import types

from app import config
from app.llm.prompt import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_api_key() -> str:
    """Retrieve Gemini API key from app.config or environment."""
    return getattr(config, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")


def _get_model_name() -> str:
    """Retrieve Gemini model name from app.config or environment."""
    return (
        getattr(config, "GEMINI_MODEL", "")
        or os.environ.get("GEMINI_MODEL", "")
        or "gemini-2.0-flash"
    )


def _get_client() -> genai.Client:
    """Lazily initialize and return the google-genai Client."""
    global _client
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Please configure it in your environment or .env file."
        )

    if _client is None:
        try:
            _client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.error("Failed to initialize google-genai Client: %s", e)
            raise RuntimeError(f"Failed to initialize Gemini client: {e}") from e
    return _client


def interpret_notes(
    scenario_id: str,
    operator_notes: list[str],
    battery_capacity_kwh: float,
    battery_minimum_kwh: float,
) -> list[dict[str, Any]]:
    """
    Interpret operator notes into structured energy directives using Gemini.

    Parameters:
        scenario_id: Identifier for the scenario being scheduled.
        operator_notes: List of raw natural language notes from the operator.
        battery_capacity_kwh: Total battery storage capacity in kWh.
        battery_minimum_kwh: Baseline battery reserve minimum in kWh.

    Returns:
        Raw list of interpretation dictionaries (prior to guardrail normalization).
    """
    if not operator_notes:
        return []

    user_prompt = build_user_prompt(
        scenario_id=scenario_id,
        operator_notes=operator_notes,
        battery_capacity_kwh=battery_capacity_kwh,
        battery_minimum_kwh=battery_minimum_kwh,
    )

    logger.info(
        "Interpreting %d operator note(s) for scenario %s",
        len(operator_notes),
        scenario_id,
    )

    start_time = time.monotonic()
    raw_response_text = _call_gemini_api(user_prompt)
    elapsed = time.monotonic() - start_time

    logger.info(
        "Received Gemini response in %.2fs for scenario %s",
        elapsed,
        scenario_id,
    )

    return _parse_json_response(raw_response_text)


def _call_gemini_api(user_prompt: str, max_retries: int = 3) -> str:
    """
    Execute request to Gemini with fallback models and retry logic for transient errors.
    """
    client = _get_client()
    primary_model = _get_model_name()
    # List candidate models to try in order if transient failures occur
    candidate_models = [primary_model]
    for fallback in ["gemini-1.5-flash", "gemini-2.0-flash-lite"]:
        if fallback not in candidate_models:
            candidate_models.append(fallback)

    gen_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.1,
        response_mime_type="application/json",
    )

    last_error: Exception | None = None

    for model in candidate_models:
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=gen_config,
                )
                if not response.text:
                    raise RuntimeError(f"Gemini API ({model}) returned an empty text response")
                return response.text
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                # 404 means model unavailable; try next model immediately
                if "404" in err_str or "not_found" in err_str:
                    logger.warning("Model %s returned 404 NOT_FOUND. Trying fallback model...", model)
                    break

                if attempt < max_retries - 1:
                    backoff_seconds = 1.0 * (2 ** attempt)  # 1s, 2s, 4s exponential backoff
                    logger.warning(
                        "Gemini API model %s attempt %d/%d failed: %s. Retrying in %.1fs...",
                        model,
                        attempt + 1,
                        max_retries,
                        e,
                        backoff_seconds,
                    )
                    time.sleep(backoff_seconds)
                else:
                    logger.warning("All %d attempts failed for model %s: %s", max_retries, model, e)

    raise RuntimeError(
        f"Gemini API call failed for all candidate models: {last_error}"
    ) from last_error


def _parse_json_response(raw_text: str) -> list[dict[str, Any]]:
    """
    Parse the raw response text from Gemini into a list of dictionaries.
    Strips markdown code fences if present and supports both direct arrays
    and wrapped objects (e.g., {'interpretations': [...]}).
    """
    cleaned_text = raw_text.strip()

    # Strip markdown code fences if present
    if cleaned_text.startswith("```"):
        lines = cleaned_text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned_text = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned_text)
    except json.JSONDecodeError as exc:
        logger.error("Failed to decode JSON from Gemini: %s | Raw text: %.300s", exc, raw_text)
        raise RuntimeError(f"Gemini returned invalid JSON: {exc}") from exc

    # If the response is already a JSON array, return directly
    if isinstance(data, list):
        return data

    # If wrapped in a dictionary container, extract the list
    if isinstance(data, dict):
        for candidate_key in (
            "interpretations",
            "directive_interpretations",
            "directives",
            "results",
            "data",
        ):
            candidate_val = data.get(candidate_key)
            if isinstance(candidate_val, list):
                return candidate_val

        # If model returned a single directive object instead of an array
        if "note_index" in data or "directive_type" in data:
            return [data]

        raise RuntimeError(
            f"Gemini response object missing interpretations array. Top-level keys: {list(data.keys())}"
        )

    raise RuntimeError(
        f"Unexpected JSON response structure: expected list or dict, got {type(data).__name__}"
    )
