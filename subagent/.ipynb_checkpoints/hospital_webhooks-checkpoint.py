"""
MedicAI MVP — Hospital Response Webhooks
==========================================
hospital_webhooks.py

Add these routes to your FastAPI api.py.

When a hospital taps Yes or No on the WhatsApp/SMS link:
  GET /hospital/respond/{session_id}/{hospital_id}/accept
  GET /hospital/respond/{session_id}/{hospital_id}/reject

The response is stored in Redis (or in-memory for prototype).
The supervisor reads it via GET /session/hospital-responses/{session_id}.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

# ── In-memory response store (replace with Redis in production) ───────────────
# { session_id: { hospital_id: { status, hospital_name, responded_at } } }
_responses: dict[str, dict[str, Any]] = {}


def get_responses(session_id: str) -> dict[str, Any]:
    return _responses.get(session_id, {})


def record_response(session_id: str, hospital_id: str, status: str, hospital_name: str = "") -> None:
    if session_id not in _responses:
        _responses[session_id] = {}
    _responses[session_id][hospital_id] = {
        "status":        status,          # "accepted" | "rejected"
        "hospital_id":   hospital_id,
        "hospital_name": hospital_name,
        "responded_at":  datetime.utcnow().isoformat(),
    }
    print(f"[webhook] {hospital_name or hospital_id} → {status.upper()} (session={session_id[:8]})", flush=True)


# ════════════════════════════════════════════════════════════════════════════
#  WEBHOOK ROUTES
#  Hospitals tap these links from WhatsApp/SMS
# ════════════════════════════════════════════════════════════════════════════

ACCEPT_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MedicAI — Response Received</title>
  <style>
    body {{ font-family: sans-serif; display: flex; align-items: center;
            justify-content: center; min-height: 100vh; margin: 0;
            background: #f0fdf4; }}
    .card {{ background: white; border-radius: 16px; padding: 40px;
             text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.08); }}
    .icon {{ font-size: 64px; }}
    h1 {{ color: #16a34a; margin: 16px 0 8px; }}
    p {{ color: #6b7280; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✅</div>
    <h1>Response Received</h1>
    <p><strong>{hospital_name}</strong> has accepted the patient.</p>
    <p>MedicAI has been notified. Thank you.</p>
  </div>
</body>
</html>
"""

REJECT_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MedicAI — Response Received</title>
  <style>
    body {{ font-family: sans-serif; display: flex; align-items: center;
            justify-content: center; min-height: 100vh; margin: 0;
            background: #fef2f2; }}
    .card {{ background: white; border-radius: 16px; padding: 40px;
             text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.08); }}
    .icon {{ font-size: 64px; }}
    h1 {{ color: #dc2626; margin: 16px 0 8px; }}
    p {{ color: #6b7280; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">❌</div>
    <h1>Response Received</h1>
    <p><strong>{hospital_name}</strong> cannot receive this patient at this time.</p>
    <p>MedicAI will notify other facilities. Thank you.</p>
  </div>
</body>
</html>
"""


@router.get("/hospital/respond/{session_id}/{hospital_id}/accept")
async def hospital_accept(session_id: str, hospital_id: str):
    """
    Hospital tapped YES — they can receive the patient.
    Returns a simple confirmation HTML page (works on any phone browser).
    """
    # look up hospital name from prototype list or just use id
    hospital_name = _get_hospital_name(hospital_id)
    record_response(session_id, hospital_id, "accepted", hospital_name)

    return HTMLResponse(
        content=ACCEPT_HTML.format(hospital_name=hospital_name),
        status_code=200,
    )


@router.get("/hospital/respond/{session_id}/{hospital_id}/reject")
async def hospital_reject(session_id: str, hospital_id: str):
    """
    Hospital tapped NO — they cannot receive the patient.
    Returns a simple confirmation HTML page.
    """
    hospital_name = _get_hospital_name(hospital_id)
    record_response(session_id, hospital_id, "rejected", hospital_name)

    return HTMLResponse(
        content=REJECT_HTML.format(hospital_name=hospital_name),
        status_code=200,
    )


@router.get("/session/hospital-responses/{session_id}")
async def get_hospital_responses(session_id: str):
    """
    Poll this to see which hospitals have responded and what they said.
    The supervisor calls this to update the user.

    Response:
    {
      "session_id":  "...",
      "total_notified": 3,
      "responded":   2,
      "accepted":    [ { hospital_id, hospital_name, responded_at } ],
      "rejected":    [ { ... } ],
      "pending":     [ { hospital_id, hospital_name } ],
      "all_responded": false
    }
    """
    responses = get_responses(session_id)

    # get full hospital list from session (prototype: use hardcoded list)
    from subagents.hospital_notifier import PROTOTYPE_HOSPITALS
    all_hospitals = PROTOTYPE_HOSPITALS

    accepted = [r for r in responses.values() if r["status"] == "accepted"]
    rejected = [r for r in responses.values() if r["status"] == "rejected"]
    pending  = [
        {"hospital_id": h["id"], "hospital_name": h["name"]}
        for h in all_hospitals
        if h["id"] not in responses
    ]

    return JSONResponse({
        "session_id":      session_id,
        "total_notified":  len(all_hospitals),
        "responded":       len(responses),
        "accepted":        accepted,
        "rejected":        rejected,
        "pending":         pending,
        "all_responded":   len(pending) == 0,
    })


def _get_hospital_name(hospital_id: str) -> str:
    """Look up hospital name by ID from prototype list."""
    from subagents.hospital_notifier import PROTOTYPE_HOSPITALS
    match = next((h for h in PROTOTYPE_HOSPITALS if h["id"] == hospital_id), None)
    return match["name"] if match else hospital_id
