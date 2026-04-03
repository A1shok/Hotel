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

def to_ist(dt):
 if dt.tzinfo is None:
  dt = pytz.utc.localize(dt)
 return dt.astimezone(IST)

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

# ✅ NEW FUNCTION (ONLY ADDITION)
def get_icon(message, intent):
 m = message.lower()

 if "towel" in m:
  return "🧻"
 if "water" in m:
  return "🚰"
 if "food" in m:
  return "🍽️"
 if "ac" in m or "temperature" in m:
  return "🔧"

 return ICON_MAP.get(intent, "📌")

# -------- AI --------
def ai_classify(message):
 prompt = f"""
You are a smart hotel operations assistant.

Understand the guest request and return structured JSON.

Rules:
- AC, hot, cold, temperature → intent = maintenance, priority = high
- cleaning, towel, bedsheet → housekeeping, priority = medium
- water, food → service, priority = medium
- complaints → high priority

Also generate a natural human-like reply.

Message: "{message}"

Return ONLY JSON:
{{
  "intent": "maintenance|housekeeping|service|general",
  "priority": "low|medium|high",
  "reply": "natural response"
}}
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

 except Exception as e:
  print("AI ERROR:", e)
  return {
   "intent": "maintenance",
   "priority": "high",
   "reply": "We are handling your request."
  }

def detect_emotion(msg):
 m = msg.lower()
 if msg.isupper() or any(x in m for x in ["still","again","worst","bad","angry"]):
  return "frustrated"
 return "normal"

def smart_priority(base, emotion):
 if emotion == "frustrated":
  if base == "medium": return "high"
  if base == "high": return "urgent"
 return base

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
   "status": r[4],
   "time": to_ist(r[5]).strftime("%H:%M:%S")
  })

 return jsonify(data)

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
 msg = request.values.get('Body', '')
 user = request.values.get('From', '')

 # completion logic
 if "done" in msg.lower():
  parts = msg.split()
  task_id = int(parts[1]) if len(parts) > 1 else None

  if task_id:
   cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
   conn.commit()

   for m in MANAGERS:
    send_whatsapp(m, f"✅ Task #{task_id} completed")

  return "<Response><Message>Task completed</Message></Response>"

 ai = ai_classify(msg)

 intent = ai["intent"]
 base_priority = ai["priority"]
 reply = ai["reply"]

 emotion = detect_emotion(msg)
 priority = smart_priority(base_priority, emotion)

 # ✅ ONLY CHANGE HERE
 icon = get_icon(msg, intent)

 if emotion == "frustrated":
  reply = f"{icon} We understand your frustration. This has been prioritized immediately."
 else:
  reply = f"{icon} {reply}"

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

 return f"<Response><Message>{reply}</Message></Response>"

if __name__ == "__main__":
 app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
