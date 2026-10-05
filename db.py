"""
Shared jobs.db + per-profile job_state.db connection helper.

`jobs.db` holds scraped postings and is shared across every profile.
Per-job *state* -- rating, notes, viewed flag, generated-CV paths -- lives
in the active profile's own `job_state.db` (see profile.job_state_db_path)
so switching profiles switches your ratings/history along with everything
else. The two databases are joined in one connection via SQLite ATTACH, so
callers can keep querying a single `jobs_full` view instead of hand-joining
everywhere. In legacy mode (no active profile), `jobs_full` is just an
alias for `jobs` -- state columns still live inline there.
"""
import profile
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "jobs.db"

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_state (
    job_id INTEGER PRIMARY KEY,
    starred INTEGER DEFAULT 0,
    notes TEXT,
    llm_score REAL,
    llm_notes TEXT,
    cv_status TEXT,
    cv_tex_path TEXT,
    cv_pdf_path TEXT,
    cover_letter_path TEXT,
    cv_generated_at TEXT,
    viewed_at TEXT
);
"""

STATE_COLS = (
    "starred", "notes", "llm_score", "llm_notes", "cv_status",
    "cv_tex_path", "cv_pdf_path", "cover_letter_path",
    "cv_generated_at", "viewed_at",
)

# jobs.db's own `jobs` table still physically carries these same column
# names from before the state split (never dropped -- see ensure_schema's
# EXTRA_COLS). `jobs.*` in the view below would shadow the real,
# currently-written state.job_state columns with those always-stale/NULL
# ones, so the scraped-data columns are enumerated explicitly instead.
_JOBS_OWN_COLS = (
    "id", "source", "source_type", "ats", "external_id", "company",
    "company_size", "title", "location", "city", "country", "region_group",
    "language", "international_flag", "recruiter_flag", "direct_company_flag",
    "url", "date_posted", "description", "keywords_raw", "normalized_text",
    "created_at",
)


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    state_path = profile.job_state_db_path()
    if state_path:
        sqlite3.connect(str(state_path)).executescript(STATE_SCHEMA)
        conn.execute("ATTACH DATABASE ? AS state", (str(state_path),))
        jobs_cols = ",".join(f"jobs.{c}" for c in _JOBS_OWN_COLS)
        state_cols = ",".join(f"s.{c}" for c in STATE_COLS)
        conn.execute(f"""
            CREATE TEMP VIEW jobs_full AS
            SELECT {jobs_cols}, {state_cols}
            FROM jobs LEFT JOIN state.job_state s ON jobs.id = s.job_id
        """)
    else:
        # legacy: no profile active yet -- state columns already live
        # directly on the jobs table (pre-migration layout)
        conn.execute("CREATE TEMP VIEW jobs_full AS SELECT * FROM jobs")
    return conn


def set_job_state(conn: sqlite3.Connection, job_id: int, **cols) -> None:
    """Upsert one or more state columns for `job_id`. Targets the active
    profile's attached job_state table, or falls back to updating `jobs`
    directly in legacy mode."""
    unknown = set(cols) - set(STATE_COLS)
    if unknown:
        raise ValueError(f"not a job_state column: {unknown}")

    if profile.job_state_db_path():
        keys = list(cols)
        placeholders = ",".join("?" * len(keys))
        col_list = ",".join(keys)
        updates = ",".join(f"{k}=excluded.{k}" for k in keys)
        conn.execute(
            f"INSERT INTO state.job_state (job_id,{col_list}) VALUES (?,{placeholders}) "
            f"ON CONFLICT(job_id) DO UPDATE SET {updates}",
            (job_id, *cols.values()),
        )
    else:
        set_clause = ",".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE id=?", (*cols.values(), job_id))


def delete_job_state(conn: sqlite3.Connection, job_ids: list) -> None:
    """Drop state rows for jobs being purged (cleanup_old_jobs), so a
    profile's job_state.db doesn't accumulate orphans as postings age out."""
    if not job_ids or not profile.job_state_db_path():
        return
    conn.executemany("DELETE FROM state.job_state WHERE job_id=?", [(i,) for i in job_ids])
