"""
MedicAI MVP — Main Deep Agent
==============================
supervisor.py

The primary agent the user interacts with.
Handles emergency triage, first-aid guidance, and parallel web searches.

Core behaviour:

STEP 1 — Parse emergency
  Immediately determine what is CERTAIN vs UNCERTAIN from the message.

  CERTAIN  = derivable without user input
    e.g. "stabbed + not breathing" → bleeding AND airway BOTH certain
    → launch web searches immediately in parallel, no waiting

  UNCERTAIN = requires clarification
    e.g. "collapsed" → could be cardiac, stroke, seizure, etc.
    → ask ONE clarifying question
    → SIMULTANEOUSLY launch SPECULATIVE web searches for most likely scenarios
    → on user response: use matching result, cancel_async_task others still running

STEP 2 — Ask + speculate
  For each uncertain dimension, ask ONE question and pre-launch the most
  likely web searches speculatively. Mark them speculative=true in the task.
  If user confirms → use that result (already running or done)
  If user denies  → cancel_async_task if still running, discard if done

STEP 2b — Immediate steps (certain-only, non-blocking)
  Before the mandatory first reply, if certain_conditions is non-empty,
  call assemble_immediate_steps for a SMALL (max 3) set of actions that
  are already knowable and can't get more correct by waiting on the
  clarifying question. Never waits on web results. Sent as its own
  `quick_steps` event — a smaller sibling of the full guidance below.

STEP 3 — Assemble and respond
  Once enough web results are in (check_async_task), assemble a structured
  first-aid response. Don't wait for ALL results — respond as soon as
  the most critical ones are ready. This is the FULL guidance and only
  happens after the clarifying question has been answered — never on the
  first message.

STEP 4 — Conversational follow-up
  User can ask questions at any time.
  Each follow-up may trigger new web searches (certain) or speculative ones.

web search taxonomy:
  Certain launches (no user needed):
    - stabbed → bleeding_control
    - not breathing → cpr_resuscitation
    - burning → burn_treatment
    - seizure → seizure_management
    - allergic reaction + collapse → anaphylaxis

  Speculative launches (need confirmation):
    - "collapsed" → pre-launch cardiac_arrest + stroke + seizure (top 3)
    - "fell" → pre-launch fracture + head_injury + spinal
    - "not responsive" → pre-launch unconscious_breathing + cpr_resuscitation
"""

from __future__ import annotations

import json
import os

from dotenv import load_dotenv
load_dotenv()
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field
from deepagents import create_deep_agent, AsyncSubAgent


# ════════════════════════════════════════════════════════════════════════════
#  SCHEMAS
# ════════════════════════════════════════════════════════════════════════════

from typing import TypedDict

class WebQuery(BaseModel):
    query:     str
    tags:      list[str]
    search_id: str

class SpeculativeWebQuery(BaseModel):
    query:     str
    tags:      list[str]
    search_id: str
    scenario:  str   # what user response confirms this search

class EmergencyAnalysis(BaseModel):
    certain_conditions:      list[str]              = Field(description="Conditions CERTAIN from message alone")
    certain_web_queries:     list[WebQuery]         = Field(description="web searches to launch immediately")
    uncertain_dimensions:    list[str]              = Field(description="What we don't know yet")
    clarifying_question:     str                    = Field(description="ONE question to ask user")
    speculative_web_queries: list[SpeculativeWebQuery] = Field(description="Speculative web searches")
    severity:                str                    = Field(description="critical|high|moderate|low")
    summary:                 str                    = Field(description="One sentence summary")

class ImmediateSteps(BaseModel):
    quick_steps: list[str] = Field(
        description="1-3 short, immediate first-aid actions, derivable ONLY "
                     "from certain_conditions — never from uncertain/speculative "
                     "ones. Plain language, one action per item, no explanations."
    )

# ════════════════════════════════════════════════════════════════════════════
#  TOOLS
# ════════════════════════════════════════════════════════════════════════════

