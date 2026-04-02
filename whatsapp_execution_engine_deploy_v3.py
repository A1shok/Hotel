from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import psycopg2
from datetime import datetime, timedelta
from openai import OpenAI

# ------------------ INIT ------------------
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

app = Flask(__name__)
CORS(app)

# ------------------ DATABASE ------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS tasks (
    id SERIAL PRIMARY KEY,
    message TEXT,
    intent TEXT,
    priority TEXT,
    status TEXT,
    created_at TIMESTAMP,
    deadline TIMESTAMP,
    user_number TEXT
)
""")
conn.commit()

# ------------------ CONFIG ------------------
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886"

STAFF_USERS = {
    "whatsapp:+916303484136": "maintenance"
}

ROLE_TO_NUMBER = {}
for num, role in STAFF_USERS.items():
    ROLE_TO_NUMBER.setdefault(role, num)

# ------------------ AI ------------------
def ai_classify(message):
    if len(message) > 200:
        return {"intent": "general", "task": message, "priority": "low"}

    prompt = f'''
Classify hotel guest request.

Message: "{message}"

Return JSON:
{{"intent": "", "task": "", "priority": ""}}
'''

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}]
        )

        import json
        return json.loads(response.choices[0].message.content)

    except Exception as e:
        print("AI error:", e)
        return {"intent": "general", "task": message, "priority": "low"}

# ------------------ HELPERS ------------------
def send_whatsapp(to_number, message):
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print(f"[SIMULATED SEND] {message}")
        return

    try:
        from twilio.rest import Client
        client_twilio = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client_twilio.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to_number
        )
    except Exception as e:
        print("Twilio error:", e)

def generate_reply(intent):
    if intent == "maintenance":
        return "Technician is on the way 🔧"
    if intent == "housekeeping":
        return "Housekeeping will handle it shortly 🧹"
    if intent == "service":
        return "Your request is being prepared 🍽️"
    if intent == "frontdesk":
        return "Front desk is checking this 🛎️"
    if intent == "billing":
        return "We are checking your bill 💳"
    return "Got it 👍"

def get_deadline(priority):
    now = datetime.now()
    if priority == "high":
        return now + timedelta(minutes=10)
    if priority == "medium":
        return now + timedelta(minutes=30)
    return now + timedelta(minutes=60)

# ------------------ ROUTES ------------------

@app.route("/test")
def test():
    return "WORKING"

@app.route("/ai_test")
def ai_test():
    return ai_classify("My room is very hot, AC not working")

@app.route("/")
def dashboard():
    return send_file("manager_dashboard_premium_v3_deploy.html")

@app.route("/tasks")
def get_tasks():
    cur.execute("SELECT * FROM tasks ORDER BY id DESC")
    rows = cur.fetchall()

    tasks = []
    for row in rows:
        tasks.append({
            "id": row[0],
            "message": row[1],
            "intent": row[2],
            "priority": row[3],
            "status": row[4],
            "created_at": str(row[5]),
            "deadline": str(row[6]),
            "user": row[7]
        })

    return jsonify(tasks)

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.values.get('Body', '')
    user = request.values.get('From', '')

    if user in STAFF_USERS:
        return handle_staff(msg, user)

    return handle_guest(msg, user)

def handle_guest(msg, user):
    ai = ai_classify(msg)

    intent = ai["intent"]
    task_text = ai["task"]
    priority = ai["priority"]
    deadline = get_deadline(priority)

    cur.execute("""
    INSERT INTO tasks (message, intent, priority, status, created_at, deadline, user_number)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (
        task_text,
        intent,
        priority,
        "Assigned",
        datetime.now(),
        deadline,
        user
    ))
    conn.commit()

    staff_number = ROLE_TO_NUMBER.get(intent)
    if staff_number:
        send_whatsapp(staff_number, f"Task: {task_text}")

    reply = generate_reply(intent)

    return f"<Response><Message>{reply}</Message></Response>"

def handle_staff(msg, user):
    if "done" in msg.lower():
        cur.execute("""
        UPDATE tasks SET status='Completed'
        WHERE id = (
            SELECT id FROM tasks WHERE status='Assigned'
            ORDER BY id DESC LIMIT 1
        )
        """)
        conn.commit()

        return "<Response><Message>Done ✅</Message></Response>"

    return "<Response><Message>Update received</Message></Response>"

# ------------------ RUN ------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
