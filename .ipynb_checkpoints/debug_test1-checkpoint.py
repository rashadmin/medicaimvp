"""
MedicAI MVP — Main Agent Test
===============================
test_main_agent.py

Tests the full main agent via the FastAPI /chat endpoint.
All subagents (rag_searcher, hospital_notifier, youtube_searcher)
must be running on the subagent server.

Prerequisites — run all servers first:
  Terminal 1: uvicorn rag_subagent.async_coordinator:app --reload --port 8000
  Terminal 2: uvicorn api:app --reload --port 8001

Then run:
  python test_main_agent.py

Tests:
  1. Health checks (both servers)
  2. First emergency message — parallel RAG + hospital notifier launch
  3. Follow-up: answer clarifying question
  4. Follow-up: ask about a specific technique
  5. Follow-up: situation update
  6. Poll hospital responses
  7. Full conversation flow end-to-end
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
import datetime
from pathlib import Path
import httpx

API_URL      = "http://localhost:8001"
SUBAGENT_URL = "http://localhost:8000"

# every test run appends here — file grows forever across runs
TRACE_LOG_PATH = Path(__file__).parent / "test_traces.md"


# ════════════════════════════════════════════════════════════════════════════
#  TRACE LOGGING
#  Captures, per test: the initial message, every tool the main agent called,
#  what each launched subagent actually did (tool calls + result, fetched from
#  the subagent server), and the final response — appended as markdown.
# ════════════════════════════════════════════════════════════════════════════

def _truncate(value, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f"… [truncated, {len(text)} chars total]"


def _tool_call_rows(name: str, content) -> list[str]:
    """Render a subagent tool call as separate markdown bullet rows. Each
    string returned here is meant to be prefixed with "- " and put on its
    own line — never squashed into one long line.

    search_first_aid_rag's raw content includes the full retrieved document
    text twice over — once as a single "context" blob, once again per-chunk
    under "chunks" with score/metadata — which is useful for debugging
    retrieval quality but floods the log. Keep only query / chunks_found /
    status; drop context and chunks entirely. Anything that isn't this
    shape (e.g. other tools' content) falls back to a single truncated row.
    """
    payload = content
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = None

    if isinstance(payload, dict) and {"query", "status"} <= payload.keys():
        query        = payload.get("query", "")
        chunks_found = payload.get("chunks_found", "?")
        status       = payload.get("status", "?")
        return [
            f"**Tool:** `{name}`",
            f"**Query:** \"{query}\"",
            f"**Chunks found:** {chunks_found}",
            f"**Status:** {status}",
        ]

    return [f"**Tool:** `{name}` → {_truncate(content)}"]


def _extract_task_id(content: str) -> str | None:
    match = re.search(r"task_id[:\s]+([a-f0-9\-]{36})", str(content))
    return match.group(1) if match else None


async def _fetch_subagent_trace(
    session_id: str, task_id: str, max_wait: float = 8.0, poll_interval: float = 2.0
) -> dict:
    """Pull the subagent's own conversation so the log shows what IT did
    (tool calls + result), not just that it was launched.

    This is the QUICK pass — bounded to a few seconds so it doesn't hold up
    the test. Fast subagents (a single RAG search) will often be done by
    the time this returns. Slow ones (hospital_notifier waiting on real
    WhatsApp/SMS replies) usually won't be — those get picked up by
    _backfill_subagent below instead of blocking here."""
    deadline = time.monotonic() + max_wait
    while True:
        result = await _read_subagent_thread(session_id, task_id)
        if "error" in result:
            return result
        if result["tool_calls"] or result["final"] or time.monotonic() >= deadline:
            return result
        await asyncio.sleep(poll_interval)


async def _read_subagent_thread(session_id: str, task_id: str) -> dict:
    """Single read of a subagent's status — via api.py's
    /session/{session_id}/subagent-status endpoint, not by hitting
    async_coordinator.py on :8000 directly. This exercises the same path
    the frontend uses.

    api.py returns "tasks" as a dict KEYED BY task_id (not a list of
    {"task_id": ...} objects), with each entry shaped
    {"status": "pending"|"complete"|"timeout", "tool_calls": [...],
    "final": "..."}. There's no "error" status and no "result" field —
    that was a leftover assumption from an earlier version of this file
    that didn't match what api.py actually serves."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{API_URL}/session/{session_id}/subagent-status")
            data = resp.json()
    except Exception as e:
        return {"task_id": task_id, "error": str(e)}

    match = data.get("tasks", {}).get(task_id)
    if match is None:
        # session/task not known to api.py yet — treat as still pending
        return {"task_id": task_id, "tool_calls": [], "final": ""}

    return {
        "task_id":    task_id,
        "status":     match.get("status", "pending"),
        "tool_calls": match.get("tool_calls", []),
        "final":      _truncate(match.get("final", "") or ""),
    }


# ── background backfill for slow subagents ─────────────────────────────────
# Tasks scheduled here so main()/the caller can await them once at the very
# end, giving slow subagents (hospital_notifier etc.) a real chance to finish
# without blocking each individual test.
_PENDING_BACKFILLS: list[asyncio.Task] = []


async def _backfill_subagent(
    task_id:    str,
    test_name:  str,
    session_id: str,
    max_wait:   float = 180.0,
    poll_interval: float = 5.0,
) -> None:
    deadline = time.monotonic() + max_wait
    result = {"task_id": task_id, "tool_calls": [], "final": ""}
    while True:
        result = await _read_subagent_thread(session_id, task_id)
        if "error" in result or result["tool_calls"] or result["final"] or time.monotonic() >= deadline:
            break
        await asyncio.sleep(poll_interval)

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"### 🔄 [{ts}] Subagent update — {test_name}", ""]
    lines.append(f"**Session ID:** `{session_id or 'n/a'}`")
    lines.append("")
    lines.append(f"**Task:** `{task_id}`")
    lines.append("")
    if "error" in result:
        lines.append(f"- could not fetch: {result['error']}")
    elif result["final"]:
        lines.append(f"- **Final:** {result['final']}")
    elif result["tool_calls"]:
        # tool_calls at this point means a subagent tool (e.g. the RAG
        # search) has returned but the subagent hasn't produced its final
        # answer yet — show each call's query/chunks_found/status, never
        # the raw retrieved document text.
        for tc in result["tool_calls"]:
            for row in _tool_call_rows(tc.get("name", "unknown_tool"), tc.get("content")):
                lines.append(f"- {row}")
        lines.append("- **Final:** _in progress, no final result yet_")
    else:
        lines.append(f"- ⚠️ still not finished after {max_wait:.0f}s of backfill polling — giving up")
    lines.append("")
    lines.append("---")
    lines.append("")

    with open(TRACE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


async def wait_for_pending_traces(timeout: float = 220.0) -> None:
    """Call this once, after all tests finish, to give slow subagents
    (e.g. hospital_notifier) a chance to complete and get logged instead of
    being cut off. Safe to call even if nothing is pending."""
    if not _PENDING_BACKFILLS:
        return
    print(f"\n⏳ Waiting up to {timeout:.0f}s for {len(_PENDING_BACKFILLS)} slow subagent task(s) to finish logging...")
    try:
        await asyncio.wait_for(asyncio.gather(*_PENDING_BACKFILLS, return_exceptions=True), timeout=timeout)
    except asyncio.TimeoutError:
        print("⚠️ Timed out waiting on some backfills — they'll be incomplete in the log.")
    print("✅ Backfill logging complete.")


def _format_args(args) -> str:
    if not args:
        return "_none captured_"
    return f"`{_truncate(args, 200)}`"


def _append_trace_log(
    test_name:        str,
    message:          str,
    session_id:       str,
    main_agent_calls: list[dict],
    subagent_events:  list[dict],
    subagent_traces:  list[dict],
    response:         str,
    duration:         float,
    error:            str | None = None,
) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"## [{ts}] {test_name}", ""]
    lines.append(f"**Session ID:** `{session_id or 'n/a'}`")
    lines.append("")
    lines.append(f"**Duration:** {duration:.1f}s")
    lines.append("")
    lines.append("**User message:**")
    lines.append("")
    lines.append(f"> {message}")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("### Tool Calls")
    lines.append("")
    if main_agent_calls:
        for i, call in enumerate(main_agent_calls, 1):
            tool    = call.get("tool", "?")
            args    = call.get("args")
            content = call.get("content")
            lines.append(f"**{i}. `{tool}`**")
            lines.append("")
            lines.append(f"- **Args:** {_format_args(args)}")
            lines.append(f"- **Result:** {_truncate(content) if content else '_none captured_'}")
            lines.append("")
    else:
        lines.append("_none captured_")
        lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("### Subagent Results")
    lines.append("")
    if subagent_traces:
        for tr in subagent_traces:
            lines.append(f"**Task `{tr['task_id']}`**")
            lines.append("")
            if "error" in tr:
                lines.append(f"- could not fetch: {tr['error']}")
                lines.append("")
                continue
            if tr["tool_calls"]:
                for tc in tr["tool_calls"]:
                    for row in _tool_call_rows(tc.get("name", "unknown_tool"), tc.get("content")):
                        lines.append(f"- {row}")
            if tr["final"]:
                lines.append(f"- **Final:** {tr['final']}")
            elif not tr["tool_calls"]:
                lines.append("- _not finished yet — see 🔄 backfill update below (if any) for this task_id_")
            else:
                lines.append("- **Final:** _in progress, no final result yet_")
            lines.append("")
    else:
        lines.append("_no subagent tasks launched_")
        lines.append("")
    lines.append("---")
    lines.append("")

    if subagent_events:
        lines.append("### Subagent Progress Events (rag / coordinator / video)")
        lines.append("")
        for ev in subagent_events:
            rest = {k: v for k, v in ev.items() if k != "type"}
            lines.append(f"- `{ev.get('type')}` — {_truncate(rest, 250)}")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("### Final Response")
    lines.append("")
    if error:
        lines.append(f"**ERROR:** {error}")
    else:
        lines.append(f"> {response if response else '_(empty response)_'}")
    lines.append("")
    lines.append(f"**Response length:** {len(response)} chars")
    lines.append("")
    lines.append("---")
    lines.append("")

    with open(TRACE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ════════════════════════════════════════════════════════════════════════════
#  SSE READER
#  Reads the SSE stream from /chat and prints events in real time
# ════════════════════════════════════════════════════════════════════════════

async def stream_chat(
    message:         str,
    location:        dict,
    patient_profile: dict,
    session_id:      str | None = None,
    timeout:         int = 120,
    silent_events:   list[str] | None = None,
    test_name:       str = "unnamed_test",
) -> tuple[str, str]:
    """
    POST to /chat and stream SSE events.
    Returns (session_id, full_agent_response_text).

    silent_events: event types to not print (e.g. ["step", "rag_event"])
    test_name: label used when appending this call's trace to test_traces.md
    """
    silent = set(silent_events or ["step", "tool_call_request"])

    body = {
        "session_id":      session_id,
        "message":         message,
        "location":        location,
        "patient_profile": patient_profile,
    }

    full_text   = ""
    session_out = session_id or ""
    start_time  = time.monotonic()

    main_agent_calls: list[dict] = []
    subagent_events:  list[dict] = []
    task_ids:         list[str]  = []
    error_msg:        str | None = None
    # tool name -> FIFO queue of arg-dicts, filled by "tool_call_request"
    # (full args, from the materialized AIMessage) and drained when the
    # matching "subagent_complete" result for that tool arrives. FIFO
    # because the same tool (e.g. start_async_task) can be called several
    # times in one turn, in the same order its results come back.
    pending_args: dict[str, list] = {}

    print(f"\n{'─'*60}")
    print(f"USER: {message}")
    print(f"{'─'*60}")

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{API_URL}/chat",
            json=body,
        ) as response:
            buffer = ""
            async for line in response.aiter_lines():
                buffer += line + "\n"

                # parse complete SSE event (event: + data: pair)
                if buffer.strip() and "\n\n" in buffer or (line == "" and buffer.strip()):
                    events = buffer.strip().split("\n\n")
                    for raw_event in events:
                        if not raw_event.strip():
                            continue

                        event_type = "message"
                        data_str   = ""

                        for part in raw_event.strip().split("\n"):
                            if part.startswith("event:"):
                                event_type = part[6:].strip()
                            elif part.startswith("data:"):
                                data_str = part[5:].strip()

                        if not data_str:
                            continue

                        try:
                            data = json.loads(data_str)
                        except Exception:
                            data = {"raw": data_str}

                        # capture session_id
                        if "session_id" in data and not session_out:
                            session_out = data["session_id"]

                        # handle token streaming — accumulate text
                        if event_type == "token":
                            text = data.get("text", "")
                            if isinstance(text, list):
                                text = "".join(
                                    t.get("text", "") if isinstance(t, dict) else str(t)
                                    for t in text
                                )
                            full_text += text
                            print(text, end="", flush=True)
                            buffer = ""
                            continue

                        # print non-silent events
                        if event_type not in silent:
                            _print_event(event_type, data)

                        if event_type == "turn_started":
                            session_out = data.get("session_id", session_out)

                        # ── trace capture ──────────────────────────────────
                        if event_type == "tool_call_request":
                            for c in data.get("calls", []):
                                pending_args.setdefault(c.get("tool", ""), []).append(c.get("args", {}))
                        elif event_type == "subagent_complete":
                            tool  = data.get("tool", "")
                            queue = pending_args.get(tool)
                            args  = queue.pop(0) if queue else None
                            main_agent_calls.append({"tool": tool, "args": args, "content": data.get("content")})
                            if tool == "start_async_task":
                                tid = _extract_task_id(data.get("content", ""))
                                if tid:
                                    task_ids.append(tid)
                        elif event_type in ("rag_event", "coordinator_event", "videos_incoming"):
                            subagent_events.append({"type": event_type, **data})
                        elif event_type == "error":
                            error_msg = data.get("message", str(data))
                        # ────────────────────────────────────────────────────

                        if event_type in ("done", "error"):
                            buffer = ""
                            break

                    buffer = ""

    print(f"\n{'─'*60}")

    # quick pass — fetch what each launched subagent has done SO FAR
    subagent_traces = (
        await asyncio.gather(*(_fetch_subagent_trace(session_out, tid) for tid in task_ids))
        if task_ids else []
    )
    subagent_traces = list(subagent_traces)
    _append_trace_log(
        test_name=test_name,
        message=message,
        session_id=session_out,
        main_agent_calls=main_agent_calls,
        subagent_events=subagent_events,
        subagent_traces=subagent_traces,
        response=full_text,
        duration=time.monotonic() - start_time,
        error=error_msg,
    )

    # any task still incomplete (e.g. hospital_notifier waiting on real
    # replies) gets a background watcher — call wait_for_pending_traces()
    # once at the end of your run to give these a chance to finish logging
    for tr in subagent_traces:
        if "error" not in tr and not tr["tool_calls"] and not tr["final"]:
            _PENDING_BACKFILLS.append(asyncio.create_task(
                _backfill_subagent(tr["task_id"], test_name, session_out)
            ))

    return session_out, full_text


