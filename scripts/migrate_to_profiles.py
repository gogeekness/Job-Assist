#!/usr/bin/env python3
"""
One-off migration: scaffold RICHARD-EN-01 and RICHARD-DE-01 from the
legacy root-level layout, and split job state (ratings/notes/viewed/
generated-CV paths) out of jobs.db into each profile's own job_state.db.

Safe to re-run: create_profile() appends -N on a name collision rather
than overwriting, so running this twice makes RICHARD-EN-01-2 /
RICHARD-DE-01-2 instead of clobbering the first pair. Nothing at the repo
root is deleted or modified -- this only reads from there and writes into
profiles/.
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import db as db_mod
import profile as profile_mod

STATE_COLS = db_mod.STATE_COLS


def migrate_state(pid: str, only_where: str = None):
    """Copy the state columns for matching jobs out of jobs.db into
    profiles/<pid>/job_state.db."""
    state_path = profile_mod.PROFILES_DIR / pid / "job_state.db"
    sqlite3.connect(str(state_path)).executescript(db_mod.STATE_SCHEMA)

    src = sqlite3.connect(str(db_mod.DB_PATH))
    src.row_factory = sqlite3.Row
    query = f"SELECT id, {', '.join(STATE_COLS)} FROM jobs"
    if only_where:
        query += f" WHERE {only_where}"
    rows = src.execute(query).fetchall()
    src.close()

    dst = sqlite3.connect(str(state_path))
    n = 0
    for row in rows:
        vals = [row[c] for c in STATE_COLS]
        if not any(v not in (None, 0) for v in vals):
            continue  # nothing set for this job -- skip the empty row
        cols = ",".join(STATE_COLS)
        qs = ",".join("?" * len(STATE_COLS))
        dst.execute(f"INSERT OR REPLACE INTO job_state (job_id,{cols}) VALUES (?,{qs})",
                     (row["id"], *vals))
        n += 1
    dst.commit()
    dst.close()
    print(f"  {pid}: migrated state for {n} job(s) -> {state_path}")


def main():
    en_pid = profile_mod.create_profile("RICHARD-EN-01")
    de_pid = profile_mod.create_profile("RICHARD-DE-01")
    en_dir = profile_mod.PROFILES_DIR / en_pid
    de_dir = profile_mod.PROFILES_DIR / de_pid
    print(f"Created {en_pid} and {de_pid}")

    # ---- RICHARD-EN-01 ----
    profile_local = BASE / "profile.local.json"
    if profile_local.exists():
        shutil.copy(profile_local, en_dir / "profile.json")
        shutil.copy(profile_local, de_dir / "profile.json")  # same person, same facts
        print("  copied profile.local.json into both profiles")

    bullet_en = BASE / "bullet_bank_en.csv"
    if bullet_en.exists():
        shutil.copy(bullet_en, en_dir / "bullet_bank.csv")
        print(f"  copied {bullet_en.name} -> {en_pid}/bullet_bank.csv")

    bullet_de = BASE / "bullet_bank_de.csv"
    if bullet_de.exists():
        shutil.copy(bullet_de, de_dir / "bullet_bank.csv")
        print(f"  copied {bullet_de.name} -> {de_pid}/bullet_bank.csv")

    single_template = BASE / "tex_templates" / "cv_template_single.tex.jinja"
    if single_template.exists():
        shutil.copy(single_template, en_dir / "template.tex.jinja")
        shutil.copy(single_template, de_dir / "template.tex.jinja")
        print("  copied the single-column template into both profiles")

    active_prompts = BASE / "active-settings" / "prompts"
    if active_prompts.exists():
        # Richard's own active prompts carry his name/specifics -- the
        # skeleton's already-generic prompts (copied in by create_profile)
        # are what should ship in the new profiles, not these. Left as-is
        # (create_profile already populated prompts/ from _skeleton).
        print("  prompts: keeping the generic skeleton versions (not copying the old personal ones)")

    retention_days = None
    local_config = BASE / "config.local.json"
    if local_config.exists():
        try:
            retention_days = json.loads(local_config.read_text(encoding="utf-8")).get("job_retention_days")
        except (OSError, json.JSONDecodeError):
            pass

    en_config = json.loads((en_dir / "config.json").read_text(encoding="utf-8"))
    en_config.update({"language": "English", "lang": "en", "include_tex_odt_archive": True})
    if retention_days:
        en_config["job_retention_days"] = retention_days
    (en_dir / "config.json").write_text(json.dumps(en_config, indent=2), encoding="utf-8")

    de_config = json.loads((de_dir / "config.json").read_text(encoding="utf-8"))
    de_config.update({"language": "German", "lang": "de", "include_tex_odt_archive": False})
    if retention_days:
        de_config["job_retention_days"] = retention_days
    (de_dir / "config.json").write_text(json.dumps(de_config, indent=2), encoding="utf-8")
    print("  wrote config.json for both profiles")

    print("Migrating job state (ratings/notes/viewed/CV paths) into RICHARD-EN-01/job_state.db ...")
    migrate_state(en_pid)
    print(f"{de_pid}/job_state.db created empty (no prior German-specific ratings to migrate)")

    profile_mod.set_active_profile(en_pid)
    print(f"Active profile set to {en_pid}")


if __name__ == "__main__":
    main()
