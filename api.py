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

import json
import os
import uuid
from typing import AsyncGenerator

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from supervisor import agent, make_config, build_input, checkpointer

app = FastAPI(title="MedicAI MVP", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# in-memory session store
sessions: dict[str, dict] = {}


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
    data       = chunk.get("data", {})
    data       = _safe_dict(data)

    is_subagent = any(str(s).startswith("tools:") for s in ns)
    ns_str      = " ".join(str(s) for s in ns)
    source      = (
        "rag_searcher"  if "rag_searcher"  in ns_str else
        "youtube"       if "youtube"       in ns_str else
        "supervisor"
    )

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

    if chunk_type == "messages":
        raw     = data
        token   = raw[0] if isinstance(raw, tuple) else raw
        content = getattr(token, "content", None)
        t       = getattr(token, "type", "")

        # handle Gemini content list format
        if isinstance(content, list):
            content = "".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content
            )

        if t == "ai" and content and not getattr(token, "tool_call_chunks", None):
            return "token", {"source": source, "text": content}

        if getattr(token, "tool_call_chunks", None):
            tc = token.tool_call_chunks[0]
            if tc.get("name"):
                return "tool_call", {"source": source, "tool": tc["name"]}

    return None


def _handle_task_launched(content: str, session_id: str) -> None:
    """Extract task_id from start_async_task result and store youtube ones."""
    try:
        import re
        match   = re.search(r"task_id[:\s]+([a-f0-9\-]{36})", str(content))
        task_id = match.group(1) if match else None
        if task_id and session_id in sessions:
            # we store all task_ids — frontend can poll videos
            sessions[session_id].setdefault("rag_task_ids", []).append(task_id)
    except Exception:
        pass


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

    try:
        async for chunk in agent.astream(
            agent_input,
            config=config,
            stream_mode=["updates", "messages", "custom"],
            subgraphs=True,
            version="v2",
        ):
            classified = _classify_chunk(chunk, session_id)
            if not classified:
                continue

            event, payload = classified

            # rag_events are silent — don't send to client chat
            # but do send so frontend can show a subtle "searching..." indicator
            yield _sse(event, payload)

    except Exception as exc:
        yield _sse("error", {"message": str(exc)})
        return

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
      tool_call         — tool being called
      rag_event         — RAG search started/complete (silent progress)
      subagent_complete — RAG result received
      token             — agent response text → stream to chat
      videos_incoming   — YouTube results being fetched
      done              — { session_id, turn_type }
      error             — something failed
    """
    is_new = False

    if not request.session_id or request.session_id not in sessions:
        # new session
        is_new     = True
        session_id = str(uuid.uuid4())
        thread_id  = session_id
        sessions[session_id] = {
            "thread_id":       thread_id,
            "location":        request.location.model_dump(),
            "patient_profile": request.patient_profile.model_dump(),
            "rag_task_ids":    [],
            "youtube_task_ids": [],
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
