import sqlite3

from scripts.quarantine_phone_email_mismatches import run, suspect


def test_suspect_requires_missing_business_identity():
    assert suspect("MICAHS CONCRETE LLC", "bokabord@sultangrill.se",
                   "https://www.sultangrill.se/")
    assert not suspect("Acme Concrete LLC", "info@acmeconcrete.com",
                       "https://acmeconcrete.com/")


def test_quarantine_keeps_audit_and_unattempted_queue_only(tmp_path):
    phone_path = tmp_path / "phone.db"
    research_path = tmp_path / "research.db"
    phone = sqlite3.connect(phone_path)
    phone.execute("CREATE TABLE phone_leads (id INTEGER PRIMARY KEY, "
                  "business_name TEXT, email TEXT, email_source TEXT, "
                  "website TEXT, enriched_at TEXT NOT NULL, updated_at TEXT)")
    phone.execute("INSERT INTO phone_leads VALUES "
                  "(1,'MICAHS CONCRETE LLC','bokabord@sultangrill.se',"
                  "'website','https://sultangrill.se/','done','old')")
    phone.commit()
    phone.close()
    research = sqlite3.connect(research_path)
    research.execute("CREATE TABLE pending_leads "
                     "(email TEXT, company TEXT, dead INTEGER, "
                     "attempt_count INTEGER, gated INTEGER)")
    research.execute("INSERT INTO pending_leads VALUES "
                     "('bokabord@sultangrill.se','MICAHS CONCRETE LLC',0,0,0)")
    research.execute("INSERT INTO pending_leads VALUES "
                     "('bokabord@sultangrill.se','MICAHS CONCRETE LLC',0,1,0)")
    research.commit()
    research.close()

    assert run(phone_path, research_path, False) == (1, 1)
    assert run(phone_path, research_path, True) == (1, 1)
    phone = sqlite3.connect(phone_path)
    research = sqlite3.connect(research_path)
    assert phone.execute("SELECT email,website,enriched_at FROM phone_leads").fetchone() == ("", "", "")
    assert phone.execute("SELECT email FROM quarantined_phone_emails").fetchone()[0] == "bokabord@sultangrill.se"
    assert research.execute("SELECT count(*) FROM pending_leads").fetchone()[0] == 1
    assert research.execute("SELECT count(*) FROM quarantined_pending_phone_emails").fetchone()[0] == 1
