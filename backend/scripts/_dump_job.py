import json
import sqlite3
import sys

db = "c:/Users/SHAKIR/Desktop/LeadHunterPro/backend/output/lead_research.db"
job_id = sys.argv[1] if len(sys.argv) > 1 else "5e9b945962f7"
conn = sqlite3.connect(db)
row = conn.execute(
    "select id, state, created_at, updated_at, error, events_json from jobs where id=?",
    (job_id,),
).fetchone()
if not row:
    print("JOB NOT FOUND", job_id)
    sys.exit(0)
print("id=%s state=%s created=%s updated=%s error=%r" % (row[0], row[1], row[2], row[3], row[4]))
evs = json.loads(row[5] or "[]")
print("events=%d" % len(evs))
for e in evs[-12:]:
    print("  [%s] step=%s: %s" % (e.get("phase"), e.get("step"), e.get("message", ""))[:160])