"""
MedicAI MVP — FastAPI
======================
api.py

Single endpoint for all conversation turns.
The agent handles everything — triage, RAG, clarification, guidance.

POST /chat
  Body: { session_id, message, location, patient_profile }
  First message: emergency text
  Subsequent: answers to questions or follow-ups
  Response: SSE stream

GET  /session/videos/{session_id}   — poll for YouTube results
GET  /sessions                      — debug: list all sessions
GET  /health
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from typing import AsyncGenerator

import httpx
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from supervisor import agent, make_config, build_input, checkpointer

logger = logging.getLogger("medicai.api")

app = FastAPI(title="MedicAI MVP", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# in-memory session store
sessions: dict[str, dict] = {}

# ── subagent polling config (PULL model) ──────────────────────────────────
# The rag_searcher / hospital_notifier / youtube_subagent tasks run on a
# separate server (async_coordinator.py) as fire-and-forget background
# tasks. There is no push channel from that server back into this one, so
# progress/results are recovered by polling its /threads/{task_id} endpoint.
SUBAGENT_URL       = os.getenv("SUBAGENT_URL", "http://localhost:8000")
SUBAGENT_POLL_SECS = float(os.getenv("SUBAGENT_POLL_SECS", "1.5"))
SUBAGENT_MAX_WAIT  = float(os.getenv("SUBAGENT_MAX_WAIT", "180"))

# ── model call retry config ────────────────────────────────────────────────
# Retries only cover the SUPERVISOR's own model calls (agent.astream()), and
# only while no "token" text has reached the client yet for this turn — once
# any text has been streamed, retrying from scratch would duplicate/garble
# the response, so a failure past that point is surfaced as a terminal error
# instead. Subagents are a separate process with their own retry surface
# (currently: silently retried by _watch_subagent_task's poll loop, which
# treats any read failure as "not ready yet" and tries again next tick).
MAX_MODEL_RETRIES     = int(os.getenv("MAX_MODEL_RETRIES", "2"))
MODEL_RETRY_BASE_DELAY = float(os.getenv("MODEL_RETRY_BASE_DELAY", "1.5"))


def _classify_error(exc: Exception) -> tuple[bool, str]:
    """Decide whether an exception from agent.astream() is worth retrying,
    and produce a message that's safe to send to the client.

    Provider exceptions (esp. Gemini 429s) come back as a giant raw JSON
    blob — quota IDs, internal doc links, retry-delay hints. That must never
    reach the end user directly (it did, previously: a 429 payload ended up
    inside the assistant's visible response). Log the full exception
    server-side; only ever send the classified, generic message."""
    text = str(exc)
    logger.warning("agent.astream error: %s", text[:2000])

    if "RESOURCE_EXHAUSTED" in text or "429" in text or "Too Many Requests" in text:
        return True, "The AI service is temporarily rate-limited."
    if (
        isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout))
        or "Network is unreachable" in text
        or "Cannot connect to host" in text
    ):
        return True, "Lost connection to the AI service."
    if "503" in text or "UNAVAILABLE" in text or "overloaded" in text.lower():
        return True, "The AI service is temporarily overloaded."
    return False, "Something went wrong while processing your message."



# ════════════════════════════════════════════════════════════════════════════
#  REQUEST MODELS
# ════════════════════════════════════════════════════════════════════════════

class Location(BaseModel):
    lat:     float = Field(..., example=6.5418)
    lng:     float = Field(..., example=3.3917)
    address: str   = Field(..., example="Lekki Phase 1, Lagos")


class PatientProfile(BaseModel):
    name:       str | None = None
    age:        int | None = None
    blood_type: str | None = None
    allergies:  list[str]  = Field(default_factory=list)
    conditions: list[str]  = Field(default_factory=list)


class ChatRequest(BaseModel):
    session_id:      str | None   = None          # None = new session
    message:         str
    location:        Location
    patient_profile: PatientProfile = Field(default_factory=PatientProfile)


# ════════════════════════════════════════════════════════════════════════════
#  SSE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _sse(event: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data)
    return f"event: {event}\ndata: {payload}\n\n"


def _safe_dict(data: any) -> dict:
    """Convert Overwrite or any non-dict to plain dict safely."""
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    try:
        return dict(data)
    except Exception:
        return {}


def _classify_chunk(chunk: dict, session_id: str) -> tuple[str, dict] | None:
    chunk_type = chunk.get("type")
    ns         = chunk.get("ns", ())
    raw_data   = chunk.get("data", {})

    is_subagent = any(str(s).startswith("tools:") for s in ns)
    ns_str      = " ".join(str(s) for s in ns)
    source      = (
        "rag_searcher"  if "rag_searcher"  in ns_str else
        "youtube"       if "youtube"       in ns_str else
        "supervisor"
    )

    # "messages"-mode chunks carry (message_chunk, metadata) as a TUPLE in
    # chunk["data"]. This must be handled before _safe_dict() ever sees it:
    # dict() on a bare 2-tuple doesn't raise, it silently reinterprets the
    # tuple as a single {key: value} pair (the message object becomes a
    # dict key), which destroys token.content. That was why every assistant
    # reply came through as an empty response — the "ai"/content branch
    # below was unreachable once `data` had already been run through
    # _safe_dict up top.
    if chunk_type == "messages":
        token   = raw_data[0] if isinstance(raw_data, tuple) else raw_data
        content = getattr(token, "content", None)
        t       = getattr(token, "type", "")

        # handle Gemini content list format
        if isinstance(content, list):
            content = "".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content
            )
        if t in ("ai", "AIMessageChunk") and content and not getattr(token, "tool_call_chunks", None):
            return "token", {"source": source, "text": content}

        if getattr(token, "tool_call_chunks", None):
            tc = token.tool_call_chunks[0]
            if tc.get("name"):
                return "tool_call", {"source": source, "tool": tc["name"]}
        return None

    data = _safe_dict(raw_data)

    if chunk_type == "custom":
        # rag search events — stream silently (don't show in chat)
        event = data.get("event", "")
        if event in ("rag_search_started", "rag_search_complete"):
            return "rag_event", {"source": source, **data}
        return "coordinator_event", {"source": source, **data}

    if chunk_type == "updates":
        for node_name, node_data in data.items():
            node_data = _safe_dict(node_data)
            if not node_data:
                continue

            # capture youtube task_ids
            for msg in node_data.get("messages", []):
                if (
                    hasattr(msg, "type") and msg.type == "tool"
                    and hasattr(msg, "name")
                ):
                    if msg.name == "start_async_task":
                        _handle_task_launched(msg.content, session_id)

            # capture tool call ARGUMENTS. These only exist fully-formed on
            # the AIMessage in "updates" mode (node_data here, before the
            # "tools" node executes) — the "messages"-mode token stream only
            # sees tool_call_chunks arriving as fragmented JSON deltas, so
            # args can't be reliably read from there without reassembling
            # partial JSON across chunks. This is the supervisor's own tool
            # calls only (not subagents' — those run in a separate process).
            if not is_subagent:
                for msg in node_data.get("messages", []):
                    if hasattr(msg, "type") and msg.type == "ai":
                        tool_calls = getattr(msg, "tool_calls", None) or []
                        if tool_calls:
                            return "tool_call_request", {
                                "source": source,
                                "calls": [
                                    {"tool": tc.get("name", ""), "args": tc.get("args", {})}
                                    for tc in tool_calls
                                ],
                            }

            if node_name == "tools" and not is_subagent:
                for msg in node_data.get("messages", []):
                    if hasattr(msg, "type") and msg.type == "tool":
                        tool_name = getattr(msg, "name", "")
                        # don't expose RAG internals in chat
                        if tool_name in ("search_first_aid_rag",):
                            return "rag_event", {
                                "source": "rag_searcher",
                                "tool":   tool_name,
                            }
                        return "subagent_complete", {
                            "source":  source,
                            "tool":    tool_name,
                            "content": str(msg.content)[:500],
                        }
            return "step", {"source": source, "node": node_name}
            
            
    return None


def _handle_task_launched(content: str, session_id: str) -> None:
    """Extract task_id from start_async_task result, store it, and kick off
    a background watcher so its progress/result is recoverable later —
    independent of whether this SSE turn is still open."""
    try:
        match   = re.search(r"task_id[:\s]+([a-f0-9\-]{36})", str(content))
        task_id = match.group(1) if match else None
        if task_id and session_id in sessions:
            sessions[session_id].setdefault("rag_task_ids", []).append(task_id)
            sessions[session_id].setdefault("subagent_status", {})[task_id] = {
                "status": "pending", "tool_calls": [], "final": "",
            }
            asyncio.create_task(
                _watch_subagent_task(session_id, task_id),
                name=f"watch-{task_id[:8]}",
            )
    except Exception:
        pass


async def _read_subagent_thread(task_id: str) -> dict | None:
    """Single read of a subagent's thread state from the coordinator server.
    Returns None on failure (caller decides whether to retry)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{SUBAGENT_URL}/threads/{task_id}")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None

    msgs = data.get("values", {}).get("messages", [])
    tool_calls, final_content = [], ""
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "tool":
            tool_calls.append({
                "name":    msg.get("name", "unknown_tool"),
                "content": msg.get("content", ""),
            })
        elif msg.get("type") == "ai" and msg.get("content"):
            final_content = msg.get("content")
    return {"tool_calls": tool_calls, "final": final_content}


async def _watch_subagent_task(session_id: str, task_id: str) -> None:
    """Poll one subagent task until it produces output or times out, updating
    sessions[session_id]['subagent_status'][task_id] as it goes. This runs
    detached from any single /chat request — it survives the SSE stream
    closing, so slow tasks (hospital_notifier) are still tracked and can be
    read later via GET /session/{session_id}/subagent-status."""
    deadline = asyncio.get_event_loop().time() + SUBAGENT_MAX_WAIT
    while asyncio.get_event_loop().time() < deadline:
        result = await _read_subagent_thread(task_id)
        if result and (result["tool_calls"] or result["final"]):
            if session_id in sessions:
                sessions[session_id]["subagent_status"][task_id] = {
                    "status": "complete", **result,
                }
            return
        await asyncio.sleep(SUBAGENT_POLL_SECS)

    if session_id in sessions:
        sessions[session_id]["subagent_status"][task_id] = {
            "status": "timeout", "tool_calls": [], "final": "",
        }


# ════════════════════════════════════════════════════════════════════════════
#  STREAM GENERATOR
# ════════════════════════════════════════════════════════════════════════════

async def _stream_chat(
    session_id: str,
    request: ChatRequest,
    is_new: bool,
) -> AsyncGenerator[str, None]:
    session     = sessions[session_id]
    agent_input = build_input(
        message=request.message,
        location=request.location.model_dump(),
        patient_profile=request.patient_profile.model_dump(),
    )
    config    = make_config(session["thread_id"])
    turn_type = "emergency" if is_new else "conversation"

    yield _sse("turn_started", {"session_id": session_id, "turn_type": turn_type})

    # task_ids whose "complete" status we've already emitted as an SSE event
    # in this turn — avoids re-sending the same coordinator_event on a later
    # poll tick, since _watch_subagent_task keeps its entry in place.
    already_emitted: set[str] = set()

    def _drain_subagent_updates() -> list[tuple[str, dict]]:
        """Best-effort, non-blocking check of subagent_status for anything
        that finished since we last looked. This is what makes fast subagent
        completions (e.g. a quick RAG search) show up as rag_event /
        coordinator_event WHILE this turn's SSE connection is still open —
        without this turn having to wait on them itself."""
        out = []
        for tid, st in sessions.get(session_id, {}).get("subagent_status", {}).items():
            if tid in already_emitted or st["status"] not in ("complete", "timeout"):
                continue
            already_emitted.add(tid)
            event = "rag_event" if st["status"] == "complete" else "coordinator_event"
            out.append((event, {
                "source": "subagent", "task_id": tid, "status": st["status"],
                "tool_calls": st.get("tool_calls", []), "final": st.get("final", ""),
            }))
        return out

    tokens_sent = False
    attempt     = 0
    while True:
        try:
            # First attempt runs the turn normally. Retries resume from the
            # checkpointer's last saved state (input=None) instead of
            # replaying agent_input — replaying would re-run every tool call
            # already made this turn (write_todos, start_async_task,
            # hospital notifications, ...), which for a live medical-alert
            # flow could mean firing a duplicate hospital notification.
            # Resuming only re-executes whatever step the graph hadn't
            # completed yet when it failed.
            turn_input = agent_input if attempt == 0 else None
            async for chunk in agent.astream(
                turn_input,
                config=config,
                stream_mode=["updates", "messages", "custom"],
                subgraphs=True,
                version="v2",
            ):
                # flush any subagent progress the background watchers picked
                # up since the last chunk, so it interleaves with the main
                # stream
                for event, payload in _drain_subagent_updates():
                    yield _sse(event, payload)

                classified = _classify_chunk(chunk, session_id)
                if classified:
                    event, payload = classified
                    if event == "token":
                        tokens_sent = True
                    yield _sse(event, payload)
            break  # completed without error

        except Exception as exc:
            is_retryable, safe_message = _classify_error(exc)
            if is_retryable and not tokens_sent and attempt < MAX_MODEL_RETRIES:
                attempt += 1
                delay = MODEL_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                yield _sse("retrying", {
                    "attempt": attempt, "max_attempts": MAX_MODEL_RETRIES,
                    "message": safe_message, "delay_secs": delay,
                })
                await asyncio.sleep(delay)
                continue
            # either not retryable, retries exhausted, or we already
            # streamed partial text (retrying now would duplicate it) —
            # surface a clean terminal error and stop.
            yield _sse("error", {"message": safe_message, "retryable": is_retryable})
            return

    # final drain — catches anything that completed in the gap between the
    # last agent chunk and here. Anything still running after this point is
    # NOT held up on — the client should poll
    # GET /session/{session_id}/subagent-status for the rest, since slow
    # tasks (hospital_notifier) can easily outlive this SSE connection.
    for event, payload in _drain_subagent_updates():
        yield _sse(event, payload)

    yield _sse("done", {"session_id": session_id, "turn_type": turn_type})


# ════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat(request: ChatRequest):
    """
    Main chat endpoint — handles ALL messages (first emergency + follow-ups).

    First message (no session_id or new session):
      → Agent analyses emergency, launches parallel RAG searches,
        asks clarifying question, returns immediate guidance

    Subsequent messages (same session_id):
      → Agent resolves uncertainty, cancels irrelevant searches,
        assembles and returns full first-aid guidance

    Body:
    {
      "session_id":      null,              ← null for new session
      "message":         "My father was stabbed and is not breathing",
      "location":        { "lat": 6.5418, "lng": 3.3917, "address": "..." },
      "patient_profile": { "name": "Emmanuel", "age": 67, ... }
    }

    SSE events:
      turn_started      — { session_id, turn_type }
      step              — agent node executing
      tool_call         — tool being called (name only, streamed early)
      tool_call_request — tool being called, with full args (supervisor's own tools only)
      rag_event         — RAG search started/complete (silent progress)
      subagent_complete — supervisor's own tool result (not RAG/hospital/youtube)
      token             — agent response text → stream to chat
      videos_incoming   — YouTube results being fetched
      retrying          — a transient model error is being retried
                           { attempt, max_attempts, message, delay_secs }
      done              — { session_id, turn_type }
      error             — terminal failure, safe to show the user
                           { message, retryable }
    """
    is_new = False

    if not request.session_id or request.session_id not in sessions:
        # new session
        is_new     = True
        session_id = str(uuid.uuid4())
        thread_id  = session_id
        sessions[session_id] = {
            "thread_id":        thread_id,
            "location":         request.location.model_dump(),
            "patient_profile":  request.patient_profile.model_dump(),
            "rag_task_ids":     [],
            "youtube_task_ids": [],
            "subagent_status":  {},   # task_id -> {status, tool_calls, final}
        }
    else:
        session_id = request.session_id
        # update location/profile in case they changed
        sessions[session_id]["location"]        = request.location.model_dump()
        sessions[session_id]["patient_profile"] = request.patient_profile.model_dump()

    return StreamingResponse(
        _stream_chat(session_id, request, is_new),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/session/videos/{session_id}")
async def get_videos(session_id: str):
    """Poll for YouTube video results."""
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    youtube_url = os.getenv("YOUTUBE_SEARCHER_URL", "http://localhost:8001")
    task_ids    = session.get("youtube_task_ids", [])

    if not task_ids:
        return JSONResponse({"status": "no_task", "videos": []})

    import httpx
    latest = task_ids[-1]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp    = await client.get(f"{youtube_url}/threads/{latest}")
            data    = resp.json()
            msgs    = data.get("values", {}).get("messages", [])
            videos  = []
            for msg in reversed(msgs):
                content = msg.get("content", "") if isinstance(msg, dict) else ""
                if "VIDEOS_READY:" in content:
                    try:
                        videos = json.loads(content.split("VIDEOS_READY:", 1)[1].strip())
                        break
                    except Exception:
                        pass
            status = "ready" if videos else "pending"
        return JSONResponse({"status": status, "videos": videos})
    except Exception as e:
        return JSONResponse({"status": "error", "videos": [], "message": str(e)})


@app.get("/session/{session_id}/subagent-status")
async def subagent_status(session_id: str):
    """
    Poll the state of every subagent task launched in this session.

    This is the PULL counterpart to the SSE stream: subagents run detached
    (via async_coordinator.py, a separate process) and can easily outlive
    a single /chat turn's SSE connection — hospital_notifier in particular
    may take minutes waiting on real replies. Rather than holding the SSE
    connection open or building a webhook channel back from the coordinator
    server, the frontend should poll this endpoint after the stream closes
    to pick up anything that was still "pending" in the last SSE frame.

    Response:
    {
      "session_id": "...",
      "tasks": {
        "<task_id>": {"status": "pending|complete|timeout",
                       "tool_calls": [...], "final": "..."}
      }
    }
    """
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    return JSONResponse({
        "session_id": session_id,
        "tasks": session.get("subagent_status", {}),
    })


@app.get("/sessions")
async def list_sessions():
    return JSONResponse({
        "total":    len(sessions),
        "sessions": [
            {
                "session_id":    sid,
                "thread_id":     s.get("thread_id"),
                "rag_searches":  len(s.get("rag_task_ids", [])),
                "youtube_tasks": len(s.get("youtube_task_ids", [])),
            }
            for sid, s in sessions.items()
        ],
    })


@app.get("/health")
async def health():
    return {"status": "ok", "service": "MedicAI MVP", "version": "1.0.0"}