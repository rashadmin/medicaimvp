"""Hospital Notifier Subagent - Alerts nearby hospitals"""
import os
from deepagents import AsyncSubAgent

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
    url=os.getenv("SUBAGENT_URL", "http://localhost:8000")
)
