from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os, psycopg2, json
from datetime import datetime, timedelta
from openai import OpenAI
from twilio.rest import Client
import pytz

app = Flask(__name__)
CORS(app)

# ---------- INIT ----------
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
 deadline TIMESTAMP,
 user_number TEXT,
 escalated BOOLEAN DEFAULT FALSE
)
""")
conn.commit()

IST = pytz.timezone("Asia/Kolkata")

# ---------- CONFIG ----------
ICON_MAP = {
 "maintenance": "🔧",
 "housekeeping": "🧹",
 "service": "🛎️",
 "general": "📌"
}

STAFF_USERS = {
 "whatsapp:+916303484136": "maintenance"
}

MANAGER_NUMBERS = [
 "whatsapp:+917780210871",
 "whatsapp:+919160373362"
]

ROLE_TO_NUMBER = {v: k for k, v in STAFF_USERS.items()}

# ---------- HELPERS ----------
def to_ist(dt):
 if dt.tzinfo is None:
  dt = pytz.utc.localize(dt)
 return dt.astimezone(IST)

def get_deadline(priority):
 now = datetime.utcnow()
 if priority == "urgent":
  return now + timedelta(minutes=5)
 if priority == "high":
  return now + timedelta(minutes=10)
 if priority == "medium":
  return now + timedelta(minutes=30)
 return now + timedelta(minutes=60)

def get_sla_status(deadline):
 now = datetime.utcnow().replace(tzinfo=pytz.utc)
 if deadline.tzinfo is None:
  deadline = pytz.utc.localize(deadline)
 remaining = deadline - now
 seconds = int(remaining.total_seconds())
 return ("overdue", 0) if seconds <= 0 else ("active", seconds // 60)

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

# ---------- AI ----------
def ai_classify(message):
 prompt = f"""
Understand hotel request and respond.

Message: "{message}"

Return JSON:
{{"intent":"","task":"","priority":"high","reply":""}}
"""
 try:
  r = client.chat.completions.create(
   model="gpt-4.1-mini",
   messages=[{"role": "user", "content": prompt}]
  )
  return json.loads(r.choices[0].message.content)
 except:
  return {
   "intent": "maintenance",
   "task": message,
   "priority": "high",
   "reply": "We are handling your request."
  }

# ---------- EMOTION ----------
def detect_emotion(msg):
 msg_lower = msg.lower()
 keywords = ["not working", "still", "again", "worst", "bad", "angry"]

 if msg.isupper():
  return "frustrated"

 if any(k in msg_lower for k in keywords):
  return "frustrated"

 return "normal"

def smart_priority(base, emotion):
 if emotion == "frustrated":
  if base == "medium":
   return "high"
  if base == "high":
   return "urgent"
 return base

# ---------- STAFF ----------
def handle_staff(msg):
 if "done" in msg.lower():
  parts = msg.split()
  task_id = int(parts[1]) if len(parts) > 1 else None

  if not task_id:
   cur.execute("SELECT id, message FROM tasks WHERE status='Assigned' ORDER BY id DESC LIMIT 1")
   task_id, task_msg = cur.fetchone()
  else:
   cur.execute("SELECT message FROM tasks WHERE id=%s", (task_id,))
   task_msg = cur.fetchone()[0]

  cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
  conn.commit()

  for m in MANAGER_NUMBERS:
   send_whatsapp(m, f"✅ Task #{task_id} completed\n{task_msg}")

  return "<Response><Message>Task completed ✅</Message></Response>"

 return "<Response><Message>Update received</Message></Response>"

# ---------- ROUTES ----------

@app.route("/")
def home():
 return "Hotel AI System Running 🚀"

@app.route("/tasks")
def tasks():
 cur.execute("SELECT * FROM tasks ORDER BY id DESC")
 rows = cur.fetchall()
 data = []

 for r in rows:
  created = to_ist(r[5])
  deadline = r[6]

  sla, mins = get_sla_status(deadline)

  if sla == "overdue" and not r[8]:
   icon = ICON_MAP.get(r[2], "📌")
   for m in MANAGER_NUMBERS:
    send_whatsapp(m, f"🚨 {icon} Task #{r[0]} overdue\n{r[1]}")
   cur.execute("UPDATE tasks SET escalated=TRUE WHERE id=%s", (r[0],))
   conn.commit()

  data.append({
   "id": r[0],
   "message": r[1],
   "priority": r[3],
   "status": r[4],
   "created_at": created.isoformat(),
   "deadline": to_ist(deadline).isoformat(),
   "sla": sla,
   "mins": mins
  })

 return jsonify(data)

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
 msg = request.values.get('Body', '')
 user = request.values.get('From', '')

 if user in STAFF_USERS:
  return handle_staff(msg)

 ai = ai_classify(msg)

 intent = ai["intent"]
 task = ai["task"]
 base_priority = ai["priority"]
 reply = ai["reply"]

 emotion = detect_emotion(msg)
 priority = smart_priority(base_priority, emotion)

 icon = ICON_MAP.get(intent, "📌")

 if emotion == "frustrated":
  reply = f"{icon} We’re really sorry for the inconvenience. This has been prioritized and our team is addressing it immediately."
 else:
  reply = f"{icon} {reply}"

 deadline = get_deadline(priority)

 cur.execute("""
 INSERT INTO tasks(message,intent,priority,status,created_at,deadline,user_number)
 VALUES(%s,%s,%s,%s,%s,%s,%s)
 """, (task, intent, priority, "Assigned", datetime.utcnow(), deadline, user))
 conn.commit()

 cur.execute("SELECT MAX(id) FROM tasks")
 task_id = cur.fetchone()[0]

 staff = ROLE_TO_NUMBER.get(intent)

 if staff:
  urgency = "🚨 URGENT" if priority == "urgent" else ""
  send_whatsapp(
   staff,
   f"""{icon} TASK #{task_id}

{task}
Priority: {priority.upper()} {urgency}

Reply: done {task_id}"""
  )

 return f"<Response><Message>{reply}</Message></Response>"

# ---------- RUN ----------
if __name__ == "__main__":
 app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
