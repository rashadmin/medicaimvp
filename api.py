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
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

import httpx
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request, Response, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from supervisor import agent, make_config, build_input, checkpointer, estimate_eta_minutes

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


# ── WhatsApp Cloud API config ──────────────────────────────────────────────
# Same LangGraph/agent backend as /chat — this is a second front door onto
# the exact same _stream_chat() turn logic, just translating SSE frames
# into WhatsApp messages instead of streaming them to a browser.
WHATSAPP_VERIFY_TOKEN    = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_APP_SECRET      = os.getenv("META_APP_SECRET", "")
WHATSAPP_ACCESS_TOKEN    = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_GRAPH_VERSION   = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0")
WHATSAPP_GRAPH_URL       = f"https://graph.facebook.com/{WHATSAPP_GRAPH_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"

# phone number ("2348012345678") -> session_id. In-memory like `sessions`
# above — fine for a pilot on a single Render instance, but move this (and
# `sessions`) to Redis/Postgres before you rely on it surviving a restart,
# since a Render free/starter dyno recycling mid-conversation would
# silently drop every in-flight WhatsApp thread.
whatsapp_sessions: dict[str, str] = {}

# phone number -> emergency text received before we had a location for
# them. WhatsApp has no equivalent of the frontend's "ask for location on
# page load" — we have to request it conversationally on the first
# message, then resume once the location share arrives.
whatsapp_pending_message: dict[str, str] = {}

# phone number -> location received before we had a description of what's
# actually wrong. WhatsApp's location-share UI doesn't let the user attach
# a caption, so if location arrives first we stash it here and ask what's
# happening — mirrors whatsapp_pending_message above so text-first and
# location-first are handled symmetrically instead of location-first
# falling back to a placeholder ("Emergency — see shared location.") that
# tells the agent nothing about the actual emergency.
whatsapp_pending_location: dict[str, dict] = {}

# phone number -> asyncio.Lock, serializing all pending-state mutations for
# that sender. Without this, a text message and a location share that
# arrive close together (either batched into one Meta payload — dispatched
# concurrently via asyncio.gather — or as two near-simultaneous webhook
# POSTs, each its own BackgroundTask) can race on whatsapp_pending_message /
# whatsapp_pending_location: e.g. the location handler can check for a
# stashed text before the text handler has finished stashing it, so each
# starts its own turn instead of the two being combined into one.
whatsapp_sender_locks: dict[str, asyncio.Lock] = {}


def _wa_lock(sender: str) -> asyncio.Lock:
    lock = whatsapp_sender_locks.get(sender)
    if lock is None:
        lock = asyncio.Lock()
        whatsapp_sender_locks[sender] = lock
    return lock

# phone number -> the exact option strings for the clarifying_question
# currently on offer, so a tapped WhatsApp button (title truncated to 20
# chars by WhatsApp itself) can be resolved back to the full original
# option text before it's sent into the agent as the next turn's message.
whatsapp_pending_options: dict[str, list[str]] = {}

# WhatsApp message ids already processed — Meta retries POSTs that don't
# get a fast 200, so the same message can arrive more than once.
seen_whatsapp_message_ids: set[str] = set()


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


class HospitalSelection(BaseModel):
    """Body for POST /session/hospitals/{session_id}/select — the frontend
    sends the hospital dict the user tapped, as returned by
    GET /session/hospitals/{session_id}, plus its own travel-time
    calculation for that hospital if it has one (e.g. from a maps/
    directions API using the user's live location) — this is preferred
    over the crude straight-line fallback estimated server-side."""
    id:          str
    name:        str
    address:     str | None   = None
    distance_km: float | None = None
    contact:     str | None   = None
    eta_minutes: float | None = None   # frontend-calculated travel time, if available


class EtaUpdate(BaseModel):
    """Body for POST /session/hospitals/{session_id}/eta — lets the
    frontend push a refreshed travel-time calculation (e.g. re-run against
    the user's live location as they move) without changing which
    hospital is selected."""
    eta_minutes: float


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


# Structural markers of restated guidance: things a short 2-3 sentence
# acknowledgement should never contain, but a paraphrased priority_steps/
# do_not/watch_for list reliably does. Kept as compiled patterns since this
# runs on every streamed chunk.
_RESTATED_GUIDANCE_PATTERNS = [
    # Not anchored to a true line-start: the streamed "text"-mode buffer is
    # plain concatenated token content, not guaranteed to carry the newlines
    # a rendered markdown view would — a header/list marker earlier tested
    # with ^...MULTILINE missed real examples that had already lost their
    # line breaks by the time they reached this buffer.
    re.compile(r"#{1,6}\s*\w"),            # markdown header, e.g. "### Priority Actions"
    re.compile(r"\bSTEP\s*\d+\b", re.IGNORECASE),  # "STEP 1", "Step 2"
    re.compile(r"(?:^|\n)\s*\d+[.)]\s"),   # numbered list: "1. " / "1) "
    re.compile(r"(?:^|\n)\s*[\*\-•]\s"),   # bullet list
]