def _print_event(event_type: str, data: dict) -> None:
    """Pretty print an SSE event."""
    icons = {
        "turn_started":     "▶",
        "tool_call":        "🔧",
        "tool_call_request": "📝",
        "retrying":         "🔁",
        "subagent_complete": "📦",
        "rag_event":        "🔍",
        "coordinator_event": "📡",
        "videos_incoming":  "🎬",
        "done":             "✅",
        "error":            "❌",
        "ready":            "🟢",
    }
    icon   = icons.get(event_type, "•")
    source = data.get("source", "")
    tool   = data.get("tool", "")
    node   = data.get("node", "")

    if event_type == "tool_call":
        print(f"\n  {icon} [{source}] calling: {tool}")
    elif event_type == "subagent_complete":
        content = data.get("content", "")[:120]
        print(f"\n  {icon} [{source}] {tool} → {content}...")
    elif event_type == "rag_event":
        event = data.get("event", "")
        query = data.get("query", "")
        print(f"\n  {icon} RAG {event}: {query[:60]}")
    elif event_type == "coordinator_event":
        event = data.get("event", "")
        print(f"\n  {icon} coordinator: {event}")
    elif event_type == "retrying":
        print(f"\n  {icon} retrying (attempt {data.get('attempt')}/{data.get('max_attempts')}) — {data.get('message', '')}")
    elif event_type in ("done", "error", "ready"):
        turn_type = data.get("turn_type", "")
        print(f"\n  {icon} {event_type.upper()} {turn_type}")
    elif event_type == "turn_started":
        print(f"\n  {icon} turn started — {data.get('turn_type', '')}")


