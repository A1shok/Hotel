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
    SELECT id, priority FROM tasks
    WHERE user_number=%s AND status='Active'
    ORDER BY id DESC LIMIT 1
    """, (user,))
    return cur.fetchone()

# ---------- SMALL TALK ----------

def handle_small_talk(msg):
    m = msg.lower().strip()

    if m in ["hi", "hello", "hey"]:
        return "Hello! How can I assist you today?"

    if m in ["thanks", "thank you"]:
        return "You're welcome! Let me know if you need anything."

    return None

# ---------- INTENT CONTROL ----------

def should_create_task(message):
    m = message.lower().strip()

    if m in ["hi", "hello", "hey", "thanks", "thank you", "ok", "okay"]:
        return False

    if "thank" in m and "fix" in m:
        return False

    return True

def is_followup(msg):
    m = msg.lower()
    return any(x in m for x in ["still", "again", "not received"]) and len(m.split()) <= 6

def is_resolution(msg):
    m = msg.lower()

    if "not fixed" in m or "not working" in m:
        return False

    return any(x in m for x in ["fixed", "resolved", "working now", "done"])

def is_angry(msg):
    m = msg.lower()
    return any(x in m for x in ["worst", "bad", "angry", "not fixed"])

def is_staff_done(msg):
    m = msg.lower()
    return any(x in m for x in ["done", "completed", "finished", "fixed", "resolved"])

# ---------- AI (RESTORED PROMPT) ----------

def ai_classify(message):
    prompt = f"""
You are a hotel operations AI.

Classify the guest request and generate a reply.

Rules:
- AC / temperature → maintenance (high)
- cleaning / towels → housekeeping (medium)
- food / water → service (medium)

Message: "{message}"

Return JSON:
{{
 "intent": "maintenance|housekeeping|service|general",
 "priority": "low|medium|high",
 "reply": "natural human-like reply"
}}
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
            "intent": "maintenance",
            "priority": "high",
            "reply": "We are handling your request."
        }

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
            "priority": r[3],
            "status": r[4]
        })

    return jsonify(data)

# ---------- WHATSAPP ----------

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.values.get('Body', '')
    user = request.values.get('From', '')

    clean_msg = msg.strip()

    # empty guard
    if not clean_msg or clean_msg in ['"', "'"]:
        return "<Response><Message>Could you please tell me what you need?</Message></Response>"

    # ---------- STAFF COMPLETION ----------
    if is_staff_done(msg):
        task = get_latest_active_task(user)

        if not task:
            return "<Response><Message>No active task found</Message></Response>"

        task_id = task[0]

        cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
        conn.commit()

        send_whatsapp(
            user,
            "Your request has been completed. Please let us know if you need anything else."
        )

        return "<Response><Message>Marked as completed 👍</Message></Response>"

    # ---------- SMALL TALK ----------
    small = handle_small_talk(msg)
    if small:
        return f"<Response><Message>{small}</Message></Response>"

    ai = ai_classify(msg)
    intent = ai["intent"]
    priority = ai["priority"]
    reply = ai["reply"]

    # ---------- ANGRY ----------
    if is_angry(msg):
        task = get_latest_active_task(user)
        if task:
            send_whatsapp(STAFF_NUMBER, "🚨 Guest is frustrated - urgent attention needed")
            return "<Response><Message>I’m really sorry about this. I’ve escalated it and we’re prioritizing it immediately.</Message></Response>"

    # ---------- RESOLUTION ----------
    if is_resolution(msg):
        task = get_latest_active_task(user)

        if not task:
            return "<Response><Message>I couldn't find an active request. Please tell me what you need.</Message></Response>"

        task_id = task[0]

        cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
        conn.commit()

        send_whatsapp(STAFF_NUMBER, "👤 Guest confirmed issue is resolved")

        return "<Response><Message>Great, glad everything is sorted out! Let me know if you need anything else.</Message></Response>"

    # ---------- FOLLOW-UP ----------
    if is_followup(msg):
        task = get_latest_active_task(user)

        if not task:
            return "<Response><Message>I couldn't find an active request. Please tell me what you need.</Message></Response>"

        task_id, current = task

        if current == "urgent":
            return "<Response><Message>I've already escalated this. The team is on it.</Message></Response>"

        cur.execute("UPDATE tasks SET priority='urgent' WHERE id=%s", (task_id,))
        conn.commit()

        send_whatsapp(STAFF_NUMBER, "🚨 Task escalated by guest")

        return "<Response><Message>Sorry about that, I've escalated this and it will be handled right away.</Message></Response>"

    # ---------- NEW TASK ----------
    task = get_latest_active_task(user)

    if should_create_task(msg) and not task:

        cur.execute("""
        INSERT INTO tasks(message,intent,priority,status,created_at,user_number)
        VALUES(%s,%s,%s,%s,%s,%s)
        """, (msg, intent, priority, "Active", datetime.utcnow(), user))
        conn.commit()

        send_whatsapp(
            STAFF_NUMBER,
            f"New task:\n{msg}\nPriority: {priority.upper()}"
        )

    return f"<Response><Message>{reply}</Message></Response>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
