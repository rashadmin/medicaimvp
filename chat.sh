#!/bin/bash
# chat.sh — send a message and stream the agent response
# Usage: ./chat.sh "<session_id>" "<message>"

SESSION_ID="$1"
MESSAGE="$2"

echo ""
echo "USER: $MESSAGE"
echo "AGENT: "

curl -s -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d "{
    \"session_id\": \"$SESSION_ID\",
    \"message\": \"$MESSAGE\",
    \"location\": {\"lat\": 6.5418, \"lng\": 3.3917, \"address\": \"Lekki, Lagos\"},
    \"patient_profile\": {\"name\": \"Emmanuel\", \"age\": 67, \"blood_type\": \"O+\", \"allergies\": [\"penicillin\"], \"conditions\": []}
  }" | python3 -c "
import sys, json

event_type = 'message'
for line in sys.stdin:
    line = line.rstrip()
    if line.startswith('event:'):
        event_type = line[6:].strip()
    elif line.startswith('data:'):
        data_str = line[5:].strip()
        try:
            d = json.loads(data_str)
            if event_type == 'token':
                text = d.get('text', '')
                if isinstance(text, list):
                    text = ''.join(t.get('text','') if isinstance(t,dict) else str(t) for t in text)
                print(text, end='', flush=True)
            elif event_type == 'tool_call':
                print(f'\n  [calling {d.get(\"tool\",\"?\")}...]', flush=True)
            elif event_type == 'done':
                print()
        except:
            pass
"
