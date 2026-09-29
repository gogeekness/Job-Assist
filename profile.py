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
  3. nothing -- every resolver below falls back to a legacy, non-profile
     path (``active-settings/prompts/``, ``tex_templates/cv_template.tex.jinja``,
     ``generated/``, etc.) for compatibility with code paths that run
     with no profile active; in normal use a profile is always active.

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

def bullet_cache_path() -> Path:
    d = active_profile_dir()
    return (d / "bullet_store_cache.json") if d else (BASE / "bullet_store_cache.json")

def template_path() -> Path:
    d = active_profile_dir()
    return (d / "template.tex.jinja") if d else (BASE / "tex_templates" / "cv_template.tex.jinja")

def prompts_dir() -> Path:
    d = active_profile_dir()
    return (d / "prompts") if d else (BASE / "active-settings" / "prompts")

def external_prompts_dir() -> Path:
    """Prompts meant to be copy-pasted into an external AI chat tool (JD
    analysis, cover-letter drafting, interview prep, ...) -- distinct from
    prompts_dir(), which holds the prompts this app's own LLM calls use.
    Profile-scoped like everything else here: switching profiles switches
    which set you see/edit, so a DE profile's prompts can be tailored to
    the German market without touching (or being touched by) any other
    profile's copy."""
    d = active_profile_dir()
    return (d / "external_prompts") if d else (BASE / "active-settings" / "external_prompts")

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

def domain_config_path() -> Path:
    """Profile-owned domain vocabulary: skill categories, match-scoring
    keyword weights, per-position bullet-count targets, pinned bullets --
    everything that's specific to THIS person's field (Linux/DevOps for
    Richard's own profiles), not to the app itself. Same JSON format as
    every other profile file here -- a "_note" string is used wherever an
    entry needs the kind of explanation a Python comment used to carry."""
    d = active_profile_dir()
    return (d / "domain.json") if d else (BASE / "domain.example.json")


def load_config() -> dict:
    p = config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


# The default per-field value each LLM connection setting falls back to
# when a profile's config.json doesn't set it -- same defaults every
# backend's own code used to hardcode individually.
_LLM_CONFIG_DEFAULTS = {
    "llm_backend":       "stub",
    "anthropic_api_key": "",
    "anthropic_model":   "claude-haiku-4-5-20251001",
    "openai_api_key":    "",
    "openai_model":      "gpt-4o-mini",
    "ollama_host":       "http://localhost:11434",
    "ollama_model":      "llama3.2",
    "ollama_timeout":    180,
    "custom_base_url":   "",
    "custom_api_key":    "",
    "custom_model":      "",
    "custom_min_tokens": 4000,
}


def llm_config() -> dict:
    """The active profile's own LLM connection settings -- which backend
    is selected, plus every backend's credentials/model/host, all stored
    together in this profile's config.json (previously scattered across
    global process env vars / .env, shared by every profile whether that
    made sense for it or not). Read fresh each call, like load_config(),
    so a profile switch or a just-saved settings change takes effect
    immediately. Every key has a sane default, so a fresh profile that's
    never touched LLM settings gets "stub" and empty credentials rather
    than a KeyError."""
    cfg = load_config()
    return {key: cfg.get(key, default) for key, default in _LLM_CONFIG_DEFAULTS.items()}


def save_llm_config(updates: dict) -> None:
    """Merge `updates` into the active profile's LLM settings and save --
    only keys present in `updates` are touched, everything else in
    config.json (lang, tex_dir, etc.) is left alone."""
    cfg = load_config()
    cfg.update(updates)
    save_config(cfg)


def load_domain_config() -> dict:
    """A profile without its own domain.json (a fresh profile in a field
    other than Richard's) gets an empty dict here -- every caller treats
    each key as optional and falls back to a generic, non-field-specific
    default, so nothing crashes and nothing silently assumes Linux/DevOps
    vocabulary for a user who hasn't configured any."""
    p = domain_config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def load_profile() -> dict:
    p = profile_json_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_profile(data: dict) -> None:
    profile_json_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def save_config(data: dict) -> None:
    config_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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
