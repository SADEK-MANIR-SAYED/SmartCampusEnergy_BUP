"""
Quick manual test of LLM integration.
Run with: python test_llm_quick.py
Requires GEMINI_API_KEY to be set.
"""
import os
import sys

from dotenv import load_dotenv
load_dotenv()

# Check for API key
if not os.environ.get("GEMINI_API_KEY"):
    print("ERROR: GEMINI_API_KEY environment variable is not set.")
    print("Set it with: $env:GEMINI_API_KEY = 'your_key_here'")
    sys.exit(1)

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from app.llm.interpreter import interpret_notes
from app.guardrails import validate_and_normalize

# Test case: SAMPLE-01
notes = [
    "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
    "The sports office moved next month's registration deadline.",
]

print("Testing LLM interpretation with 2 notes...")
print(f"Note 0: {notes[0]}")
print(f"Note 1: {notes[1]}")
print()

try:
    raw = interpret_notes(
        scenario_id="SAMPLE-01-TEST",
        operator_notes=notes,
        battery_capacity_kwh=220.0,
        battery_minimum_kwh=40.0,
    )
    print("Raw LLM output:")
    import json
    print(json.dumps(raw, indent=2))
    print()

    validated = validate_and_normalize(raw, 2, 220.0)
    print("Validated interpretations:")
    for v in validated:
        print(f"  Note {v.note_index}: {v.directive_type} | applies={v.applies}")
        if v.structured_adjustment:
            print(f"    adjustment: {v.structured_adjustment}")
        print(f"    explanation: {v.explanation}")

    print()
    print("Expected:")
    print("  Note 0: solar_reduction | hours=[12,13] | factor=0.25")
    print("  Note 1: no_op")

    # Check
    if validated[0].directive_type == "solar_reduction" and validated[1].directive_type == "no_op":
        print()
        print("[SUCCESS] LLM correctly interpreted both notes!")
    else:
        print()
        print("[PARTIAL] LLM interpretation differs from expected - check above output")

except Exception as e:
    print(f"[ERROR] {e}")
    import traceback
    traceback.print_exc()
