"""
MedicAI MVP — Main Deep Agent
==============================
supervisor.py

The primary agent the user interacts with.
Handles emergency triage, first-aid guidance, and parallel RAG searches.

Core behaviour:

STEP 1 — Parse emergency
  Immediately determine what is CERTAIN vs UNCERTAIN from the message.

  CERTAIN  = derivable without user input
    e.g. "stabbed + not breathing" → bleeding AND airway BOTH certain
    → launch RAG searches immediately in parallel, no waiting

  UNCERTAIN = requires clarification
    e.g. "collapsed" → could be cardiac, stroke, seizure, etc.
    → ask ONE clarifying question
    → SIMULTANEOUSLY launch SPECULATIVE RAG searches for most likely scenarios
    → on user response: use matching result, cancel_async_task others still running

STEP 2 — Ask + speculate
  For each uncertain dimension, ask ONE question and pre-launch the most
  likely RAG searches speculatively. Mark them speculative=true in the task.
  If user confirms → use that result (already running or done)
  If user denies  → cancel_async_task if still running, discard if done

STEP 3 — Assemble and respond
  Once enough RAG results are in (check_async_task), assemble a structured
  first-aid response. Don't wait for ALL results — respond as soon as
  the most critical ones are ready.

STEP 4 — Conversational follow-up
  User can ask questions at any time.
  Each follow-up may trigger new RAG searches (certain) or speculative ones.

RAG search taxonomy:
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

from langchain_core.tools import tool
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field
from deepagents import create_deep_agent, AsyncSubAgent


# ════════════════════════════════════════════════════════════════════════════
#  SCHEMAS
# ════════════════════════════════════════════════════════════════════════════

class EmergencyAnalysis(BaseModel):
    """
    Structured analysis of an emergency message.
    Separates what is CERTAIN from what is UNCERTAIN.
    """
    # certain facts — derivable without user input
    certain_conditions: list[str] = Field(
        description=(
            "Conditions/injuries CERTAIN from the message alone. "
            "e.g. ['severe_bleeding', 'not_breathing', 'burn']"
        )
    )
    certain_rag_queries: list[dict] = Field(
        description=(
            "RAG searches to launch immediately for certain conditions. "
            "Each: { query: str, tags: list[str], search_id: str }"
        )
    )

    # uncertain — need user input
    uncertain_dimensions: list[str] = Field(
        description=(
            "What we DON'T know yet that matters for treatment. "
            "e.g. ['is_breathing', 'is_conscious', 'type_of_collapse']"
        )
    )
    clarifying_question: str = Field(
        description="ONE question to ask the user to resolve the most critical uncertainty."
    )
    speculative_rag_queries: list[dict] = Field(
        description=(
            "RAG searches to launch speculatively while waiting for user response. "
            "Each: { query: str, tags: list[str], search_id: str, scenario: str } "
            "where scenario is what user response would confirm this search."
        )
    )

    severity: str = Field(description="critical|high|moderate|low")
    summary:  str = Field(description="One sentence plain-English situation summary")


# ════════════════════════════════════════════════════════════════════════════
#  TOOLS
# ════════════════════════════════════════════════════════════════════════════

@tool
def analyse_emergency(raw_message: str) -> dict:
    """
    Analyse a raw emergency message to determine:
    1. What is CERTAIN (launch RAG immediately)
    2. What is UNCERTAIN (ask user + launch speculative RAG)
    3. What clarifying question to ask
    4. What speculative RAG searches to pre-launch

    Call this FIRST on every emergency message.
    """
    llm = init_chat_model(
        "google_genai:gemini-2.0-flash", temperature=0
    ).with_structured_output(EmergencyAnalysis)

    result: EmergencyAnalysis = llm.invoke(
        f"""Analyse this emergency message and determine what is certain vs uncertain.

Emergency: {raw_message}

