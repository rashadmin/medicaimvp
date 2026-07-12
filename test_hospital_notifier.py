"""
MedicAI MVP — Hospital Notifier Test
======================================
test_hospital_notifier.py

Tests the hospital notifier subagent independently.

Run the subagent server first:
  uvicorn rag_subagent.async_coordinator:app --reload --port 8000

Run FastAPI for webhook testing:
  uvicorn api:app --reload --port 8001

Then run this test:
  python test_hospital_notifier.py

Tests:
  1. Health check
  2. Single hospital notification (WhatsApp/SMS)
  3. Broadcast to all 3 hospitals simultaneously
  4. Simulate hospital Yes/No responses via webhooks
  5. Poll responses endpoint
  6. Full end-to-end flow
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
import httpx

SUBAGENT_URL = "http://localhost:8000"   # async coordinator server
API_URL      = "http://localhost:8001"   # FastAPI (for webhooks + response polling)


# ════════════════════════════════════════════════════════════════════════════
#  SHARED HELPERS  (same as RAG test)
# ════════════════════════════════════════════════════════════════════════════

async def health_check(url: str, name: str) -> bool:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{url}/ok")
            ok   = resp.json().get("ok", False)
            print(f"[health] {name}: {'✅ up' if ok else '❌ not ok'}")
            return ok
        except Exception as e:
            print(f"[health] {name}: ❌ {e}")
            return False


async def create_thread() -> str:
    async with httpx.AsyncClient() as client:
        resp      = await client.post(f"{SUBAGENT_URL}/threads", json={})
        thread_id = resp.json()["thread_id"]
        print(f"[thread] Created: {thread_id[:8]}...")
        return thread_id


async def start_notifier_run(
    thread_id:        str,
    session_id:       str,
    emergency_payload: dict,
    patient_profile:  dict,
    location:         dict,
    hospitals:        list | None = None,
) -> str:
    task_payload = json.dumps({
        "emergency_payload": emergency_payload,
        "patient_profile":   patient_profile,
        "location":          location,
        "session_id":        session_id,
        "hospitals":         hospitals or [],   # empty = use prototype list
    })
    body = {
        "assistant_id": "hospital_notifier",
        "input": {
            "messages": [{"role": "user", "content": task_payload}]
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SUBAGENT_URL}/threads/{thread_id}/runs",
            json=body,
        )
        print(f"[start] status={resp.status_code}")
        data   = resp.json()
        run_id = data.get("run_id", "")
        print(f"[start] run_id={run_id[:8]}... assistant=hospital_notifier")
        return run_id


async def poll_run(thread_id: str, run_id: str, timeout: int = 60) -> dict:
    start = time.time()
    async with httpx.AsyncClient() as client:
        while time.time() - start < timeout:
            resp   = await client.get(
                f"{SUBAGENT_URL}/threads/{thread_id}/runs/{run_id}"
            )
            status = resp.json().get("status")
            elapsed = int(time.time() - start)
            print(f"[poll] {elapsed}s — run_id={run_id[:8]}... status={status}")

            if status == "success":
                thread_resp = await client.get(f"{SUBAGENT_URL}/threads/{thread_id}")
                return thread_resp.json()
            if status == "error":
                error = resp.json().get("error", "unknown")
                print(f"[poll] ❌ Error: {error}")
                return {"error": error}
            if status == "cancelled":
                return {"cancelled": True}

            await asyncio.sleep(3)

    print(f"[poll] ⏰ Timeout after {timeout}s")
    return {"timeout": True}


def print_result(result: dict, label: str = "") -> None:
    print(f"\n{'='*60}")
    print(f"RESULT {f'— {label}' if label else ''}")
    print(f"{'='*60}")

    for key in ("error", "timeout", "cancelled"):
        if result.get(key):
            print(f"{'❌' if key=='error' else '⏰' if key=='timeout' else '⚠️'} {key.upper()}: {result[key]}")
            return

    messages = result.get("values", {}).get("messages", result.get("messages", []))
    for msg in messages:
        role    = msg.get("role", "?")
        content = msg.get("content", "")
        if role == "assistant":
            print(f"\n[NOTIFIER OUTPUT]\n{content[:1500]}")
    print(f"{'='*60}\n")


# ════════════════════════════════════════════════════════════════════════════
#  SAMPLE DATA
# ════════════════════════════════════════════════════════════════════════════

SAMPLE_EMERGENCY = {
    "emergency_type":   "cardiac_arrest",
    "severity":         "critical",
    "description":      "67-year-old male collapsed, clutching chest, not breathing properly.",
    "symptoms":         ["chest pain", "not breathing", "collapsed"],
    "unconscious":      False,
    "bleeding":         False,
    "breathing_issues": True,
    "special_notes":    None,
}

SAMPLE_PATIENT = {
    "name":       "Emmanuel Okafor",
    "age":        67,
    "blood_type": "O+",
    "allergies":  ["penicillin"],
    "conditions": ["hypertension"],
}

SAMPLE_LOCATION = {
    "lat":     6.5418,
    "lng":     3.3917,
    "address": "14 Admiralty Way, Lekki Phase 1, Lagos",
}


# ════════════════════════════════════════════════════════════════════════════
#  TESTS
# ════════════════════════════════════════════════════════════════════════════

async def test_graphs_available():
    """Test 0 — Confirm hospital_notifier graph is registered."""
    print("\n" + "="*60)
    print("TEST 0 — Confirm graphs available")
    print("="*60)

    async with httpx.AsyncClient() as client:
        resp   = await client.get(f"{SUBAGENT_URL}/graphs")
        graphs = resp.json().get("graphs", [])
        print(f"[graphs] Available: {graphs}")

        if "hospital_notifier" not in graphs:
            print("❌ hospital_notifier not registered in GRAPHS dict")
            print("   Add it to async_coordinator.py GRAPHS dict")
        else:
            print("✅ hospital_notifier is registered")

        return "hospital_notifier" in graphs


async def test_generate_report_only():
    """Test 1 — Just test report generation (no WhatsApp)."""
    print("\n" + "="*60)
    print("TEST 1 — Alert Report Generation")
    print("="*60)

    # test the report generation tool directly without sending messages
    # by passing empty hospitals list and checking notifier output
    session_id = str(uuid.uuid4())
    thread_id  = await create_thread()

    # override with a custom task that only generates report
    task_payload = json.dumps({
        "emergency_payload": SAMPLE_EMERGENCY,
        "patient_profile":   SAMPLE_PATIENT,
        "location":          SAMPLE_LOCATION,
        "session_id":        session_id,
        "hospitals":         [],       # empty = use prototype list
        "dry_run":           True,     # hint to notifier (logs only, no real send)
    })

    body = {
        "assistant_id": "hospital_notifier",
        "input": {
            "messages": [{"role": "user", "content": task_payload}]
        }
    }

    async with httpx.AsyncClient() as client:
        resp   = await client.post(
            f"{SUBAGENT_URL}/threads/{thread_id}/runs", json=body
        )
        run_id = resp.json().get("run_id", "")

    result = await poll_run(thread_id, run_id, timeout=60)
    print_result(result, "report_generation")
    return result


async def test_broadcast_to_hospitals():
    """Test 2 — Full broadcast to all 3 prototype hospitals."""
    print("\n" + "="*60)
    print("TEST 2 — Broadcast to 3 Hospitals")
    print("="*60)
    print("⚠️  This will send REAL WhatsApp/SMS if Twilio is configured.")
    print("   Set TWILIO_ACCOUNT_SID=test in .env to skip actual sending.\n")

    session_id = str(uuid.uuid4())
    print(f"[test] session_id: {session_id}")
    print(f"[test] Response webhook base: {API_URL}/hospital/respond/{session_id}/")

    thread_id = await create_thread()
    run_id    = await start_notifier_run(
        thread_id=thread_id,
        session_id=session_id,
        emergency_payload=SAMPLE_EMERGENCY,
        patient_profile=SAMPLE_PATIENT,
        location=SAMPLE_LOCATION,
        hospitals=[],   # use prototype list
    )

    result = await poll_run(thread_id, run_id, timeout=90)
    print_result(result, "broadcast")
    return session_id, result


async def test_simulate_hospital_responses(session_id: str):
    """
    Test 3 — Simulate hospitals tapping Yes/No links.
    Calls the webhook endpoints directly (no real phone needed).
    """
    print("\n" + "="*60)
    print("TEST 3 — Simulate Hospital Yes/No Responses")
    print("="*60)

    responses = [
        ("hospital_1", "accept"),   # Gbagada → YES
        ("hospital_2", "reject"),   # R-Jolad → NO
        ("hospital_3", "accept"),   # Ladi-Lak → YES
    ]

    async with httpx.AsyncClient() as client:
        for hospital_id, decision in responses:
            url  = f"{API_URL}/hospital/respond/{session_id}/{hospital_id}/{decision}"
            resp = await client.get(url)
            icon = "✅" if decision == "accept" else "❌"
            print(f"[webhook] {icon} {hospital_id} → {decision} (HTTP {resp.status_code})")
            await asyncio.sleep(0.5)

    print("\n[simulate] All responses submitted")


async def test_poll_responses(session_id: str):
    """Test 4 — Poll the responses endpoint to see who said yes/no."""
    print("\n" + "="*60)
    print("TEST 4 — Poll Hospital Responses")
    print("="*60)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{API_URL}/session/hospital-responses/{session_id}"
        )
        data = resp.json()

    print(f"\n  Session:        {session_id[:8]}...")
    print(f"  Total notified: {data.get('total_notified', 0)}")
    print(f"  Responded:      {data.get('responded', 0)}")
    print(f"  All responded:  {data.get('all_responded', False)}")

    accepted = data.get("accepted", [])
    rejected = data.get("rejected", [])
    pending  = data.get("pending", [])

    if accepted:
        print(f"\n  ✅ Accepted ({len(accepted)}):")
        for h in accepted:
            print(f"     • {h.get('hospital_name', h.get('hospital_id'))} "
                  f"at {h.get('responded_at', '')[:19]}")

    if rejected:
        print(f"\n  ❌ Rejected ({len(rejected)}):")
        for h in rejected:
            print(f"     • {h.get('hospital_name', h.get('hospital_id'))}")

    if pending:
        print(f"\n  ⏳ Pending ({len(pending)}):")
        for h in pending:
            print(f"     • {h.get('hospital_name', h.get('hospital_id'))}")

    return data


async def test_custom_hospitals():
    """Test 5 — Pass custom hospital list instead of prototype."""
    print("\n" + "="*60)
    print("TEST 5 — Custom Hospital List")
    print("="*60)

    session_id = str(uuid.uuid4())
    custom_hospitals = [
        {
            "id":          "custom_h1",
            "name":        "Lagos Island General Hospital",
            "phone":       os.getenv("TEST_PHONE_1", "+2348099999991"),
            "distance_km": 3.2,
        },
        {
            "id":          "custom_h2",
            "name":        "First Cardiology Consultants",
            "phone":       os.getenv("TEST_PHONE_2", "+2348099999992"),
            "distance_km": 4.5,
        },
    ]

    thread_id = await create_thread()
    run_id    = await start_notifier_run(
        thread_id=thread_id,
        session_id=session_id,
        emergency_payload=SAMPLE_EMERGENCY,
        patient_profile=SAMPLE_PATIENT,
        location=SAMPLE_LOCATION,
        hospitals=custom_hospitals,
    )

    result = await poll_run(thread_id, run_id, timeout=90)
    print_result(result, "custom_hospitals")


async def test_full_e2e():
    """
    Test 6 — Full end-to-end:
      broadcast → simulate responses → poll results
    """
    print("\n" + "="*60)
    print("TEST 6 — Full End-to-End Flow")
    print("="*60)

    # step 1: broadcast
    print("\n[e2e] Step 1: Broadcasting to hospitals...")
    session_id, broadcast_result = await test_broadcast_to_hospitals()

    if broadcast_result.get("error") or broadcast_result.get("timeout"):
        print("[e2e] ❌ Broadcast failed — stopping")
        return

    print("\n[e2e] Step 2: Waiting 2s for messages to be 'delivered'...")
    await asyncio.sleep(2)

    # step 2: simulate hospital responses
    print("\n[e2e] Step 3: Simulating hospital responses...")
    await test_simulate_hospital_responses(session_id)

    # step 3: poll results
    print("\n[e2e] Step 4: Polling response status...")
    responses = await test_poll_responses(session_id)

    # summary
    print("\n[e2e] ══ SUMMARY ══")
    accepted = responses.get("accepted", [])
    if accepted:
        names = [h.get("hospital_name", "?") for h in accepted]
        print(f"  ✅ {len(accepted)} hospital(s) accepted: {', '.join(names)}")
        print(f"  → Recommend routing patient to: {names[0]}")
    else:
        print("  ⚠️  No hospitals accepted yet")


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════

import os

async def main():
    print("\nMedicAI Hospital Notifier Test Suite")
    print("="*60)

    # health checks
    subagent_ok = await health_check(SUBAGENT_URL, "Subagent server (8000)")
    api_ok      = await health_check(API_URL,      "FastAPI server (8001)")

    if not subagent_ok:
        print("\n❌ Subagent server not reachable. Start with:")
        print("   uvicorn rag_subagent.async_coordinator:app --reload --port 8000")
        return

    if not api_ok:
        print("\n⚠️  FastAPI not reachable — webhook tests will be skipped")
        print("   Start with: uvicorn api:app --reload --port 8001")

    # test 0 — confirm graph registered
    registered = await test_graphs_available()
    if not registered:
        return

    # test 1 — report generation
    await test_generate_report_only()

    # test 2 + 3 + 4 — broadcast + simulate responses + poll
    await test_full_e2e()

    # test 5 — custom hospitals
    await test_custom_hospitals()

    print("\n✅ Hospital notifier tests complete.")


if __name__ == "__main__":
    asyncio.run(main())
