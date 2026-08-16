"""Agent Tools - All LLM-based emergency analysis functions"""
import json
import os
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool


# ════════════════════════════════════════════════════════════════
#  SCHEMAS
# ════════════════════════════════════════════════════════════════

class WebQuery(BaseModel):
    query: str
    tags: list[str]
    search_id: str


class SpeculativeWebQuery(BaseModel):
    query: str
    tags: list[str]
    search_id: str
    scenario: str  # what user response confirms this search


class EmergencyAnalysis(BaseModel):
    certain_conditions: list[str] = Field(description="Conditions CERTAIN from message alone")
    certain_web_queries: list[WebQuery] = Field(description="web searches to launch immediately")
    uncertain_dimensions: list[str] = Field(description="What we don't know yet")
    clarifying_question: str = Field(description="ONE question to ask user")
    speculative_web_queries: list[SpeculativeWebQuery] = Field(description="Speculative web searches")
    severity: str = Field(description="critical|high|moderate|low")
    summary: str = Field(description="One sentence summary")


class ImmediateSteps(BaseModel):
    narrative: str = Field(
        description="A short 2-3 sentence acknowledgement: what appears to "
                     "be happening, that nearby hospitals are being alerted "
                     "so they can expect the patient, and a prompt to pick "
                     "which hospital to head to from the nearby list. Plain, "
                     "calm, empathetic language. This must NEVER contain, "
                     "restate, number, or list any of the quick_steps "
                     "actions — narrative and quick_steps are two separate "
                     "outputs shown in two separate places."
    )
    quick_steps: list[str] = Field(
        description="1-3 short, immediate first-aid actions, derivable ONLY "
                     "from certain_conditions — never from uncertain/speculative "
                     "ones. Plain language, one action per item, no explanations. "
                     "Never restated inside narrative."
    )


class ClarifyingQuestion(BaseModel):
    question: str = Field(description="The question to ask the user")
    options: list[str] = Field(description="Preset answer options e.g. ['Yes', 'No']")
    context: str = Field(description="Brief context for why this matters medically")
    suggested_replies: list[str] = Field(
        default_factory=list,
        description=(
            "2-4 short, tappable free-text reply options, contextual to THIS "
            "question — phrased the way a person at the scene would actually "
            "type/say them, e.g. for 'Is the person breathing?' -> "
            "['He's breathing now', 'Still not breathing', 'Breathing but "
            "very slowly']. Distinct from `options` (fixed buttons)."
        ),
    )


# ════════════════════════════════════════════════════════════════
#  TOOLS
# ════════════════════════════════════════════════════════════════

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
        model="gemini-2.0-flash",
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
    speculative_results: dict,
) -> dict:
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
        model="gemini-2.0-flash",
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
    content = response.content
    if isinstance(content, list):
        content = content[0].get("text", "") if content else ""
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(content)
    except Exception:
        return {
            "confirmed_search_ids": [],
            "cancel_task_ids": [],
            "discard_search_ids": [],
            "new_certain_queries": [],
            "summary": user_response,
        }


