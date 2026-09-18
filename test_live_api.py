"""Live end-to-end API test (requires server running on port 8000 and GEMINI_API_KEY)."""
import json
import sys
import os
import urllib.request
import urllib.error

from dotenv import load_dotenv
load_dotenv()

BASE_URL = f"http://localhost:{os.environ.get('PORT', '8000')}"

def get(path):
    url = BASE_URL + path
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read())

def post(path, data):
    url = BASE_URL + path
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

print("=== Live API Tests ===")
print()

# Test 1: Health
status, body = get("/health")
assert status == 200 and body == {"status": "ok"}, f"FAIL: {status} {body}"
print("[PASS] GET /health -> 200 {'status':'ok'}")

# Test 2: Malformed JSON
req = urllib.request.Request(
    BASE_URL + "/optimize-energy",
    data=b"not valid json",
    method="POST",
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req) as r:
        status = r.status
except urllib.error.HTTPError as e:
    status = e.code
assert status == 400, f"FAIL: Expected 400 got {status}"
print("[PASS] POST /optimize-energy (malformed JSON) -> 400")

# Test 3: Missing field
status, body = post("/optimize-energy", {"operator_notes": ["test"], "hours": [], "battery": {}})
assert status == 422, f"FAIL: Expected 422 got {status}"
print("[PASS] POST /optimize-energy (missing scenario_id) -> 422")

# Test 4: Full valid request (requires GEMINI_API_KEY)
if not os.environ.get("GEMINI_API_KEY"):
    print()
    print("NOTE: GEMINI_API_KEY not set - skipping full optimization test")
    print("      Set it and re-run to test LLM integration")
else:
    with open("data/sample_cases.json") as f:
        cases = json.load(f)["cases"]
    
    case = cases[0]  # SAMPLE-01
    inp = case["input"]
    status, body = post("/optimize-energy", inp)
    
    if status == 200:
        print(f"[PASS] POST /optimize-energy (SAMPLE-01) -> 200")
        print(f"       scenario_id: {body['scenario_id']}")
        print(f"       total_cost_bdt: {body['total_cost_bdt']}")
        print(f"       total_grid_kwh: {body['total_grid_kwh']}")
        print(f"       hourly_plan entries: {len(body['hourly_plan'])}")
        
        # Check directive interpretation
        interps = body["directive_interpretation"]
        print(f"       Interpretations: {[(i['note_index'], i['directive_type']) for i in interps]}")
        
        # Verify expected
        exp = case["expected_output"]
        cost_diff = abs(body["total_cost_bdt"] - exp["total_cost_bdt"])
        if cost_diff <= 1.0:
            print(f"       [PASS] Cost matches reference (diff={cost_diff:.2f} BDT)")
        else:
            print(f"       [WARN] Cost diff={cost_diff:.2f} BDT (ref={exp['total_cost_bdt']})")
    else:
        print(f"[FAIL] POST /optimize-energy (SAMPLE-01) -> {status}")
        print(f"       {body}")

print()
print("=== Live tests complete ===")
