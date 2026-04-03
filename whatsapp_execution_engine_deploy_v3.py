from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os, psycopg2, json
from datetime import datetime, timedelta
from openai import OpenAI
from twilio.rest import Client
import pytz

app = Flask(__name__)
CORS(app)

# -------- INIT --------
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

# -------- CONFIG --------
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

# -------- HELPERS --------
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

# -------- AI --------
def ai_classify(message):
 prompt = f"""
You are a hotel operations AI.

Classify the guest request and generate a reply.

Rules:
- AC / temperature → maintenance (high)
- cleaning / towels → housekeeping (medium)
- food / water → service (medium)

Message: "{message}"

Return JSON:
{{
 "intent": "maintenance|housekeeping|service|general",
 "priority": "low|medium|high",
 "reply": "natural human-like reply"
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

# -------- EMOTION --------
def detect_emotion(msg):
 m = msg.lower()

 if msg.isupper():
  return "frustrated"

 if any(x in m for x in ["still", "again", "worst", "bad", "angry"]):
  return "frustrated"

 return "normal"

def smart_priority(base, emotion):
 if emotion == "frustrated":
  if base == "medium":
   return "high"
  if base == "high":
   return "urgent"
 return base

# -------- ROUTES --------

@app.route("/")
def home():
 return send_file("dashboard.html")

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

 ai = ai_classify(msg)

 intent = ai["intent"]
 base_priority = ai["priority"]
 reply = ai["reply"]

 emotion = detect_emotion(msg)
 priority = smart_priority(base_priority, emotion)

 icon = ICON_MAP.get(intent, "📌")

 if emotion == "frustrated":
  reply = f"{icon} We understand your frustration. This has been prioritized immediately."
 else:
  reply = f"{icon} {reply}"

 # store task
 cur.execute("""
 INSERT INTO tasks(message,intent,priority,status,created_at,user_number)
 VALUES(%s,%s,%s,%s,%s,%s)
 """, (msg, intent, priority, "Active", datetime.utcnow(), user))
 conn.commit()

 cur.execute("SELECT MAX(id) FROM tasks")
 task_id = cur.fetchone()[0]

 # ALWAYS send to staff
 send_whatsapp(
  STAFF_NUMBER,
  f"""{icon} TASK #{task_id}

{msg}
Priority: {priority.upper()}"""
 )

 return f"<Response><Message>{reply}</Message></Response>"

# -------- RUN --------
if __name__ == "__main__":
 app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
