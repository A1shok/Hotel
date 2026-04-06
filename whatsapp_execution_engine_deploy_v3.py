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

def get_latest_active(user):
    cur.execute("""
    SELECT id, intent, priority FROM tasks
    WHERE user_number=%s AND status='Active'
    ORDER BY id DESC LIMIT 1
    """, (user,))
    return cur.fetchone()

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
"description": "clean summary",
"resolution": true/false
}}

---

🚨 CRITICAL DECISION RULE

If message requires staff action:
"type" MUST be "task"
"create_task" MUST be true

---

🧠 DECISION PRIORITY

1. Requires action → task
2. Status check → followup
3. Question → query
4. Greeting → greeting
5. Else → noise

---

🔁 RESOLUTION RULE

If guest confirms issue is solved:
- type → followup
- create_task → false
- resolution → true

Else:
- resolution → false

---

⚠️ OUTPUT RULES

- ONLY JSON
- ALL fields required
- NO extra text

---

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
            "description": message,
            "resolution": False
        }

# ---------- VALIDATION ----------

def validate_ai_output(data, original_msg):
    required = ["type", "intent", "urgency", "create_task", "description", "resolution"]

    for field in required:
        if field not in data:
            raise ValueError(f"Missing field: {field}")

    if data["type"] not in ["greeting", "task", "query", "followup", "noise"]:
        data["type"] = "task"

    if data["urgency"] not in ["low", "medium", "high"]:
        data["urgency"] = "medium"

    if not isinstance(data["create_task"], bool):
        data["create_task"] = data["type"] == "task"

    if not isinstance(data["resolution"], bool):
        data["resolution"] = False

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

    # TEMP override
    user = "whatsapp:+917780210871"

    print("INCOMING:", msg, "| USER:", user)

    if not msg:
        return "<Response><Message>Please tell me how I can help.</Message></Response>"

    # AI + validation
    ai_data = ai_classify(msg)
    ai_data = validate_ai_output(ai_data, msg)

    print("FINAL AI:", ai_data)

    msg_type = ai_data["type"]

    # ---------- RESOLUTION (CLEAN) ----------
    if ai_data["resolution"]:
        task = get_latest_active(user)
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

    # ---------- NON-TASK ----------
    if msg_type in ["greeting", "query", "noise"]:
        return f"<Response><Message>{build_reply(msg_type)}</Message></Response>"

    # ---------- TASK ----------
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
