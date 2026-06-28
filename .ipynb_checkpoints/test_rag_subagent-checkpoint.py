"""
MedicAI MVP — RAG Subagent Test
=================================
test_rag_subagent.py

Tests the RAG subagent independently via the async coordinator server.

Run the subagent server first:
  uvicorn rag_subagent.async_coordinator:app --reload --port 8000

Then run this test:
  python test_rag_subagent.py

Tests:
  1. Health check
  2. Single RAG search
  3. Multiple parallel RAG searches
  4. Poll until complete
  5. Cancel a running task
"""

from __future__ import annotations

import asyncio
import json
import time
import httpx

BASE_URL = "http://localhost:8000"


# ════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════════════════

async def health_check() -> bool:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{BASE_URL}/ok")
            ok   = resp.json().get("ok", False)
            print(f"[health] {'✅ Server is up' if ok else '❌ Server not ok'}")
            return ok
        except Exception as e:
            print(f"[health] ❌ Cannot reach server: {e}")
            return False


async def create_thread() -> str:
    async with httpx.AsyncClient() as client:
        resp      = await client.post(f"{BASE_URL}/threads", json={})
        thread_id = resp.json()["thread_id"]
        print(f"[thread] Created thread_id: {thread_id}")
        return thread_id


async def start_run(thread_id: str, query: str, tags: list[str], search_id: str,
                    speculative: bool = False, assistant_id: str = "rag_searcher") -> str:
    task_payload = json.dumps({
        "query":       query,
        "tags":        tags,
        "search_id":   search_id,
        "speculative": speculative,
    })
    body = {
        "assistant_id": assistant_id,
        "input": {
            "messages": [{"role": "user", "content": task_payload}]
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BASE_URL}/threads/{thread_id}/runs", json=body)

        # ✅ add this
        print(f"[start_run] status={resp.status_code}")
        print(f"[start_run] body={resp.text[:500]}")

        data   = resp.json()
        run_id = data["run_id"]
        print(f"[run] Started run_id: {run_id} for search_id: {search_id}")
        return run_id


async def poll_run(thread_id: str, run_id: str, timeout: int = 30) -> dict:
    """Poll until run is complete or timeout."""
    start = time.time()
    async with httpx.AsyncClient() as client:
        while time.time() - start < timeout:
            resp   = await client.get(f"{BASE_URL}/threads/{thread_id}/runs/{run_id}")
            status = resp.json().get("status")
            print(f"[poll] run_id={run_id[:8]}... status={status}")

            if status == "success":
                # get thread result
                thread_resp = await client.get(f"{BASE_URL}/threads/{thread_id}")
                return thread_resp.json()

            if status == "error":
                error = resp.json().get("error", "unknown error")
                print(f"[poll] ❌ Run failed: {error}")
                return {"error": error}

            if status == "cancelled":
                print(f"[poll] ⚠️  Run was cancelled")
                return {"cancelled": True}

            await asyncio.sleep(2)

    print(f"[poll] ⏰ Timeout after {timeout}s")
    return {"timeout": True}


async def cancel_run(thread_id: str, run_id: str) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/threads/{thread_id}/runs/{run_id}/cancel"
        )
        print(f"[cancel] run_id={run_id[:8]}... → {resp.json().get('status')}")


def print_result(result: dict, search_id: str = "") -> None:
    print(f"\n{'='*60}")
    print(f"RESULT {f'— {search_id}' if search_id else ''}")
    print(f"{'='*60}")

    if result.get("error"):
        print(f"❌ Error: {result['error']}")
        return

    if result.get("timeout"):
        print("⏰ Timed out")
        return

    if result.get("cancelled"):
        print("⚠️  Cancelled")
        return

    messages = result.get("values", {}).get("messages", [])
    if not messages:
        messages = result.get("messages", [])

    for msg in messages:
        role    = msg.get("role", "?")
        content = msg.get("content", "")
        if role == "assistant":
            print(f"\n[{role.upper()}]\n{content[:1000]}")
            if len(content) > 1000:
                print(f"... ({len(content)} chars total)")

    print(f"{'='*60}\n")