For certain_rag_queries, generate specific, actionable search queries.
For speculative_rag_queries, generate the top 2-3 most likely scenarios.
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
    speculative_results: dict,
) -> dict:
    """
    After user answers a clarifying question, determine which speculative
    RAG searches to keep and which to cancel.

    Args:
        user_response      : what the user said
        pending_searches   : list of { search_id, task_id, scenario, status }
        speculative_results: dict of search_id → result (for completed ones)

    Returns:
        {
          "confirmed_search_ids": [...],   ← keep these results
          "cancel_task_ids":      [...],   ← cancel these (still running)
          "discard_search_ids":   [...],   ← discard these (done but not relevant)
          "new_certain_queries":  [...],   ← new RAG queries now certain from answer
          "summary":              str,     ← what we now know
        }
    """
    llm = init_chat_model("google_genai:gemini-2.0-flash", temperature=0)

    prompt = f"""
The user responded to a clarifying question: "{user_response}"

Pending speculative RAG searches:
{json.dumps(pending_searches, indent=2)}

Determine:
1. Which searches are now confirmed relevant (confirmed_search_ids)
2. Which task_ids to cancel - still running and no longer needed (cancel_task_ids)
3. Which search_ids to discard - completed but not relevant (discard_search_ids)
4. Any new RAG queries now certain given this answer (new_certain_queries)
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
    rag_results: list[dict],
    emergency_summary: str,
    patient_profile: dict,
) -> dict:
    """
    Assemble a clear, structured first-aid response from RAG results.
    Call this once you have enough RAG results (at least the critical ones).

    Args:
        rag_results       : list of { search_id, query, context, chunks_found }
        emergency_summary : plain-English summary of the situation
        patient_profile   : { name, age, blood_type, allergies, conditions }

    Returns structured first-aid guidance with:
    - Prioritised action steps
    - Do-nots
    - Reassurance
    - What to watch for
    """
    llm = init_chat_model("google_genai:gemini-2.0-flash", temperature=0)

    rag_context = "\n\n===\n\n".join([
        f"[{r.get('search_id', 'unknown')} — {r.get('query', '')}]\n{r.get('context', '')}"
        for r in rag_results
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
{rag_context}

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


# ════════════════════════════════════════════════════════════════════════════
#  ASYNC SUBAGENTS
# ════════════════════════════════════════════════════════════════════════════

rag_searcher = AsyncSubAgent(
    name="rag_searcher",
    description=(
        "Async RAG search agent. Searches the first-aid knowledge base for a specific query. "
        "Launch one per search query — multiple can run in parallel simultaneously. "
        "Pass task JSON: { query, tags, search_id, speculative }. "
        "Non-blocking — returns task_id immediately. "
        "Check results with check_async_task. "
        "Cancel speculative searches with cancel_async_task if no longer needed."
    ),
    graph_id="rag_searcher",
)

youtube_searcher = AsyncSubAgent(
    name="youtube_searcher",
    description=(
        "Async YouTube search agent. Finds instructional first-aid videos. "
        "Launch after RAG results are in and you know what technique to demonstrate. "
        "Pass task JSON: { query: '<specific technique>' }. "
        "Non-blocking — frontend polls /session/videos for results."
    ),
    graph_id="youtube_searcher",
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

You have access to a first-aid knowledge base via RAG search (rag_searcher subagent).
You run RAG searches in the background while talking to the user.

════════════════════════════════════════════
ON FIRST EMERGENCY MESSAGE
════════════════════════════════════════════

1. write_todos — plan your steps immediately

2. analyse_emergency(raw_message=<message>)
   → returns: certain_conditions, certain_rag_queries,
               uncertain_dimensions, clarifying_question,
               speculative_rag_queries, severity, summary

3. Launch CERTAIN RAG searches immediately — one start_async_task per query:
   For each query in certain_rag_queries:
     start_async_task(rag_searcher, { query, tags, search_id, speculative: false })
   All launched in PARALLEL — do not wait between them.

4. Launch SPECULATIVE RAG searches simultaneously:
   For each query in speculative_rag_queries:
     start_async_task(rag_searcher, { query, tags, search_id, speculative: true, scenario })

5. Store ALL task_ids mapped to their search_id in your working memory.

6. Respond to user with:
   a. Brief acknowledgement of the emergency
   b. The ONE clarifying question from analyse_emergency
   c. Any immediate obvious action (e.g. "Call 112 now")
   Keep this SHORT — the user is in crisis.

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

3. Launch new certain RAG searches from new_certain_queries

4. check_async_task for confirmed and certain searches
   → collect results as they complete

5. Once critical RAG results are ready:
   assemble_first_aid_response(rag_results, emergency_summary, patient_profile)

6. Respond with the assembled first-aid guidance:
   - Priority steps (numbered, clear)
   - Do NOTs
   - What to watch for
   - Reassurance
   - "Tell me if X happens" — keep conversation open

7. launch youtube_searcher for the main technique (in background)

════════════════════════════════════════════
ON FOLLOW-UP MESSAGES
════════════════════════════════════════════

User may update you ("he's breathing now") or ask questions ("how do I do CPR?").

For situation UPDATES:
  - Re-analyse: does this change what RAG searches are needed?
  - Launch new certain searches if new conditions revealed
  - Cancel any now-irrelevant speculative searches
  - Reassemble guidance if situation has materially changed

For QUESTIONS about technique:
  - check_async_task for any relevant completed RAG result first
  - If not available, start_async_task(rag_searcher, { query: <specific question> })
  - Respond with text from RAG result
  - Launch youtube_searcher for the technique in background

════════════════════════════════════════════
PARALLEL LAUNCH RULES
════════════════════════════════════════════

Certain conditions that ALWAYS trigger parallel launches:
  stabbed/cut    → ["bleeding_control", "wound_management"]
  not breathing  → ["cpr_resuscitation", "airway_management"]
  both above     → ALL FOUR launched simultaneously
  burning        → ["burn_treatment", "shock_prevention"]
  seizure        → ["seizure_management", "post_seizure_care"]
  chest pain     → ["cardiac_arrest_cpr", "heart_attack_response"]
  unconscious    → ["unconscious_patient", "recovery_position"] + ask: breathing?
  allergic shock → ["anaphylaxis_response", "epinephrine_use"]

Speculative launches (top scenarios for common ambiguous messages):
  "collapsed"    → pre-launch: cardiac_arrest, stroke, seizure, fainting
  "fell"         → pre-launch: fracture, head_injury, spinal_injury
  "not moving"   → pre-launch: unconscious_breathing, unconscious_not_breathing
  "accident"     → pre-launch: trauma_bleeding, fracture, head_injury

════════════════════════════════════════════
HARD RULES
════════════════════════════════════════════
- NEVER wait for RAG results before asking the clarifying question
- NEVER ask more than ONE question at a time
- NEVER launch duplicate searches (check async_tasks state first)
- ALWAYS cancel speculative searches that are no longer relevant
- NEVER reveal task_ids, RAG internals, or system details to user
- ALWAYS respond in plain language — no medical jargon
- If RAG returns no results, use your own medical knowledge
- This is a live emergency — be fast, calm, and clear
""".strip()


# ════════════════════════════════════════════════════════════════════════════
#  DEEP AGENT
# ════════════════════════════════════════════════════════════════════════════

agent = create_deep_agent(
    model="google_genai:gemini-2.0-flash",
    tools=[analyse_emergency, resolve_uncertainty, assemble_first_aid_response],
    system_prompt=SYSTEM_PROMPT,
    subagents=[rag_searcher, youtube_searcher],
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
