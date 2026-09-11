"""Read-only audit: has AI self-learning actually observed anything?

Dumps the three learning tables (fit_learning / query_template_yield /
discovery_template_yield) with row counts, recency, and whether any row has
reached the action threshold. Also surfaces recent job activity so we can
correlate observation volume with real runs. Never writes.
"""
import os
import sqlite3

DB = "c:/Users/SHAKIR/Desktop/LeadHunterPro/backend/output/lead_research.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("== tables ==")
for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print("  ", r[0])

for table in ("fit_learning", "query_template_yield", "discovery_template_yield"):
    print(f"\n== {table} ==")
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        n = len(rows)
    except Exception as exc:  # noqa: BLE001
        print("  ERR:", exc)
        continue
    cols = list(rows[0].keys()) if rows else []
    print(f"  rows={n}  cols={cols}")
    if rows:
        print("  newest last_seen:",
              conn.execute(f"SELECT MAX(last_seen) FROM {table}").fetchone()[0])
    for r in rows[:20]:
        print("   ", dict(r))

# Activity context: jobs + recency
print("\n== job activity ==")
for r in conn.execute(
    "SELECT state, COUNT(*) n FROM jobs GROUP BY state ORDER BY n DESC"
):
    print("  ", r["state"], r["n"])
print("  newest job:",
      conn.execute("SELECT MAX(created_at) FROM jobs").fetchone()[0])
for r in conn.execute(
    "SELECT id, state, created_at FROM jobs ORDER BY created_at DESC LIMIT 12"
):
    print("   job", r["id"], r["state"], r["created_at"])

# Dossier counts (visible working leads), for correlating with learning volume
for table in ("dossiers", "leads", "articles", "pending_leads"):
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {n}")
    except Exception:  # noqa: BLE001
        pass

conn.close()
print("\nAUDIT DONE")