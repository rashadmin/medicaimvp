"""
MedicAI MVP — YouTube Subagent Test
=====================================
test_youtube_subagent.py

Tests the YouTube searcher subagent independently via the async coordinator
server.

Run the subagent server first:
  uvicorn youtube_searcher.async_coordinator:app --reload --port 8001

Then run this test:
  python test_youtube_subagent.py

Tests:
  1. Health check
  2. Single video search
  3. Multiple parallel video searches
  4. Poll until complete
  5. Cancel a running task
  6. Update task (change query mid-run)
  7. Full flow — certain + speculative searches, then cancel wrong one
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


async def start_run(thread_id: str, query: str, search_id: str = "",
                     assistant_id: str = "youtube_subagent") -> str:
    task_payload = json.dumps({"query": query})
    body = {
        "assistant_id": assistant_id,
        "input": {
            "messages": [{"role": "user", "content": task_payload}]
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{BASE_URL}/threads/{thread_id}/runs", json=body)

        print(f"[start_run] status={resp.status_code}")
        print(f"[start_run] body={resp.text[:500]}")

        data   = resp.json()
        run_id = data["run_id"]
        label  = search_id or query
        print(f"[run] Started run_id: {run_id} for: {label}")
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


def _extract_videos(content: str) -> list[dict]:
    """Parse 'VIDEOS_READY: [...]' out of the agent's final text content."""
    marker = "VIDEOS_READY:"
    idx = content.find(marker)
    if idx == -1:
        return []
    raw = content[idx + len(marker):].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def print_result(result: dict, label: str = "") -> None:
    print(f"\n{'='*60}")
    print(f"RESULT {f'— {label}' if label else ''}")
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

            videos = _extract_videos(content)
            if videos:
                print(f"\n🎬 Parsed {len(videos)} video(s):")
                for v in videos:
                    print(f"  - {v.get('title', '?')} → {v.get('url', '?')}")

    print(f"{'='*60}\n")


# ════════════════════════════════════════════════════════════════════════════
#  TESTS
# ════════════════════════════════════════════════════════════════════════════

async def test_single_search():
    """Test 1 — Single video search for CPR."""
    print("\n" + "="*60)
    print("TEST 1 — Single YouTube Search (CPR)")
    print("="*60)

    thread_id = await create_thread()
    run_id    = await start_run(
        thread_id = thread_id,
        query     = "how to perform CPR on an adult",
        search_id = "cpr_video",
    )

    result = await poll_run(thread_id, run_id, timeout=30)
    print_result(result, "cpr_video")


async def test_parallel_searches():
    """Test 2 — Multiple video searches launched in parallel."""
    print("\n" + "="*60)
    print("TEST 2 — Parallel YouTube Searches")
    print("="*60)

    queries = [
        {"query": "how to control severe bleeding stab wound",   "search_id": "bleeding_video"},
        {"query": "recovery position unconscious not breathing", "search_id": "recovery_position_video"},
        {"query": "how to clear a blocked airway choking adult", "search_id": "airway_video"},
    ]

    tasks = []
    for q in queries:
        thread_id = await create_thread()
        run_id    = await start_run(thread_id=thread_id, **q)
        tasks.append((thread_id, run_id, q["search_id"]))

    print(f"\n[parallel] {len(tasks)} searches running simultaneously...")

    results = await asyncio.gather(*[
        poll_run(thread_id, run_id, timeout=30)
        for thread_id, run_id, _ in tasks
    ])

    for (thread_id, run_id, search_id), result in zip(tasks, results):
        print_result(result, search_id)


async def test_speculative_then_cancel():
    """Test 3 — Launch a video search then cancel it before completion."""
    print("\n" + "="*60)
    print("TEST 3 — Search + Cancel")
    print("="*60)

    thread_id = await create_thread()
    run_id    = await start_run(
        thread_id = thread_id,
        query     = "stroke FAST test demonstration video",
        search_id = "stroke_speculative_video",
    )

    await asyncio.sleep(0.5)
    await cancel_run(thread_id, run_id)

    async with httpx.AsyncClient() as client:
        resp   = await client.get(f"{BASE_URL}/threads/{thread_id}/runs/{run_id}")
        status = resp.json().get("status")
        print(f"[verify] Final status: {status} {'✅' if status == 'cancelled' else '❌'}")


async def test_update_task():
    """Test 4 — Update a running task with a more specific query."""
    print("\n" + "="*60)
    print("TEST 4 — Update Task (general → specific)")
    print("="*60)

    thread_id = await create_thread()

    run_id = await start_run(
        thread_id = thread_id,
        query     = "basic first aid overview video",
        search_id = "initial_search",
    )
    print(f"[update] Initial run started: {run_id[:8]}...")

    await asyncio.sleep(1)

    update_body = {
        "assistant_id":       "youtube_subagent",
        "multitask_strategy": "interrupt",
        "input": {
            "messages": [{
                "role":    "user",
                "content": json.dumps({"query": "CPR and AED use demonstration video"})
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
    print_result(result, "cpr_aed_specific_video")


async def test_full_emergency_flow():
    """Test 5 — Full flow: certain + speculative searches → clarify → assemble."""
    print("\n" + "="*60)
    print("TEST 5 — Full Video Search Flow")
    print("="*60)

    certain_queries = [
        ("how to control bleeding from a stab wound", "bleeding_video"),
        ("CPR steps for adult not breathing",          "cpr_video"),
    ]
    speculative_queries = [
        ("recovery position for unconscious patient", "unconscious_spec_video"),
        ("treating shock in a conscious patient",      "conscious_spec_video"),
    ]

    print("\n[flow] Step 1 — Launching certain searches in parallel...")
    certain_tasks = []
    for query, sid in certain_queries:
        thread_id = await create_thread()
        run_id    = await start_run(thread_id, query, sid)
        certain_tasks.append((thread_id, run_id, sid))

    print("\n[flow] Step 2 — Launching speculative searches in parallel...")
    spec_tasks = []
    for query, sid in speculative_queries:
        thread_id = await create_thread()
        run_id    = await start_run(thread_id, query, sid)
        spec_tasks.append((thread_id, run_id, sid))

    print("\n[flow] Step 3 — Simulating user answer: 'He is NOT conscious'")
    print("[flow]          → confirm 'unconscious_spec_video', cancel 'conscious_spec_video'")

    await asyncio.sleep(0.5)

    for thread_id, run_id, sid in spec_tasks:
        if sid == "conscious_spec_video":
            await cancel_run(thread_id, run_id)
            print(f"[flow] Cancelled: {sid}")

    print("\n[flow] Step 4 — Polling all remaining searches...")
    all_remaining = certain_tasks + [t for t in spec_tasks if t[2] != "conscious_spec_video"]

    results = await asyncio.gather(*[
        poll_run(thread_id, run_id, timeout=30)
        for thread_id, run_id, _ in all_remaining
    ])

    print(f"\n[flow] ✅ Got {len(results)} video search results")
    for (_, _, sid), result in zip(all_remaining, results):
        msgs     = result.get("values", {}).get("messages", result.get("messages", []))
        has_data = any(m.get("role") == "assistant" for m in msgs)
        print(f"  {sid}: {'✅ has content' if has_data else '❌ empty'}")


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════

async def main():
    print("\nMedicAI YouTube Subagent Test Suite")
    print("="*60)

    if not await health_check():
        print("\n❌ Server not reachable. Start it with:")
        print("   uvicorn youtube_subagent.async_coordinator:app --reload --port 8001")
        return

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
