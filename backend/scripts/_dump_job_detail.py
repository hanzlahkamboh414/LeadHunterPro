"""Read-only: describe schemas + reconcile 16-buffered vs 14-results, and dossier
state of the result emails. No writes."""
import json
import sqlite3
import sys

DB = "c:/Users/SHAKIR/Desktop/LeadHunterPro/backend/output/lead_research.db"
job_id = sys.argv[1] if len(sys.argv) > 1 else "4a332973639c"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("== TABLE SCHEMAS (dossiers / leads / pending_leads) ==")
for table in ("dossiers", "pending_leads"):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    print(f"  {table}: {cols}")

j = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
res = json.loads(j["results_json"] or "[]")
emails = [r.get("email") for r in res if r.get("email")]

print("\n== DOSSIERS / LEADS / PENDING FOR %d RESULT EMAILS ==" % len(emails))
for e in emails:
    d = conn.execute("SELECT * FROM dossiers WHERE email_hash=?", (e,)).fetchone()
    print("  email=%s" % e)
    if d:
        dd = dict(d)
        shown = {k: dd[k] for k in dd if k in ("status", "segment", "folder", "verdict", "recommendation", "score") and dd[k]}
        print("     dossier:", shown)
    for r in conn.execute("SELECT email, company, person, source_url, dead, attempt_count, dork, created_at FROM pending_leads WHERE email=?", (e,)):
        print("     pending: company=%r person=%r dead=%r attempts=%r dork=%r created=%s" % (
            r["company"], r["person"], r["dead"], r["attempt_count"], r["dork"], r["created_at"]))

print("\nDONE")
conn.close()