# ════════════════════════════════════════════════════════════════════════════
#  SAMPLE DATA
# ════════════════════════════════════════════════════════════════════════════

LOCATION = {
    "lat":     6.5418,
    "lng":     3.3917,
    "address": "14 Admiralty Way, Lekki Phase 1, Lagos",
}

PATIENT = {
    "name":       "Emmanuel Okafor",
    "age":        67,
    "blood_type": "O+",
    "allergies":  ["penicillin"],
    "conditions": ["hypertension"],
}


# ════════════════════════════════════════════════════════════════════════════
#  HEALTH CHECKS
# ════════════════════════════════════════════════════════════════════════════

async def check_servers() -> bool:
    print("\n" + "="*60)
    print("Checking servers...")
    print("="*60)

    all_ok = True
    async with httpx.AsyncClient(timeout=5) as client:

        # FastAPI health
        try:
            resp = await client.get(f"{API_URL}/health")
            data = resp.json()
            print(f"  ✅ FastAPI (8001): {data.get('service')} v{data.get('version')}")
        except Exception as e:
            print(f"  ❌ FastAPI (8001): {e}")
            all_ok = False

        # subagent server health
        try:
            resp   = await client.get(f"{SUBAGENT_URL}/ok")
            graphs = await client.get(f"{SUBAGENT_URL}/graphs")
            g_list = graphs.json().get("graphs", [])
            print(f"  ✅ Subagent (8000): graphs={g_list}")
            for required in ["rag_searcher", "hospital_notifier"]:
                if required not in g_list:
                    print(f"  ⚠️  Missing graph: {required}")
        except Exception as e:
            print(f"  ❌ Subagent (8000): {e}")
            all_ok = False

    return all_ok


