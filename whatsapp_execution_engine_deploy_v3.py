from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os, psycopg2, json, re
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

def extract_task_id(msg):
    match = re.search(r"\d+", msg)
    return int(match.group()) if match else None

def get_task(task_id):
    cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
    return cur.fetchone()

def get_latest_active(user):
    cur.execute("""
    SELECT id, intent, priority
    FROM tasks
    WHERE user_number=%s AND status='Active'
    ORDER BY id DESC LIMIT 1
    """, (user,))
    return cur.fetchone()

def create_task(user, description, intent, priority):
    cur.execute("""
    INSERT INTO tasks(message,intent,priority,status,created_at,user_number,assigned_to)
    VALUES(%s,%s,%s,%s,%s,%s,%s)
    RETURNING id
    """, (description, intent, priority, "Active", datetime.utcnow(), user, STAFF_NUMBER))
    task_id = cur.fetchone()[0]
    conn.commit()
    return task_id

def complete_task(task_id):
    cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
    conn.commit()

def escalate_task(task_id):
    cur.execute("UPDATE tasks SET priority='high' WHERE id=%s", (task_id,))
    conn.commit()

# ---------- GUARDRAILS ----------

def is_noise(msg):
    return msg.lower().strip() in ["ok", "okay", "thanks", "hmm", "👍"]

def is_resolution(msg):
    m = msg.lower()
    if "not fixed" in m or "still not fixed" in m:
        return False
    return any(x in m for x in ["fixed", "fixex", "resolved", "done", "completed"])

def is_followup_text(msg):
    m = msg.lower()
    return any(x in m for x in ["still not", "not fixed", "waiting", "where is", "how long"])

def is_single_word_ambiguous(msg):
    words = msg.strip().split()
    if len(words) != 1:
        return False
    return words[0].lower() in ["ac", "help", "tv", "geyser"]

# ---------- AI ----------

def ai_classify(message):
    prompt = f"""
Classify hotel message.

Return JSON:
{{
"type": "greeting|task|query|followup|noise",
"intent": "housekeeping|maintenance|food|complaint|information|unknown",
"urgency": "low|medium|high",
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
        return json.loads(r.choices[0].message.content)
    except:
        return {
            "type": "task",
            "intent": "unknown",
            "urgency": "high",
            "create_task": True,
            "description": message
        }

# ---------- RESPONSE ----------

def build_reply(intent):
    if intent == "housekeeping":
        return "Got it 👍 Housekeeping will handle this shortly."
    if intent == "maintenance":
        return "Got it 👍 Our maintenance team is on the way."
    if intent == "food":
        return "Sure 👍 Your request has been sent."
    if intent == "complaint":
        return "We’re really sorry. This is being handled immediately."
    return "Got it 👍 We're taking care of your request."

# ---------- STAFF ----------

def handle_staff(msg):
    if "done" in msg.lower():
        task_id = extract_task_id(msg)

        if not task_id:
            return "Send: done <task_id>"

        task = get_task(task_id)

        if task:
            complete_task(task_id)
            guest = task[6]
            send_whatsapp(guest, "Your request has been completed 👍")

        return "Task completed"

    return "Reply: done <task_id>"

# ---------- ROUTE ----------

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.values.get("Body", "").strip()
    user = request.values.get("From", "")

    if not msg:
        return "<Response><Message>Please tell me how I can help.</Message></Response>"

    # STAFF FLOW
    if user == STAFF_NUMBER:
        return f"<Response><Message>{handle_staff(msg)}</Message></Response>"

    # NOISE
    if is_noise(msg):
        return "<Response><Message>👍</Message></Response>"

    # RESOLUTION
    if is_resolution(msg):
        task = get_latest_active(user)
        if task:
            complete_task(task[0])
            send_whatsapp(STAFF_NUMBER, f"Guest resolved task #{task[0]}")
        return "<Response><Message>Great 👍 Happy to help.</Message></Response>"

    # FOLLOWUP
    if is_followup_text(msg):
        task = get_latest_active(user)
        if task:
            escalate_task(task[0])
            send_whatsapp(STAFF_NUMBER, f"🚨 Follow-up on task #{task[0]}")
        return "<Response><Message>Sorry, we're expediting this.</Message></Response>"

    # SINGLE WORD
    if is_single_word_ambiguous(msg):
        return "<Response><Message>Could you please tell me more details?</Message></Response>"

    # AI
    ai = ai_classify(msg)

    msg_type = ai["type"]
    intent = ai["intent"]
    urgency = ai["urgency"]
    create = ai["create_task"]
    desc = ai["description"]

    # GREETING
    if msg_type == "greeting":
        return "<Response><Message>Hi 👋 How can I help you?</Message></Response>"

    # QUERY
    if msg_type == "query":
        return "<Response><Message>Yes 👍 I can help with that. Let me know if you'd like me to arrange it.</Message></Response>"

    # DUPLICATE CONTROL (FIXED)
    existing = get_latest_active(user)
    if existing:
        if desc.lower().strip() == msg.lower().strip():
            return "<Response><Message>We're already working on your previous request 👍</Message></Response>"

    # TASK
    if create:
        task_id = create_task(user, desc, intent, urgency)

        send_whatsapp(
            STAFF_NUMBER,
            f"🆕 TASK #{task_id}\n{desc}\nPriority: {urgency.upper()}"
        )

        return f"<Response><Message>{build_reply(intent)}</Message></Response>"

    return "<Response><Message>Please clarify your request.</Message></Response>"

# ---------- RUN ----------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
