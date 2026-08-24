#!/usr/bin/env python3
"""
Nightly duty-cycled job rating: rates unrated jobs matching one saved
search preset using the configured LLM backend (LLM_BACKEND in .env,
typically Ollama), in 45-minutes-active / 15-minutes-rest cycles so a
long overnight run doesn't keep the machine under sustained load.

Deliberately scoped to a saved search -- never rates outside that
filter, and always skips already-rated jobs regardless of scope, so
it's safe to run repeatedly (idempotent, resumable across nights).

Run manually:
    .venv/bin/python scripts/nightly_rate_preset.py "My Preset Name"

Or via cron (see scripts/install_nightly_rate_cron.sh).
"""
import fcntl
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import FindJobs  # noqa: E402 -- must come after sys.path insert
import cv_bank    # noqa: E402

ACTIVE_SECONDS = 45 * 60
REST_SECONDS = 15 * 60
LOCK_PATH = BASE / "backups" / "nightly_rate.lock"


def log(msg):
    print(f"{datetime.now().isoformat(timespec='seconds')}  {msg}", flush=True)


def fetch_candidates(query_string):
    args = dict(parse_qsl(query_string))
    conn = FindJobs.connect_db()
    where, params = FindJobs.build_filter_sql(args)
    where = (where + " AND llm_score IS NULL") if where else "WHERE llm_score IS NULL"
    rows = conn.execute(f"SELECT * FROM jobs {where} ORDER BY created_at DESC", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def main():
    if len(sys.argv) < 2:
        print("Usage: nightly_rate_preset.py <preset name>", file=sys.stderr)
        sys.exit(1)
    preset_name = sys.argv[1]

    LOCK_PATH.parent.mkdir(exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another nightly_rate_preset.py run is already active -- exiting.")
        return

    conn = FindJobs.connect_db()
    preset = conn.execute("SELECT * FROM search_presets WHERE name=?", (preset_name,)).fetchone()
    conn.close()
    if not preset:
        log(f"No saved preset named {preset_name!r} -- create it on the Jobs page first.")
        sys.exit(1)
    query_string = preset["query_string"]
    log(f"Starting nightly rate for preset {preset_name!r} (scope: /jobs?{query_string})")

    candidates = fetch_candidates(query_string)
    log(f"{len(candidates)} unrated job(s) currently match this search.")
    if not candidates:
        log("Nothing to rate -- done.")
        return

    conn = FindJobs.connect_db()
    lid = FindJobs._log_start(conn, f"nightly_rate:{preset_name}")
    rated_count, error = 0, None
    cv_candidates = cv_bank.recent_bullets()

    cycle = 0
    idx = 0
    try:
        while idx < len(candidates):
            cycle += 1
            cycle_start = time.monotonic()
            log(f"=== Cycle {cycle} start -- {len(candidates) - idx} unrated job(s) remaining, "
                f"{rated_count} rated so far ===")

            while idx < len(candidates) and (time.monotonic() - cycle_start) < ACTIVE_SECONDS:
                job = candidates[idx]
                idx += 1
                if not FindJobs.is_it_relevant(job):
                    continue
                try:
                    result = FindJobs._rate_one_job(job, cv_candidates)
                except Exception as e:
                    error = str(e)
                    log(f"  rate failed for job {job['id']}: {e}")
                    continue
                conn.execute(
                    "UPDATE jobs SET llm_score=?, llm_notes=? WHERE id=?",
                    (result.get("score"), json.dumps(result, ensure_ascii=False), job["id"]),
                )
                conn.commit()
                rated_count += 1

            if idx >= len(candidates):
                break

            log(f"=== Cycle {cycle} done -- resting {REST_SECONDS // 60} min to let the machine cool ===")
            time.sleep(REST_SECONDS)
    finally:
        FindJobs._log_finish(conn, lid, rated_count, error)
        conn.close()
        log(f"Finished -- rated {rated_count} job(s) across {cycle} cycle(s).")
        fcntl.flock(lock_file, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
