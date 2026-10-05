"""connect_db()'s two modes: legacy (no active profile, state columns
live inline on `jobs`) and profile-attached (state lives in a separate
job_state.db, joined in via SQLite ATTACH)."""
import sqlite3

import pytest

import db

JOBS_SCHEMA_LEGACY = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_type TEXT, ats TEXT, external_id TEXT, company TEXT,
    company_size INTEGER, title TEXT, location TEXT, city TEXT,
    country TEXT, region_group TEXT, language TEXT,
    international_flag INTEGER DEFAULT 0, recruiter_flag INTEGER DEFAULT 0,
    direct_company_flag INTEGER DEFAULT 0, url TEXT, date_posted TEXT,
    description TEXT, keywords_raw TEXT, normalized_text TEXT,
    created_at TEXT NOT NULL,
    starred INTEGER DEFAULT 0, notes TEXT, llm_score REAL, llm_notes TEXT,
    cv_status TEXT, cv_tex_path TEXT, cv_pdf_path TEXT,
    cover_letter_path TEXT, cv_generated_at TEXT, viewed_at TEXT
);
"""


@pytest.fixture
def jobs_db_path(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    sqlite3.connect(str(path)).executescript(JOBS_SCHEMA_LEGACY)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


def test_connect_db_legacy_mode_has_jobs_full_view(jobs_db_path, monkeypatch):
    monkeypatch.setattr(db.profile, "job_state_db_path", lambda: None)
    conn = db.connect_db()
    conn.execute(
        "INSERT INTO jobs (source, created_at) VALUES ('test', '2026-01-01')"
    )
    row = conn.execute("SELECT * FROM jobs_full").fetchone()
    assert row["source"] == "test"


def test_connect_db_profile_mode_attaches_state_db(jobs_db_path, tmp_path, monkeypatch):
    state_path = tmp_path / "job_state.db"
    monkeypatch.setattr(db.profile, "job_state_db_path", lambda: state_path)

    conn = db.connect_db()
    conn.execute(
        "INSERT INTO jobs (source, created_at) VALUES ('test', '2026-01-01')"
    )
    job_id = conn.execute("SELECT id FROM jobs").fetchone()["id"]

    db.set_job_state(conn, job_id, starred=1, notes="looks good")
    row = conn.execute("SELECT * FROM jobs_full WHERE id=?", (job_id,)).fetchone()
    assert row["starred"] == 1
    assert row["notes"] == "looks good"


def test_delete_job_state_noop_in_legacy_mode(jobs_db_path, monkeypatch):
    monkeypatch.setattr(db.profile, "job_state_db_path", lambda: None)
    conn = db.connect_db()
    db.delete_job_state(conn, [1, 2, 3])  # must not raise


def test_set_job_state_rejects_unknown_column(jobs_db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(db.profile, "job_state_db_path", lambda: tmp_path / "job_state.db")
    conn = db.connect_db()
    with pytest.raises(ValueError):
        db.set_job_state(conn, 1, not_a_real_column="x")
