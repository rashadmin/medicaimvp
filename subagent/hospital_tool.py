"""
MedicAI MVP — Hospital Notifier Tool
======================================
tools/hospital_tool.py

Handles hospital alerting via WhatsApp + SMS (Twilio).

Notification Pipeline:
  1. Generate a concise alert report (LLM) from the emergency payload
  2. Broadcast to all hospitals simultaneously via asyncio.gather
  3. Each message contains a Yes/No response link
  4. Hospitals tap Yes/No → webhook fires → state updated
  5. Supervisor checks responses via check_async_task

WhatsApp: Twilio WhatsApp API (sandbox for prototype)
SMS:      Twilio SMS API (fallback)
Webhooks: Your FastAPI /hospital/respond/{session_id}/{hospital_id}/{response} endpoint

Replace the prototype hospital list / Twilio client with your actual
implementation (Foursquare-sourced hospital list, production WhatsApp
sender, etc.)
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
import httpx
# from langgraph.config import get_stream_writer

load_dotenv()


# ════════════════════════════════════════════════════════════════════════════
#  PROTOTYPE HOSPITALS
#  In production these come from the Foursquare query results.
#  For prototype, hardcode 3 test numbers.
# ════════════════════════════════════════════════════════════════════════════

# PROTOTYPE_HOSPITALS = [
#     # {
#     #     "id":    "hospital_1",
#     #     "name":  "Gbagada General Hospital",
#     #     "phone": os.getenv("HOSPITAL_1_PHONE", "+2349125098107"),
#     #     "distance_km": 1.1,
#     # },
#     {
#         "id":    "hospital_2",
#         "name":  "R-Jolad Hospital",
#         "phone": os.getenv("HOSPITAL_2_PHONE", "+2349032732342"),
#         "distance_km": 0.8,
#     },
#     # {
#     #     "id":    "hospital_3",
#     #     "name":  "Ladi-Lak Medical Centre",
#     #     "phone": os.getenv("HOSPITAL_3_PHONE", "+2349030788952"),
#     #     "distance_km": 0.98,
#     # },
# ]
# @tool
async def query_hospital_registry(lat: float ,lng: float ,radius_km: int = 5,) -> list[dict[str, Any]]:   
    """
    Query Foursquare Places API for hospitals/clinics within radius_km of (lat, lng).
    Sorted by distance. Returns up to 10 results.
    Each: { id, name, address, lat, lng, distance_km, api_url }
    Call this once at the start of Phase 1 — reuse results in Phase 2.
    """
    # writer = get_stream_writer()
    # writer({"event": "hospital_search_started", "lat": lat, "lng": lng, "radius_km": radius_km})

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            "https://places-api.foursquare.com/places/search",
            params={
                "ll": f"{lat},{lng}",
                "radius": radius_km * 1000,
                "fsq_category_ids": ",".join([
                    "56aa371be4b08b9a8d5734ff",  # Hospital
                    "58daa1558bbb0b01f18ec1f7",  # Emergency Room
                    "63be6904847c3692a84b9bc0",  # Medical Center
                    "63be6904847c3692a84b9bbe",  # Clinic
                    "63be6904847c3692a84b9bbd",  # Urgent Care
                    "4bf58dd8d48988d196941735",  # Healthcare
                    "63be6904847c3692a84b9bdf",  # Trauma Center
                    "63be6904847c3692a84b9bde",  # Ambulatory Care
                ]),
                "sort": "DISTANCE",
                "limit": 10,
            },
            headers={
                "Authorization":f'Bearer {os.getenv("FOURSQUARE_API_KEY", "")}',
                "X-Places-Api-Version": "2025-06-17",
                "accept": "application/json",
            },
        )
			
    hospitals = [
        {
            "id":          r["fsq_place_id"],
            "name":        r["name"],
            "address":     r["location"].get("formatted_address", ""),
            "lat":         r["latitude"],
            "lng":         r["longitude"],
            "distance_km": round(r.get("distance", 0) / 1000, 2),
            "api_url":     None,   # set real hospital endpoint in production
            "contact":None
        }
        for r in resp.json().get("results", [])
    ]
    # config    = get_config()
    # thread_id = config.get("configurable", {}).get("thread_id")
    # if thread_id:
    #     await set_hospitals(thread_id, hospitals)

    # writer({"event": "hospitals_found", "count": len(hospitals),
    #         "names": [h["name"] for h in hospitals]})
    import numpy as np
    #selected_index = np.random.randint(1,10)
    for hospital in hospitals:
    	hospital['contact'] = np.random.choice(['+2349061346884','+2349032732342'])[0]
    return hospitals

def resolve_hospitals(hospitals: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """
    Resolve the hospital list IN CODE — never rely on the LLM to "remember"
    or fill in the prototype list. If the caller-supplied hospitals list is
    empty/missing, fall back to PROTOTYPE_HOSPITALS. Also drops any entry
    that is missing a usable phone number so we never try to send to a
    fabricated/placeholder number.
    """
    source = hospitals if hospitals else PROTOTYPE_HOSPITALS
    resolved = []
    for h in source:
        phone = (h.get("contact") or "").strip()
        if not phone:
            print(f"[notifier] ⚠️ Skipping {h.get('name', h.get('id', '?'))} — no phone number", flush=True)
            continue
        resolved.append(h)
    return resolved


# ════════════════════════════════════════════════════════════════════════════
#  TWILIO CLIENT
#  Replace with your actual messaging provider client
# ════════════════════════════════════════════════════════════════════════════

def _get_twilio_client():
    from twilio.rest import Client
    return Client(
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
    )


# ════════════════════════════════════════════════════════════════════════════
#  WHATSAPP CONTENT TEMPLATE (buttons)
#
#  Set TWILIO_ALERT_TEMPLATE_SID once you've created + gotten WhatsApp
#  approval for a `twilio/call-to-action` Content Template in the Twilio
#  Console (Messaging → Content Template Builder). The template body should
#  take one variable ({{1}} = alert text) and its two URL buttons should each
#  take one variable ({{2}} for accept, {{3}} for reject) appended to the
#  FIXED base URL you configured on the button in the console — that base
#  must match API_BASE_URL exactly, since the console can't read env vars.
#
#  If this isn't set, we fall back to the old plain-text body with links
#  inline (no tappable buttons, but nothing breaks while approval is pending).
# ════════════════════════════════════════════════════════════════════════════

TWILIO_ALERT_TEMPLATE_SID = os.getenv("TWILIO_ALERT_TEMPLATE_SID")  # e.g. "HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


# def _wa(number: str) -> str:
#     """Ensure a number has exactly one 'whatsapp:' prefix, never double it."""
#     number = number.strip()
#     return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


async def _send_sms_fallback(
    hospital:   dict,
    message:    str,
    accept_url: str,
    reject_url: str,
    # writer:     Any,
) -> dict:
    """SMS fallback if WhatsApp fails."""
    try:
        sms_from = os.getenv("TWILIO_SMS_FROM")
        if not sms_from:
            raise RuntimeError("TWILIO_SMS_FROM is not set in .env — required for SMS fallback")

        client = _get_twilio_client()

        # SMS has 160 char limit — truncate and include just the URLs
        sms_body = (
            f"🚨 MedicAI EMERGENCY — {hospital['name']}\n"
            f"Patient needs immediate care.\n"
            f"Accept: {accept_url}\n"
            f"Reject: {reject_url}"
        )

        msg = client.messages.create(
            body=sms_body,
            from_=sms_from,
            to=hospital["phone"],
        )

        # writer({"event": "sms_sent", "hospital": hospital["name"], "sid": msg.sid})
        print(f"[notifier] ✅ SMS sent to {hospital['name']} — SID: {msg.sid}", flush=True)

        return {
            "hospital_id":   hospital["id"],
            "hospital_name": hospital["name"],
            "hospital_address":hospital['address'],
            "contact":hospital['contact'],
            "distance":hospital['distance_km'],
            "status":        "sent",
            "channel":       "sms",
            "message_sid":   msg.sid,
            "accept_url":    accept_url,
            "reject_url":    reject_url,
        }

    except Exception as e:
        print(f"[notifier] ❌ SMS also failed for {hospital['name']}: {e}", flush=True)
        return {
            "hospital_id":   hospital["id"],
            "hospital_name": hospital["name"],
            "status":        "failed",
            "error":         str(e),
        }


# ════════════════════════════════════════════════════════════════════════════
#  TOOLS
# ════════════════════════════════════════════════════════════════════════════

@tool
def generate_alert_report(
    emergency_payload: dict[str, Any],
    patient_profile:   dict[str, Any],
    location:          dict[str, Any],
) -> str:
    """
    Generate a concise emergency alert report for hospitals.
    This is what gets sent via WhatsApp/SMS.
    Returns a short, clear message a hospital dispatcher can read in seconds.

    Args:
        emergency_payload : dict describing the emergency (type, severity, symptoms, etc.)
        patient_profile   : dict with patient info (name, age, blood_type, allergies)
        location          : dict with lat, lng, address
    """
    llm = ChatGoogleGenerativeAI(
        model="gemini-3.1-flash-lite",
        google_api_key=os.environ.get("GOOGLE_API_KEY"),
        temperature=0,
    )

    patient_str = (
        f"Patient: {patient_profile.get('name', 'Unknown')}, "
        f"Age: {patient_profile.get('age', '?')}, "
        f"Blood type: {patient_profile.get('blood_type', '?')}, "
        f"Allergies: {', '.join(patient_profile.get('allergies', []) or ['none known'])}"
    )

    response = llm.invoke(f"""
