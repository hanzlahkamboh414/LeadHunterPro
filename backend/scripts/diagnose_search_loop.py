"""Read-only diagnostic: what actually ran, and how often, on a Search click.

Opens the live jobs DB (``output/lead_research.db``) READ-ONLY and reconstructs,
from the ``jobs`` table's own ``pass_log`` + ``events`` telemetry, exactly what
each Search job did:

  * how many TOP-UP ROUNDS ran (a ``pass == 1`` entry marks a new round),
  * per-pass discovery yield (new_leads / non_client_drops / total),
  * how many research events fired vs how many DISTINCT emails were touched
    (total > distinct  ==>  the SAME lead was processed more than once),
  * the CACHED / dead-SKIPPED / ERROR / working breakdown.

This proves or disproves the "endless loop / same data re-processed" report
with the run's own recorded evidence. It writes NOTHING (mode=ro).

Usage (from backend/):  python scripts/diagnose_search_loop.py [db_path] [job_id]
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter

# Windows consoles default to cp1252 and choke on non-ASCII; force UTF-8 so the
# report never crashes mid-print (read-only tool — output only).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _db_path() -> str:
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "output", "lead_research.db")


def _connect_ro(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise SystemExit(f"DB not found: {path}")
    # Read-only URI so the diagnostic can never mutate live data.
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_jobs(conn: sqlite3.Connection) -> list[dict]:
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "jobs" not in have:
        raise SystemExit("No 'jobs' table in this DB — no Search jobs recorded here.")
    cur = conn.execute(
        "SELECT id, query_json, state, events_json, results_json, "
        "pass_log_json, error, created_at, updated_at, elapsed_s "
        "FROM jobs ORDER BY created_at DESC"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fmt_query(query_json: str) -> str:
    try:
        q = json.loads(query_json)
    except Exception:
        return query_json[:60]
    return (f"trade={q.get('trade','')!r} loc={q.get('location','')!r} "
            f"target={q.get('target_emails','?')}")


def _overview(jobs: list[dict]) -> None:
    print("=" * 78)
    print(f"JOBS IN DB: {len(jobs)}  (newest first)")
    print("=" * 78)
    for j in jobs[:12]:
        try:
            events = json.loads(j["events_json"])
            results = json.loads(j["results_json"])
            passes = json.loads(j["pass_log_json"])
        except Exception:
            events = results = passes = []
        print(f"  {j['id']}  {j['state']:<10} {j['created_at']}  "
              f"elapsed={j['elapsed_s']:.0f}s  events={len(events)} "
              f"results={len(results)} passlog={len(passes)}  "
              f"[{_fmt_query(j['query_json'])}]")
    print()


def _deep_dive(job: dict) -> None:
    passes = json.loads(job["pass_log_json"])
    events = json.loads(job["events_json"])
    print("=" * 78)
    print(f"DEEP DIVE: job {job['id']}  state={job['state']}  "
          f"{_fmt_query(job['query_json'])}")
    print(f"created={job['created_at']}  elapsed={job['elapsed_s']:.0f}s")
    print("=" * 78)

    # ---- Rounds & discovery passes -------------------------------------
    rounds = sum(1 for p in passes if p.get("pass") == 1)
    print(f"\nDISCOVERY: {len(passes)} pass-entries across {rounds} top-up "
          f"round(s)  (a 'pass 1' entry = a new round)")
    if passes:
        print(f"  {'rnd.pass':>9} {'trade':<26} {'status':<10} "
              f"{'new':>4} {'drops':>5} {'total':>5}  reason")
        rnd = 0
        for p in passes:
            if p.get("pass") == 1:
                rnd += 1
            tag = f"{rnd}.{p.get('pass','?')}"
            print(f"  {tag:>9} {str(p.get('trade',''))[:26]:<26} "
                  f"{str(p.get('status',''))[:10]:<10} "
                  f"{p.get('new_leads',0):>4} {p.get('non_client_drops',0):>5} "
                  f"{p.get('total_leads',0):>5}  "
                  f"{'EXHAUSTED:'+p.get('reason','') if p.get('exhausted') else ''}")
    total_new = sum(p.get("new_leads", 0) for p in passes)
    total_drops = sum(p.get("non_client_drops", 0) for p in passes)
    print(f"\n  SUM new_leads across ALL passes = {total_new}   "
          f"SUM non_client_drops = {total_drops}")

    # ---- Research events: repetition check -----------------------------
    research = [e for e in events if e.get("phase") == "research"]
    emails = [e.get("email", "") for e in research if e.get("email")]
    distinct = set(emails)
    dupes = Counter(emails)
    repeated = {em: c for em, c in dupes.items() if c > 1}

    cached = dead = errors = working = skip_scored = 0
    for e in research:
        data = e.get("data") or {}
        msg = e.get("message", "")
        if data.get("cached") or "CACHED" in msg:
            cached += 1
        elif "SKIPPED (dead" in msg or data.get("reason") == "dead_domain":
            dead += 1
        elif "ERROR" in msg or "error" in data:
            errors += 1
        elif data.get("working") is True:
            working += 1
        else:
            skip_scored += 1

    print(f"\nRESEARCH EVENTS: {len(research)} fired  |  DISTINCT emails "
          f"touched: {len(distinct)}")
    if len(research) > len(distinct):
        print(f"  >>> RE-PROCESSING: {len(research) - len(distinct)} event(s) "
              f"were repeats of an email already researched THIS run.")
    print(f"  breakdown:  working(new visible)={working}  cached(reused)={cached}  "
          f"dead-skipped={dead}  scored-skip={skip_scored}  errors={errors}")
    if repeated:
        top = sorted(repeated.items(), key=lambda kv: -kv[1])[:8]
        print(f"  most-repeated emails this run: "
              + ", ".join(f"{em} x{c}" for em, c in top))

    # ---- served-from-cache notices -------------------------------------
    served = [e for e in events
              if e.get("phase") == "discovery" and "from discovery cache" in e.get("message", "")]
    if served:
        print(f"\nCACHE-SERVE notices: {len(served)}")
        for e in served[:6]:
            print(f"  - {e.get('message','')}")

    print(f"\n  READING:")
    print(f"  rounds={rounds}  passes={len(passes)}  SUMnew={total_new}  "
          f"research_events={len(research)}  distinct_emails={len(distinct)}  "
          f"working={working}")
    print("-" * 78)


def main() -> None:
    path = _db_path()
    print(f"DB: {os.path.abspath(path)}  (read-only)\n")
    conn = _connect_ro(path)
    try:
        jobs = _load_jobs(conn)
        if not jobs:
            print("No jobs recorded — click Search once, then re-run this.")
            return
        _overview(jobs)
        # Pick the job to dissect: explicit job_id arg, else the newest job
        # that actually has telemetry (pass_log or events).
        target_id = sys.argv[2] if len(sys.argv) > 2 else None
        chosen = None
        if target_id:
            chosen = next((j for j in jobs if j["id"] == target_id), None)
            if chosen is None:
                raise SystemExit(f"job id {target_id} not found")
        else:
            for j in jobs:
                if json.loads(j["pass_log_json"]) or json.loads(j["events_json"]):
                    chosen = j
                    break
        if chosen is None:
            print("No job has telemetry yet.")
            return
        _deep_dive(chosen)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