def _looks_like_restated_guidance(text: str) -> bool:
    """Heuristic backstop for the semantic-duplication case the literal
    prefix/hash dedupe checks in _classify_chunk can't see: the model
    paraphrasing priority_steps/do_not/watch_for as its own prose instead
    of the short ack SYSTEM_PROMPT asks for. A paraphrase won't share a
    prefix with, or hash identically to, the guidance/quick_steps card's
    structured payload — so it needs its own check, based on shape rather
    than exact text.

    Deliberately structural, not content-based: a legitimate ack is never
    supposed to contain headers, numbered/bulleted steps, or the same
    section label repeated, regardless of phrasing. Two or more "do not" /
    "watch for" occurrences is also treated as a signal, since a real ack
    has no reason to use either phrase more than once."""
    if any(p.search(text) for p in _RESTATED_GUIDANCE_PATTERNS):
        return True
    lowered = text.lower()
    if lowered.count("do not") >= 2 or lowered.count("watch for") >= 2:
        return True
    return False


DEDUPE_PREFIX_LEN = 30
GUIDANCE_SNIFF_CAP = 500


def _is_repeat_text(buffer: str, message_state: dict, self_msg_id: str) -> bool:
    """Shared duplicate-detection: is `buffer` a repeat of (a) another
    text-mode message already streamed this turn, or (b) a narrative
    already popped and sent as `token` by the guidance/quick_steps branch
    (the cross-channel case — a model writing its own free-text echo of a
    tool's narrative, despite being told not to). Used both mid-stream
    (once a buffer reaches GUIDANCE_SNIFF_CAP) and at the end-of-turn
    dangling-buffer flush, on whatever length buffer we actually got —
    a short buffer just compares its full length instead of a 30-char
    prefix, since there's nothing more coming to wait for at that point."""
    if not buffer:
        return False
    prefix = buffer[:DEDUPE_PREFIX_LEN]
    return any(
        other_id != self_msg_id
        and isinstance(other_state, dict)
        and other_state.get("mode") == "text"
        and other_state.get("buffer", "").startswith(prefix)
        for other_id, other_state in message_state.items()
    ) or any(
        narrative_text.startswith(prefix)
        for narrative_text in message_state.get("_tool_narratives", [])
    )


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
            state  = message_state.setdefault(
                msg_id,
                {"mode": None, "buffer": "", "first_attempt": message_state.get("_current_attempt", 0)},
            )

            # RETRY-REPLAY GUARD: if this exact msg_id already finished
            # streaming as real user-facing text in a PRIOR attempt (i.e.
            # it was fully decided as "not a repeat" and flushed), and it's
            # now showing up again after a retry resumed the graph from the
            # checkpoint, this is a replay of already-committed state, not
            # new content — LangGraph resuming a "messages"-mode stream can
            # re-emit messages already persisted to the checkpoint. Route
            # it to internal_output instead of streaming the same text to
            # the user a second (or third) time.
            if (
                state.get("dedupe_decided")
                and not state.get("is_repeat")
                and state.get("first_attempt") != message_state.get("_current_attempt", 0)
            ):
                return "internal_output", {"source": source, "text": content}

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
            # This only catches LITERAL repeats (same/near-identical text,
            # or the model echoing a tool's exact `narrative` string). It
            # does NOT catch the model paraphrasing the same priority_steps/
            # do_not/watch_for content in different words/order — e.g.
            # writing its own "### Priority Actions ... ### Do Not ... ###
            # Watch For" prose and then a guidance/quick_steps card renders
            # the same steps again, structurally, right after. Two
            # differently-worded blocks share no 30-char prefix and hash
            # differently, so this check alone waves it through. That gap
            # is what _looks_like_restated_guidance below closes.
            #
            # We hold "text"-mode output back briefly so we have enough of
            # it to compare BEFORE committing to streaming it live — once a
            # message has started streaming to the user we can't un-send
            # it, so the check has to happen before the first token of a
            # given message goes out, not after.
            DEDUPE_PREFIX_LEN = 30

            # STRUCTURAL SAFETY NET: legitimate free-text acks are short
            # (SYSTEM_PROMPT: "SHORT, 2-3 sentences MAXIMUM, ... NO steps")
            # and never contain headers, numbered steps, or repeated
            # do_not/watch_for-style section labels — those only belong in
            # the guidance/quick_steps card, rendered straight from the
            # tool's structured output. So a free-text buffer that DOES
            # contain them is, by construction, restating content that
            # belongs to that card — a genuine ack could never legitimately
            # look like this. We keep buffering (silently) past the
            # DEDUPE_PREFIX_LEN floor, up to GUIDANCE_SNIFF_CAP, so a marker
            # that shows up later in the message (not in the first 30
            # chars) still gets caught before anything streams live.
            GUIDANCE_SNIFF_CAP = 500

            if not state.get("dedupe_decided"):
                buffer = state["buffer"]

                # Only treat list/header-shaped text as a restated-guidance
                # duplicate if a guidance/quick_steps card was actually
                # assembled THIS turn — otherwise this fires on any
                # legitimately bulleted reply (e.g. "how will I know if
                # he's improving" answered with a plain bullet list),
                # dropping real, novel content into internal_output even
                # though there's no card anywhere for it to be duplicating.
                if (
                    message_state.get("_guidance_emitted_this_turn")
                    and _looks_like_restated_guidance(buffer)
                ):
                    state["dedupe_decided"] = True
                    state["is_repeat"] = True
                    return "internal_output", {"source": source, "text": content}

                if len(buffer) < DEDUPE_PREFIX_LEN:
                    return None  # keep buffering silently, decide once we have enough
                if len(buffer) < GUIDANCE_SNIFF_CAP:
                    # long enough for the literal-prefix check below, but
                    # still short of the cap — keep buffering silently in
                    # case a restated-guidance marker is still coming
                    return None

                is_repeat = _is_repeat_text(buffer, message_state, msg_id)
                state["dedupe_decided"] = True
                state["is_repeat"] = is_repeat
                if not is_repeat:
                    # first time we can emit: flush the whole held-back
                    # buffer as one token, then stream normally from here
                    return "token", {"source": source, "text": buffer}

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
                                    "source":            source,
                                    "question":          parsed.get("question", ""),
                                    "options":           parsed.get("options", []),
                                    "context":           parsed.get("context", ""),
                                    "suggested_replies": parsed.get("suggested_replies", []),
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
                                    "suggested_replies": parsed.get("suggested_replies", []),
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
        selected_hospital=session.get("selected_hospital"),
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

    # signatures of guidance/quick_steps tool results already streamed this
    # turn (event name + hash of the full payload, narrative included) —
    # persists across retry attempts (declared outside the while loop), so
    # if a retry resumes the graph and replays an already-committed tool
    # result, it's dropped instead of re-sent as a duplicate narrative +
    # duplicate card.
    sent_structured_sigs: set[str] = set()

    tokens_sent = False
    attempt     = 0
    while True:
        message_state["_current_attempt"] = attempt
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
                        # Flag set BEFORE the dedupe-sig check below (even a
                        # replayed/duplicate card means a card genuinely ran
                        # this turn) — this is what _classify_chunk's
                        # restated-guidance heuristic gates on, so it only
                        # ever fires when there's an actual card for
                        # free-text to be duplicating.
                        message_state["_guidance_emitted_this_turn"] = True
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
                        # REPLAY GUARD: hash the full result (before popping
                        # narrative) so a retry that resumes the graph and
                        # re-delivers this same already-committed tool
                        # result is dropped wholesale — no duplicate
                        # narrative token, no duplicate card.
                        sig = event + ":" + hashlib.sha256(
                            json.dumps(payload, sort_keys=True, default=str).encode()
                        ).hexdigest()
                        if sig in sent_structured_sigs:
                            continue
                        sent_structured_sigs.add(sig)

                        narrative = payload.pop("narrative", "")
                        if narrative:
                            tokens_sent = True
                            # Register so the free-text dedupe safety net in
                            # _classify_chunk can catch the model separately
                            # writing its own version of this same ack (the
                            # cross-channel gap that let the nose-injury
                            # message double/triple up).
                            message_state.setdefault("_tool_narratives", []).append(narrative)
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

    # DANGLING-BUFFER FLUSH: a "text"-mode message can finish streaming
    # before its buffer ever crosses DEDUPE_PREFIX_LEN (30 chars) — e.g. a
    # short ack like "Okay, understood." — so _classify_chunk never reaches
    # a dedupe decision and just keeps returning None each time. Once the
    # astream() loop is done, no more chunks are coming for that message id,
    # so the buffer would otherwise be silently dropped: sent as neither
    # `token` nor `internal_output`, just gone. Catch any such states here
    # and resolve them now that we know the message is actually finished.
    # Snapshot via list(...) — the loop body below can call
    # message_state.setdefault("_tool_narratives", []), which inserts a new
    # key into message_state the first time it's called. Iterating the live
    # dict directly raises "dictionary changed size during iteration" the
    # first time that happens in a given turn; iterating a snapshot avoids
    # it regardless of what the loop body mutates.
    for msg_id, state in list(message_state.items()):
        if not isinstance(state, dict) or msg_id == "_current_attempt":
            continue
        if state.get("mode") != "text" or state.get("dedupe_decided"):
            continue
        buffer = state.get("buffer", "")
        if not buffer:
            continue
        is_restated_guidance = (
            message_state.get("_guidance_emitted_this_turn")
            and _looks_like_restated_guidance(buffer)
        )
        # Same repeat check used mid-stream (cross-message-id prefix match,
        # plus the cross-channel match against already-sent tool
        # narratives) — this is what was missing here before, and it's
        # exactly what let a short free-text echo of a narrative (too
        # short to ever reach GUIDANCE_SNIFF_CAP mid-stream) get flushed
        # as a brand-new, duplicate `token` instead of being recognized as
        # a repeat.
        if is_restated_guidance or _is_repeat_text(buffer, message_state, msg_id):
            activity_log.append({
                "event": "internal_output", "source": "supervisor", "text": buffer,
            })
            continue
        tokens_sent = True
        message_state.setdefault("_tool_narratives", []).append(buffer)
        yield _sse("token", {"source": "supervisor", "text": buffer})

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
#  WHATSAPP — send helpers
# ════════════════════════════════════════════════════════════════════════════

