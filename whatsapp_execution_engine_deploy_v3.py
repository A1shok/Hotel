from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import psycopg2
import json
import re
from datetime import datetime
from openai import OpenAI
from twilio.rest import Client

app = Flask(__name__)
CORS(app)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
cur = conn.cursor()

STAFF_NUMBER = "whatsapp:+916303484136"

# ---------- DB ----------

cur.execute("""
CREATE TABLE IF NOT EXISTS tasks (
    id SERIAL PRIMARY KEY,
    message TEXT,
    intent TEXT,
    priority TEXT,
    status TEXT,
    created_at TIMESTAMP,
    user_number TEXT,
    assigned_to TEXT
)
""")
conn.commit()

# ---------- HELPERS ----------

def send_whatsapp(to: str, msg: str) -> None:
    try:
        Client(
            os.environ.get("TWILIO_ACCOUNT_SID"),
            os.environ.get("TWILIO_AUTH_TOKEN")
        ).messages.create(
            body=msg,
            from_="whatsapp:+14155238886",
            to=to
        )
    except Exception as e:
        print("Twilio error:", e)

def extract_task_id(msg: str):
    match = re.search(r"\d+", msg)
    return int(match.group()) if match else None

def get_task(task_id: int):
    cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
    return cur.fetchone()

def get_latest_active(user_number: str):
    cur.execute("""
    SELECT id, intent, priority
    FROM tasks
    WHERE user_number=%s AND status='Active'
    ORDER BY id DESC
    LIMIT 1
    """, (user_number,))
    return cur.fetchone()

def get_latest_active_by_intent(user_number: str, intent: str):
    cur.execute("""
    SELECT id, intent, priority
    FROM tasks
    WHERE user_number=%s AND intent=%s AND status='Active'
    ORDER BY id DESC
    LIMIT 1
    """, (user_number, intent))
    return cur.fetchone()

def create_task(user_number: str, description: str, intent: str, priority: str):
    cur.execute("""
    INSERT INTO tasks(message, intent, priority, status, created_at, user_number, assigned_to)
    VALUES(%s, %s, %s, %s, %s, %s, %s)
    RETURNING id
    """, (description, intent, priority, "Active", datetime.utcnow(), user_number, STAFF_NUMBER))
    task_id = cur.fetchone()[0]
    conn.commit()
    return task_id

def complete_task(task_id: int):
    cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
    conn.commit()

def escalate_task(task_id: int):
    cur.execute("UPDATE tasks SET priority='high' WHERE id=%s", (task_id,))
    conn.commit()

def is_noise_text(msg: str) -> bool:
    return msg.lower().strip() in ["ok", "okay", "thanks", "thank you", "hmm", "👍"]

def is_resolution_text(msg: str) -> bool:
    m = msg.lower().strip()
    if "not fixed" in m or "still not fixed" in m or "not working" in m:
        return False
    return any(x in m for x in ["fixed", "fixex", "resolved", "done", "completed", "finished"])

def is_followup_override(msg: str) -> bool:
    m = msg.lower().strip()
    return any(x in m for x in [
        "still not", "not fixed", "still waiting", "where is", "how long", "still not received", "waiting"
    ])

def is_single_word_ambiguous(msg: str) -> bool:
    words = msg.strip().split()
    if len(words) != 1:
        return False
    return words[0].lower() in ["ac", "water", "menu", "help", "tv", "geyser", "wifi"]

# ---------- AI ----------