# ════════════════════════════════════════════════════════════════════════════
#  TESTS
# ════════════════════════════════════════════════════════════════════════════

async def test_single_search():
    """Test 1 — Single RAG search for CPR."""
    print("\n" + "="*60)
    print("TEST 1 — Single RAG Search (CPR)")
    print("="*60)

    thread_id = await create_thread()
    run_id    = await start_run(
        thread_id  = thread_id,
        query      = "CPR steps for adult cardiac arrest",
        tags       = ["cardiac_arrest", "cpr"],
        search_id  = "cpr_test",
        speculative = False,
    )

    result = await poll_run(thread_id, run_id, timeout=30)
    print_result(result, "cpr_test")


async def test_parallel_searches():
    """Test 2 — Multiple RAG searches launched in parallel (stab + not breathing)."""
    print("\n" + "="*60)
    print("TEST 2 — Parallel RAG Searches (stabbing + not breathing)")
    print("="*60)

    queries = [
        {
            "query":      "severe bleeding control stab wound",
            "tags":       ["bleeding", "trauma", "wound"],
            "search_id":  "bleeding_control",
            "speculative": False,
        },
        {
            "query":      "CPR resuscitation not breathing adult",
            "tags":       ["cpr", "resuscitation", "not_breathing"],
            "search_id":  "cpr_resuscitation",
            "speculative": False,
        },
        {
            "query":      "airway management unconscious trauma patient",
            "tags":       ["airway", "unconscious", "trauma"],
            "search_id":  "airway_management",
            "speculative": False,
        },
    ]

    # create one thread per search (each runs independently)
    tasks = []
    for q in queries:
        thread_id = await create_thread()
        run_id    = await start_run(thread_id=thread_id, **q)
        tasks.append((thread_id, run_id, q["search_id"]))

    print(f"\n[parallel] {len(tasks)} searches running simultaneously...")

    # poll all in parallel
    results = await asyncio.gather(*[
        poll_run(thread_id, run_id, timeout=30)
        for thread_id, run_id, _ in tasks
    ])

    for (thread_id, run_id, search_id), result in zip(tasks, results):
        print_result(result, search_id)


async def test_speculative_then_cancel():
    """Test 3 — Launch speculative search then cancel it."""
    print("\n" + "="*60)
    print("TEST 3 — Speculative Search + Cancel")
    print("="*60)

    # launch speculative search
    thread_id = await create_thread()
    run_id    = await start_run(
        thread_id   = thread_id,
        query       = "stroke symptoms treatment FAST response",
        tags        = ["stroke", "neurological"],
        search_id   = "stroke_speculative",
        speculative = True,
    )

    # immediately cancel (simulating user said "not a stroke")
    await asyncio.sleep(0.5)
    await cancel_run(thread_id, run_id)

    # verify it was cancelled
    async with httpx.AsyncClient() as client:
        resp   = await client.get(f"{BASE_URL}/threads/{thread_id}/runs/{run_id}")
        status = resp.json().get("status")
        print(f"[verify] Final status: {status} {'✅' if status == 'cancelled' else '❌'}")