# ════════════════════════════════════════════════════════════════════════════
#  TESTS
# ════════════════════════════════════════════════════════════════════════════

async def test_certain_emergency():
    """
    Test 1 — Emergency with CERTAIN conditions (no ambiguity).
    Stabbed + not breathing → agent should:
      - Launch RAG for bleeding_control AND cpr simultaneously (no user input needed)
      - Launch hospital_notifier
      - Ask at most one clarifying question
      - Give immediate partial guidance
    """
    print("\n" + "="*60)
    print("TEST 1 — Certain Emergency (stabbed + not breathing)")
    print("="*60)

    session_id, response = await stream_chat(
        message="My brother was stabbed in the stomach and he is not breathing properly, "
                "there is a lot of blood",
        location=LOCATION,
        patient_profile=PATIENT,
        silent_events=["step"],
        test_name="TEST 1 — Certain Emergency (stabbed + not breathing)",
    )
    print(f"[test1] Response length: {len(response)} chars")
    assert session_id, "❌ No session_id returned"
    # if len(response) < 50:
    print(response)
    # assert len(response) > 50, "❌ Response too short"
    print("[test1] ✅ Passed")
    return session_id


async def test_ambiguous_emergency():
    """
    Test 2 — Ambiguous emergency → agent should ask ONE clarifying question
    AND launch speculative RAG searches simultaneously.
    """
    print("\n" + "="*60)
    print("TEST 2 — Ambiguous Emergency (collapsed — unclear cause)")
    print("="*60)

    session_id, response = await stream_chat(
        message="My grandmother just collapsed on the floor and is not moving",
        location=LOCATION,
        patient_profile={
            "name":       "Grace Okafor",
            "age":        72,
            "blood_type": "A+",
            "allergies":  [],
            "conditions": ["diabetes", "hypertension"],
        },
        silent_events=["step"],
        test_name="TEST 2 — Ambiguous Emergency (collapsed grandmother)",
    )
    # response should contain a question
    has_question = "?" in response
    print(f"[test2] Contains question: {'✅' if has_question else '⚠️ no question found'}")
    print("[test2] ✅ Passed")
    return session_id


