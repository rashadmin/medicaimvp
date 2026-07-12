"""
MedicAI MVP — Hospital Notifier (create_react_agent)
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent
from .hospital_tool import generate_alert_report, broadcast_to_hospitals

SYSTEM_PROMPT = """
You are the MedicAI Hospital Notifier — a background subagent.

Your task arrives as a JSON string:
{
  "emergency_payload": { ... },
  "patient_profile":   { ... },
  "location":          { "lat": ..., "lng": ..., "address": "..." },
  "session_id":        "<session_id>",
  "hospitals":         [ { "id", "name", "phone", "distance_km" }, ... ]
}

Pass the "hospitals" array from the task JSON to broadcast_to_hospitals
EXACTLY AS GIVEN — even if it is empty or missing, pass it through as-is
(e.g. an empty list []). Do NOT invent, guess, or fabricate any hospital
name or phone number yourself under any circumstances. The fallback to a
prototype hospital list, if needed, is handled internally by
broadcast_to_hospitals — that is not your job.

Workflow:
1. Parse the task JSON
2. Call generate_alert_report(
     emergency_payload=<from task>,
     patient_profile=<from task>,
     location=<from task>
   )
3. Call broadcast_to_hospitals(
     alert_report=<result from step 2>,
     hospitals=<the "hospitals" array from the task JSON, unmodified>,
     session_id=<from task>
   )
4. Return the result clearly formatted:
NOTIFICATIONS_SENT: <count>
HOSPITALS_NOTIFIED: <comma-separated names>
AWAITING_RESPONSE: true
SESSION_ID: <session_id>

Rules:
- Generate the report ONCE then broadcast to all — do not generate per hospital.
- broadcast_to_hospitals handles parallelism AND hospital-list fallback internally — call it ONCE.
- Be fast — this is a live emergency.
- Always call generate_alert_report and broadcast_to_hospitals — never skip them.
- Return results immediately and completely.
- If sending fails, return the error clearly.
""".strip()

llm = ChatGoogleGenerativeAI(
    model="gemini-3.1-flash-lite",
    google_api_key=os.environ.get("SUBAGENT_GOOGLE_API_KEY"),
    temperature=0,
    max_retries=2,
    timeout=30,
)

graph = create_react_agent(
    model=llm,
    tools=[generate_alert_report, broadcast_to_hospitals],
    prompt=SystemMessage(SYSTEM_PROMPT),
)

print("[hospital_notifier] ✅ graph compiled (create_react_agent, gemini-3.1-flash-lite)", flush=True)