Generate a SHORT emergency alert for a hospital WhatsApp message.
Maximum 5 lines. Must include: emergency type, severity, key symptoms,
patient info, and location. Be direct — hospital dispatchers are busy.

Emergency: {json.dumps(emergency_payload, indent=2)}
Patient: {patient_str}
Location: {location.get('address', 'Unknown')} ({location.get('lat')}, {location.get('lng')})

Format:
🚨 EMERGENCY ALERT — MedicAI
Type: <emergency_type> | Severity: <severity>
Patient: <name>, <age>yo, <blood_type>
Symptoms: <key symptoms>
Location: <address>
""")

    content = response.content
    if isinstance(content, list):
        content = content[0].get("text", "") if content else ""
    return content.strip()


@tool
async def send_whatsapp_alert(
    hospital:     dict[str, Any],
    alert_report: str,
    session_id:   str,
) -> dict[str, Any]:
    """
    Send a WhatsApp message to a hospital with Yes/No response buttons.
    Uses Twilio WhatsApp API. Falls back to SMS on failure.

    Args:
        hospital     : dict with id, name, phone, distance_km
        alert_report : the alert text generated by generate_alert_report
        session_id   : unique session label used to build the response webhook URLs

    Returns { hospital_id, hospital_name, status, channel, message_sid, accept_url, reject_url }
    """
    # writer  = get_stream_writer()
    api_base = os.getenv("API_BASE_URL", "http://localhost:8000")

    # Yes/No response URLs — hospitals tap these
    accept_url = f"{api_base}/hospital/respond/{session_id}/{hospital['id']}/accept"
    reject_url = f"{api_base}/hospital/respond/{session_id}/{hospital['id']}/reject"

    # full message with response links
    message = (
        f"{alert_report}\n\n"
        f"📍 Distance from your facility: {hospital.get('distance_km', '?')} km\n\n"
        f"Can you receive this patient?\n"
        f"✅ YES: \n\n"
        f"❌ NO:  "
    )

    # writer({"event": "whatsapp_sending", "hospital": hospital["name"]})

    # ── Twilio WhatsApp send ──────────────────────────────────────────────
    try:
        client = _get_twilio_client()

        # Twilio sandbox: prefix is "whatsapp:+1415..."
        # Production: use your approved WhatsApp number
        # NOTE: default has NO "whatsapp:" prefix baked in — _wa() adds exactly one.
        from_number = f"whatsapp:{os.getenv('TWILIO_WHATSAPP_FROM', '+14155238886')}"  # no whatsapp: in the default
        to_number   = f"whatsapp:{hospital['phone']}"
        print(from_number, to_number)

        if TWILIO_ALERT_TEMPLATE_SID:
            # ── Templated send: real tappable Yes/No buttons ──────────────
            # {{2}} / {{3}} are just the suffix appended to whatever fixed
            # base URL you set on each button in the Content Template Builder
            # (that base must equal f"{api_base}/hospital/respond/").
            import re

            report_with_distance = re.sub(
                r"\s+", " ",
                f"{alert_report} 📍 Distance from your facility: {hospital.get('distance_km', '?')} km"
            ).strip()
            print(report_with_distance)
            accept_suffix = f"{session_id}/{hospital['id']}/accept"
            reject_suffix = f"{session_id}/{hospital['id']}/reject"

            msg = client.messages.create(
                content_sid=TWILIO_ALERT_TEMPLATE_SID,
                content_variables=json.dumps({
                    "1": report_with_distance,
                    "2": accept_suffix,
                    "3": reject_suffix,
                }),
                from_=from_number,
                to=to_number,
            )
        else:
            # ── Fallback: plain text, links inline (no real buttons) ──────
            msg = client.messages.create(
                body=message,
                from_=from_number,
                to=to_number,   # fixed: was wrapped in whatsapp: twice before
            )

        # writer({"event": "whatsapp_sent", "hospital": hospital["name"],
        #         "sid": msg.sid})
        print(f"[notifier] ✅ WhatsApp sent to {hospital['name']} — SID: {msg.sid}", flush=True)

        return {
            "hospital_id":   hospital["id"],
            "hospital_name": hospital["name"],
            "hospital_address":hospital['address'],
            "contact":hospital['contact'],
            "distance":hospital['distance_km'],
            "status":        "sent",
            "channel":       "whatsapp_template" if TWILIO_ALERT_TEMPLATE_SID else "whatsapp",
            "message_sid":   msg.sid,
            "accept_url":    accept_url,
            "reject_url":    reject_url,
        }

    except Exception as e:
        # fallback to SMS
        print(f"[notifier] WhatsApp failed for {hospital['name']}: {e}", flush=True)
        print(f"[notifier] Trying SMS fallback...", flush=True)
        return await _send_sms_fallback(hospital, message, accept_url, reject_url)


@tool
async def broadcast_to_hospitals(
    alert_report: str,
    hospitals:    list[dict[str, Any]],
    session_id:   str,
    lat: float | None = None,
    lng:float | None = None,
) -> list[dict[str, Any]]:
    """
    Send the alert report to all hospitals simultaneously via asyncio.gather.

    Args:
        alert_report : the alert text generated by generate_alert_report
        hospitals    : list of hospital dicts (id, name, phone, distance_km).
                       If empty/missing, falls back to PROTOTYPE_HOSPITALS
                       — resolved here in code, never left to the LLM.
        session_id   : unique session label used to build response webhook URLs

    Returns list of send results per hospital.
    """
    # writer = get_stream_writer()
    if not hospitals:
    	if lat is None or lng is None:
    		raise ValueError("broadcast_to_hospitals: no hospitals list and no lat/lng to look them up")
    	hospitals = await query_hospital_registry(lat=lat,lng=lng)
    resolved = resolve_hospitals(hospitals)

    if not resolved:
        print("[notifier] ❌ No hospitals with valid phone numbers to notify", flush=True)
        return []

    # writer({"event": "broadcast_started", "hospital_count": len(resolved)})
    print(f"[notifier] Broadcasting to {len(resolved)} hospitals...", flush=True)
    results = resolved
    #results = list(await asyncio.gather(*[
        #send_whatsapp_alert.ainvoke({
         #   "hospital":     h,
        #    "alert_report": alert_report,
       #     "session_id":   session_id,
      #  })
     #   for h in resolved
    #]))

    #sent     = [r for r in results if r.get("status") == "sent"]
    #failed   = [r for r in results if r.get("status") == "failed"]

    # writer({
    #     "event":   "broadcast_complete",
    #     "sent":    len(sent),
    #     "failed":  len(failed),
    #     "results": results,
    # })

#    print(f"[notifier] ✅ Sent: {len(sent)} | Failed: {len(failed)}", flush=True)
    return results