async def _wa_send_text(to: str, body: str) -> None:
    """Plain text message. WhatsApp caps body at 4096 chars — truncate
    rather than let the Graph API reject the whole call."""
    if not body:
        return
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body[:4096]},
    }
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(WHATSAPP_GRAPH_URL, headers=headers, json=payload)
        if resp.status_code >= 400:
            logger.error("WhatsApp send failed (%s): %s", resp.status_code, resp.text[:500])


async def _wa_send_buttons(to: str, body: str, options: list[str]) -> None:
    """Up to 3 tappable reply buttons — used for clarifying_question when
    there are 3 or fewer options. Button titles are capped at 20 chars by
    WhatsApp itself, so the FULL option text is stashed in
    whatsapp_pending_options and recovered by id when the button is
    tapped, rather than relying on the (possibly truncated) title."""
    buttons = [
        {"type": "reply", "reply": {"id": f"q_{i}", "title": opt[:20]}}
        for i, opt in enumerate(options[:3])
    ]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {"buttons": buttons},
        },
    }
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(WHATSAPP_GRAPH_URL, headers=headers, json=payload)
        if resp.status_code >= 400:
            logger.error("WhatsApp send failed (%s): %s", resp.status_code, resp.text[:500])


async def _wa_send_option_list(to: str, body: str, options: list[str]) -> None:
    """>3 options don't fit WhatsApp's button UI (max 3) — fall back to an
    interactive list (max 10 rows)."""
    rows = [{"id": f"q_{i}", "title": opt[:24]} for i, opt in enumerate(options[:10])]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:1024]},
            "action": {"button": "Choose", "sections": [{"title": "Options", "rows": rows}]},
        },
    }
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(WHATSAPP_GRAPH_URL, headers=headers, json=payload)
        if resp.status_code >= 400:
            logger.error("WhatsApp send failed (%s): %s", resp.status_code, resp.text[:500])


