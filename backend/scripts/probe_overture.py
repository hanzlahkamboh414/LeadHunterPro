"""Live probe of Overture Maps S3 access (P8 planning).

One-off diagnostic, not production code: verifies (1) anonymous S3 read of
the Overture places theme via DuckDB httpfs, (2) the latest release path,
(3) the actual schema around phones/emails, and (4) the phone-keyed batch
lookup shape (list_has_any) the Overture connector will use.

Overture data: CDLA Permissive 2.0, free, no API key needed (anon S3 GET).
"""
import duckdb

con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs;")
con.execute("SET s3_region='us-west-2';")
con.execute("SET enable_progress_bar=false;")
# Places theme is partitioned per release date; find the latest release.
rows = con.execute("""
    SELECT max(regexp_extract(file,
        'release/([^/]+)/theme=places', 1)) AS latest
    FROM glob('s3://overturemaps-us-west-2/release/2026-*/theme=places/type=place/*')
""").fetchall()
print("2026 releases found:", rows)
rows25 = con.execute("""
    SELECT max(regexp_extract(file,
        'release/([^/]+)/theme=places', 1)) AS latest
    FROM glob('s3://overturemaps-us-west-2/release/2025-*/theme=places/type=place/*')
""").fetchall()
print("2025 releases found:", rows25)

latest = rows[0][0] or rows25[0][0]
if not latest:
    raise SystemExit("no releases listed")
print("USING RELEASE:", latest)
base = f"s3://overturemaps-us-west-2/release/{latest}/theme=places/type=place/*"

# One file, a few rows — verify the phones/emails column shapes for real.
cols = con.execute(f"""
    SELECT name FROM parquet_schema('{base}') WHERE name IN
    ('id','phones','emails','websites','socials','names','categories','addresses')
""").fetchall()
print("schema cols present:", [c[0] for c in cols])

sample = con.execute(f"""
    SELECT emails, phones FROM read_parquet('{base}')
    WHERE emails IS NOT NULL
    LIMIT 3
""").fetchall()
for r in sample:
    print("SAMPLE:", r)

# The connector's real lookup: batch of phones -> email via list_has_any.
probe_phones = ["+15125551234", "+12145551234"]
q = f"""
    SELECT unnest(phones) AS phone, emails
    FROM read_parquet('{base}')
    WHERE list_has_any(phones, {probe_phones!r}::VARCHAR[])
    LIMIT 5
"""
try:
    hits = con.execute(q).fetchall()
    print("phone-keyed hits (synthetic numbers, expect 0):", hits)
except Exception as exc:  # noqa: BLE001 - diagnostic script
    print("phone-keyed query ERROR:", exc)
