from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os, psycopg2, json
from datetime import datetime
from openai import OpenAI
from twilio.rest import Client

app = Flask(__name__)
CORS(app)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS tasks (
 id SERIAL PRIMARY KEY,
 message TEXT,
 intent TEXT,
 priority TEXT,
 status TEXT,
 created_at TIMESTAMP,
 user_number TEXT
)
""")
conn.commit()

STAFF_NUMBER = "whatsapp:+916303484136"

# ---------- HELPERS ----------

def send_whatsapp(to, msg):
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

def get_latest_active_task(user):
    cur.execute("""
    SELECT id, intent FROM tasks
    WHERE user_number=%s AND status='Active'
    ORDER BY id DESC LIMIT 1
    """, (user,))
    return cur.fetchone()

# ---------- GUARDRAILS ----------

def is_noise(msg):
    return msg.lower().strip() in ["ok", "okay", "thanks", "hmm", "👍"]

def is_resolution(msg):
    return any(x in msg.lower() for x in ["fixed", "fixex", "resolved", "done", "completed"])

def is_single_word(msg):
    return len(msg.strip().split()) == 1

def is_menu_query(msg):
    return "menu" in msg.lower()

# ---------- AI ----------

def ai_classify(message):
    prompt = f"""
You are a hotel operations AI assistant.

Your job is to convert messy guest messages into structured operational decisions.

🎯 OBJECTIVE
Understand the guest message and return structured JSON.

🧠 MESSAGE TYPES
- greeting
- task
- query
- followup
- noise

🧾 INTENT
- housekeeping
- maintenance
- food
- complaint
- information
- unknown

⚡ URGENCY
- high
- medium
- low

🧩 RULES
- ANY request needing staff → task
- Questions → query
- Status check → followup
- Complaints → high
- Greeting + request → task
- Vague urgent → task + high
- Understand meaning, not keywords

🧱 OUTPUT
Return ONLY JSON:
{{
  "type": "",
  "intent": "",
  "urgency": "",
  "create_task": true/false,
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

# ---------- HUMAN RESPONSE ----------

def build_reply(ai):
    t = ai["type"]
    intent = ai["intent"]

    if t == "greeting":
        return "Hi 👋 How can I help you?"

    if t == "query":
        return "Let me check that for you."

    if t == "followup":
        return "We're already checking this 👍"

    if t == "noise":
        return "👍"

    if intent == "housekeeping":
        return "Got it 👍 Housekeeping will handle this shortly."

    if intent == "maintenance":
        return "Got it 👍 Our maintenance team is on the way."

    if intent == "food":
        return "Sure 👍 Your request has been sent to the kitchen."

    if intent == "complaint":
        return "We’re really sorry. This is being handled immediately."

    return "Got it 👍 We're taking care of your request."

# ---------- ROUTES ----------

@app.route("/")
def home():
    return send_file("dashboard.html")

@app.route("/tasks")
def tasks():
    cur.execute("SELECT * FROM tasks ORDER BY id DESC")
    rows = cur.fetchall()

    data = []
    for r in rows:
        data.append({
            "id": r[0],
            "message": r[1],
            "priority": r[3],
            "status": r[4]
        })

    return jsonify(data)

# ---------- WHATSAPP ----------

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.values.get('Body', '').strip()
    user = request.values.get('From', '')

    if not msg:
        return "<Response><Message>Please tell me how I can help.</Message></Response>"

    # ---------- NOISE ----------
    if is_noise(msg):
        return "<Response><Message>👍</Message></Response>"

    # ---------- RESOLUTION ----------
    if is_resolution(msg):
        task = get_latest_active_task(user)
        if task:
            cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task[0],))
            conn.commit()
            send_whatsapp(user, "Glad it's resolved 👍")
        return "<Response><Message>Great 👍 Happy to help.</Message></Response>"

    # ---------- SINGLE WORD ----------
    if is_single_word(msg):
        return "<Response><Message>Could you please tell me more details?</Message></Response>"

    # ---------- AI ----------
    ai = ai_classify(msg)

    msg_type = ai["type"]
    intent = ai["intent"]
    urgency = ai["urgency"]
    create_task = ai["create_task"]
    description = ai["description"]

    # ---------- MENU FIX ----------
    if is_menu_query(msg):
        msg_type = "query"
        create_task = False
        intent = "information"

    # ---------- GREETING ----------
    if msg_type == "greeting":
        return "<Response><Message>Hi 👋 How can I help you?</Message></Response>"

    # ---------- FOLLOWUP (SMART) ----------
    if msg_type == "followup":
        task = get_latest_active_task(user)

        if task:
            task_id, task_intent = task

            cur.execute("UPDATE tasks SET priority='high' WHERE id=%s", (task_id,))
            conn.commit()

            send_whatsapp(
                STAFF_NUMBER,
                f"🚨 FOLLOW-UP on TASK #{task_id} (Guest waiting)"
            )

            return "<Response><Message>Sorry about that, we're expediting this right now.</Message></Response>"

        else:
            return "<Response><Message>I couldn't find any active request. Please tell me again.</Message></Response>"

    # ---------- DUPLICATE CONTROL ----------
    existing = get_latest_active_task(user)
    if create_task and existing and existing[1] == intent:
        return "<Response><Message>We're already working on this 👍</Message></Response>"

    # ---------- CREATE TASK ----------
    if create_task:
        cur.execute("""
        INSERT INTO tasks(message,intent,priority,status,created_at,user_number)
        VALUES(%s,%s,%s,%s,%s,%s)
        """, (description, intent, urgency, "Active", datetime.utcnow(), user))
        conn.commit()

        send_whatsapp(
            STAFF_NUMBER,
            f"New task:\n{description}\nType: {intent}\nPriority: {urgency.upper()}"
        )

    # ---------- RESPONSE ----------
    reply = build_reply(ai)
    return f"<Response><Message>{reply}</Message></Response>"

# ---------- RUN ----------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