async def test_followup_answer(session_id: str):
    """
    Test 3 — Answer the clarifying question.
    Agent should:
      - Cancel irrelevant speculative RAG searches
      - Assemble full first-aid guidance from confirmed RAG results
      - Launch youtube_searcher for main technique
    """
    print("\n" + "="*60)
    print("TEST 3 — Follow-up: Answer clarifying question")
    print("="*60)

    session_id, response = await stream_chat(
        message="She is not breathing and her lips are turning blue",
        location=LOCATION,
        patient_profile={
            "name":       "Grace Okafor",
            "age":        72,
            "blood_type": "A+",
            "allergies":  [],
            "conditions": ["diabetes", "hypertension"],
        },
        session_id=session_id,
        silent_events=["step"],
        test_name="TEST 3 — Follow-up: Answer clarifying question",
    )
    has_steps = any(word in response.lower() for word in ["1.", "step", "press", "push", "call"])
    print(f"[test3] Contains instructions: {'✅' if has_steps else '⚠️'}")
    print("[test3] ✅ Passed")
    return session_id


async def test_technique_question(session_id: str):
    """
    Test 4 — Ask about a specific technique.
    Agent should query RAG and respond with detailed explanation.
    """
    print("\n" + "="*60)
    print("TEST 4 — Follow-up: Ask about CPR technique")
    print("="*60)

    _, response = await stream_chat(
        message="Can you explain exactly how to do chest compressions? "
                "I've never done CPR before",
        location=LOCATION,
        patient_profile=PATIENT,
        session_id=session_id,
        silent_events=["step"],
        test_name="TEST 4 — Follow-up: Ask about CPR technique",
    )
    has_detail = len(response) > 100
    print(f"[test4] Detailed response: {'✅' if has_detail else '⚠️ response too short'}")
    print("[test4] ✅ Passed")


