"""Dump a job's results + events to reconcile the 'General Contractors' 14-lead run
and check whether BOTH research AI lanes (main research + deep_research) fired."""
import json
import sqlite3
import sys
from collections import Counter

DB = "c:/Users/SHAKIR/Desktop/LeadHunterPro/backend/output/lead_research.db"
job_id = sys.argv[1] if len(sys.argv) > 1 else "4a332973639c"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchall()
if not rows:
    print("JOB NOT FOUND", job_id)
    sys.exit(0)
j = rows[0]
print("id=%s state=%s" % (j["id"], j["state"]))
print("query:", json.loads(j["query_json"]))
print("created=%s updated=%s elapsed=%.1fs error=%r" % (
    j["created_at"], j["updated_at"], j["elapsed_s"], j["error"]))

res = json.loads(j["results_json"] or "[]")
print("RESULTS:", len(res))
for r in res:
    print("   %r rec=%s company=%r revenue=%r" % (
        r.get("email"), r.get("rec"), r.get("company"), r.get("revenue_tier")))

evs = json.loads(j["events_json"] or "[]")
print("EVENTS:", len(evs))
print("  phase counts:", dict(Counter(e.get("phase") for e in evs)))
dr = [e for e in evs if "deep" in json.dumps(e).lower()]
print("  deep_research mentions:", len(dr))
pl = json.loads(j["pass_log_json"] or "[]")
print("PASS_LOG:", len(pl))
for p in pl:
    print("   ", json.dumps(p)[:180])

conn.close()