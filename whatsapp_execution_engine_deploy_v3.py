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

# ---------- SMALL TALK (NEW) ----------

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

 greetings = ["hi", "hello", "hey"]
 small_talk = ["thanks", "thank you", "ok", "okay"]

 if m.startswith(tuple(greetings)):
  return False

 if any(word in m for word in small_talk):
  return False

 return True

# ---------- LAYER 3 ----------

def is_followup(msg):
 m = msg.lower().strip()
 follow_words = ["still", "again", "not received", "same issue"]
 return any(w in m for w in follow_words) and len(m.split()) <= 6

def is_resolution(msg):
 m = msg.lower()
 return any(w in m for w in ["fixed", "resolved", "working now"])

def get_last_active_task(user, intent):
 cur.execute("""
 SELECT id, priority FROM tasks
 WHERE user_number=%s AND status='Active' AND intent=%s
 ORDER BY id DESC LIMIT 1
 """, (user, intent))
 return cur.fetchone()

# ---------- EXISTING ----------

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
  content = r.choices[0].message.content.strip()

  if content.startswith("```"):
   content = content.split("```")[1]

  return json.loads(content)

 except:
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

# ---------- ROUTES ----------

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

 # -------- SMALL TALK FIRST (ONLY ADDITION) --------
 small_reply = handle_small_talk(msg)
 if small_reply:
  return f"<Response><Message>📌 {small_reply}</Message></Response>"

 ai = ai_classify(msg)

 intent = ai["intent"]
 base_priority = ai["priority"]
 reply = ai["reply"]

 emotion = detect_emotion(msg)
 priority = smart_priority(base_priority, emotion)

 icon = get_icon(msg, intent)

 # ---------- RESOLUTION ----------
 if is_resolution(msg):
  last_task = get_last_active_task(user, intent)
  if last_task:
   task_id = last_task[0]

   cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
   conn.commit()

   for m in MANAGERS:
    send_whatsapp(m, f"✅ Task #{task_id} completed by guest")

   return "<Response><Message>✅ Glad to hear it's resolved.</Message></Response>"

 # ---------- FOLLOW-UP ----------
 if is_followup(msg):
  last_task = get_last_active_task(user, intent)

  if last_task:
   task_id, current_priority = last_task

   if current_priority == "urgent":
    return "<Response><Message>⚠️ Already escalated.</Message></Response>"

   cur.execute("UPDATE tasks SET priority='urgent' WHERE id=%s", (task_id,))
   conn.commit()

   send_whatsapp(STAFF_NUMBER, f"🚨 TASK #{task_id} ESCALATED")

   for m in MANAGERS:
    send_whatsapp(m, f"🚨 Task #{task_id} escalated")

   return "<Response><Message>⚠️ Escalated immediately.</Message></Response>"

 # ---------- NORMAL ----------
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

 if emotion == "frustrated":
  reply = f"{icon} We understand your frustration."
 else:
  reply = f"{icon} {reply}"

 return f"<Response><Message>{reply}</Message></Response>"

if __name__ == "__main__":
 app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
