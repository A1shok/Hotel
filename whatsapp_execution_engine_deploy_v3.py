from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os, psycopg2, json
from datetime import datetime
from openai import OpenAI
from twilio.rest import Client
import pytz

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

IST = pytz.timezone("Asia/Kolkata")

ICON_MAP = {
 "maintenance": "🔧",
 "housekeeping": "🧹",
 "service": "🛎️",
 "general": "📌"
}

STAFF_NUMBER = "whatsapp:+916303484136"

MANAGERS = [
 "whatsapp:+917780210871",
 "whatsapp:+919160373362"
]

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
 ORDER BY created_at DESC LIMIT 1
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

# ---------- LAYER 2 ----------

def should_create_task(message):
 m = message.lower().strip()

 if m.startswith(("hi", "hello", "hey")):
  return False

 if any(x in m for x in ["thanks", "thank you", "ok", "okay"]):
  return False

 return True

# ---------- LAYER 3 ----------

def is_followup(msg):
 m = msg.lower()
 return any(x in m for x in ["still", "again", "not received"]) and len(m.split()) <= 6

# 🔥 UPDATED (handles typos like fixex, fixd etc)
def is_resolution(msg):
 m = msg.lower()
 return any(x in m for x in ["fix", "resolv", "working now"])

# ---------- AI ----------

def get_icon(message, intent):
 m = message.lower()

 if "towel" in m:
  return "🧻"
 if "water" in m:
  return "🚰"
 if "food" in m:
  return "🍽️"
 if "ac" in m:
  return "🔧"

 return ICON_MAP.get(intent, "📌")

def ai_classify(message):
 prompt = f"""
You are a smart hotel operations assistant.

Understand the guest request and return structured JSON.

Rules:
- AC, hot, cold, temperature → maintenance (high)
- cleaning, towel → housekeeping (medium)
- water, food → service (medium)

Message: "{message}"

Return ONLY JSON:
{{"intent":"","priority":"low|medium|high","reply":""}}
"""
 try:
  r = client.chat.completions.create(
   model="gpt-4.1-mini",
   messages=[{"role": "user", "content": prompt}]
  )
  return json.loads(r.choices[0].message.content)
 except:
  return {"intent":"maintenance","priority":"high","reply":"We are handling your request."}

# ---------- UI ROUTES ----------

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

 # ---------- STAFF COMMAND FIX ----------
 if msg.lower().startswith("done"):
  parts = msg.split()

  if len(parts) > 1 and parts[1].isdigit():
   task_id = int(parts[1])

   cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
   conn.commit()

   return "<Response><Message>✅ Task marked completed</Message></Response>"

  return "<Response><Message>⚠️ Use: done <task_id></Message></Response>"

 # ---------- SMALL TALK ----------
 small = handle_small_talk(msg)
 if small:
  return f"<Response><Message>📌 {small}</Message></Response>"

 ai = ai_classify(msg)
 intent = ai["intent"]
 priority = ai["priority"]
 reply = ai["reply"]

 icon = get_icon(msg, intent)

 # ---------- RESOLUTION ----------
 if is_resolution(msg):
  task = get_latest_active_task(user)
  if task:
   task_id = task[0]

   cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
   conn.commit()

   # 🔥 UPDATED MESSAGE (clearer)
   send_whatsapp(STAFF_NUMBER, f"👤 Guest resolved TASK #{task_id}")

   for m in MANAGERS:
    send_whatsapp(m, f"✅ Task #{task_id} completed")

   return "<Response><Message>✅ Glad to hear it's resolved.</Message></Response>"

 # ---------- FOLLOW-UP ----------
 if is_followup(msg):
  task = get_latest_active_task(user)

  if task:
   task_id, current = task

   if current == "urgent":
    return "<Response><Message>⚠️ Already escalated.</Message></Response>"

   cur.execute("UPDATE tasks SET priority='urgent' WHERE id=%s", (task_id,))
   conn.commit()

   send_whatsapp(STAFF_NUMBER, f"🚨 TASK #{task_id} ESCALATED")

   return "<Response><Message>⚠️ Escalated immediately.</Message></Response>"

 # ---------- NEW TASK ----------
 if should_create_task(msg):

  cur.execute("""
  INSERT INTO tasks(message,intent,priority,status,created_at,user_number)
  VALUES(%s,%s,%s,%s,%s,%s)
  """, (msg, intent, priority, "Active", datetime.utcnow(), user))
  conn.commit()

  cur.execute("SELECT MAX(id) FROM tasks")
  task_id = cur.fetchone()[0]

  send_whatsapp(
   STAFF_NUMBER,
   f"{icon} TASK #{task_id}\n{msg}\nPriority: {priority.upper()}"
  )

 return f"<Response><Message>{icon} {reply}</Message></Response>"

if __name__ == "__main__":
 app.run(host="0.0.0.0", port=5000)