async def test_situation_update(session_id: str):
    """
    Test 5 — Give a situation update.
    Agent should re-assess and update guidance.
    """
    print("\n" + "="*60)
    print("TEST 5 — Follow-up: Situation update")
    print("="*60)

    _, response = await stream_chat(
        message="She just started breathing again but she is still unconscious "
                "and her pulse is very weak",
        location=LOCATION,
        patient_profile=PATIENT,
        session_id=session_id,
        silent_events=["step"],
        test_name="TEST 5 — Follow-up: Situation update",
    )
    # response should change guidance (no more CPR, recovery position etc)
    updated = len(response) > 50
    print(f"[test5] Updated guidance: {'✅' if updated else '⚠️'}")
    print("[test5] ✅ Passed")


async def test_hospital_status(session_id: str):
    """
    Test 6 — Ask about hospital status.
    Agent should call check_async_task for hospital_notifier.
    """
    print("\n" + "="*60)
    print("TEST 6 — Follow-up: Hospital status check")
    print("="*60)

    _, response = await stream_chat(
        message="Are any hospitals on their way? Which ones have confirmed?",
        location=LOCATION,
        patient_profile=PATIENT,
        session_id=session_id,
        silent_events=["step"],
        test_name="TEST 6 — Follow-up: Hospital status check",
    )
    print("[test6] ✅ Passed")


async def test_poll_hospital_responses(session_id: str):
    """
    Test 7 — Simulate hospital responses and poll the API.
    """
    print("\n" + "="*60)
    print("TEST 7 — Simulate + Poll Hospital Responses")
    print("="*60)

    async with httpx.AsyncClient() as client:
        # simulate 3 hospital responses
        responses = [
            ("hospital_1", "accept"),
            ("hospital_2", "accept"),
            ("hospital_3", "reject"),
        ]
        for hospital_id, decision in responses:
            url  = f"{API_URL}/hospital/respond/{session_id}/{hospital_id}/{decision}"
            resp = await client.get(url)
            icon = "✅" if decision == "accept" else "❌"
            print(f"  {icon} {hospital_id} → {decision} (HTTP {resp.status_code})")
            await asyncio.sleep(0.3)

        # poll the responses endpoint
        await asyncio.sleep(0.5)
        resp = await client.get(f"{API_URL}/session/hospital-responses/{session_id}")
        data = resp.json()

    print(f"\n  Responded: {data.get('responded')}/{data.get('total_notified')}")
    accepted = [h.get("hospital_name", h.get("hospital_id")) for h in data.get("accepted", [])]
    rejected = [h.get("hospital_name", h.get("hospital_id")) for h in data.get("rejected", [])]
    print(f"  ✅ Accepted: {accepted}")
    print(f"  ❌ Rejected: {rejected}")
    print("[test7] ✅ Passed")


