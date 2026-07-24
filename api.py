"""
MedicAI MVP — FastAPI
======================
api.py

Single endpoint for all conversation turns.
The agent handles everything — triage, web, clarification, guidance.

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
# The web_searcher / hospital_notifier / youtube_subagent tasks run on a
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


# Tool calls that mark a real, user-noticeable wait between turns. When the
# supervisor starts one of these, the client should already have shown the
# question (or ack) and now needs a "still working" signal for the gap before
# the next live event (token/question/guidance) arrives — otherwise a turn
# that's quietly waiting on resolve_uncertainty + web results looks identical
# to a dead connection. Mapped to a short, stable stage id the frontend can
# key copy off of ("checking the latest guidance…", etc.) without parsing
# tool names itself.
STATUS_STAGE_TOOLS = {
    "resolve_uncertainty":         "resolving_answer",
    "assemble_first_aid_response": "assembling_guidance",
}


def _classify_error(exc: Exception) -> tuple[bool, str]:
    """Decide whether an exception from agent.astream() is worth retrying,
    and produce a message that's safe to send to the client.

    Provider exceptions (esp. Gemini 429s) come back as a giant raw JSON
    blob — quota IDs, internal doc links, retry-delay hints. That must never
    reach the end user directly (it did, previously: a 429 payload ended up
    inside the assistant's visible response). Log the full exception
    server-side; only ever send the classified, generic message."""
    text = str(exc)
    # str(exc) alone (e.g. "'list' object has no attribute 'get'") tells you
    # WHAT broke but not WHERE — no file/line, no call stack. logger.exception
    # captures the full traceback so the next occurrence is actually
    # debuggable instead of a repeat of this same guessing exercise.
    logger.exception("agent.astream error: %s", text[:2000])

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


def _safe_json_loads(text: str) -> dict | None:
    """Best-effort JSON parse of a tool's raw content. Returns None rather
    than raising — callers fall back to the generic subagent_complete
    logging path when this fails, so a malformed/partial payload never
    breaks the stream."""
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


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

def _capture_task_ids(chunk: dict, session_id: str) -> None:
    """
    Scan updates chunks for async_tasks state channel updates.
    This is where deepagents stores { task_id, agent_name, status }.
    """
    # Every stream_mode this app uses ("updates"/"messages"/"custom") is
    # expected to hand us a dict wrapper like {"type": ..., "data": ...,
    # "ns": ...}. That's been assumed everywhere via chunk.get(...) without
    # ever checking chunk itself — if any chunk ever arrives as something
    # else (a bare list has been observed from some custom-stream-mode
    # payloads), chunk.get(...) raises AttributeError, which bubbles up
    # through agent.astream()'s loop and gets misreported as an "agent"
    # failure even though it's really our own code. Guard here so an
    # unexpected shape is just skipped instead of taking the whole turn down.
    if not isinstance(chunk, dict):
        return
    if chunk.get("type") != "updates":
        return

    data = chunk.get("data", {})
    if not isinstance(data, dict):
        try:    data = dict(data)
        except: return

    for node_name, node_data in data.items():
        if node_data is None:
            continue
        if not isinstance(node_data, dict):
            try:    node_data = dict(node_data)
            except: continue

        # async_tasks is a state channel updated by deepagents middleware
        async_tasks = node_data.get("async_tasks", {})
        if not async_tasks or not isinstance(async_tasks, dict):
            continue

        for task_id, task_info in async_tasks.items():
            if not isinstance(task_info, dict):
                continue
            agent_name = task_info.get("agent_name", "")
            print(f"[api] task captured: {agent_name} → {task_id[:8]}", flush=True)

            if session_id not in sessions:
                continue

            if "hospital" in agent_name:
                ids = sessions[session_id].setdefault("hospital_task_ids", [])
                if task_id not in ids:
                    ids.append(task_id)

            elif "youtube" in agent_name:
                ids = sessions[session_id].setdefault("youtube_task_ids", [])
                if task_id not in ids:
                    ids.append(task_id)

            elif "rag" in agent_name:
                ids = sessions[session_id].setdefault("rag_task_ids", [])
                if task_id not in ids:
                    ids.append(task_id)


def _classify_chunk(chunk: dict, session_id: str, message_state: dict) -> tuple[str, dict] | None:
    # See matching guard/comment in _capture_task_ids above — chunk itself
    # was never checked before being .get()'d, only its nested "data".
    if not isinstance(chunk, dict):
        return None
    chunk_type = chunk.get("type")
    ns         = chunk.get("ns", ())
    raw_data   = chunk.get("data", {})

    is_subagent = any(str(s).startswith("tools:") for s in ns)
    ns_str      = " ".join(str(s) for s in ns)
    source      = (
        "web_searcher"  if "web_searcher"  in ns_str else
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
        if isinstance(raw_data, tuple):
            token    = raw_data[0]
            metadata = raw_data[1] if len(raw_data) > 1 else {}
        else:
            token, metadata = raw_data, {}
        if not isinstance(metadata, dict):
            metadata = {}
        content = getattr(token, "content", None)
        t       = getattr(token, "type", "")

        # handle Gemini content list format
        if isinstance(content, list):
            content = "".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content
            )
        if t in ("ai", "AIMessageChunk") and content and not getattr(token, "tool_call_chunks", None):
            # Some tools (e.g. analyse_emergency, assemble_first_aid_response,
            # assemble_immediate_steps) apparently run their own nested LLM
            # call using the same config/callback context as the
            # supervisor's own generation — so their output streams through
            # THIS SAME "messages" channel, indistinguishable from real
            # user-facing text at the chunk level (no tool_call_chunks on
            # either).
            #
            # PRIMARY check: langgraph tags every "messages"-mode chunk with
            # metadata identifying which graph node produced it. The
            # supervisor's own user-facing reply is always generated by the
            # model-calling node (e.g. "agent"/"call_model") — a nested
            # llm.invoke() running INSIDE a tool function is generated while
            # the "tools" node is executing. That's a structural fact, not a
            # guess about content shape, so route anything tagged as coming
            # from the tools node straight to internal_output regardless of
            # whether it happens to look like JSON or plain prose. This is
            # what catches the assemble_immediate_steps case, where the
            # nested call's leaked output is prose and would otherwise be
            # indistinguishable from the real reply (both look like normal
            # sentences), causing the same paragraph to be sent twice.
            node_hint = str(metadata.get("langgraph_node", ""))
            if node_hint == "tools":
                return "internal_output", {"source": source, "text": content}

            # FALLBACK heuristic, for whenever metadata doesn't carry
            # langgraph_node (some streaming configs omit it) or the nested
            # call somehow runs outside a "tools"-tagged step. Buffer by
            # message id and check once we have enough leading content to
            # tell: JSON-shaped output is never a real reply. Weaker than
            # the metadata check above (can't catch a prose-shaped leak),
            # kept only as a stopgap for whatever the primary check misses.
            msg_id = getattr(token, "id", None) or "unknown"
            state  = message_state.setdefault(msg_id, {"mode": None, "buffer": ""})
            state["buffer"] += content

            if state["mode"] is None:
                stripped = state["buffer"].lstrip()
                if not stripped:
                    return None  # nothing decisive yet, wait for next chunk
                looks_like_json = stripped[0] == "{" or stripped.startswith("```json")
                state["mode"] = "internal_json" if looks_like_json else "text"

            if state["mode"] == "internal_json":
                return "internal_output", {"source": source, "text": content}

            # DEDUPE SAFETY NET: even with prompt-level rules asking for
            # exactly one acknowledgement per turn, an LLM can still repeat
            # itself (e.g. writing a short ack, then later writing a
            # near-identical one again before/after a tool call in the same
            # turn) — this has happened in practice. message_state is fresh
            # per-turn (recreated in _stream_chat for every /chat call), so
            # comparing against OTHER msg_ids already seen this turn only
            # ever catches same-turn repeats, never flags a legitimately
            # similar-sounding reply on a later, separate turn.
            #
            # We hold "text"-mode output back briefly (a short prefix) so we
            # have enough of it to compare BEFORE committing to streaming it
            # live — once a message has started streaming to the user we
            # can't un-send it, so the check has to happen before the first
            # token of a given message goes out, not after.
            DEDUPE_PREFIX_LEN = 30
            if not state.get("dedupe_decided"):
                if len(state["buffer"]) < DEDUPE_PREFIX_LEN:
                    return None  # keep buffering silently, decide once we have enough
                prefix = state["buffer"][:DEDUPE_PREFIX_LEN]
                is_repeat = any(
                    other_id != msg_id
                    and isinstance(other_state, dict)  # message_state also
                    # holds non-per-message entries (e.g. the list under
                    # "pending_subagent_types" set elsewhere in this file) —
                    # this crashed in production exactly because that list
                    # got treated as if it were one of our {"mode",...}
                    # state dicts. Every other value in this loop MUST be
                    # checked before .get() is called on it.
                    and other_state.get("mode") == "text"
                    and other_state.get("buffer", "").startswith(prefix)
                    for other_id, other_state in message_state.items()
                )
                state["dedupe_decided"] = True
                state["is_repeat"] = is_repeat
                if not is_repeat:
                    # first time we can emit: flush the whole held-back
                    # prefix as one token, then stream normally from here
                    return "token", {"source": source, "text": state["buffer"]}

            if state.get("is_repeat"):
                return "internal_output", {"source": source, "text": content}
            return "token", {"source": source, "text": content}

        if getattr(token, "tool_call_chunks", None):
            tc = token.tool_call_chunks[0]
            if tc.get("name"):
                return "tool_call", {"source": source, "tool": tc["name"]}
        return None

    data = _safe_dict(raw_data)

    if chunk_type == "custom":
        # web search events — stream silently (don't show in chat)
        event = data.get("event", "")
        if event in ("web_search_started", "web_search_complete"):
            return "web_event", {"source": source, **data}
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
                        pending = message_state.get("pending_subagent_types", [])
                        subagent_type = pending.pop(0) if pending else ""
                        _handle_task_launched(msg.content, session_id, subagent_type)

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
                            # Queue subagent_type per start_async_task call,
                            # in call order — the ToolMessage carrying the
                            # resulting task_id shows up in a LATER "updates"
                            # chunk (once the "tools" node executes) with no
                            # type info of its own, just "Launched async
                            # subagent. task_id: ...". FIFO-pairing them here
                            # is what lets _handle_task_launched below file
                            # youtube tasks into youtube_task_ids instead of
                            # everything defaulting to web_task_ids.
                            for tc in tool_calls:
                                if tc.get("name") == "start_async_task":
                                    message_state.setdefault(
                                        "pending_subagent_types", []
                                    ).append(tc.get("args", {}).get("subagent_type", ""))
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
                        content   = getattr(msg, "content", "")
                        # don't expose web internals in chat
                        if tool_name in ("search_first_aid_web",):
                            return "web_event", {
                                "source": "web_searcher",
                                "tool":   tool_name,
                            }

                        # analyse_emergency's clarifying_question field is
                        # internal reasoning input ONLY (per SYSTEM_PROMPT's
                        # hard rules) — it's a plain string with no preset
                        # options, and the model decides what to actually ask
                        # via a SEPARATE, later call to ask_clarifying_question
                        # (see below). Do NOT surface it here — that would
                        # leak a button-less, premature question event ahead
                        # of the real one.

                        # ask_clarifying_question is the tool that actually
                        # produces the user-facing question — called AFTER
                        # the text response, per step 7/8 of SYSTEM_PROMPT.
                        # Its result is already { question, options, context,
                        # type }, so surface it straight through as its own
                        # live event — this is what the frontend renders as
                        # a card with tappable buttons, distinct from the
                        # free-form `token` text that precedes it.
                        if tool_name == "ask_clarifying_question":
                            parsed = _safe_json_loads(content)
                            if parsed:
                                return "clarifying_question", {
                                    "source":   source,
                                    "question": parsed.get("question", ""),
                                    "options":  parsed.get("options", []),
                                    "context":  parsed.get("context", ""),
                                }

                        # assemble_first_aid_response already returns fully
                        # structured guidance — send it straight through as
                        # its own event instead of letting the model
                        # re-narrate the same fields as prose (which is also
                        # where the JSON/code-fence leaks upstream came
                        # from). Frontend maps each field to its own visual
                        # treatment: numbered steps, a red "do not" box, an
                        # amber "watch for" box, a muted reassurance line,
                        # and a follow-up prompt button.
                        # assemble_immediate_steps is the small, certain-only
                        # counterpart to guidance below — sent on the FIRST
                        # message, before the clarifying question is
                        # answered. Frontend should render it visually
                        # distinct from `guidance` (fewer, smaller, no
                        # do_not/watch_for/reassurance) since it's a partial,
                        # not the full picture.
                        if tool_name == "assemble_immediate_steps":
                            parsed = _safe_json_loads(content)
                            if parsed:
                                # same duplication fix as assemble_first_aid_
                                # response below: this tool's JSON now carries
                                # its own short `narrative` (the 2-3 sentence
                                # ack + "hospitals are being contacted" + "call
                                # 112" text), separate from the quick_steps
                                # list. Popped out here and flushed as a
                                # `token` event by the caller so it's said
                                # exactly once, never repeated inside the
                                # quick_steps card's own rendering.
                                return "quick_steps", {
                                    "source":      source,
                                    "narrative":   parsed.get("narrative", ""),
                                    "quick_steps": parsed.get("quick_steps", []),
                                }

                        # assemble_first_aid_response's JSON now carries a
                        # `narrative` field (short, prose, empathetic ack +
                        # call to action) SEPARATE from the structured
                        # fields — that's the fix for the duplication bug
                        # where the model's free-text response and this
                        # tool's structured output said the same thing
                        # twice. We pull narrative out here so the caller
                        # (_stream_chat) can flush it as its own `token`
                        # event, then send everything else as `guidance`
                        # with narrative already stripped out — the two
                        # channels never carry the same content.
                        if tool_name == "assemble_first_aid_response":
                            parsed = _safe_json_loads(content)
                            if parsed:
                                return "guidance", {
                                    "source":            source,
                                    "narrative":         parsed.get("narrative", ""),
                                    "priority_steps":    parsed.get("priority_steps", []),
                                    "do_not":            parsed.get("do_not", []),
                                    "watch_for":         parsed.get("watch_for", []),
                                    "reassurance":       parsed.get("reassurance", ""),
                                    "when_to_update_me": parsed.get("when_to_update_me", ""),
                                }

                        return "subagent_complete", {
                            "source":  source,
                            "tool":    tool_name,
                            "content": str(content)[:500],
                        }
            return "step", {"source": source, "node": node_name}
            
            
    return None


def _extract_videos_ready(final) -> list[dict] | None:
    """If a completed subagent's final output is a youtube_subagent result
    (marked with the VIDEOS_READY: prefix), parse and return the video list.
    Returns None for any other kind of subagent output — the caller falls
    back to the generic web_event/coordinator_event path in that case.
    `final` may be a plain string or the list-of-content-block shape LangGraph
    messages use ([{"type": "text", "text": "..."}])."""
    text = final
    if isinstance(text, list):
        text = "".join(
            block.get("text", "") for block in text if isinstance(block, dict)
        )
    if not isinstance(text, str) or "VIDEOS_READY:" not in text:
        return None
    try:
        return json.loads(text.split("VIDEOS_READY:", 1)[1].strip())
    except Exception:
        return None

def _handle_task_launched(content: str, session_id: str, tool_name_hint: str = "") -> None:
    """Extract task_id from start_async_task result and store by type."""
    try:
        import re
        match   = re.search(r"task_id[:\s]+([a-f0-9\-]{36})", str(content))
        task_id = match.group(1) if match else None
        if task_id and session_id in sessions:
            # store all task_ids — categorize by agent name in content
            content_lower = str(content).lower()
            if "youtube" in content_lower:
                sessions[session_id].setdefault("youtube_task_ids", []).append(task_id)
            elif "hospital" in content_lower:
                sessions[session_id].setdefault("hospital_task_ids", []).append(task_id)
            else:
                sessions[session_id].setdefault("web_task_ids", []).append(task_id)
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
        completions (e.g. a quick web search) show up as web_event /
        coordinator_event WHILE this turn's SSE connection is still open —
        without this turn having to wait on them itself.

        A completed youtube_subagent task is a special case: rather than
        falling into the generic web_event bucket (batched, silent until
        `activity` fires), it's detected via its VIDEOS_READY: marker and
        returned as its own "videos" event — the caller streams this live,
        same treatment as `guidance`, so the frontend has a clear signal for
        where to place the video instead of discovering it only by polling
        /session/videos after the fact."""
        out = []
        for tid, st in sessions.get(session_id, {}).get("subagent_status", {}).items():
            if tid in already_emitted or st["status"] not in ("complete", "timeout"):
                continue
            already_emitted.add(tid)

            if st["status"] == "complete":
                videos = _extract_videos_ready(st.get("final", ""))
                if videos is not None:
                    out.append(("videos", {
                        "source": "youtube_subagent", "task_id": tid, "videos": videos,
                    }))
                    continue

            event = "web_event" if st["status"] == "complete" else "coordinator_event"
            out.append((event, {
                "source": "subagent", "task_id": tid, "status": st["status"],
                "tool_calls": st.get("tool_calls", []), "final": st.get("final", ""),
            }))
        return out

    # everything that ISN'T the user-facing response text gets collected
    # here instead of streamed live — tool_call, tool_call_request,
    # web_event, subagent_complete, step, coordinator_event all land in
    # this list and go out as a single batched event, not one SSE frame
    # each. Only "token" streams live, since that's the actual answer.
    activity_log: list[dict] = []

    # tracks, per streamed-message id, whether that generation turned out to
    # be real user-facing text or a tool's internal JSON output leaking
    # through the same channel — see _classify_chunk for why this exists
    message_state: dict = {}

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
                # subagent progress picked up since the last chunk. `videos`
                # is user-facing (same treatment as guidance) so it streams
                # live; everything else queues into activity as before.
                for event, payload in _drain_subagent_updates():
                    if event == "videos":
                        yield _sse(event, payload)
                    else:
                        activity_log.append({"event": event, **payload})
                # Our own chunk parsing is defended locally, separate from
                # the outer try/except around the whole astream() loop.
                # That outer one exists for genuine upstream failures
                # (rate limits, dropped connections) and its retry logic
                # assumes the WHOLE turn needs re-running — that's wrong
                # for a bug in our own parsing of one chunk, which should
                # just be logged and skipped so the rest of the turn (and
                # the model's actual response) still goes through.
                try:
                    _capture_task_ids(chunk, session_id)
                    classified = _classify_chunk(chunk, session_id, message_state)
                except Exception:
                    logger.exception(
                        "chunk parsing error (session=%s) — skipping this chunk",
                        session_id,
                    )
                    classified = None
                if classified:
                    event, payload = classified
                    if event == "subagent_complete" and payload.get("tool") == "send_alerts":
                    	try:
                    		content = json.loads(payload.get("content", "{}"))
                    		if isinstance(content, list):
                    			sessions[session_id]["alerts"] = content
                    	except Exception:
                    		pass
                    if event == "token":
                        tokens_sent = True
                        yield _sse(event, payload)
                    elif event in ("guidance", "quick_steps"):
                        # Both assemble_first_aid_response and assemble_
                        # immediate_steps's JSON now carry a short `narrative`
                        # field alongside their structured list — that's the
                        # ONLY prose the user should see for that tool
                        # result, so it's flushed as its own `token` event
                        # first (same rendering path as any other supervisor
                        # text), and THEN the structured fields go out under
                        # their own event name, with `narrative` already
                        # popped off. This is what keeps the two channels
                        # from ever repeating the same content — no separate
                        # "pending narrative" state needed, since both
                        # events are emitted back-to-back here, in order,
                        # before the next chunk is processed.
                        narrative = payload.pop("narrative", "")
                        if narrative:
                            tokens_sent = True
                            yield _sse("token", {
                                "source": payload.get("source", "supervisor"),
                                "text":   narrative,
                            })
                        yield _sse(event, payload)
                    elif event == "clarifying_question":
                        # this IS the user-facing answer, just structured
                        # instead of prose — stream live like token, not
                        # batched into activity
                        yield _sse(event, payload)
                    elif event == "tool_call_request":
                        # normally batched into activity_log below — but if
                        # this call is one of the "the user is waiting on
                        # this" tools, also fire a live status event first so
                        # the client can show a working indicator during the
                        # gap. The tool_call_request itself still goes into
                        # activity as usual; this is additive, not a replacement.
                        for call in payload.get("calls", []):
                            stage = STATUS_STAGE_TOOLS.get(call.get("tool"))
                            if stage:
                                yield _sse("status", {
                                    "source": payload.get("source", "supervisor"),
                                    "stage":  stage,
                                })
                        activity_log.append({"event": event, **payload})
                    else:
                        # internal_output (JSON leaking from a tool's nested
                        # LLM call) and every other non-token event land
                        # here — batched, never streamed live
                        activity_log.append({"event": event, **payload})
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
        if event == "videos":
            yield _sse(event, payload)
        else:
            activity_log.append({"event": event, **payload})

    # everything non-text goes out here, once, as a single frame — the
    # client gets the full "what happened behind the scenes" picture
    # in one shot instead of a live trickle of tool_call/step/etc. events.
    if activity_log:
        yield _sse("activity", {"session_id": session_id, "events": activity_log})

    yield _sse("done", {"session_id": session_id, "turn_type": turn_type})


# ════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat(request: ChatRequest):
    """
    Main chat endpoint — handles ALL messages (first emergency + follow-ups).

    First message (no session_id or new session):
      → Agent analyses emergency, launches parallel web searches,
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
      turn_started — { session_id, turn_type }
      token        — free-form response prose, streamed live. May still
                     appear alongside `question`/`guidance` on a turn (e.g.
                     a short intro sentence) — don't assume it's the ONLY
                     source of user-facing content, just the unstructured
                     part of it. In particular, `guidance` is now ALWAYS
                     preceded by exactly one `token` event carrying that
                     tool result's own short `narrative` (a couple of
                     empathetic + call-to-action sentences) — the model's
                     own free-text response is instructed not to also
                     write that content, so this token is the only prose
                     tied to a guidance card, never duplicated in it.
      clarifying_question — { source, question, options: [...], context }
                     sent ONCE, live, straight from ask_clarifying_question's
                     structured tool result. This is a SEPARATE event from
                     the preceding `token` text — the model's text response
                     never contains the question itself (see SYSTEM_PROMPT
                     step 7/8), so the frontend gets prose first, then this
                     event with preset answer options. Render as a card with
                     tappable buttons, one per option; when the user taps
                     one, send that option's exact string back as the next
                     /chat message's `message` field. `context` is a short
                     line on why the question matters medically (e.g. "This
                     determines if CPR is needed") — optional to display.
      quick_steps  — { source, quick_steps: [...] } sent ONCE, live, on the
                     FIRST message only — straight from
                     assemble_immediate_steps's structured tool result, and
                     (like `guidance`) ALWAYS preceded by exactly one
                     `token` event carrying that tool result's own short
                     `narrative` — popped off before this event goes out,
                     so it's never duplicated between the two. Max 3
                     quick_steps items, built only from certain_conditions,
                     no do_not/watch_for/reassurance. Absent whenever
                     certain_conditions was empty (nothing certain yet to
                     act on). This is a partial, smaller sibling of
                     `guidance` below — render it visually distinct (e.g.
                     a compact "do this now" list) so it isn't mistaken for
                     the full guidance that arrives after the clarifying
                     question is answered.
      videos       — { source, task_id, videos: [...] } sent live, as many
                     times as youtube_subagent tasks complete during this
                     turn's connection (0, 1, or more — a technique change
                     mid-conversation can trigger a second one). Each
                     `videos` item is { title, url, thumbnail, channel,
                     description }. youtube_subagent is otherwise
                     fire-and-forget/non-blocking, so most completions land
                     well after the triggering turn has ended — this event
                     only fires for ones that finish INSIDE the current SSE
                     connection. For anything that finishes after the
                     stream closes, keep polling
                     GET /session/videos/{session_id} as before; this event
                     is an optimization for the common case, not a
                     replacement for that fallback.
      guidance     — { source, priority_steps: [...], do_not: [...],
                     watch_for: [...], reassurance, when_to_update_me }
                     sent ONCE, live, straight from
                     assemble_first_aid_response's structured tool result —
                     not re-derived from the model's prose, and no longer
                     carries a `narrative` field itself (that's popped off
                     and sent as the preceding `token` event instead — see
                     above). Map each field to its own visual treatment
                     (numbered steps, a "do not" callout, a "watch for"
                     callout, etc.) instead of parsing markdown headers out
                     of token text.
      activity     — { session_id, events: [...] } sent ONCE, right before
                     `done`. Bundles everything that isn't user-facing:
                     step, tool_call, tool_call_request, web_event,
                     subagent_complete, coordinator_event, internal_output
                     — each list item has the same shape it used to have
                     as its own SSE frame, just collected instead of
                     streamed live.
      status       — { source, stage } sent live whenever the supervisor
                     starts a tool call the user is meaningfully waiting on
                     (resolve_uncertainty, assemble_first_aid_response).
                     `stage` is one of: "resolving_answer",
                     "assembling_guidance". Fires between a user's reply and
                     the next live event on that turn — use it to show a
                     "checking the latest guidance…" indicator, and clear it
                     as soon as any token/question/guidance event arrives.
                     May fire more than once per turn (e.g. resolving_answer
                     then assembling_guidance back to back) — always show
                     the most recent stage received, don't queue them.
      retrying     — a transient model error is being retried (still live,
                     since the client needs to know a retry is in progress)
                     { attempt, max_attempts, message, delay_secs }
      done         — { session_id, turn_type }
      error        — terminal failure, safe to show the user (still live,
                     ends the stream)
                     { message, retryable }

    Note: `activity`, `clarifying_question`, `quick_steps`, `videos`,
    `guidance`, and `status` may all be ABSENT on any given turn — don't
    assume any of them always arrives. A turn with no tool calls at all
    sends only token/turn_started/done.
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
            "web_task_ids":     [],
            "youtube_task_ids": [],
            "hospital_task_ids":[],
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

    youtube_url = os.getenv("YOUTUBE_SEARCHER_URL", "http://localhost:8000")
    task_ids    = session.get("youtube_task_ids", [])

    if not task_ids:
        return JSONResponse({"status": "no_task", "videos": []})

    import httpx
    latest = task_ids[-1]
    print(task_ids)
    print('youtube tasj id',latest)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp    = await client.get(f"{youtube_url}/threads/{latest}")
            data    = resp.json()
            msgs    = data.get("values", {}).get("messages", [])
            videos  = []
            for msg in reversed(msgs):
                content = msg.get("content", "") if isinstance(msg, dict) else ""
                try:
                	videos = json.loads(msg.get('content',[]))
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


@app.get("/session/hospitals/{session_id}")
async def get_hospitals(session_id: str):
    """
    Get hospital details discovered by the hospital coordinator for this session.
    Poll this after /chat to see which hospitals were found and their alert status.

    Response:
    {
      "session_id":   "...",
      "status":       "pending" | "ready",
      "hospitals":    [ { id, name, address, lat, lng, distance_km, api_url } ],
      "alerts":       [ { hospital, status, hospital_response } ],
      "accepted":     [ "Hospital Name", ... ]
    }
    """
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    youtube_url = os.getenv("YOUTUBE_SEARCHER_URL", "http://localhost:8000")
    task_ids    = session.get("hospital_task_ids", [])

    if not task_ids:
        return JSONResponse({"status": "no_task", "hospital_task": []})

    import httpx
    latest = task_ids[-1]
    print('hos',task_ids)
    print('hospital',latest)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp    = await client.get(f"{youtube_url}/threads/{latest}")
            data    = resp.json()
            msgs    = data.get("values", {}).get("messages", [])
            hospitals  = []
            for msg in reversed(msgs):
                if msg['name'] == 'broadcast_to_hospitals':
                    content = msg.get("content", "") if isinstance(msg, dict) else ""
                    try:
                        hospitals = json.loads(msg.get('content',[]))
                        break
                    except Exception:
                        pass
            status = "ready" if hospitals else "pending"

        return JSONResponse({"status": status, "hospitals": hospitals})
    except Exception as e:
        return JSONResponse({"status": "error", "hospitals": [], "message": str(e)})
    
    # return JSONResponse({
    #     "session_id": session_id,
    #     "status":     "ready" if session.get("hospitals") else "pending",
    #     "hospitals":  session.get("hospitals", []),
    #     "alerts":     session.get("alerts", []),
    #     "accepted":   [
    #         a["hospital"]["name"]
    #         for a in session.get("alerts", [])
    #         if a.get("hospital_response", {}).get("accepted")
    #     ],
    # })


@app.get("/sessions")
async def list_sessions():
    return JSONResponse({
        "total":    len(sessions),
        "sessions": [
            {
                "session_id":    sid,
                "thread_id":     s.get("thread_id"),
                "web_searches":  len(s.get("web_task_ids", [])),
                "youtube_tasks": len(s.get("youtube_task_ids", [])),
            }
            for sid, s in sessions.items()
        ],
    })


@app.get("/health")
async def health():
    return {"status": "ok", "service": "MedicAI MVP", "version": "1.0.0"}
