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

STAFF_NUMBER = "whatsapp:+916303484136"

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
 SELECT id FROM tasks
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

def is_resolution(msg):
 m = msg.lower()
 return any(x in m for x in ["fix", "resolv", "working now"])

# ---------- AI ----------

def ai_classify(message):
 prompt = f"""
You are a hotel assistant.

Message: "{message}"

Return JSON:
{{"intent":"","priority":"low|medium|high","reply":""}}
"""
 try:
  r = client.chat.completions.create(
   model="gpt-4.1-mini",
   messages=[{"role": "user", "content": prompt}]
  )
  return json.loads(r.choices[0].message.content)
 except:
  return {"intent":"general","priority":"low","reply":"We are handling your request."}

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
   "status": r[4]
  })

 return jsonify(data)

# ---------- WHATSAPP ----------

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
 msg = request.values.get('Body', '')
 user = request.values.get('From', '')

 # ---------- STAFF DONE ----------
 if msg.lower().startswith("done"):
  parts = msg.split()

  if len(parts) > 1 and parts[1].isdigit():
   task_id = int(parts[1])

   cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
   conn.commit()

   send_whatsapp(STAFF_NUMBER, "✅ Task marked as completed")

   return "<Response><Message>Done noted 👍</Message></Response>"

  return "<Response><Message>Use: done <task_id></Message></Response>"

 # ---------- SMALL TALK ----------
 small = handle_small_talk(msg)
 if small:
  return f"<Response><Message>{small}</Message></Response>"

 ai = ai_classify(msg)
 intent = ai["intent"]
 priority = ai["priority"]
 reply = ai["reply"]

 # ---------- RESOLUTION ----------
 if is_resolution(msg):
  task = get_latest_active_task(user)
  if task:
   task_id = task[0]

   cur.execute("UPDATE tasks SET status='Completed' WHERE id=%s", (task_id,))
   conn.commit()

   send_whatsapp(STAFF_NUMBER, "👤 Guest confirmed issue is resolved")

   return "<Response><Message>Great, glad everything is sorted out! Let me know if you need anything else.</Message></Response>"

 # ---------- FOLLOW-UP ----------
 if is_followup(msg):
  task = get_latest_active_task(user)

  if task:
   task_id = task[0]

   cur.execute("UPDATE tasks SET priority='urgent' WHERE id=%s", (task_id,))
   conn.commit()

   send_whatsapp(STAFF_NUMBER, "🚨 Task escalated by guest")

   return "<Response><Message>Sorry about that, I've escalated this and it will be handled right away.</Message></Response>"

 # ---------- NEW TASK ----------
 if should_create_task(msg):

  cur.execute("""
  INSERT INTO tasks(message,intent,priority,status,created_at,user_number)
  VALUES(%s,%s,%s,%s,%s,%s)
  """, (msg, intent, priority, "Active", datetime.utcnow(), user))
  conn.commit()

  send_whatsapp(
   STAFF_NUMBER,
   f"New task:\n{msg}\nPriority: {priority.upper()}"
  )

 return f"<Response><Message>{reply}</Message></Response>"

if __name__ == "__main__":
 app.run(host="0.0.0.0", port=5000)
