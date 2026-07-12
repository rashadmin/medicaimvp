"""
MedicAI MVP — Async Subagent Server (Debug Edition)
=====================================================
async_coordinator.py

Debug checkpoints are tagged with [CHECKPOINT N] so you can trace exactly
where execution stalls or fails.

Run:
    uvicorn rag_subagent.async_coordinator:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from langchain_core.messages import HumanMessage

load_dotenv(Path(__file__).parent.parent / ".env")

# ── import subagent graphs ────────────────────────────────────────────────────
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# [CHECKPOINT 0] — graph import
print("\n[CHECKPOINT 0] importing rag_graph...", flush=True)
try:
    from subagent.rag_searcher import graph as rag_graph
    from subagent.youtube_subagent import graph as youtube_graph
    from subagent.hospital_notifier import graph as hospital_graph
    print("[CHECKPOINT 0] ✅ rag_graph imported successfully", flush=True)
    print(f"[CHECKPOINT 0]    type(rag_graph) = {type(rag_graph)}", flush=True)
    print(f"[CHECKPOINT 0]    has ainvoke    = {hasattr(rag_graph, 'ainvoke')}", flush=True)
    print(f"[CHECKPOINT 0]    has invoke     = {hasattr(rag_graph, 'invoke')}", flush=True)
    print("[CHECKPOINT 0] ✅ youtube_graph imported successfully", flush=True)
    print(f"[CHECKPOINT 0]    type(youtube_graph) = {type(youtube_graph)}", flush=True)
    print(f"[CHECKPOINT 0]    has ainvoke    = {hasattr(youtube_graph, 'ainvoke')}", flush=True)
    print(f"[CHECKPOINT 0]    has invoke     = {hasattr(youtube_graph, 'invoke')}", flush=True)
except Exception as e:
    print(f"[CHECKPOINT 0] ❌ FAILED to import graph: {e}", flush=True)
    traceback.print_exc()
    raise

GRAPHS: dict[str, Any] = {
    "rag_searcher": rag_graph,
    "youtube_subagent":youtube_graph,
    "hospital_notifier": hospital_graph
}

# ── SQLite ────────────────────────────────────────────────────────────────────

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_db_lock = asyncio.Lock()   # prevents concurrent write contention


def _init_db() -> None:
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS threads (
            thread_id  TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            messages   TEXT NOT NULL DEFAULT '[]',
            values_    TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id       TEXT PRIMARY KEY,
            thread_id    TEXT NOT NULL REFERENCES threads(thread_id),
            assistant_id TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT NOT NULL,
            error        TEXT
        );
    """)
    _conn.commit()
    print("[db] ✅ SQLite tables initialised", flush=True)


def _get_thread(thread_id: str) -> dict[str, Any] | None:
    row = _conn.execute(
        "SELECT thread_id, created_at, messages, values_ FROM threads WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "thread_id":  row["thread_id"],
        "created_at": row["created_at"],
        "messages":   json.loads(row["messages"]),
        "values":     json.loads(row["values_"]),
    }