def ai_classify(message: str):
    prompt = f"""
You are a hotel operations AI assistant.

Your job is to convert messy guest messages into structured operational decisions.

OBJECTIVE:
Understand the guest message and return structured JSON.

MESSAGE TYPES:
- greeting
- task
- query
- followup
- noise

INTENT:
- housekeeping
- maintenance
- food
- complaint
- information
- unknown

URGENCY:
- high
- medium
- low

RULES:
- ANY request needing hotel staff action -> task
- Informational question only -> query
- Status/update request for previous request -> followup
- Polite conversation opener -> greeting
- No meaningful action -> noise
- Greeting + request -> task
- Complaint about unresolved issue -> followup or complaint with high urgency
- Do NOT rely only on examples. Understand meaning.
- "Need menu" / "share menu" / "menu please" = query, not task
- "Can you send water?" / "Can you send meals to room?" = task
- "Breakfast started?" / "Is breakfast available?" = query
- "Still not fixed" / "Where is it?" / "How long?" = followup
- "Send someone immediately" / "Urgent help needed" = task, intent unknown, urgency high

Return ONLY valid JSON in this format:
{{
  "type": "",
  "intent": "",
  "urgency": "",
  "create_task": true,
  "description": ""
}}

Message: "{message}"
"""
    try:
        r = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        content = r.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
        return json.loads(content)
    except Exception as e:
        print("AI ERROR:", e)
        return {
            "type": "task",
            "intent": "unknown",
            "urgency": "high",
            "create_task": True,
            "description": message
        }

# ---------- RESPONSE LAYER ----------

def build_guest_reply(msg_type: str, intent: str, description: str) -> str:
    d = description.lower()

    if msg_type == "greeting":
        return "Hi 👋 How can I help you? You can ask for things like towels, food, or any help with your room."

    if msg_type == "noise":
        return "👍"

    if msg_type == "query":
        return "Yes 👍 I can help with that. Let me know if you'd like me to arrange it."

    if msg_type == "followup":
        return "Sorry about that. We're checking this right away and expediting it."

    if intent == "housekeeping":
        if "towel" in d:
            return "Got it 👍 Sending fresh towels to your room now."
        if "soap" in d or "toiletries" in d:
            return "Got it 👍 Toiletries will be delivered to your room shortly."
        if "clean" in d or "dirty" in d:
            return "Got it 👍 Housekeeping is on the way."
        return "Got it 👍 Housekeeping will handle this shortly."

    if intent == "maintenance":
        if "ac" in d:
            return "Got it 👍 Our maintenance team is on the way to check the AC."
        if "tv" in d:
            return "Got it 👍 Our maintenance team will check the TV shortly."
        if "geyser" in d or "hot water" in d:
            return "Got it 👍 Our maintenance team will check this right away."
        return "Got it 👍 Our maintenance team is on the way."

    if intent == "food":
        if "water" in d:
            return "Sure 👍 Drinking water is being sent to your room."
        if "meal" in d or "food" in d or "breakfast" in d or "lunch" in d or "dinner" in d:
            return "Sure 👍 Your request has been sent to the kitchen."
        return "Sure 👍 We're arranging that for you now."

    if intent == "complaint":
        return "We’re really sorry for the inconvenience. This is being handled immediately."

    return "Got it 👍 We're taking care of your request."

# ---------- STAFF FLOW ----------

def handle_staff_message(msg: str, staff_number: str) -> str:
    m = msg.lower().strip()

    if any(x in m for x in ["done", "completed", "finished"]):
        task_id = extract_task_id(msg)

        if not task_id:
            return "Reply like: done 52"

        task = get_task(task_id)
        if not task:
            return "Task not found."

        # task columns:
        # 0 id, 1 message, 2 intent, 3 priority, 4 status, 5 created_at, 6 user_number, 7 assigned_to
        guest_number = task[6]
        status = task[4]

        if status == "Completed":
            return "Task already completed."

        complete_task(task_id)
        send_whatsapp(
            guest_number,
            "Your requested service has been completed 👍 Please let us know if you need anything else."
        )
        return "✅ Marked completed and guest notified."

    if m == "1":
        return "✅ Assigned to you.\nReply:\n1 → Completed\n2 → Need help"

    if m == "2":
        return "⚠️ Need help noted. Escalating to manager flow can be added next."

    return "Reply with:\n1 → Accept\nor\ndone <task_id>"

# ---------- ROUTES ----------

@app.route("/")
def home():
    return send_file("manager_dashboard_premium_v3_deploy.html")

