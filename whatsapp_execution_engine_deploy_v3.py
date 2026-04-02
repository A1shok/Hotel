from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import json
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

DATA_FILE = "tasks.json"

def load_tasks():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            try:
                return json.load(f)
            except Exception:
                return []
    return []

def save_tasks(tasks):
    with open(DATA_FILE, "w") as f:
        json.dump(tasks, f, indent=2)

tasks = load_tasks()

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886"

STAFF_USERS = {
    "whatsapp:+916303484136": "maintenance"
}

ROLE_TO_NUMBER = {}
for num, role in STAFF_USERS.items():
    ROLE_TO_NUMBER.setdefault(role, num)

def send_whatsapp(to_number, message):
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print(f"[SIMULATED SEND] {message}")
        return

    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to_number
        )
    except Exception as e:
        print("Twilio error:", e)

def get_priority(msg):
    msg = msg.lower()
    if "ac" in msg or "not working" in msg:
        return "high"
    if "dirty" in msg:
        return "medium"
    return "low"

def get_deadline(priority):
    now = datetime.now()
    if priority == "high":
        return (now + timedelta(minutes=10)).isoformat()
    if priority == "medium":
        return (now + timedelta(minutes=30)).isoformat()
    return (now + timedelta(minutes=60)).isoformat()

def classify(msg):
    msg = msg.lower()
    if "ac" in msg or "not working" in msg:
        return "maintenance"
    if "water" in msg or "food" in msg:
        return "service"
    return "general"

@app.route("/test")
def test():
    return "WORKING"

@app.route("/")
def dashboard():
    try:
        path = os.path.join(os.getcwd(), "manager_dashboard_premium_v3_deploy.html")
        return send_file(path)
    except Exception as e:
        return f"ERROR: {str(e)}", 500

@app.route("/tasks", methods=["GET"])
def get_tasks():
    return jsonify(tasks)

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    msg = request.values.get('Body', '')
    user = request.values.get('From', '')

    if user in STAFF_USERS:
        return handle_staff(msg, user)

    return handle_guest(msg, user)

def handle_guest(msg, user):
    intent = classify(msg)
    priority = get_priority(msg)
    deadline = get_deadline(priority)

    task = {
        "id": len(tasks) + 1,
        "user": user,
        "message": msg,
        "intent": intent,
        "priority": priority,
        "created_at": datetime.now().isoformat(),
        "deadline": deadline,
        "status": "Assigned",
        "escalated": False
    }

    tasks.append(task)
    save_tasks(tasks)

    staff_number = ROLE_TO_NUMBER.get(intent)
    if staff_number:
        send_whatsapp(staff_number, f"Task {task['id']}\n{msg}")

    return "<Response><Message>Request received</Message></Response>"

def handle_staff(msg, user):
    if "done" in msg.lower():
        for task in reversed(tasks):
            if task["status"] == "Assigned":
                task["status"] = "Completed"
                save_tasks(tasks)
                send_whatsapp(task["user"], "Issue resolved 👍")
                return "<Response><Message>Done</Message></Response>"

    return "<Response><Message>OK</Message></Response>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