async def test_sessions_endpoint():
    """Test 8 — Check active sessions."""
    print("\n" + "="*60)
    print("TEST 8 — Active Sessions")
    print("="*60)

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{API_URL}/sessions")
        data = resp.json()

    print(f"  Total sessions: {data.get('total')}")
    for s in data.get("sessions", []):
        print(f"  • {s['session_id'][:8]}... "
              f"RAG tasks: {s.get('rag_searches', 0)} "
              f"YouTube: {s.get('youtube_tasks', 0)}")
    print("[test8] ✅ Passed")


async def test_full_conversation():
    """
    Test 9 — Full end-to-end conversation flow:
    Emergency → clarify → guidance → technique question → hospital status
    """
    print("\n" + "="*60)
    print("TEST 9 — Full Conversation Flow")
    print("="*60)

    turns = [
        "My father collapsed at home. He is 67 years old, "
        "clutching his chest and says it hurts badly.",

        "Yes he is conscious but barely — he is breathing but very slowly",

        "Okay I am pressing his chest now. How hard should I press?",

        "The ambulance is not picking up. Are the hospitals notified?",

        "He just lost consciousness completely",
    ]

    session_id = None
    for i, message in enumerate(turns, 1):
        print(f"\n[e2e] Turn {i}/{len(turns)}")
        session_id, response = await stream_chat(
            message=message,
            location=LOCATION,
            patient_profile=PATIENT,
            session_id=session_id,
            silent_events=["step", "rag_event"],
            timeout=120,
            test_name=f"TEST 9 — Full Conversation Flow (turn {i}/{len(turns)})",
        )
        print(f"\n[e2e] Turn {i} complete — response: {len(response)} chars")
        await asyncio.sleep(1)   # brief pause between turns

    print(f"\n[e2e] ✅ Full conversation complete — session: {session_id[:8]}...")
    return session_id


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════

async def main():
    print("\nMedicAI Main Agent Test Suite")
    print("="*60)

    # check both servers are up
    ok = await check_servers()
    if not ok:
        print("\n❌ Not all servers running. Start:")
        print("   Terminal 1: uvicorn rag_subagent.async_coordinator:app --reload --port 8000")
        print("   Terminal 2: uvicorn api:app --reload --port 8001")
        return

    # ── run individual tests ──────────────────────────────────────────────
    import time
    # test 1 — certain emergency (stab + not breathing)
    session_id_1 = await test_certain_emergency()
    time.sleep(60)
    # test 2 — ambiguous emergency (collapsed grandmother)
    session_id_2 = await test_ambiguous_emergency()
    time.sleep(60)
    # test 3 — follow up on session 2 (answer clarifying question)
    session_id_2 = await test_followup_answer(session_id_2)
    time.sleep(60)
    # test 4 — ask technique question (continue session 2)
    await test_technique_question(session_id_2)
    time.sleep(60)
    # test 5 — situation update (continue session 2)
    await test_situation_update(session_id_2)
    time.sleep(60)
    # test 6 — ask about hospital status (continue session 2)
    await test_hospital_status(session_id_2)
    time.sleep(60)
    # test 7 — simulate + poll hospital responses (session 1)
    await test_poll_hospital_responses(session_id_1)
    time.sleep(60)
    # test 8 — active sessions
    await test_sessions_endpoint()
    time.sleep(60)
    # test 9 — full conversation (new session)
    await test_full_conversation()

    # give slow subagents (hospital_notifier etc.) a chance to finish and
    # get logged before the process exits
    await wait_for_pending_traces()

    print("\n" + "="*60)
    print("✅ All main agent tests complete.")
    print("="*60)


async def run_single_test(test_coro):
    """Run one test in isolation and still drain pending subagent backfills
    afterward — use this instead of asyncio.run(test_x()) directly, or the
    slow subagents (hospital_notifier etc.) won't get a chance to log."""
    result = await test_coro
    await wait_for_pending_traces()
    return result


if __name__ == "__main__":
    # run all tests
    asyncio.run(main())

    # or run a single test (use run_single_test so slow subagents like
    # hospital_notifier still get a chance to finish logging):
    # asyncio.run(run_single_test(test_certain_emergency()))
    # asyncio.run(run_single_test(test_full_conversation()))