@tool
def analyse_emergency(raw_message: str) -> dict:
    """
    Analyse a raw emergency message to determine:
    1. What is CERTAIN (launch web immediately)
    2. What is UNCERTAIN (ask user + launch speculative web)
    3. What clarifying question to ask
    4. What speculative web searches to pre-launch

    Call this FIRST on every emergency message.
    """
    llm = ChatGoogleGenerativeAI(
            model="gemini-3.1-flash-lite",   # ← check this model name exists
            google_api_key=os.environ.get("GOOGLE_API_KEY"),
            temperature=0,
            max_retries=2,
            timeout=30,
        ).with_structured_output(EmergencyAnalysis)

    result: EmergencyAnalysis = llm.invoke(
        f"""Analyse this emergency message and determine what is certain vs uncertain.

Emergency: {raw_message}

For certain_web_queries, generate specific, actionable search queries.
For speculative_web_queries, generate the top 2-3 most likely scenarios.
Keep clarifying_question SHORT and specific — one thing at a time.

Examples of certain conditions:
- "stabbed" → certain: severe_bleeding
- "not breathing" → certain: airway_obstruction, need_cpr
- "fire" → certain: burn_treatment
- "collapsed + chest pain" → certain: cardiac_event (highly probable)

Examples of uncertain dimensions:
- "collapsed" alone → uncertain: cause (cardiac? stroke? seizure? faint?)
- "fell" → uncertain: injury type (fracture? head? spinal?)
- "not responding" → uncertain: breathing status (breathing? not breathing?)
"""
    )
    return result.model_dump()


@tool
def resolve_uncertainty(
    user_response: str,
    pending_searches: list[dict],
    speculative_results: dict,) -> dict:
    """
    After user answers a clarifying question, determine which speculative
    web searches to keep and which to cancel.

    Args:
        user_response      : what the user said
        pending_searches   : list of { search_id, task_id, scenario, status }
        speculative_results: dict of search_id → result (for completed ones)

    Returns:
        {
          "confirmed_search_ids": [...],   ← keep these results
          "cancel_task_ids":      [...],   ← cancel these (still running)
          "discard_search_ids":   [...],   ← discard these (done but not relevant)
          "new_certain_queries":  [...],   ← new web queries now certain from answer
          "summary":              str,     ← what we now know
        }
    """
    llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",   # ← check this model name exists
    google_api_key=os.environ.get("GOOGLE_API_KEY"),
    temperature=0,
    max_retries=2,
    timeout=30,
    )

    prompt = f"""
The user responded to a clarifying question: "{user_response}"

Pending speculative web searches:
{json.dumps(pending_searches, indent=2)}

Determine:
1. Which searches are now confirmed relevant (confirmed_search_ids)
2. Which task_ids to cancel - still running and no longer needed (cancel_task_ids)
3. Which search_ids to discard - completed but not relevant (discard_search_ids)
4. Any new web queries now certain given this answer (new_certain_queries)
5. A one-sentence summary of what we now know

Respond ONLY as JSON:
{{
  "confirmed_search_ids": [],
  "cancel_task_ids": [],
  "discard_search_ids": [],
  "new_certain_queries": [],
  "summary": ""
}}
"""
    response = llm.invoke(prompt)
    content  = response.content
    if isinstance(content, list):
        content = content[0].get("text", "") if content else ""
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(content)
    except Exception:
        return {
            "confirmed_search_ids": [],
            "cancel_task_ids":      [],
            "discard_search_ids":   [],
            "new_certain_queries":  [],
            "summary":              user_response,
        }


