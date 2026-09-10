"""
Active-profile path resolution.

A *profile* is one self-contained directory under ``profiles/<ID>/`` holding
everything that defines a user context: personal fields (``profile.json``),
the bullet bank (``bullet_bank.csv``), the four LLM prompts (``prompts/``),
the CV template (``template.tex.jinja``), settings (``config.json``),
per-profile job state (``job_state.db``), and its own generated output
(``generated_cvs/``, ``generated_cover_letters/``). Moving the folder moves
the whole profile.

Which profile is active is decided by, in order:
  1. the ``JOB_ASSIST_PROFILE`` environment variable, else
  2. the ``.active_profile`` file at the repo root (one line: the profile ID), else
  3. nothing -- every resolver below falls back to the pre-profile layout
     (root-level ``profile.local.json``, ``bullet_bank_en.csv``,
     ``active-settings/prompts/``, ``tex_templates/cv_template.tex.jinja``,
     ``generated/``) so the app keeps working during the migration.

The ``_skeleton`` directory is the template a new profile is copied from;
it is never itself a selectable profile (leading underscore).
"""
import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent
PROFILES_DIR = BASE / "profiles"
SKELETON_DIR = PROFILES_DIR / "_skeleton"
ACTIVE_MARKER = BASE / ".active_profile"

MAX_LABEL_LEN = 15


def active_profile_id() -> Optional[str]:
    env = os.environ.get("JOB_ASSIST_PROFILE", "").strip()
    if env:
        return env
    if ACTIVE_MARKER.exists():
        return ACTIVE_MARKER.read_text(encoding="utf-8").strip() or None
    return None


def active_profile_dir() -> Optional[Path]:
    pid = active_profile_id()
    if not pid:
        return None
    d = PROFILES_DIR / pid
    return d if d.is_dir() else None


def set_active_profile(pid: str) -> None:
    ACTIVE_MARKER.write_text(pid.strip() + "\n", encoding="utf-8")


def list_profiles() -> list:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.name for p in PROFILES_DIR.iterdir()
                  if p.is_dir() and not p.name.startswith("_"))


# ---- path resolvers: the profile's file if a profile is active, else legacy ----

def profile_json_path() -> Path:
    d = active_profile_dir()
    return (d / "profile.json") if d else (BASE / "profile.local.json")

def config_path() -> Path:
    d = active_profile_dir()
    return (d / "config.json") if d else (BASE / "config.local.json")

def bullet_bank_path() -> Path:
    d = active_profile_dir()
    return (d / "bullet_bank.csv") if d else (BASE / "bullet_bank_en.csv")

def template_path() -> Path:
    d = active_profile_dir()
    return (d / "template.tex.jinja") if d else (BASE / "tex_templates" / "cv_template.tex.jinja")

def prompts_dir() -> Path:
    d = active_profile_dir()
    return (d / "prompts") if d else (BASE / "active-settings" / "prompts")

def job_state_db_path() -> Optional[Path]:
    """Where per-profile job state (viewed/rated) lives. None in legacy mode
    -- there the state stays in jobs.db's own columns."""
    d = active_profile_dir()
    return (d / "job_state.db") if d else None

def generated_cv_dir() -> Path:
    d = active_profile_dir()
    return (d / "generated_cvs") if d else (BASE / "generated")

def generated_cover_letter_dir() -> Path:
    d = active_profile_dir()
    return (d / "generated_cover_letters") if d else (BASE / "generated")


def load_config() -> dict:
    p = config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def sanitize_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", label or "")[:MAX_LABEL_LEN]


def create_profile(label: str) -> str:
    """Scaffold a new profile by copying ``_skeleton``. ``label`` is the
    user's name for it (<=15 chars, letters/digits/-/_); a numeric suffix
    is appended only if that name is already taken. Returns the final
    profile ID."""
    label = sanitize_label(label) or "profile"
    pid, n = label, 1
    while (PROFILES_DIR / pid).exists():
        n += 1
        pid = f"{label}-{n}"
    shutil.copytree(SKELETON_DIR, PROFILES_DIR / pid)
    return pid
