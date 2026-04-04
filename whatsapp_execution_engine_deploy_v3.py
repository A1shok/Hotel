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

def get_active_task_by_intent(user, intent):
    cur.execute("""
    SELECT id FROM tasks
    WHERE user_number=%s AND intent=%s AND status='Active'
    ORDER BY id DESC LIMIT 1
    """, (user, intent))
    return cur.fetchone()

def get_latest_active_task(user):
    cur.execute("""
    SELECT id, intent FROM tasks
    WHERE user_number=%s AND status='Active'
    ORDER BY id DESC LIMIT 1
    """, (user,))
    return cur.fetchone()

# ---------- MESSAGE TYPE ENGINE ----------

def classify_message_type(msg):
    m = msg.lower().strip()

    if m in ["hi", "hello", "hey", "good morning", "good evening", "hi bro"]:
        return "greeting"

    if any(x in m for x in ["still", "where", "how long", "not received", "waiting"]):
        return "followup"

    if any(x in m for x in ["what", "where", "when", "wifi", "timing", "?"]):
        return "query"

    if any(x in m for x in ["need", "send", "bring", "not working", "clean"]):
        return "task"

    if m in ["ok", "okay", "thanks", "hmm"]:
        return "noise"

    return "unknown"

# ---------- HUMAN RESPONSE ----------

def build_human_reply(intent, message):
    m = message.lower()

    if "towel" in m:
        return "Got it 👍 Sending fresh towels to your room now."

    if "water" in m:
        return "Sure 👍 Drinking water is being sent to your room."

    if "soap" in m:
        return "Got it 👍 Soap will be delivered shortly."

    if "ac" in m or "not working" in m:
        return "Got it 👍 Our maintenance team is on the way."

    if "clean" in m:
        return "Housekeeping is on the way 👍"

    return "Got it 👍 We're taking care of your request."

# ---------- AI ----------

def ai_classify(message):
    prompt = f"""
You are a hotel operations AI.

Classify the guest request.

Rules:
- AC → maintenance
- cleaning/towels → housekeeping
- water/food → service

Message: "{message}"

Return JSON:
{{"intent":"","priority":"low|medium|high"}}
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

    except:
        return {"intent": "general", "priority": "low"}

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

    if not msg.strip():
        return "<Response><Message>Could you please tell me what you need?</Message></Response>"

    msg_type = classify_message_type(msg)

    # ---------- STAFF COMPLETION ----------
    if any(x in msg.lower() for x in ["done", "completed", "finished", "fixed"]):
        task = get_latest_active_task(user)

        if task:
            cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task[0],))
            conn.commit()

            send_whatsapp(user, "Your request has been completed 👍")

        return "<Response><Message>Done 👍</Message></Response>"

    # ---------- GREETING ----------
    if msg_type == "greeting":
        return "<Response><Message>Hi 👋 How can I help you?</Message></Response>"

    # ---------- QUERY ----------
    if msg_type == "query":
        return "<Response><Message>Let me check that for you.</Message></Response>"

    # ---------- FOLLOW-UP ----------
    if msg_type == "followup":
        task = get_latest_active_task(user)

        if task:
            send_whatsapp(STAFF_NUMBER, "🚨 Guest asked for update")
            return "<Response><Message>We're checking on this and will update you shortly.</Message></Response>"

        return "<Response><Message>I couldn't find any active request.</Message></Response>"

    # ---------- TASK ----------
    if msg_type == "task":
        ai = ai_classify(msg)
        intent = ai["intent"]

        # 🔥 FIX 1: FORCE INTENT CORRECTION
        m = msg.lower()
        if "ac" in m:
            intent = "maintenance"
        elif "towel" in m or "soap" in m:
            intent = "housekeeping"
        elif "water" in m:
            intent = "service"

        existing = get_active_task_by_intent(user, intent)

        # 🔥 FIX 2: FIRST TASK ALWAYS CREATED
        if existing is None:

            cur.execute("""
            INSERT INTO tasks(message,intent,priority,status,created_at,user_number)
            VALUES(%s,%s,%s,%s,%s,%s)
            """, (msg, intent, ai["priority"], "Active", datetime.utcnow(), user))
            conn.commit()

            send_whatsapp(
                STAFF_NUMBER,
                f"New task:\n{msg}\nPriority: {ai['priority'].upper()}"
            )

            reply = build_human_reply(intent, msg)
            return f"<Response><Message>{reply}</Message></Response>"

        else:
            return "<Response><Message>Got it 👍 We're already working on this issue.</Message></Response>"

    # ---------- NOISE ----------
    if msg_type == "noise":
        return "<Response><Message>👍</Message></Response>"

    return "<Response><Message>Could you please clarify your request?</Message></Response>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