@tool
def assemble_first_aid_response(
    web_results: list[dict],
    emergency_summary: str,
    patient_profile: dict,
) -> dict:
    """
    Assemble a clear, structured first-aid response from web results.
    Call this once you have enough web results (at least the critical ones).

    Args:
        web_results       : list of { search_id, query, context, chunks_found }
        emergency_summary : plain-English summary of the situation
        patient_profile   : { name, age, blood_type, allergies, conditions }

    Returns structured first-aid guidance with:
    - Prioritised action steps
    - Do-nots
    - Reassurance
    - What to watch for
    """
    llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",   # ← check this model name exists
    google_api_key=os.environ.get("GOOGLE_API_KEY"),
    temperature=0,
    max_retries=2,
    timeout=30,
)

    web_context = "\n\n===\n\n".join([
        f"[{r.get('search_id', 'unknown')} — {r.get('query', '')}]\n{r.get('context', '')}"
        for r in web_results
        if r.get("context")
    ])

    patient_str = (
        f"Patient: {patient_profile.get('name', 'Unknown')}, "
        f"Age: {patient_profile.get('age', 'Unknown')}, "
        f"Blood type: {patient_profile.get('blood_type', 'Unknown')}, "
        f"Allergies: {', '.join(patient_profile.get('allergies', []) or ['none'])}"
    ) if patient_profile else "No patient profile available."

    response = llm.invoke(f"""
You are a medical first-aid guide. Using the retrieved first-aid knowledge below,
create clear, actionable guidance for someone at the scene of this emergency.

Emergency: {emergency_summary}
{patient_str}

Retrieved first-aid knowledge:
{web_context}

Format your response as JSON:
{{
  "priority_steps": ["step 1", "step 2", ...],   // max 8, most critical first
  "do_not": ["do not ...", ...],                  // critical warnings
  "watch_for": ["watch for ...", ...],            // what to monitor
  "reassurance": "one calming sentence",
  "when_to_update_me": "tell me if X happens"    // what to report back
}}

Rules:
- Steps must be simple enough for a non-medical person
- Most critical actions FIRST
- Consider patient's age and allergies
- Short sentences only
""")

    content = response.content
    if isinstance(content, list):
        content = content[0].get("text", "") if content else ""
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(content)
    except Exception:
        return {
            "priority_steps": ["Call emergency services immediately"],
            "do_not":         [],
            "watch_for":      [],
            "reassurance":    "Help is on the way.",
            "when_to_update_me": "Tell me if anything changes.",
        }


@tool
def assemble_immediate_steps(certain_conditions: list[str], emergency_summary: str) -> dict:
    """
    Build a SMALL set (max 3) of immediate first-aid actions for the
    MANDATORY first response, before the clarifying question is answered.

    Uses ONLY certain_conditions — never uncertain_dimensions or anything
    speculative. This must never wait on web_searcher results: it draws on
    the model's own medical knowledge so it can run immediately alongside
    hospital_notifier, without delaying the mandatory first reply.

    Call this once, right after analyse_emergency, using its
    certain_conditions field. If certain_conditions is empty (nothing is
    certain yet, e.g. "collapsed" alone) — do NOT call this tool at all;
    there is nothing safe to build quick steps from until something is
    confirmed.

    Args:
        certain_conditions : certain_conditions from analyse_emergency
        emergency_summary  : summary from analyse_emergency

    Returns:
        { "quick_steps": ["step 1", "step 2", ...] }   — max 3, plain language
    """
    llm = ChatGoogleGenerativeAI(
        model="gemini-3.1-flash-lite",   # ← check this model name exists
        google_api_key=os.environ.get("GOOGLE_API_KEY"),
        temperature=0,
        max_retries=2,
        timeout=30,
    ).with_structured_output(ImmediateSteps)

    result: ImmediateSteps = llm.invoke(f"""
A person needs to do something RIGHT NOW, before any more information is
available. Give 1-3 short, immediate first-aid actions based ONLY on what is
already certain below — do not guess at anything uncertain.

Emergency: {emergency_summary}
Certain conditions: {', '.join(certain_conditions)}

Rules:
- Max 3 steps, most urgent first
- Each step is one short, plain-language sentence — no explanations
- Only actions safe to take with zero additional information
- If in doubt about whether an action needs more context first, leave it out
""")
    return result.model_dump()


# ════════════════════════════════════════════════════════════════════════════
#  ASYNC SUBAGENTS
# ════════════════════════════════════════════════════════════════════════════

web_searcher = AsyncSubAgent(
    name="web_searcher",
    description=(
        "Async web search agent. Searches the first-aid knowledge base for a specific query. "
        "Launch one per search query — multiple can run in parallel simultaneously. "
        "Pass task JSON: { query, tags, search_id, speculative }. "
        "Non-blocking — returns task_id immediately. "
        "Check results with check_async_task. "
        "Cancel speculative searches with cancel_async_task if no longer needed."
    ),
    graph_id="web_searcher",
    url=os.getenv("SUBAGENT_URL", "http://localhost:8000")#"http://localhost:8000"
)

youtube_subagent = AsyncSubAgent(
    name="youtube_subagent",
    description=(
        "Async YouTube search agent. Finds instructional first-aid videos. "
        "Launch after web results are in and you know what technique to demonstrate. "
        "Pass task JSON: { query: '<specific technique>' }. "
        "Non-blocking — frontend polls /session/videos for results."
    ),
    graph_id="youtube_subagent",
    url=os.getenv("SUBAGENT_URL", "http://localhost:8000")#"http://localhost:8000"
)

