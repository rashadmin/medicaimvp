"""
Simulates Meta's WhatsApp webhook POSTs so you can test /webhook/whatsapp
without going through the Meta dashboard each time.

Usage:
  export META_APP_SECRET=...          # same value your server uses
  export WEBHOOK_URL=https://medicaimvp.onrender.com/webhook/whatsapp
  python test_whatsapp_webhook.py text "My dad is having chest pains"  +2348012345678
  python test_whatsapp_webhook.py location 6.5244 3.3792              +2348012345678
"""
import hashlib
import hmac
import json
import os
import sys
import time

import httpx

APP_SECRET  = os.environ["META_APP_SECRET"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://localhost:8000/webhook/whatsapp")


def _text_payload(sender: str, text: str) -> dict:
    return {
        "entry": [{
            "id": "TEST_WABA_ID",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "TEST_PHONE_ID"},
                    "contacts": [{"profile": {"name": "Test User"}, "wa_id": sender}],
                    "messages": [{
                        "from": sender,
                        "id": f"wamid.TEST{int(time.time() * 1000)}",
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {"body": text},
                    }],
                },
            }],
        }],
    }


def _location_payload(sender: str, lat: float, lng: float) -> dict:
    return {
        "entry": [{
            "id": "TEST_WABA_ID",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "TEST_PHONE_ID"},
                    "contacts": [{"profile": {"name": "Test User"}, "wa_id": sender}],
                    "messages": [{
                        "from": sender,
                        "id": f"wamid.TEST{int(time.time() * 1000)}",
                        "timestamp": str(int(time.time())),
                        "type": "location",
                        "location": {"latitude": lat, "longitude": lng, "name": "Test location"},
                    }],
                },
            }],
        }],
    }


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def send(payload: dict) -> None:
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sign(body),
    }
    resp = httpx.post(WEBHOOK_URL, content=body, headers=headers, timeout=10.0)
    print(f"→ {resp.status_code} {resp.text[:200]}")


if __name__ == "__main__":
    kind = sys.argv[1]
    if kind == "text":
        message, sender = sys.argv[2], sys.argv[3]
        send(_text_payload(sender, message))
    elif kind == "location":
        lat, lng, sender = float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
        send(_location_payload(sender, lat, lng))
    else:
        print("First arg must be 'text' or 'location'")