@app.route("/tasks")
def tasks():
    cur.execute("SELECT * FROM tasks ORDER BY id DESC")
    rows = cur.fetchall()

    data = []
    for r in rows:
        data.append({
            "id": r[0],
            "message": r[1],
            "intent": r[2],
            "priority": r[3],
            "status": r[4],
            "user_number": r[6],
            "assigned_to": r[7]
        })

    return jsonify(data)

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.values.get("Body", "").strip()
    user = request.values.get("From", "").strip()

    if not msg:
        return "<Response><Message>Please tell me how I can help.</Message></Response>"

    # ---------- STAFF SIDE ----------
    if user == STAFF_NUMBER:
        staff_reply = handle_staff_message(msg, user)
        return f"<Response><Message>{staff_reply}</Message></Response>"

    # ---------- GUEST SIDE GUARDRAILS ----------
    if is_noise_text(msg):
        return "<Response><Message>👍</Message></Response>"

    if is_resolution_text(msg):
        active = get_latest_active(user)
        if active:
            task_id = active[0]
            complete_task(task_id)
            send_whatsapp(STAFF_NUMBER, f"👤 Guest confirmed task #{task_id} is resolved")
            return "<Response><Message>Great 👍 Happy to help.</Message></Response>"
        return "<Response><Message>Glad it's resolved 👍</Message></Response>"

    if is_followup_override(msg):
        active = get_latest_active(user)
        if active:
            task_id = active[0]
            escalate_task(task_id)
            send_whatsapp(STAFF_NUMBER, f"🚨 FOLLOW-UP on TASK #{task_id} (Guest waiting)")
            return "<Response><Message>Sorry about that. We're expediting this right now.</Message></Response>"
        return "<Response><Message>I couldn't find any active request. Please tell me again.</Message></Response>"

    if is_single_word_ambiguous(msg):
        return "<Response><Message>Could you please tell me a little more detail?</Message></Response>"

    # ---------- AI DECISION ----------
    ai = ai_classify(msg)

    msg_type = ai.get("type", "task")
    intent = ai.get("intent", "unknown")
    urgency = ai.get("urgency", "medium")
    create_task = ai.get("create_task", True)
    description = ai.get("description", msg)

    # Guardrail for menu/info
    if "menu" in msg.lower():
        msg_type = "query"
        intent = "information"
        create_task = False

    # ---------- GREETING ----------
    if msg_type == "greeting":
        return f"<Response><Message>{build_guest_reply(msg_type, intent, description)}</Message></Response>"

    # ---------- QUERY ----------
    if msg_type == "query":
        return f"<Response><Message>{build_guest_reply(msg_type, intent, description)}</Message></Response>"

    # ---------- FOLLOWUP ----------
    if msg_type == "followup":
        active = get_latest_active(user)
        if active:
            task_id = active[0]
            escalate_task(task_id)
            send_whatsapp(STAFF_NUMBER, f"🚨 FOLLOW-UP on TASK #{task_id} (Guest waiting)")
            return f"<Response><Message>{build_guest_reply(msg_type, intent, description)}</Message></Response>"
        return "<Response><Message>I couldn't find any active request. Please tell me what you need.</Message></Response>"

    # ---------- TASK ----------
    if create_task:
        existing_same_intent = get_latest_active_by_intent(user, intent)

        if existing_same_intent and urgency != "high":
            return "<Response><Message>We’ve already informed the team 👍</Message></Response>"

        task_id = create_task(user, description, intent, urgency)

        staff_text = (
            f"🆕 TASK #{task_id}\n"
            f"{description}\n"
            f"Type: {intent}\n"
            f"Priority: {urgency.upper()}\n\n"
            f"Reply:\n"
            f"1 → Accept\n"
            f"done {task_id} → Complete"
        )
        send_whatsapp(STAFF_NUMBER, staff_text)

        guest_reply = build_guest_reply("task", intent, description)
        return f"<Response><Message>{guest_reply}</Message></Response>"

    # ---------- NOISE / UNKNOWN ----------
    if msg_type == "noise":
        return "<Response><Message>👍</Message></Response>"

    return "<Response><Message>Could you please clarify your request?</Message></Response>"

# ---------- RUN ----------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