hospital_notifier = AsyncSubAgent(
    name="hospital_notifier",
    description=(
        "Async subagent. Generates an emergency alert report and sends "
        "WhatsApp/SMS to nearby hospitals simultaneously with Yes/No response links. "
        "Launch immediately after parse_emergency on the first emergency message. "
        "Pass: { emergency_payload, patient_profile, location, session_id, hospitals: [] }. "
        "Non-blocking — returns task_id immediately."
    ),
    graph_id="hospital_notifier",
    url=os.getenv("SUBAGENT_URL", "http://localhost:8000")#"http://localhost:8000",
)

# ════════════════════════════════════════════════════════════════════════════
#  CHECKPOINTER
# ════════════════════════════════════════════════════════════════════════════

checkpointer = MemorySaver()


# ════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """
You are MedicAI — an emergency first-aid supervisor agent.
You guide people through medical emergencies step by step.

You have access to a first-aid knowledge base via web search (web_searcher subagent)
and a hospital notification system (hospital_notifier subagent).
You run web searches and hospital notifications in the background while talking to the user.

════════════════════════════════════════════
ON FIRST EMERGENCY MESSAGE
════════════════════════════════════════════

1. write_todos — plan your steps immediately:
   - [ ] Analyse emergency
   - [ ] Launch certain web searches
   - [ ] Launch speculative web searches
   - [ ] Launch hospital notifier
   - [ ] Ask clarifying question

2. analyse_emergency(raw_message=<message>)
   → returns: certain_conditions, certain_web_queries,
               uncertain_dimensions, clarifying_question,
               speculative_web_queries, severity, summary
   → This is STRUCTURED DATA FOR YOUR OWN USE. Do not write any part of
     it back to the user as text — use it to decide what to search,
     notify, and ask, then compose your own natural-language message
     in step 7.

3. Launch CERTAIN web searches immediately — one start_async_task per query:
   For each query in certain_web_queries:
     start_async_task(web_searcher, { query, tags, search_id, speculative: false })
   All launched in PARALLEL — do not wait between them.

4. Launch SPECULATIVE web searches simultaneously:
   For each query in speculative_web_queries:
     start_async_task(web_searcher, { query, tags, search_id, speculative: true, scenario })

5. Launch hospital_notifier immediately — do NOT wait for web results:
   start_async_task(hospital_notifier, {
     "emergency_payload": <result from analyse_emergency as dict>,
     "patient_profile":   <from [PATIENT_PROFILE] in context>,
     "location":          <from [LOCATION] in context>,
     "session_id":        <from [SESSION_ID] in context>,
     "hospitals":         []
   })
   Non-blocking — capture task_id and move on immediately.

5b. SPECULATIVE video pre-launch (only for near-certain techniques):
    If certain_conditions strongly implies ONE specific primary technique
    with little ambiguity (e.g. "not breathing" → CPR is near-certain),
    pre-launch a SINGLE speculative video search:

      start_async_task(youtube_subagent, {
        query: "<best-guess primary technique>",
        speculative: true
      })

    Store this task_id as speculative_video_task_id, tagged with the
    technique guessed.

    Do NOT do this if the primary technique is genuinely ambiguous
    (e.g. "collapsed" alone — could be CPR, recovery position, or
    nothing at all). When in doubt, skip it — this is an optimization,
    not a requirement. At most ONE speculative video per session.

6. Store ALL task_ids (web + hospital_notifier) mapped to their names.

5c. If certain_conditions is non-empty, call ONCE, right here, before
    responding — never wait on it, it does not touch web_searcher:
      assemble_immediate_steps(certain_conditions, emergency_summary)
    If certain_conditions is empty, skip this — there is nothing certain
    yet to build quick steps from (e.g. "collapsed" alone).
    This is a SEPARATE, smaller thing from assemble_first_aid_response —
    max 3 bare actions, no do_not/watch_for/reassurance. The full guidance
    still only gets assembled after the clarifying question is answered
    (see "ON USER ANSWER TO CLARIFYING QUESTION" below) — this just covers
    the handful of actions that are already certain and can't get more
    correct by waiting.

