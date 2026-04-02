from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import psycopg2
from datetime import datetime, timedelta
from openai import OpenAI
from twilio.rest import Client
import pytz

app = Flask(__name__)
CORS(app)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

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

IST = pytz.timezone("Asia/Kolkata")

STAFF_USERS = {
    "whatsapp:+916303484136": "maintenance"
}

ROLE_TO_NUMBER = {v: k for k, v in STAFF_USERS.items()}

def ai_classify(message):
    prompt = f'''
You are an intelligent hotel operations AI.

Rules:
- AC issues → maintenance, high
- Cleaning → housekeeping, medium
- Food → service, medium
- Urgent discomfort → high

Message: "{message}"

Return ONLY JSON:
{{"intent": "", "task": "", "priority": ""}}
'''
    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        import json
        return json.loads(response.choices[0].message.content)
    except:
        return {"intent": "general", "task": message, "priority": "low"}

def get_deadline(priority):
    now = datetime.utcnow()
    if priority == "high":
        return now + timedelta(minutes=10)
    elif priority == "medium":
        return now + timedelta(minutes=30)
    return now + timedelta(minutes=60)

def send_whatsapp(to_number, message):
    try:
        client_twilio = Client(
            os.environ.get("TWILIO_ACCOUNT_SID"),
            os.environ.get("TWILIO_AUTH_TOKEN")
        )
        client_twilio.messages.create(
            body=message,
            from_="whatsapp:+14155238886",
            to=to_number
        )
    except Exception as e:
        print("Twilio error:", e)

def generate_reply(intent):
    if intent == "maintenance":
        return "Technician is on the way 🔧"
    if intent == "housekeeping":
        return "Housekeeping will handle it 🧹"
    if intent == "service":
        return "Service request received 🍽️"
    return "Got it 👍"

@app.route("/")
def dashboard():
    return send_file("manager_dashboard_premium_v3_deploy.html")

@app.route("/tasks")
def get_tasks():
    cur.execute("SELECT * FROM tasks ORDER BY id DESC")
    rows = cur.fetchall()

    tasks = []
    for row in rows:
        created = row[5].replace(tzinfo=pytz.utc).astimezone(IST)
        deadline = row[6].replace(tzinfo=pytz.utc).astimezone(IST)

        tasks.append({
            "id": row[0],
            "message": row[1],
            "intent": row[2],
            "priority": row[3],
            "status": row[4],
            "created_at": created.strftime("%I:%M:%S %p"),
            "deadline": deadline.strftime("%I:%M:%S %p"),
            "user": row[7]
        })

    return jsonify(tasks)

@app.route("/ai_test")
def ai_test():
    return ai_classify("My room is too hot")

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.values.get('Body', '')
    user = request.values.get('From', '')

    ai = ai_classify(msg)

    intent = ai["intent"]
    task_text = ai["task"]
    priority = ai["priority"]

    if "hot" in msg.lower() or "ac" in msg.lower():
        intent = "maintenance"
        priority = "high"

    deadline = get_deadline(priority)

    cur.execute("""
    INSERT INTO tasks (message, intent, priority, status, created_at, deadline, user_number)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (
        task_text,
        intent,
        priority,
        "Assigned",
        datetime.utcnow(),
        deadline,
        user
    ))
    conn.commit()

    staff_number = ROLE_TO_NUMBER.get(intent)
    if staff_number:
        send_whatsapp(
            staff_number,
            f"🚨 New Task:\n{task_text}\nPriority: {priority}"
        )

    return f"<Response><Message>{generate_reply(intent)}</Message></Response>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