@tool
def ask_clarifying_question(
    question: str,
    options: list[str],
    context: str = "",
    suggested_replies: list[str] | None = None,
) -> dict:
    """
    Ask the user a clarifying question with preset options (buttons).
    Call this AFTER sending your initial text response to the user.
    The question will be rendered as a separate UI element with clickable buttons.
    Do NOT include the question in your text response — call this tool instead.

    Args:
        question : the question to ask, e.g. "Is your son breathing?"
        options  : preset choices e.g. ["Yes", "No"] or
                   ["Conscious", "Unconscious"] or
                   ["Chest pain", "Shortness of breath", "Both"]
        context  : why this matters e.g. "This determines if CPR is needed"
        suggested_replies : 2-4 short, natural-language reply shortcuts
                   specific to THIS question (e.g. "He's breathing now",
                   "Still not breathing") — NOT a generic Yes/No restatement
                   of `options`. Omit or leave empty only if no good
                   contextual replies come to mind; never fabricate filler.
    """
    return {
        "question": question,
        "options": options,
        "context": context,
        "suggested_replies": suggested_replies or [],
        "type": "clarifying_question",
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
    - Contextual suggested replies for the user's next message
    """
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.0-flash",
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

Format your response as JSON with TWO separate sections:

{{
  "narrative": "One short empathetic sentence + call to action. MAX 2 sentences. NO steps here.",
  "priority_steps": ["step 1", "step 2", ...],
  "do_not": ["do not ...", ...],
  "watch_for": ["watch for ...", ...],
  "reassurance": "one calming sentence",
  "when_to_update_me": "tell me if X happens",
  "suggested_replies": ["short reply 1", "short reply 2", ...]
}}
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
            "do_not": [],
            "watch_for": [],
            "reassurance": "Help is on the way.",
            "when_to_update_me": "Tell me if anything changes.",
            "suggested_replies": ["Something's changed", "No change yet"],
        }


@tool
def assemble_immediate_steps(certain_conditions: list[str], emergency_summary: str) -> dict:
    """
    Build the MANDATORY first response: a short acknowledgement narrative
    PLUS a small set (max 3) of immediate first-aid actions, before the
    clarifying question is answered.

    Uses ONLY certain_conditions — never uncertain_dimensions or anything
    speculative. This must never wait on web_searcher results: it draws on
    the model's own medical knowledge so it can run immediately alongside
    hospital_notifier, without delaying the mandatory first reply.

    Returns:
        {
          "narrative":   "<2-3 sentence acknowledgement, no steps in it>",
          "quick_steps": ["step 1", "step 2", ...],   # max 3, plain language
        }
    """
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.0-flash",
        google_api_key=os.environ.get("GOOGLE_API_KEY"),
        temperature=0,
        max_retries=2,
        timeout=30,
    ).with_structured_output(ImmediateSteps)

    result: ImmediateSteps = llm.invoke(f"""
A person needs to do something RIGHT NOW, before any more information is
available.

Emergency: {emergency_summary}
Certain conditions: {', '.join(certain_conditions)}

Produce TWO separate things:

1. narrative: a short 2-3 sentence acknowledgement, in this shape —
   "Your [relationship] has [what happened] — this is critical.
   Nearby hospitals are being alerted so they can expect you. Pick
   which hospital you'd like to head to from the list." Plain, calm,
   empathetic. This must NEVER contain, number, or list any of the
   quick_steps actions.

2. quick_steps: 1-3 short, immediate first-aid actions based ONLY on
   what is already certain above — do not guess at anything uncertain.

Rules:
- Max 3 quick_steps, most urgent first
- Each quick_step is one short, plain-language sentence — no explanations
- Only actions safe to take with zero additional information
- If in doubt whether an action needs more context first, leave it out
- narrative and quick_steps are two SEPARATE outputs, shown in two
  separate places to the user — never duplicate content between them
""")
    return result.model_dump()


def estimate_eta_minutes(distance_km: float | None, avg_speed_kmh: float = 25.0) -> float:
    """Estimate travel time in minutes based on distance."""
    if not distance_km or distance_km <= 0:
        return 5.0
    return round((distance_km / avg_speed_kmh) * 60, 1)


@tool
def get_selected_hospital_eta(selected_hospital: dict) -> dict:
    """
    Compute how long is left, RIGHT NOW, until the user reaches the hospital
    they've already chosen to go to. Adjusts for time elapsed since selection
    — not just a stale one-time estimate.

    Args:
        selected_hospital: pass the [SELECTED_HOSPITAL] dict from your
            context through EXACTLY as given.

    Returns:
        {
          "hospital_name":         str,
          "distance_km":           float | None,
          "remaining_eta_minutes": float,
          "note":                  str,
        }
    """
    name = selected_hospital.get("name", "the selected hospital")
    distance_km = selected_hospital.get("distance_km")
    initial_eta = selected_hospital.get("eta_minutes")
    if initial_eta is None:
        initial_eta = estimate_eta_minutes(distance_km)
    selected_at = selected_hospital.get("selected_at")

    elapsed_minutes = 0.0
    if selected_at:
        try:
            selected_dt = datetime.fromisoformat(str(selected_at).replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            elapsed_minutes = max(0.0, (now - selected_dt).total_seconds() / 60)
        except Exception:
            elapsed_minutes = 0.0

    remaining = max(0.0, round(initial_eta - elapsed_minutes, 1))

    return {
        "hospital_name": name,
        "distance_km": distance_km,
        "remaining_eta_minutes": remaining,
        "note": (
            "Estimate based on travel time known at selection — refined "
            "by the frontend's own routing calculation when available, "
            "otherwise a rough straight-line approximation. Treat as "
            "approximate either way."
        ),
    }