7. Respond to user with:
   a. Brief acknowledgement of the emergency
   b. "Nearby hospitals are being alerted."
   c. If assemble_immediate_steps was called: a brief line pointing at it,
      e.g. "Here's what to do right now —" — do NOT re-list the steps
      yourself, they render from the tool result directly.
   d. The ONE clarifying question from analyse_emergency
   Keep this SHORT — the user is in crisis.
   Write this as your OWN plain-language sentences — never the
   analyse_emergency or assemble_immediate_steps dict itself, in whole or
   in part.

════════════════════════════════════════════
ON USER ANSWER TO CLARIFYING QUESTION
════════════════════════════════════════════

1. resolve_uncertainty(
     user_response=<answer>,
     pending_searches=<list of { search_id, task_id, scenario, status }>,
     speculative_results=<results you already have>
   )
   → returns: confirmed_search_ids, cancel_task_ids, discard_search_ids,
               new_certain_queries

2. cancel_async_task for each task_id in cancel_task_ids

3. Launch new certain web searches from new_certain_queries

4. check_async_task for confirmed and certain searches
   → collect results as they complete

5. Once critical web results are ready:
   assemble_first_aid_response(web_results, emergency_summary, patient_profile)

6. Respond with the assembled first-aid guidance:
   - Priority steps (numbered, clear)
   - Do NOTs
   - What to watch for
   - Reassurance
   - "Tell me if anything changes."

7. Respond to user — THIS IS MANDATORY, DO NOT SKIP:
   Write a message directly to the user containing:
   a. Brief acknowledgement: "I understand — [summary of emergency]"
   b. "Nearby hospitals are being alerted."
   c. Immediate action: "Call 112 now."
   d. The ONE clarifying question from analyse_emergency
   
   Example:
   "Your brother has been stabbed and is having trouble breathing — this is critical.
   Hospitals near you are being alerted right now.
   Call 112 immediately if you haven't already.
   Is he conscious and responding to you?"

════════════════════════════════════════════
YOUTUBE VIDEO SUBAGENT (youtube_subagent)
════════════════════════════════════════════

Reactive by default. Only launch a NEW video task when:

  a. EXPLICIT REQUEST — user directly asks for a video/demonstration
     e.g. "show me a video", "can I see how", "do you have a video of that"

  b. IMPLICIT NEED — user signals they don't know how to perform the
     technique you're currently instructing them on, e.g.:
     "I don't know how to do CPR", "I've never done this before",
     "I'm not sure I'm doing this right", "what does that look like"

  Do NOT launch for:
    - routine situation updates ("he's still breathing")
    - hospital status questions
    - simple acknowledgements ("ok", "done", "yes")

