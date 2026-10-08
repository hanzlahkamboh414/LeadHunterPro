from app.leads.import_research_store import ImportResearchStore


def test_pause_and_resume_keep_finished_rows(tmp_path):
    store = ImportResearchStore(str(tmp_path / "imports.db"))
    job = store.create_job("owner", "emails.csv", ["one@example.com", "two@example.com"])
    first = store.next_rows()[0]
    assert store.mark_working(first["id"], job["id"])
    assert store.pause_job(job["id"], "owner")
    assert store.next_rows() == []
    store.finish_row(first["id"], job["id"], "irrelevant")
    assert store.get_job(job["id"], "owner")["status"] == "paused"
    assert store.resume_job(job["id"], "owner")
    assert [row["email"] for row in store.next_rows()] == ["two@example.com"]


def test_cancelled_job_resumes_only_unfinished_rows(tmp_path):
    store = ImportResearchStore(str(tmp_path / "imports.db"))
    job = store.create_job("owner", "emails.csv", ["one@example.com", "two@example.com"])
    first = store.next_rows()[0]
    assert store.mark_working(first["id"], job["id"])
    store.finish_row(first["id"], job["id"], "relevant")
    assert not store.cancel_job(job["id"], "other-user")
    assert store.get_job(job["id"], "owner")["status"] == "running"
    assert store.cancel_job(job["id"], "owner")
    assert store.get_job(job["id"], "owner")["pending"] == 1
    assert store.next_rows() == []
    assert store.resume_job(job["id"], "owner")
    assert [row["email"] for row in store.next_rows()] == ["two@example.com"]
    assert store.get_job(job["id"], "owner")["relevant"] == 1


def test_pause_cannot_be_undone_by_stale_worker_selection(tmp_path):
    store = ImportResearchStore(str(tmp_path / "imports.db"))
    job = store.create_job("owner", "emails.csv", ["one@example.com"])
    row = store.next_rows()[0]
    assert store.pause_job(job["id"], "owner")
    assert not store.mark_working(row["id"], job["id"])
    assert store.get_job(job["id"], "owner")["status"] == "paused"
