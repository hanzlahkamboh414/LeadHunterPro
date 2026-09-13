"""Live check of the materialized Overture table (P8) — sample real US
phones from place_contacts, run the production lookup, measure timing.
One-off diagnostic."""
import sys
import time

sys.path.insert(0, ".")

import duckdb

from app.phones.overture import OvertureStore

store = OvertureStore()
print("local release:", store.release())

con = duckdb.connect("output/overture.duckdb", read_only=True)
sample = [r[0] for r in con.execute(
    "SELECT phone FROM place_contacts "
    "WHERE phone LIKE '+1%' AND email LIKE '%@%.com' AND website <> '' "
    "LIMIT 5"
).fetchall()]
total = con.execute("SELECT count(*) FROM place_contacts").fetchone()[0]
con.close()
print(f"table: {total} contacts; sample phones: {sample}")

t0 = time.time()
hits = store.lookup_emails(sample)
print(f"lookup took {round((time.time() - t0) * 1000)} ms "
      f"({len(hits)}/{len(sample)} hits)")
for phone, hit in hits.items():
    print(f"HIT {phone} -> {hit}")

# A miss must be absent, and an empty batch cheap.
t0 = time.time()
miss = store.lookup_emails(["+19999999999"])
print(f"miss lookup {round((time.time() - t0) * 1000)} ms -> {miss}")