async def test_update_task():
    """Test 4 — Update a running task with new instructions (phase 2 emergency)."""
    print("\n" + "="*60)
    print("TEST 4 — Update Task (warmup → emergency)")
    print("="*60)

    thread_id = await create_thread()

    # phase 1 — warmup search
    run_id = await start_run(
        thread_id  = thread_id,
        query      = "general first aid assessment unconscious patient",
        tags       = ["assessment", "unconscious"],
        search_id  = "initial_assessment",
        speculative = True,
    )
    print(f"[update] Initial run started: {run_id[:8]}...")

    await asyncio.sleep(1)

    # phase 2 — update with specific emergency (interrupt strategy)
    update_body = {
        "assistant_id":      "rag_searcher",
        "multitask_strategy": "interrupt",
        "input": {
            "messages": [{
                "role":    "user",
                "content": json.dumps({
                    "query":      "cardiac arrest CPR defibrillation",
                    "tags":       ["cardiac_arrest", "cpr", "aed"],
                    "search_id":  "cardiac_specific",
                    "speculative": False,
                })
            }]
        }
    }

    async with httpx.AsyncClient() as client:
        resp       = await client.post(
            f"{BASE_URL}/threads/{thread_id}/runs",
            json=update_body,
        )
        new_run_id = resp.json()["run_id"]
        print(f"[update] Updated run: {new_run_id[:8]}...")

    result = await poll_run(thread_id, new_run_id, timeout=30)
    print_result(result, "cardiac_specific")


async def test_full_emergency_flow():
    """Test 5 — Full flow: stabbed + not breathing → clarify → assemble."""
    print("\n" + "="*60)
    print("TEST 5 — Full Emergency Flow")
    print("="*60)

    # simulate what the supervisor would do:
    # 1. certain searches (parallel)
    # 2. speculative searches (parallel)
    # 3. user answers → cancel wrong speculative

    certain_queries = [
        ("severe bleeding knife stab wound control", ["bleeding", "trauma"], "bleeding"),
        ("CPR adult not breathing cardiac arrest",   ["cpr", "cardiac"],     "cpr"),
    ]
    speculative_queries = [
        ("unconscious patient trauma first aid",  ["unconscious"],  "unconscious_spec"),
        ("conscious stab victim shock treatment", ["shock", "stab"], "conscious_spec"),
    ]

    print("\n[flow] Step 1 — Launching certain searches in parallel...")
    certain_tasks = []
    for query, tags, sid in certain_queries:
        thread_id = await create_thread()
        run_id    = await start_run(thread_id, query, tags, sid, speculative=False)
        certain_tasks.append((thread_id, run_id, sid))

    print("\n[flow] Step 2 — Launching speculative searches in parallel...")
    spec_tasks = []
    for query, tags, sid in speculative_queries:
        thread_id = await create_thread()
        run_id    = await start_run(thread_id, query, tags, sid, speculative=True)
        spec_tasks.append((thread_id, run_id, sid))

    print("\n[flow] Step 3 — Simulating user answer: 'He is NOT conscious'")
    print("[flow]          → confirm 'unconscious_spec', cancel 'conscious_spec'")

    await asyncio.sleep(0.5)

    # cancel the wrong speculative
    for thread_id, run_id, sid in spec_tasks:
        if sid == "conscious_spec":
            await cancel_run(thread_id, run_id)
            print(f"[flow] Cancelled: {sid}")

    print("\n[flow] Step 4 — Polling all remaining searches...")
    all_remaining = certain_tasks + [t for t in spec_tasks if t[2] != "conscious_spec"]

    results = await asyncio.gather(*[
        poll_run(thread_id, run_id, timeout=30)
        for thread_id, run_id, _ in all_remaining
    ])

    print(f"\n[flow] ✅ Got {len(results)} RAG results")
    for (_, _, sid), result in zip(all_remaining, results):
        msgs     = result.get("values", {}).get("messages", result.get("messages", []))
        has_data = any(m.get("role") == "assistant" for m in msgs)
        print(f"  {sid}: {'✅ has content' if has_data else '❌ empty'}")


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════

async def main():
    print("\nMedicAI RAG Subagent Test Suite")
    print("="*60)

    # health check first
    if not await health_check():
        print("\n❌ Server not reachable. Start it with:")
        print("   uvicorn rag_subagent.async_coordinator:app --reload --port 8000")
        return

    # run tests
    await test_single_search()
    await test_parallel_searches()
    await test_speculative_then_cancel()
    await test_update_task()
    await test_full_emergency_flow()

    print("\n✅ All tests complete.")


if __name__ == "__main__":
    # run a specific test by commenting others out in main()
    # or run all:
    asyncio.run(main())
