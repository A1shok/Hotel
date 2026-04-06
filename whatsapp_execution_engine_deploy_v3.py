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

def get_active_tasks(user):
    cur.execute("""
    SELECT intent, description FROM tasks
    WHERE user_number=%s AND status='Active'
    ORDER BY created_at ASC
    LIMIT 5
    """, (user,))
    return cur.fetchall()

def format_tasks_context(tasks):
    if not tasks:
        return "No active tasks"

    lines = []
    for i, (intent, desc) in enumerate(tasks, 1):
        lines.append(f"{i}. {intent} - {desc}")

    return "\n".join(lines)

def get_task_by_intent(user, intent):
    cur.execute("""
    SELECT id FROM tasks
    WHERE user_number=%s AND status='Active' AND intent=%s
    ORDER BY created_at ASC LIMIT 1
    """, (user, intent))
    return cur.fetchone()

def get_latest_active(user):
    cur.execute("""
    SELECT id FROM tasks
    WHERE user_number=%s AND status='Active'
    ORDER BY created_at DESC LIMIT 1
    """, (user,))
    return cur.fetchone()

# ---------- AI ----------

def ai_classify(message, tasks_context):
    prompt = f"""
You are a hotel operations AI assistant.

---

🧾 ACTIVE TASKS (CONTEXT)

{tasks_context}

---

🎯 OUTPUT FORMAT (STRICT JSON)

{{
"type": "greeting | task | query | followup | noise",
"intent": "housekeeping | maintenance | food | complaint | information | unknown",
"urgency": "low | medium | high",
"create_task": true/false,
"description": "clean summary",
"resolution": true/false,
"reference_intent": ""
}}

---

🧠 DECISION HIERARCHY (STRICT — FOLLOW EXACTLY)

1. If message refers to an EXISTING task (delay, not received, not fixed, waiting, where is it):
   → type = "followup"
   → create_task = false
   → MUST set reference_intent using ACTIVE TASKS

2. If message confirms completion:
   → type = "followup"
   → resolution = true
   → create_task = false

3. If message requests NEW work:
   → type = "task"
   → create_task = true

4. If informational:
   → query

5. Greeting:
   → greeting

6. Else:
   → noise

---

🚨 FOLLOW-UP OVERRIDES TASK

Even if message sounds like a problem,
if it relates to an existing task,
it MUST be followup — NOT task.

---

⚠️ RULES

- ALWAYS return valid JSON
- NO extra text
- ALL fields required

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
            "resolution": False,
            "reference_intent": "unknown"
        }

# ---------- VALIDATION ----------

def validate_ai_output(data, original_msg):
    required = ["type", "intent", "urgency", "create_task", "description", "resolution", "reference_intent"]

    for field in required:
        if field not in data:
            data[field] = False if field in ["create_task", "resolution"] else "unknown"

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
    user = "whatsapp:+917780210871"

    tasks = get_active_tasks(user)
    tasks_context = format_tasks_context(tasks)

    print("TASK CONTEXT:\n", tasks_context)

    ai_data = validate_ai_output(ai_classify(msg, tasks_context), msg)

    print("FINAL AI:", ai_data)

    msg_type = ai_data["type"]

    # ---------- RESOLUTION ----------
    if ai_data["resolution"]:
        task = get_latest_active(user)
        if task:
            cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task[0],))
            conn.commit()
            send_whatsapp(STAFF_NUMBER, f"✅ TASK #{task[0]} marked completed")
        return "<Response><Message>Great 👍 Happy to help.</Message></Response>"

    # ---------- FOLLOWUP ----------
    if msg_type == "followup":
        task = None

        if ai_data["reference_intent"] != "unknown":
            task = get_task_by_intent(user, ai_data["reference_intent"])

        if not task:
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

        send_whatsapp(STAFF_NUMBER, f"🆕 TASK #{task_id}\n{ai_data['description']}")

    return f"<Response><Message>{build_reply(msg_type)}</Message></Response>"

# ---------- RUN ----------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
