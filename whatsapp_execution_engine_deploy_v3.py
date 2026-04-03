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
    user_number TEXT,
    escalated BOOLEAN DEFAULT FALSE
)
""")
conn.commit()

IST = pytz.timezone("Asia/Kolkata")

STAFF_USERS = {
    "whatsapp:+916303484136": "maintenance"
}

# Multiple managers
MANAGER_NUMBERS = [
    "whatsapp:+917780210871",
    "whatsapp:+919160373362"
]

ROLE_TO_NUMBER = {v: k for k, v in STAFF_USERS.items()}

def to_ist(dt):
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(IST)

def get_sla_status(deadline):
    now = datetime.utcnow().replace(tzinfo=pytz.utc)
    if deadline.tzinfo is None:
        deadline = pytz.utc.localize(deadline)

    remaining = deadline - now
    seconds = int(remaining.total_seconds())

    if seconds <= 0:
        return "overdue", 0

    return "active", seconds // 60

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

def handle_staff(msg, user):
    if "done" in msg.lower():

        cur.execute("""
        SELECT id, message FROM tasks
        WHERE status='Assigned'
        ORDER BY id DESC LIMIT 1
        """)
        task = cur.fetchone()

        if task:
            task_id, task_msg = task

            cur.execute("""
            UPDATE tasks SET status='Completed'
            WHERE id=%s
            """, (task_id,))
            conn.commit()

            # Notify all managers
            for manager in MANAGER_NUMBERS:
                send_whatsapp(
                    manager,
                    f"✅ Task Completed\nTask #{task_id}\n{task_msg}"
                )

        return "<Response><Message>Task completed ✅</Message></Response>"

    return "<Response><Message>Update received</Message></Response>"

@app.route("/")
def dashboard():
    return send_file("manager_dashboard_premium_v3_deploy.html")

@app.route("/tasks")
def get_tasks():
    cur.execute("SELECT * FROM tasks ORDER BY id DESC")
    rows = cur.fetchall()

    tasks = []

    for row in rows:
        created = to_ist(row[5])
        deadline = row[6]

        sla_status, minutes_left = get_sla_status(deadline)

        if sla_status == "overdue" and not row[8]:
            for manager in MANAGER_NUMBERS:
                send_whatsapp(
                    manager,
                    f"🚨 ESCALATION: Task #{row[0]} overdue\n{row[1]}"
                )
            cur.execute("UPDATE tasks SET escalated=TRUE WHERE id=%s", (row[0],))
            conn.commit()

        tasks.append({
            "id": row[0],
            "message": row[1],
            "intent": row[2],
            "priority": row[3],
            "status": row[4],
            "created_at": created.isoformat(),
            "deadline": to_ist(deadline).isoformat(),
            "sla_status": sla_status,
            "minutes_left": minutes_left,
            "escalated": row[8]
        })

    return jsonify(tasks)

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.values.get('Body', '')
    user = request.values.get('From', '')

    if user in STAFF_USERS:
        return handle_staff(msg, user)

    intent = "maintenance"
    task_text = msg
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
        send_whatsapp(staff_number, f"🚨 Task: {task_text}")

    return "<Response><Message>Task received</Message></Response>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