async def _wa_send_hospital_list(to: str, hospitals: list[dict]) -> None:
    """Interactive list message standing in for the frontend's hospital-
    selection button. Row id carries the hospital id (prefixed so the
    interactive handler can tell it apart from a q_N clarifying-question
    reply); title/description carry name/address so the reply itself
    contains enough to reconstruct the selection without a server-side
    cache lookup."""
    rows = [
        {
            "id": f"hosp_{h.get('id', i)}",
            "title": (h.get("name") or "Hospital")[:24],
            "description": (h.get("address") or "")[:72],
        }
        for i, h in enumerate(hospitals[:10])
    ]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": "Nearby hospitals — tap one to select it:"},
            "action": {
                "button": "Select hospital",
                "sections": [{"title": "Nearby hospitals", "rows": rows}],
            },
        },
    }
    headers = {"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(WHATSAPP_GRAPH_URL, headers=headers, json=payload)
        if resp.status_code >= 400:
            logger.error("WhatsApp send failed (%s): %s", resp.status_code, resp.text[:500])


# ════════════════════════════════════════════════════════════════════════════
#  WHATSAPP — driving _stream_chat() from a webhook instead of a browser
# ════════════════════════════════════════════════════════════════════════════

def _parse_sse_frame(frame: str) -> tuple[str, dict | str]:
    """_stream_chat() yields raw `event: X\\ndata: {...}\\n\\n` strings built
    by _sse() — this reverses that so the WhatsApp consumer can branch on
    (event, payload) the same way the frontend's SSE parser would, without
    duplicating any of _stream_chat's own turn/dedupe/retry logic."""
    event_line, _, rest = frame.partition("\n")
    event = event_line.split(": ", 1)[1] if ": " in event_line else event_line
    data_raw = rest.strip()
    if data_raw.startswith("data: "):
        data_raw = data_raw[len("data: "):]
    try:
        data = json.loads(data_raw)
    except (json.JSONDecodeError, TypeError):
        data = data_raw
    return event, data


def _format_guidance(payload: dict) -> str:
    lines = []
    if payload.get("priority_steps"):
        lines.append("*Priority steps:*")
        lines += [f"{i+1}. {s}" for i, s in enumerate(payload["priority_steps"])]
    if payload.get("do_not"):
        lines.append("\n*Do NOT:*")
        lines += [f"⚠️ {s}" for s in payload["do_not"]]
    if payload.get("watch_for"):
        lines.append("\n*Watch for:*")
        lines += [f"👀 {s}" for s in payload["watch_for"]]
    if payload.get("reassurance"):
        lines.append(f"\n{payload['reassurance']}")
    if payload.get("when_to_update_me"):
        lines.append(f"\n_Update me: {payload['when_to_update_me']}_")
    return "\n".join(lines)


def _format_quick_steps(payload: dict) -> str:
    steps = payload.get("quick_steps", [])
    return "*Do this now:*\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))


def _format_videos(payload: dict) -> str:
    lines = ["Helpful videos:"]
    for v in payload.get("videos", []):
        lines.append(f"▶️ {v.get('title', 'Video')} — {v.get('url', '')}")
    return "\n".join(lines)


async def _wa_run_turn(sender: str, session_id: str, request: ChatRequest, is_new: bool) -> None:
    """Consume _stream_chat's SSE frames and translate them into one or
    more WhatsApp messages. Token text is buffered and flushed as a single
    message right before any structured event (clarifying_question,
    guidance, quick_steps, videos), matching the same ordering the
    frontend renders (short ack text, then the structured card) without
    needing WhatsApp-side streaming, which doesn't exist."""
    buffer = ""

    async def flush():
        nonlocal buffer
        if buffer.strip():
            await _wa_send_text(sender, buffer.strip())
        buffer = ""

    async for frame in _stream_chat(session_id, request, is_new):
        event, payload = _parse_sse_frame(frame)
        if not isinstance(payload, dict):
            continue

        if event == "token":
            buffer += payload.get("text", "")

        elif event == "clarifying_question":
            await flush()
            options = payload.get("options", [])
            whatsapp_pending_options[sender] = options
            question = payload.get("question", "")
            if not options:
                await _wa_send_text(sender, question)
            elif len(options) <= 3:
                await _wa_send_buttons(sender, question, options)
            else:
                await _wa_send_option_list(sender, question, options)

        elif event == "quick_steps":
            await flush()
            await _wa_send_text(sender, _format_quick_steps(payload))

        elif event == "guidance":
            await flush()
            await _wa_send_text(sender, _format_guidance(payload))

        elif event == "videos":
            await flush()
            await _wa_send_text(sender, _format_videos(payload))

        elif event == "error":
            await flush()
            await _wa_send_text(sender, f"⚠️ {payload.get('message', 'Something went wrong.')}")

        elif event == "done":
            await flush()

        # turn_started / status / retrying / activity are internal —
        # deliberately not surfaced to WhatsApp, same as they're not
        # rendered as their own bubble in a typical frontend either.

    await flush()  # safety net in case `done` never fired (e.g. error path)


async def _wa_start_or_continue(sender: str, message: str, location: dict | None = None) -> None:
    """Builds the same ChatRequest /chat would build from a POST body, then
    drives it through _wa_run_turn. `location` is only required on a
    session's first turn — subsequent turns reuse whatever's cached on the
    session (same as how the frontend only needs to resend it if it
    changed)."""
    session_id = whatsapp_sessions.get(sender)
    is_new = session_id is None or session_id not in sessions

    if is_new:
        if location is None:
            # Shouldn't happen if callers check first, but fail safe rather
            # than crash a background task.
            await _wa_send_text(sender, "Please share your location first so I can find nearby hospitals.")
            return
        session_id = str(uuid.uuid4())
        whatsapp_sessions[sender] = session_id
        sessions[session_id] = {
            "thread_id":         session_id,
            "location":          location,
            "patient_profile":   PatientProfile().model_dump(),
            "web_task_ids":      [],
            "youtube_task_ids":  [],
            "hospital_task_ids": [],
            "subagent_status":   {},
            "selected_hospital": None,
        }
    else:
        if location is not None:
            sessions[session_id]["location"] = location

    req = ChatRequest(
        session_id=session_id,
        message=message,
        location=Location(**sessions[session_id]["location"]),
        patient_profile=PatientProfile(**sessions[session_id]["patient_profile"]),
    )

    await _wa_send_text(sender, "On it — finding help now…")
    await _wa_run_turn(sender, session_id, req, is_new)


# Tight, explicit set of common openers only — deliberately NOT a generic
# "short message" heuristic. A misclassified real emergency ("help now" /
# "he's not breathing") costs a full turn during a crisis, so this only
# matches when the WHOLE message is essentially just a greeting; anything
# it doesn't confidently recognize falls through to the emergency path by
# default (false negatives here are the safe direction, not false
# positives).
_GREETING_RE = re.compile(
    r"^\s*(hi+|hello+|hey+|yo|sup|howdy|greetings|hiya|"
    r"good\s?(morning|afternoon|evening|day))[\s!.,]*$",
    re.IGNORECASE,
)
_GREETING_MAX_LEN = 40  # a bare greeting is short; an emergency description generally isn't


def _looks_like_greeting(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or len(stripped) > _GREETING_MAX_LEN:
        return False
    return bool(_GREETING_RE.match(stripped))


async def _wa_handle_text(sender: str, text: str) -> None:
    """Called with the sender's lock held. Matches an inbound text message
    against any location that arrived first, so the two combine into one
    agent turn no matter which order they came in.

    The very first message can reasonably be either a greeting ("hi") or
    the emergency itself ("my father was stabbed") — distressed users
    typically skip pleasantries, so this defaults to treating a first
    message as the emergency description unless it's unambiguously just a
    greeting (see _looks_like_greeting)."""
    if whatsapp_sessions.get(sender) is not None:
        # ongoing conversation — just a normal follow-up turn
        await _wa_start_or_continue(sender, text)
        return

    pending_location = whatsapp_pending_location.pop(sender, None)
    if pending_location is not None:
        # location was shared first and was waiting on a description —
        # this text completes it, start the turn now. (Not greeting-checked
        # here: they were already explicitly asked what's happening, so
        # whatever they send back is treated as that answer.)
        await _wa_start_or_continue(sender, text, location=pending_location)
        return

    if _looks_like_greeting(text):
        # Bare "hi" with nothing else stashed yet — don't jump straight to
        # asking for location off a plain greeting, and deliberately do NOT
        # stash it into whatsapp_pending_message: "hi" must never become
        # the text that gets combined with a location a moment later as if
        # it were the emergency description.
        await _wa_send_text(
            sender,
            "👋 Hi, this is *MedicAI* — your emergency first-aid assistant. "
            "Tell me what's happening (what happened, symptoms, who it's "
            "for) and I'll help right away.",
        )
        return

    # Reads as an actual description of what's wrong — stash it and ask for
    # location, referencing that we've got the description so the prompt
    # doesn't feel like a generic, unrelated form field right after someone
    # describes an emergency. Only nag once per wait rather than on every
    # message that comes in.
    already_waiting = sender in whatsapp_pending_message
    whatsapp_pending_message[sender] = text
    if not already_waiting:
        await _wa_send_text(
            sender,
            "Got it — to find the nearest help, please share your location "
            "(📎 → Location).",
        )


async def _wa_handle_location(sender: str, location: dict) -> None:
    """Called with the sender's lock held. Matches an inbound location
    share against any text that arrived first, so the two combine into one
    agent turn no matter which order they came in."""
    if whatsapp_sessions.get(sender) is not None:
        # ongoing conversation — refresh the stored location, but don't
        # kick off a fresh agent turn off a bare location share
        session_id = whatsapp_sessions[sender]
        sessions[session_id]["location"] = location
        await _wa_send_text(sender, "📍 Location updated.")
        return

    pending_text = whatsapp_pending_message.pop(sender, None)
    if pending_text is not None:
        # text was sent first and was waiting on a location — this
        # completes it, start the turn now
        await _wa_start_or_continue(sender, pending_text, location=location)
        return

    # location arrived with nothing to attach it to — stash it and ask
    # what's actually going on, instead of silently starting a turn with a
    # placeholder ("Emergency — see shared location.") that tells the
    # agent nothing about the real emergency
    already_waiting = sender in whatsapp_pending_location
    whatsapp_pending_location[sender] = location
    if not already_waiting:
        await _wa_send_text(
            sender,
            "📍 Got your location. Please describe what's happening (symptoms, what happened, who it's for, etc.) so I can help.",
        )


async def _wa_handle_hospital_reply(sender: str, hospital_id: str, title: str, description: str) -> None:
    session_id = whatsapp_sessions.get(sender)
    if not session_id or session_id not in sessions:
        await _wa_send_text(sender, "I've lost track of your session — please send your emergency again to restart.")
        return
    selection = HospitalSelection(id=hospital_id, name=title, address=description or None)
    result = await select_hospital(session_id, selection)
    data = json.loads(result.body)
    eta = data["selected_hospital"].get("eta_minutes")
    eta_text = f"~{eta:.0f} min away" if isinstance(eta, (int, float)) else "ETA unavailable"
    await _wa_send_text(sender, f"Got it — heading to *{title}*. {eta_text}. I'll keep tracking this for you.")


def _wa_verify_signature(payload_body: bytes, signature_header: str) -> bool:
    if not signature_header or not WHATSAPP_APP_SECRET:
        return False
    expected = hmac.new(WHATSAPP_APP_SECRET.encode(), payload_body, hashlib.sha256).hexdigest()
    received = signature_header.replace("sha256=", "")
    return hmac.compare_digest(expected, received)


async def _wa_process_payload(payload: dict) -> None:
    """Runs as a BackgroundTask. Must be `async def` — FastAPI runs sync
    background functions in a worker thread with no event loop, which is
    exactly what broke `asyncio.create_task(...)` here before (RuntimeError:
    no running event loop). Being async means BackgroundTasks awaits this
    directly on the main event loop instead, so awaiting _wa_dispatch_message
    below is safe.

    By the time this executes, the webhook route has already returned its
    200, so nothing in here can make Meta think delivery failed / retry."""
    messages = [
        message
        for entry in payload.get("entry", [])
        for change in entry.get("changes", [])
        for message in change.get("value", {}).get("messages", [])
    ]
    # Multiple messages in one batch are handled concurrently rather than
    # one-by-one, so a slow agent turn for one sender doesn't delay the
    # webhook's internal processing of another sender's message.
    await asyncio.gather(
        *(_wa_dispatch_message(message) for message in messages),
        return_exceptions=True,
    )


async def _wa_dispatch_message(message: dict) -> None:
    msg_id = message.get("id")
    if not msg_id or msg_id in seen_whatsapp_message_ids:
        return  # dedup — Meta retries deliveries that don't get a fast 200
    seen_whatsapp_message_ids.add(msg_id)

    sender   = message["from"]
    msg_type = message.get("type")

    try:
        if msg_type == "text":
            text = message["text"]["body"]
            async with _wa_lock(sender):
                await _wa_handle_text(sender, text)

        elif msg_type == "location":
            loc = message["location"]
            location = {
                "lat":     loc["latitude"],
                "lng":     loc["longitude"],
                "address": loc.get("address") or loc.get("name") or "Shared via WhatsApp",
            }
            async with _wa_lock(sender):
                await _wa_handle_location(sender, location)

        elif msg_type == "interactive":
            interactive  = message.get("interactive", {})
            reply        = interactive.get("button_reply") or interactive.get("list_reply")
            if not reply:
                return
            reply_id = reply.get("id", "")

            if reply_id.startswith("hosp_"):
                await _wa_handle_hospital_reply(
                    sender,
                    hospital_id=reply_id[len("hosp_"):],
                    title=reply.get("title", "Selected hospital"),
                    description=reply.get("description", ""),
                )
            elif reply_id.startswith("q_"):
                idx = int(reply_id[len("q_"):])
                options = whatsapp_pending_options.get(sender, [])
                full_text = options[idx] if 0 <= idx < len(options) else reply.get("title", "")
                await _wa_start_or_continue(sender, full_text)

        # other types (image, audio, unsupported) are silently ignored
        # for now — flag to the user rather than dropping silently:
        else:
            await _wa_send_text(sender, "I can only read text and shared location right now.")

    except Exception:
        logger.exception("WhatsApp message handling failed (sender=%s)", sender)
        await _wa_send_text(sender, "Something went wrong on our end — please try again.")


# ════════════════════════════════════════════════════════════════════════════
#  WHATSAPP — webhook routes
# ════════════════════════════════════════════════════════════════════════════

@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    """Meta hits this once, whenever the Callback URL / Verify token
    fields are (re)saved in the App Dashboard."""
    if (
        request.query_params.get("hub.mode") == "subscribe"
        and request.query_params.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN
    ):
        return PlainTextResponse(request.query_params.get("hub.challenge", ""), status_code=200)
    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/webhook/whatsapp")
async def whatsapp_receive(request: Request, background_tasks: BackgroundTasks):
    """Every inbound WhatsApp event lands here. Must return 200 fast —
    the actual agent turn (which can take several seconds) runs as a
    background task so Meta never times out waiting on it and retries."""
    raw_body = await request.body()
    if not _wa_verify_signature(raw_body, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(raw_body)
    background_tasks.add_task(_wa_process_payload, payload)
    return Response(status_code=200)


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
      clarifying_question — { source, question, options: [...], context,
                     suggested_replies: [...] }
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
                     `suggested_replies` is a separate list of 2-4 short,
                     natural-language reply shortcuts contextual to THIS
                     question (not a restatement of `options`) — meant as
                     tappable shortcuts for the free-text input rather than
                     the button row. Can be empty; never fabricate a
                     fallback if so.
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
                     watch_for: [...], reassurance, when_to_update_me,
                     suggested_replies: [...] }
                     sent ONCE, live, straight from
                     assemble_first_aid_response's structured tool result —
                     not re-derived from the model's prose, and no longer
                     carries a `narrative` field itself (that's popped off
                     and sent as the preceding `token` event instead — see
                     above). Map each field to its own visual treatment
                     (numbered steps, a "do not" callout, a "watch for"
                     callout, etc.) instead of parsing markdown headers out
                     of token text. `suggested_replies` is 2-4 short,
                     natural-language reply shortcuts contextual to
                     `when_to_update_me` and the current situation (e.g.
                     "He's breathing better now", "No change yet") — same
                     per-turn, non-accumulating shortcut list as on
                     `clarifying_question`, just without a fixed `options`
                     counterpart here. Can be empty.
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
            "selected_hospital": None,
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


@app.post("/session/hospitals/{session_id}/select")
async def select_hospital(session_id: str, selection: HospitalSelection):
    """
    Record which hospital the user has chosen to head to.

    Call this when the user taps a hospital from the nearby list returned
    by GET /session/hospitals/{session_id}. If the frontend has its own
    travel-time calculation for that hospital (e.g. from a maps/
    directions API against the user's live location), pass it as
    eta_minutes — that's used as-is. Otherwise a crude straight-line
    estimate is computed server-side as a fallback. Either way, this
    stores a selection timestamp so the supervisor agent can answer
    "how long until we get there?" on later turns — it reads this back
    via the [SELECTED_HOSPITAL] block injected into its context on every
    subsequent /chat call, and re-derives the *current* remaining time
    (accounting for elapsed time) itself.

    No ambulance/medical service is dispatched here — this only tells the
    agent which hospital the user is now heading to on their own.

    Body: { id, name, address?, distance_km?, contact?, eta_minutes? }

    Response:
    {
      "status": "ok",
      "selected_hospital": {
        id, name, address, distance_km, contact,
        "eta_minutes":  <frontend-supplied, or straight-line fallback>,
        "selected_at":  "<ISO timestamp>"
      }
    }
    """
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    selected = selection.model_dump()
    if selected.get("eta_minutes") is None:
        selected["eta_minutes"] = estimate_eta_minutes(selection.distance_km)
    selected["selected_at"] = datetime.now(timezone.utc).isoformat()

    session["selected_hospital"] = selected

    return JSONResponse({"status": "ok", "selected_hospital": selected})


@app.post("/session/hospitals/{session_id}/eta")
async def refresh_hospital_eta(session_id: str, update: EtaUpdate):
    """
    Push a refreshed travel-time calculation for the already-selected
    hospital, without changing which hospital is selected.

    Use this as the user travels and the frontend recomputes ETA against
    their live location (traffic conditions change, they may take a
    different route, etc.). This resets the "selected_at" anchor to now,
    so the supervisor's elapsed-time decay restarts from this fresh
    number rather than compounding against the original estimate.

    404s if no hospital has been selected yet for this session — select
    one first via POST /session/hospitals/{session_id}/select.
    """
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    if not session.get("selected_hospital"):
        raise HTTPException(status_code=404, detail="No hospital selected for this session yet.")

    session["selected_hospital"]["eta_minutes"] = update.eta_minutes
    session["selected_hospital"]["selected_at"] = datetime.now(timezone.utc).isoformat()

    return JSONResponse({"status": "ok", "selected_hospital": session["selected_hospital"]})


@app.get("/session/hospitals/{session_id}/selected")
async def get_selected_hospital(session_id: str):
    """Get the hospital the user has selected for this session, if any."""
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    return JSONResponse({"selected_hospital": session.get("selected_hospital")})


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