def _get_run(run_id: str) -> dict[str, Any] | None:
    row = _conn.execute(
        "SELECT run_id, thread_id, assistant_id, status, created_at, error "
        "FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return dict(row) if row else None


# ── Run executor ──────────────────────────────────────────────────────────────

async def _execute_run(
    run_id:       str,
    thread_id:    str,
    user_message: str,
    assistant_id: str,
) -> None:
    short_run = run_id[:8]
    t_start   = time.perf_counter()

    print(f"\n{'='*60}", flush=True)
    print(f"[CHECKPOINT 1] executor started", flush=True)
    print(f"[CHECKPOINT 1]   run_id       = {short_run}", flush=True)
    print(f"[CHECKPOINT 1]   thread_id    = {thread_id[:8]}", flush=True)
    print(f"[CHECKPOINT 1]   assistant_id = {assistant_id}", flush=True)
    print(f"[CHECKPOINT 1]   message      = {user_message[:300]}", flush=True)

    # ── mark running ──────────────────────────────────────────────────────
    print(f"[CHECKPOINT 2] marking run as 'running'...", flush=True)
    async with _db_lock:
        _conn.execute("UPDATE runs SET status='running' WHERE run_id=?", (run_id,))
        _conn.commit()
    print(f"[CHECKPOINT 2] ✅ status = running", flush=True)

    # ── route to graph ────────────────────────────────────────────────────
    print(f"[CHECKPOINT 3] looking up graph for assistant_id='{assistant_id}'...", flush=True)
    graph = GRAPHS.get(assistant_id)
    if graph is None:
        err = f"Unknown assistant_id: '{assistant_id}'. Available: {list(GRAPHS.keys())}"
        print(f"[CHECKPOINT 3] ❌ {err}", flush=True)
        async with _db_lock:
            _conn.execute(
                "UPDATE runs SET status='error', error=? WHERE run_id=?",
                (err, run_id),
            )
            _conn.commit()
        return
    print(f"[CHECKPOINT 3] ✅ graph found: {type(graph).__name__}", flush=True)

    # ── invoke ────────────────────────────────────────────────────────────
    try:
        config    = {"configurable": {"thread_id": thread_id}}
        input_msg = {"messages": [HumanMessage(user_message)]}

        print(f"[CHECKPOINT 4] building HumanMessage — content length = {len(user_message)}", flush=True)
        print(f"[CHECKPOINT 4] config = {config}", flush=True)

        # ── CHECKPOINT 5 is the most important: does ainvoke even start? ──
        print(f"[CHECKPOINT 5] calling graph.ainvoke()... (this is where hangs usually happen)", flush=True)
        t_invoke = time.perf_counter()

        result = await asyncio.wait_for(
            graph.ainvoke(input_msg, config=config),
            timeout=60.0,   # hard ceiling — raises TimeoutError if graph hangs
        )

        elapsed = time.perf_counter() - t_invoke
        print(f"[CHECKPOINT 6] ✅ graph.ainvoke() returned in {elapsed:.2f}s", flush=True)
        print(f"[CHECKPOINT 6]   result keys       = {list(result.keys()) if isinstance(result, dict) else type(result)}", flush=True)

        # ── extract last message ──────────────────────────────────────────
        messages = result.get("messages", [])
        print(f"[CHECKPOINT 7] messages in result = {len(messages)}", flush=True)
        for i, m in enumerate(messages):
            role    = getattr(m, "type", "?")
            content = m.content if hasattr(m, "content") else str(m)
            print(f"[CHECKPOINT 7]   [{i}] role={role} content[:120]={str(content)[:120]}", flush=True)

        last = messages[-1] if messages else None
        if last is None:
            print("[CHECKPOINT 7] ⚠️  no messages returned from graph!", flush=True)
            output = ""
        elif isinstance(last.content, str):
            output = last.content
        else:
            output = json.dumps(last.content)

        print(f"[CHECKPOINT 7]   output length = {len(output)}", flush=True)
        print(f"[CHECKPOINT 7]   output[:300]  = {output[:300]}", flush=True)

        # ── persist to DB ─────────────────────────────────────────────────
        print(f"[CHECKPOINT 8] persisting result to DB...", flush=True)
        # assistant_msg = {"role": "assistant", "content": output}
        def _serialize_msg(m) -> dict:
            return {
                "type":    getattr(m, "type", "ai"),
                "name":    getattr(m, "name", None),
                "content": m.content if hasattr(m, "content") else str(m),
            }
        
        serialized = [_serialize_msg(m) for m in messages] 
        async with _db_lock:
            # row  = _conn.execute(
            #     "SELECT messages FROM threads WHERE thread_id=?", (thread_id,)
            # ).fetchone()
            # msgs = json.loads(row[0]) if row else []
            # msgs.append(assistant_msg)
            # _conn.execute(
            #     "UPDATE threads SET messages=?, values_=? WHERE thread_id=?",
            #     (json.dumps(msgs), json.dumps({"messages": msgs}), thread_id),
            # )
            # _conn.execute("UPDATE runs SET status='success' WHERE run_id=?", (run_id,))
            # _conn.commit()
            _conn.execute(
            "UPDATE threads SET messages=?, values_=? WHERE thread_id=?",
            (json.dumps(serialized), json.dumps({"messages": serialized}), thread_id),
            )
            _conn.execute("UPDATE runs SET status='success' WHERE run_id=?", (run_id,))
            _conn.commit()

        total = time.perf_counter() - t_start
        print(f"[CHECKPOINT 8] ✅ persisted — run completed in {total:.2f}s total", flush=True)
        print(f"{'='*60}\n", flush=True)

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - t_invoke
        msg = f"graph.ainvoke() timed out after {elapsed:.1f}s — graph is hanging"
        print(f"[CHECKPOINT 5/6] ❌ TIMEOUT — {msg}", flush=True)
        print(f"[CHECKPOINT 5/6]   Possible causes:", flush=True)
        print(f"[CHECKPOINT 5/6]     • Sync blocking call inside the graph (use asyncio.to_thread)", flush=True)
        print(f"[CHECKPOINT 5/6]     • LLM API call hanging (no timeout set on the client)", flush=True)
        print(f"[CHECKPOINT 5/6]     • Vector DB connection hanging", flush=True)
        print(f"[CHECKPOINT 5/6]     • Embedding API unreachable", flush=True)
        async with _db_lock:
            _conn.execute(
                "UPDATE runs SET status='error', error=? WHERE run_id=?",
                (msg, run_id),
            )
            _conn.commit()

    except Exception as exc:
        elapsed = time.perf_counter() - t_start
        print(f"[CHECKPOINT ERROR] ❌ exception after {elapsed:.2f}s — {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        async with _db_lock:
            _conn.execute(
                "UPDATE runs SET status='error', error=? WHERE run_id=?",
                (f"{type(exc).__name__}: {exc}", run_id),
            )
            _conn.commit()
        print(f"[CHECKPOINT ERROR] ✅ error saved to DB", flush=True)


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    _init_db()
    print(f"[server] Loaded graphs: {list(GRAPHS.keys())}", flush=True)

    # [CHECKPOINT 0b] — smoke-test the graph synchronously at startup
    print("[CHECKPOINT 0b] verifying graph attributes at startup...", flush=True)
    for name, g in GRAPHS.items():
        print(f"[CHECKPOINT 0b]   {name}: type={type(g).__name__} ainvoke={hasattr(g, 'ainvoke')}", flush=True)

    yield


app = FastAPI(title="MedicAI Subagent Server (debug)", lifespan=_lifespan)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/ok")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/graphs")
async def list_graphs() -> dict:
    return {"graphs": list(GRAPHS.keys())}


@app.post("/threads")
async def create_thread() -> dict[str, Any]:
    thread_id = str(uuid.uuid4())
    now       = datetime.now(UTC).isoformat()
    async with _db_lock:
        _conn.execute(
            "INSERT INTO threads (thread_id, created_at) VALUES (?, ?)",
            (thread_id, now),
        )
        _conn.commit()
    print(f"[server] created thread {thread_id}", flush=True)
    return {"thread_id": thread_id, "created_at": now, "messages": [], "values": {}}


@app.post("/threads/{thread_id}/runs")
async def create_run(thread_id: str, request: Request) -> dict[str, Any]:
    thread = _get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    body               = await request.json()
    multitask_strategy = body.get("multitask_strategy")
    assistant_id       = body.get("assistant_id") or "rag_searcher"

    print(f"\n[CHECKPOINT A] create_run called", flush=True)
    print(f"[CHECKPOINT A]   thread_id    = {thread_id[:8]}", flush=True)
    print(f"[CHECKPOINT A]   assistant_id = {assistant_id}", flush=True)
    print(f"[CHECKPOINT A]   multitask    = {multitask_strategy}", flush=True)

    if multitask_strategy == "interrupt":
        async with _db_lock:
            _conn.execute(
                "UPDATE runs SET status='cancelled' "
                "WHERE thread_id=? AND status='running'",
                (thread_id,),
            )
            _conn.execute(
                "UPDATE threads SET values_='{}' WHERE thread_id=?",
                (thread_id,),
            )
            _conn.commit()
        print(f"[CHECKPOINT A]   interrupted existing runs on thread {thread_id[:8]}", flush=True)

    messages     = (body.get("input") or {}).get("messages") or []
    user_message = next(
        (m["content"] for m in messages if m.get("role") == "user"), ""
    )

    print(f"[CHECKPOINT B] extracted user_message length = {len(user_message)}", flush=True)
    print(f"[CHECKPOINT B]   user_message[:200] = {user_message[:200]}", flush=True)

    if not user_message:
        print("[CHECKPOINT B] ⚠️  user_message is EMPTY — graph will receive an empty string!", flush=True)

    if user_message:
        async with _db_lock:
            existing = json.loads(
                _conn.execute(
                    "SELECT messages FROM threads WHERE thread_id=?", (thread_id,)
                ).fetchone()[0]
            )
            existing.append({"role": "user", "content": user_message})
            _conn.execute(
                "UPDATE threads SET messages=? WHERE thread_id=?",
                (json.dumps(existing), thread_id),
            )
            _conn.commit()
        print(f"[CHECKPOINT B] ✅ message appended to thread history", flush=True)

    run_id = str(uuid.uuid4())
    now    = datetime.now(UTC).isoformat()
    async with _db_lock:
        _conn.execute(
            "INSERT INTO runs (run_id, thread_id, assistant_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (run_id, thread_id, assistant_id, now),
        )
        _conn.commit()

    print(f"[CHECKPOINT C] run record created: run_id={run_id[:8]} status=pending", flush=True)
    print(f"[CHECKPOINT C] scheduling background task...", flush=True)

    task = asyncio.create_task(
        _execute_run(run_id, thread_id, user_message, assistant_id),
        name=f"run-{run_id[:8]}",
    )

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            print(f"[CHECKPOINT C] ⚠️  task {t.get_name()} was cancelled", flush=True)
        elif t.exception():
            print(f"[CHECKPOINT C] ❌ task {t.get_name()} raised an uncaught exception:", flush=True)
            traceback.print_exception(
                type(t.exception()), t.exception(), t.exception().__traceback__
            )
        else:
            print(f"[CHECKPOINT C] ✅ task {t.get_name()} completed cleanly", flush=True)

    task.add_done_callback(_on_done)

    print(f"[CHECKPOINT C] ✅ background task scheduled as '{task.get_name()}'", flush=True)

    return {
        "run_id":       run_id,
        "thread_id":    thread_id,
        "assistant_id": assistant_id,
        "status":       "pending",
        "created_at":   now,
        "error":        None,
    }


@app.get("/threads/{thread_id}/runs/{run_id}")
async def get_run(thread_id: str, run_id: str) -> dict[str, Any]:
    run = _get_run(run_id)
    if run is None or run["thread_id"] != thread_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str) -> dict[str, Any]:
    thread = _get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


@app.post("/threads/{thread_id}/runs/{run_id}/cancel")
async def cancel_run(thread_id: str, run_id: str) -> dict[str, Any]:
    run = _get_run(run_id)
    if run is None or run["thread_id"] != thread_id:
        raise HTTPException(status_code=404, detail="Run not found")
    async with _db_lock:
        _conn.execute(
            "UPDATE runs SET status='cancelled' WHERE run_id=?", (run_id,)
        )
        _conn.commit()
    print(f"[server] cancelled run {run_id[:8]}", flush=True)
    return {**run, "status": "cancelled"}