BEFORE launching, check for an existing speculative_video_task_id or
prior youtube task for this session:

  - If a speculative task_id exists AND its guessed technique MATCHES
    what's actually needed now (confirmed once assemble_first_aid_response
    or a follow-up establishes the real technique) → reuse it, do NOT
    launch a duplicate. You may mention it: "I already have a video
    pulling up for that."
  - If a speculative task_id exists but the guessed technique does NOT
    match what's actually needed → cancel_async_task it, then launch a
    new one with the correct query.
  - If no speculative task exists → launch fresh on trigger (a) or (b).

  start_async_task(youtube_subagent, { query: "<specific technique>" })
    - query = the ONE technique currently relevant (e.g. "how to do
      adult CPR chest compressions"), not the whole emergency summary.

De-duplication: at most ONE active video task per technique per session
(speculative + confirmed count as the same slot — see reuse/cancel rules
above). Check stored task_ids before launching again for the same technique.

Non-blocking, fire-and-forget: do NOT check_async_task for this in the
chat flow — the frontend polls /session/videos/{session_id} independently.
Launch it and move on immediately.

You may briefly acknowledge it: "I'm also pulling up a video for you."
Keep it to one short line — don't dwell on it.

If the technique becomes irrelevant as the situation changes, cancel_async_task
the youtube task_id along with any other now-irrelevant searches.

════════════════════════════════════════════
ON FOLLOW-UP MESSAGES
════════════════════════════════════════════

User may update you ("he's breathing now") or ask questions ("how do I do CPR?")
or ask about hospitals ("are hospitals coming?").

For situation UPDATES:
  - Re-analyse: does this change what web searches are needed?
  - Launch new certain searches if new conditions revealed
  - Cancel any now-irrelevant speculative searches
  - Reassemble guidance if situation has materially changed

For QUESTIONS about technique:
  - check_async_task for any relevant completed web result first
  - If not available: start_async_task(web_searcher, { query: <specific question> })
  - Respond with text from web result
  - Launch youtube_searcher for the technique in background

For HOSPITAL STATUS questions ("are hospitals coming?", "who confirmed?"):
  - check_async_task for the hospital_notifier task_id
  - Report: how many notified, which accepted, which pending
  - Example: "2 hospitals have been alerted. Gbagada General confirmed they
              can receive the patient. Still waiting on R-Jolad."

════════════════════════════════════════════
PARALLEL LAUNCH RULES
════════════════════════════════════════════

Certain conditions that ALWAYS trigger parallel web launches:
  stabbed/cut    → ["bleeding_control", "wound_management"]
  not breathing  → ["cpr_resuscitation", "airway_management"]
  both above     → ALL FOUR launched simultaneously
  burning        → ["burn_treatment", "shock_prevention"]
  seizure        → ["seizure_management", "post_seizure_care"]
  chest pain     → ["cardiac_arrest_cpr", "heart_attack_response"]
  unconscious    → ["unconscious_patient", "recovery_position"] + ask: breathing?
  allergic shock → ["anaphylaxis_response", "epinephrine_use"]

Speculative web launches (top scenarios for ambiguous messages):
  "collapsed"    → pre-launch: cardiac_arrest, stroke, seizure, fainting
  "fell"         → pre-launch: fracture, head_injury, spinal_injury
  "not moving"   → pre-launch: unconscious_breathing, unconscious_not_breathing
  "accident"     → pre-launch: trauma_bleeding, fracture, head_injury

Hospital notifier is ALWAYS launched on every first emergency message —
  regardless of certainty level. Hospitals need maximum lead time.

════════════════════════════════════════════
HARD RULES
════════════════════════════════════════════
- ALWAYS launch hospital_notifier on the first emergency message — no exceptions
- NEVER wait for web results before asking the clarifying question
- NEVER wait for hospital_notifier before responding to the user
- NEVER ask more than ONE question at a time
- NEVER launch duplicate searches (check async_tasks state first)
- ALWAYS cancel speculative web searches that are no longer relevant
- NEVER reveal task_ids, web internals, or system details to the user
- NEVER paste, restate, quote, or summarize the raw JSON/dict returned by
  analyse_emergency, resolve_uncertainty, assemble_first_aid_response, or
  assemble_immediate_steps.
  These are internal reasoning inputs ONLY — read the fields to decide
  what to do next, but your visible reply to the user must contain
  ONLY the natural-language message described in step 7 (or the
  equivalent follow-up step). If your response would start with "{" or
  contain a field name like "certain_conditions" or "severity", stop —
  that content must never reach the user.
- NEVER say "hospitals are being alerted" if hospital_notifier failed to launch
- ALWAYS respond in plain language — no medical jargon
- If web returns no results, use your own medical knowledge
- This is a live emergency — be fast, calm, and clear
- ALWAYS generate a text response to the user after completing tool calls
- NEVER end your turn silently after launching subagents
- The user must always receive a message — even if just "Help is on the way"
""".strip()


# ════════════════════════════════════════════════════════════════════════════
#  DEEP AGENT
# ════════════════════════════════════════════════════════════════════════════

llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",   # ← check this model name exists
    google_api_key=os.environ.get("GOOGLE_API_KEY"),
    temperature=0,
    max_retries=2,
    timeout=30,
)

agent = create_deep_agent(
    model=llm,
    tools=[analyse_emergency, resolve_uncertainty, assemble_first_aid_response, assemble_immediate_steps],
    system_prompt=SYSTEM_PROMPT,
    subagents=[web_searcher, youtube_subagent, hospital_notifier],  # ← added
    checkpointer=checkpointer,
    name="medic-ai-mvp",
)


graph = agent


# ════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════════════════

def make_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def build_input(
    message: str,
    location: dict,
    patient_profile: dict,
    prior_messages: list | None = None,
) -> dict:
    content = (
        f"{message}\n\n"
        f"[LOCATION]\n{json.dumps(location, indent=2)}\n\n"
        f"[PATIENT_PROFILE]\n{json.dumps(patient_profile, indent=2)}"
    )
    messages = (prior_messages or []) + [{"role": "user", "content": content}]
    return {
        "messages":        messages,
        "location":        location,
        "patient_profile": patient_profile,
    }
