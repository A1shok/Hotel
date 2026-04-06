from flask import Flask, request
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
 user_number TEXT
)
""")
conn.commit()

# ---------- WHATSAPP ----------

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

# ---------- DB HELPERS ----------

def get_followup_task(user):
    cur.execute("""
    SELECT id, intent, created_at FROM tasks
    WHERE user_number=%s AND status='Active'
    ORDER BY created_at ASC
    """, (user,))
    
    tasks = cur.fetchall()
    
    if not tasks:
        return None

    # Return oldest active task (most likely pending)
    return tasks[0]

# ---------- AI ----------

def ai_classify(message):
    prompt = f"""
You are a hotel operations AI assistant.

Convert the guest message into STRICT JSON.

OUTPUT FORMAT:
{{
"type": "greeting | task | query | followup | noise",
"intent": "housekeeping | maintenance | food | complaint | information | unknown",
"urgency": "low | medium | high",
"create_task": true/false,
"description": "clean summary"
}}

RULES:
- If type = task → create_task MUST be true
- If type != task → create_task MUST be false
- If message needs action → MUST be task
- followup → urgency MUST be high
- greeting + request → MUST be task
- Prefer task if unsure
- If guest confirms issue is solved → type MUST be followup, create_task false, description must indicate resolution
- Output ONLY JSON

Message: "{message}"
"""

    try:
        r = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )

        content = r.choices[0].message.content.strip()

        if content.startswith("```"):
            content = content.split("```")[1]

        print("AI RAW:", content)

        return json.loads(content)

    except Exception as e:
        print("AI ERROR:", e)
        return {
            "type": "task",
            "intent": "unknown",
            "urgency": "medium",
            "create_task": True,
            "description": message
        }

# ---------- VALIDATION ----------

def validate_ai_output(data, original_msg):
    required = ["type", "intent", "urgency", "create_task", "description"]

    for field in required:
        if field not in data:
            raise ValueError(f"Missing field: {field}")

    if data["type"] not in ["greeting", "task", "query", "followup", "noise"]:
        data["type"] = "task"

    if data["urgency"] not in ["low", "medium", "high"]:
        data["urgency"] = "medium"

    if not isinstance(data["create_task"], bool):
        data["create_task"] = data["type"] == "task"

    if not data["description"]:
        data["description"] = original_msg

    return data

# ---------- RESPONSE ----------

def build_reply(msg_type):
    if msg_type == "greeting":
        return "Hi 👋 How can I help you?"
    if msg_type == "query":
        return "Sure 👍 Let me know if you want me to arrange it."
    if msg_type == "followup":
        return "Sorry about that, we're checking this."
    if msg_type == "noise":
        return "👍"
    return "Got it 👍 We're taking care of your request."

# ---------- ROUTE ----------

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.values.get("Body", "").strip()
    real_user = request.values.get("From", "")
    print("REAL NUMBER:", real_user)

    # TEMP OVERRIDE (use your working number)
    user = "whatsapp:+917780210871"

    print("INCOMING:", msg, "| USER:", user)

    if not msg:
        return "<Response><Message>Please tell me how I can help.</Message></Response>"

    # AI + VALIDATION
    ai_data = ai_classify(msg)
    ai_data = validate_ai_output(ai_data, msg)

    print("FINAL AI:", ai_data)

    msg_type = ai_data["type"]

    # ---------- RESOLUTION HANDLING ----------
    if msg_type == "followup" and "resolved" in ai_data["description"].lower():
        task = get_followup_task(user)
        if task:
            cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task[0],))
            conn.commit()

            send_whatsapp(STAFF_NUMBER, f"✅ TASK #{task[0]} marked completed by guest")

        return "<Response><Message>Great 👍 Happy to help.</Message></Response>"

    # ---------- FOLLOWUP ----------
    if msg_type == "followup":
        task = get_latest_active(user)
        if task:
            send_whatsapp(STAFF_NUMBER, f"🚨 FOLLOW-UP on TASK #{task[0]}")
        return "<Response><Message>Sorry about that, we're expediting this.</Message></Response>"

    # ---------- GREETING / QUERY / NOISE ----------
    if msg_type in ["greeting", "query", "noise"]:
        return f"<Response><Message>{build_reply(msg_type)}</Message></Response>"

    # ---------- DUPLICATE ----------
    existing = get_latest_active(user)
    if existing and existing[1] == ai_data["intent"]:
        return "<Response><Message>We're already working on your previous request 👍</Message></Response>"

    # ---------- TASK CREATION ----------
    if ai_data["create_task"]:
        cur.execute("""
        INSERT INTO tasks(message,intent,priority,status,created_at,user_number)
        VALUES(%s,%s,%s,%s,%s,%s)
        RETURNING id
        """, (
            ai_data["description"],
            ai_data["intent"],
            ai_data["urgency"],
            "Active",
            datetime.utcnow(),
            user
        ))

        task_id = cur.fetchone()[0]
        conn.commit()

        print("TASK CREATED:", task_id)

        send_whatsapp(
            STAFF_NUMBER,
            f"🆕 TASK #{task_id}\n{ai_data['description']}"
        )

    return f"<Response><Message>{build_reply(msg_type)}</Message></Response>"

# ---------- RUN ----------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
