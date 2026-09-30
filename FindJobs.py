#!/usr/bin/env python3
"""
Job Assist — local web UI for job harvesting and CV mapping.

Run:
    python3.12 FindJobs.py

Options (environment variables):
    HOST=0.0.0.0    bind address (default 127.0.0.1 for localhost only)
    PORT=5000       port (default 5000)

Open: http://localhost:5000  or  http://<your-lan-ip>:5000
"""

import csv
import html
import io
import json
import os
import re
import shutil
import sys
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import urlencode

import feedparser
import requests
from flask import (Flask, Response, jsonify, redirect,
                   render_template, request, send_file, url_for)

import db
import profile

BASE         = Path(__file__).parent
BOARDS_FILE  = BASE / "boards.txt"
LOCAL_CONFIG_PATH = BASE / "config.local.json"

TIMEOUT = 20

app = Flask(__name__)

_REGION_COLORS = {
    "berlin":  "primary",
    "germany": "success",
    "eu":      "purple",   # custom — defined in base.html
    "remote":  "info",
    "other":   "secondary",
}

@app.template_filter("region_color")
def region_color_filter(region):
    return _REGION_COLORS.get(region or "other", "secondary")

@app.template_filter("fromjson")
def fromjson_filter(value):
    try:
        return json.loads(value) if value else {}
    except (ValueError, TypeError):
        return {}

@app.context_processor
def inject_profile_nav():
    return {
        "active_profile": profile.active_profile_id(),
        "all_profiles": profile.list_profiles(),
    }

# ── schema ─────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL,
    source_type         TEXT,
    ats                 TEXT,
    external_id         TEXT,
    company             TEXT,
    company_size        INTEGER,
    title               TEXT,
    location            TEXT,
    city                TEXT,
    country             TEXT,
    region_group        TEXT,
    language            TEXT,
    international_flag  INTEGER DEFAULT 0,
    recruiter_flag      INTEGER DEFAULT 0,
    direct_company_flag INTEGER DEFAULT 0,
    url                 TEXT,
    date_posted         TEXT,
    description         TEXT,
    keywords_raw        TEXT,
    normalized_text     TEXT,
    created_at          TEXT NOT NULL,
    starred             INTEGER DEFAULT 0,
    notes               TEXT,
    llm_score           REAL,
    llm_notes           TEXT,
    UNIQUE(source, external_id, url)
);
CREATE TABLE IF NOT EXISTS harvest_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    count       INTEGER DEFAULT 0,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS search_presets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    query_string TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

# Columns added after initial schema — safe to apply on upgrade
EXTRA_COLS = [
    ("starred",       "INTEGER DEFAULT 0"),
    ("notes",         "TEXT"),
    ("llm_score",     "REAL"),
    ("llm_notes",     "TEXT"),
    ("cv_status",     "TEXT"),       # NULL | 'generated' | 'approved'
    ("cv_tex_path",   "TEXT"),
    ("cv_pdf_path",   "TEXT"),
    ("cover_letter_path", "TEXT"),
    ("cv_generated_at", "TEXT"),
    ("viewed_at",     "TEXT"),       # set the first time the job detail page is opened
]

# ── geo / language data ────────────────────────────────────────────────────────

GERMANY_CITIES = {
    "berlin","hamburg","munich","münchen","frankfurt","stuttgart","karlsruhe",
    "cologne","köln","bremen","hannover","bonn","heidelberg","dresden","leipzig",
    "jülich","juelich","ulm","mannheim","nuremberg","nürnberg","böblingen",
    "boeblingen","darmstadt","augsburg","freiburg","kiel","wiesbaden","bielefeld",
    "dortmund","duisburg","essen","düsseldorf","bochum","erfurt","mainz",
    "rostock","saarbrücken","regensburg","aachen","göttingen","kaiserslautern",
    "garching","oberpfaffenhofen","jülichforschungszentrum",
}

EU_COUNTRIES = {
    "germany","deutschland","austria","österreich","france","netherlands",
    "belgium","ireland","spain","portugal","italy","poland","czech republic",
    "czechia","denmark","sweden","finland","slovakia","slovenia","croatia",
    "hungary","romania","bulgaria","luxembourg","latvia","lithuania","estonia",
    "malta","cyprus","greece","switzerland","norway","iceland",
}

# jobspy/Indeed often return "City, Region, XX" with an ISO country code
# instead of a spelled-out name (e.g. "Milano, LOM, IT") -- EU_COUNTRIES
# substring matching alone misses these entirely.
ISO_COUNTRY_CODES = {
    "de": "Germany", "es": "Spain", "pt": "Portugal", "it": "Italy", "mt": "Malta",
    "at": "Austria", "fr": "France", "nl": "Netherlands", "be": "Belgium",
    "ie": "Ireland", "pl": "Poland", "cz": "Czech Republic", "dk": "Denmark",
    "se": "Sweden", "fi": "Finland", "sk": "Slovakia", "si": "Slovenia",
    "hr": "Croatia", "hu": "Hungary", "ro": "Romania", "bg": "Bulgaria",
    "lu": "Luxembourg", "lv": "Latvia", "lt": "Lithuania", "ee": "Estonia",
    "cy": "Cyprus", "gr": "Greece", "ch": "Switzerland", "no": "Norway",
    "is": "Iceland",
}

# Common function words used to catch a posting written in a language
# other than English/German (the LANGUAGE_HINTS below detect *requirement*
# phrases like "fluent German", not what language the ad itself is in --
# a fully Italian/Spanish/French ad matches none of those hints and fell
# through as "unknown" instead of being recognized as non-English).
ENGLISH_STOPWORDS = {
    "the","and","of","to","in","for","with","is","are","we","our","you",
    "your","a","an","on","as","this","that","will","be","have","has","or",
    "at","from","by","team","role","work","experience",
}
FOREIGN_STOPWORDS = {  # Italian/Spanish/French/Portuguese/Dutch/Polish, deliberately overlapping
    "il","lo","gli","le","di","che","per","con","non","è","sono","siamo",
    "dei","delle","un","una","questo","questa","nostro","azienda","lavoro",
    "el","los","las","es","somos","empresa","trabajo","experiencia",
    "des","pas","est","sommes","notre","entreprise","travail","expérience",
    "o","os","as","não","é","nossa","trabalho","experiência",
    "de","het","een","van","dat","voor","niet","zijn","onze","bedrijf","werk",
    "i","w","na","z","do","nie","jest","jestem","nasz","firma","praca",
}

LANGUAGE_HINTS = {
    "german": [
        "fluent german","verhandlungssicher deutsch","fließend deutsch",
        "sehr gute deutschkenntnisse","german required",
        "deutschkenntnisse erforderlich","c1","c2","muttersprachlich",
    ],
    "partial_german": [
        "german is a plus","basic german","good written german","b1","a2","a2-b1",
        "german advantageous","german welcome","german beneficial",
    ],
    "english": [
        "english","international team","working language is english",
        "german desirable","business english","in english","english speaking",
    ],
}

# Broader-than-the-active-profile's-keyword_weights terms for the gross
# IT-relevance pre-filter --
# a job can be legitimately IT-relevant (worth an LLM rating call) without
# mentioning any Linux/HPC-specific term, e.g. "IT Support Technician".
IT_TITLE_HINTS = {
    "it ","i.t.","software","developer","engineer","engineering","system",
    "systems","sysadmin","administrator","admin","devops","sre","cloud",
    "network","security","cyber","data","database","infrastructure",
    "platform","technical","technician","support","help desk","helpdesk",
    "backend","frontend","full stack","fullstack","architect","qa","tester",
    "programmer","informatik","ingenieur","entwickler","systemadministrator",
}

def is_it_relevant(job: dict) -> bool:
    """Gross pre-filter (pipeline step 1): drop obviously non-IT/unrelated
    jobs before spending LLM calls rating them. Cheap and deliberately
    generous -- a job only needs ONE signal (a real tech-keyword hit, or
    an IT-ish word in the title) to pass through to rating."""
    if (job.get("keywords_raw") or "").strip():
        return True
    title = (job.get("title") or "").lower()
    return any(h in title for h in IT_TITLE_HINTS)

# Coarse trade/profession buckets for the title histogram and job-list
# filter -- order matters, first match wins (checked most-specific first).
PROFESSION_CATEGORIES = {
    "hpc_research":   ["hpc", "high performance computing", "cluster", "research", "wissenschaft", "scientific"],
    # AI/ML buckets placed early (before the generic "engineer"/"other_it"
    # catch-alls) so e.g. "Machine Learning Engineer" lands here, not in
    # other_it just because it contains the word "engineer".
    "ml_engineer":    ["machine learning engineer", "ml engineer", "machine learning"],
    "ai_engineer":    ["ai engineer", "artificial intelligence", "generative ai", "llm engineer", "genai"],
    "data_scientist": ["data scientist", "data science"],
    "mlops_engineer": ["mlops", "ml ops"],
    "devops_sre":     ["devops", "site reliability", " sre", "platform engineer"],
    "cloud":          ["cloud engineer", "cloud architect", "aws engineer", "azure engineer", "cloud"],
    "linux_sysadmin": ["linux", "sysadmin", "systemadministrator", "system administrator", "systemtechniker"],
    "network":        ["network", "netzwerk"],
    "security":       ["security", "cyber", "sicherheit"],
    "data_db":        ["data engineer", "database administrator", "dba"],
    "software_dev":   ["developer", "entwickler", "software engineer", "programmer", "full stack", "fullstack", "backend", "frontend"],
    "it_support":     ["support", "helpdesk", "help desk", "service desk", "technician"],
    "other_it":       ["it ", "informatik", "engineer", "engineering", "system", "technical"],
}

def classify_profession(title: str) -> str:
    lower = (title or "").lower()
    for cat, keywords in PROFESSION_CATEGORIES.items():
        if any(kw in lower for kw in keywords):
            return cat
    return "unclassified"

# ── helpers ────────────────────────────────────────────────────────────────────

def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def normalize_space(text):
    return re.sub(r"\s+", " ", (text or "")).strip()

def strip_html(text):
    if not text:
        return ""
    return normalize_space(html.unescape(re.sub(r"<[^>]+>", " ", text)))

def detect_content_language(text):
    """Rough guess at the actual language the posting is written in
    (distinct from LANGUAGE_HINTS, which detects language *requirement*
    phrases). Returns 'foreign', 'english', or None if too short/unclear
    to judge. Not precise about *which* foreign language -- only whether
    it's predominantly non-English, which is all the EU-country filter
    needs."""
    words = re.findall(r"[a-zà-ÿ]+", text)
    if len(words) < 20:
        return None
    total = len(words)
    en_ratio = sum(1 for w in words if w in ENGLISH_STOPWORDS) / total
    foreign_ratio = sum(1 for w in words if w in FOREIGN_STOPWORDS) / total
    if foreign_ratio > 0.08 and foreign_ratio > en_ratio * 1.5:
        return "foreign"
    if en_ratio > 0.05:
        return "english"
    return None

def detect_language(text):
    t = text.lower()
    if detect_content_language(t) == "foreign":
        return "foreign"
    for level in ("german", "partial_german", "english"):
        if any(h in t for h in LANGUAGE_HINTS[level]):
            return level
    if detect_content_language(t) == "english":
        return "english"
    return "unknown"

def detect_international(text):
    t = text.lower()
    return 1 if any(f in t for f in [
        "international team","global team","worldwide","distributed team","english"
    ]) else 0

def parse_location(location) -> Tuple[str, str, str]:
    lower = (location or "").lower()
    city = country = ""
    region_group = "other"

    for c in GERMANY_CITIES:
        if c in lower:
            city = c
            country = "Germany"
            region_group = "berlin" if c == "berlin" else "germany"
            break

    if not country:
        for c in EU_COUNTRIES:
            if c in lower:
                country = c.title()
                region_group = "eu"
                break

    if not country:
        # fallback: trailing ISO country code, e.g. "Milano, LOM, IT"
        parts = [p.strip() for p in (location or "").split(",")]
        if parts and len(parts[-1]) == 2:
            iso = ISO_COUNTRY_CODES.get(parts[-1].lower())
            if iso:
                country = iso
                region_group = "germany" if iso == "Germany" else "eu"

    # override specifics
    if "berlin" in lower:
        city, country, region_group = "berlin", "Germany", "berlin"
    elif "germany" in lower or "deutschland" in lower:
        country = "Germany"
        region_group = region_group if region_group in ("berlin",) else "germany"
    elif "remote" in lower and not country:
        region_group = "remote"
    elif country and region_group == "other":
        region_group = "eu"

    return city, country, region_group

def _keyword_patterns():
    # Rebuilt from the active profile's own domain.json each call (not
    # cached at import) so a profile switch changes which vocabulary
    # jobs get harvested/matched against, same as everywhere else
    # profile-scoped in this app -- see cv_bank.keyword_weights().
    import cv_bank
    return {
        kw: re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")
        for kw in cv_bank.keyword_weights()
    }

def extract_keywords(text):
    lower = text.lower()
    return " ".join(kw for kw, pat in _keyword_patterns().items() if pat.search(lower))

def safe_get(obj, path, default=None):
    cur = obj
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur

# ── database ───────────────────────────────────────────────────────────────────

def ensure_schema():
    conn = db.connect_db()
    conn.executescript(SCHEMA_SQL)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col, coldef in EXTRA_COLS:
        if col not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coldef}")
    conn.commit()
    conn.close()

def _load_local_config() -> dict:
    if LOCAL_CONFIG_PATH.exists():
        try:
            loaded = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except (OSError, json.JSONDecodeError):
            pass
    return {}

def _save_local_config(updates: dict):
    # merge, not overwrite -- config.local.json holds settings from
    # multiple independent forms (cv paths, job retention, ...)
    cfg = _load_local_config()
    cfg.update(updates)
    LOCAL_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

DEFAULT_JOB_RETENTION_DAYS = 45  # within the requested 30-60 day range

def _parse_job_date(value: str):
    """date_posted formats vary a lot by source: arbeitnow gives a unix
    epoch ("1786021243"), jobspy (linkedin/indeed/...) gives "YYYY-MM-DD",
    EURAXESS's RSS gives RFC 822 ("Wed, 07 Aug 2026 12:00:00 GMT"), and
    Greenhouse gives nothing at all. Try each; return None if none fit."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        try:
            return datetime.utcfromtimestamp(int(value))
        except (ValueError, OSError):
            return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except (TypeError, ValueError):
        return None

def cleanup_old_jobs(days: int) -> int:
    """Delete postings older than `days` -- prefers the actual posting
    date (date_posted) when it's present and parseable, falling back to
    created_at (when we scraped it) otherwise, since date_posted is
    entirely missing for Greenhouse, our largest source. Jobs with a
    generated/approved CV or saved notes are kept regardless of age, so
    cleanup can't silently discard work already invested."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    conn = db.connect_db()
    rows = conn.execute(
        "SELECT id, date_posted, created_at FROM jobs_full "
        "WHERE cv_status IS NULL AND (notes IS NULL OR notes='')"
    ).fetchall()
    stale_ids = []
    for row in rows:
        effective = _parse_job_date(row["date_posted"])
        if effective is None:
            try:
                effective = datetime.strptime(row["created_at"][:19], "%Y-%m-%dT%H:%M:%S")
            except (ValueError, TypeError):
                continue
        if effective < cutoff:
            stale_ids.append(row["id"])
    if stale_ids:
        conn.executemany("DELETE FROM jobs WHERE id=?", [(i,) for i in stale_ids])
        db.delete_job_state(conn, stale_ids)
        conn.commit()
    conn.close()
    return len(stale_ids)

# Generated CVs/cover letters live either under the legacy BASE/"generated"
# dir, or under a profile's own generated_cvs/generated_cover_letters
# (BASE/"profiles/<pid>/..."). Both are valid roots for serving/deleting a
# generated file -- resolved paths outside both are refused.
_GENERATED_ROOTS = [(BASE / "generated").resolve(), (BASE / "profiles").resolve()]

def _under_generated_root(path: Path) -> bool:
    return any(root in path.parents for root in _GENERATED_ROOTS)

def upsert_job(conn, rec: dict, skip_filters: bool = False):
    # Locked-in filtering rule: non-Germany EU postings must be English --
    # a posting confidently detected as written in another language is
    # hard-excluded (not just flagged) here, uniformly across every
    # harvester, rather than relying on each one to remember the rule.
    # skip_filters=True bypasses this for a manually-added job (see
    # create_job) -- the rule exists to cut harvest-scale noise, not to
    # second-guess one deliberate manual entry the profile owner typed in
    # themselves.
    if not skip_filters and rec.get("region_group") == "eu" and rec.get("language") == "foreign":
        return None
    cols = [
        "source","source_type","ats","external_id","company","company_size",
        "title","location","city","country","region_group","language",
        "international_flag","recruiter_flag","direct_company_flag","url",
        "date_posted","description","keywords_raw","normalized_text","created_at",
    ]
    vals = [rec.get(c) for c in cols]
    placeholders = ",".join("?" * len(cols))
    return conn.execute(
        f"INSERT OR IGNORE INTO jobs ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )

# ── harvest workers ────────────────────────────────────────────────────────────

_harvest_status: Dict[str, dict] = {}
_harvest_lock = threading.Lock()

def _set_status(source, state, count=0, error=None):
    with _harvest_lock:
        _harvest_status[source] = {
            "state": state, "count": count, "error": error, "ts": now_iso()
        }

def _log_start(conn, source) -> int:
    lid = conn.execute(
        "INSERT INTO harvest_log(source,started_at) VALUES(?,?)", (source, now_iso())
    ).lastrowid
    conn.commit()
    return lid

def _log_finish(conn, lid, count, error):
    conn.execute(
        "UPDATE harvest_log SET finished_at=?,count=?,error=? WHERE id=?",
        (now_iso(), count, error, lid)
    )
    conn.commit()

def _do_harvest_greenhouse(boards_file):
    source = "greenhouse"
    _set_status(source, "running")
    conn = db.connect_db()
    lid = _log_start(conn, source)
    count, error = 0, None
    try:
        tokens = [x.strip() for x in Path(boards_file).read_text().splitlines() if x.strip()]
        for token in tokens:
            try:
                url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
                jobs = requests.get(url, timeout=TIMEOUT).json().get("jobs", [])
            except Exception:
                continue
            for j in jobs:
                title    = safe_get(j, ["title"], "")
                loc_raw  = safe_get(j, ["location"], "")
                if isinstance(loc_raw, list):
                    location = ", ".join(l.get("name","") for l in loc_raw)
                else:
                    location = safe_get(j, ["location","name"], "")
                desc     = strip_html(safe_get(j, ["content"], ""))
                combined = f"{title} {token} {location} {desc}"
                city, country, rg = parse_location(location)
                upsert_job(conn, {
                    "source": f"greenhouse:{token}", "source_type": "company",
                    "ats": "greenhouse", "external_id": str(safe_get(j,["id"],"")),
                    "company": token, "company_size": None,
                    "title": title, "location": location, "city": city,
                    "country": country, "region_group": rg,
                    "language": detect_language(combined),
                    "international_flag": detect_international(combined),
                    "recruiter_flag": 0, "direct_company_flag": 1,
                    "url": safe_get(j, ["absolute_url"], ""),
                    "date_posted": None, "description": desc,
                    "keywords_raw": extract_keywords(combined),
                    "normalized_text": normalize_space(combined.lower()),
                    "created_at": now_iso(),
                })
                count += 1
        conn.commit()
    except Exception as e:
        error = str(e)
    _log_finish(conn, lid, count, error)
    conn.close()
    _set_status(source, "done", count, error)

def _do_harvest_arbeitnow():
    """Arbeitnow free API — Germany / EU focused, no auth required."""
    source = "arbeitnow"
    _set_status(source, "running")
    conn = db.connect_db()
    lid = _log_start(conn, source)
    count, error = 0, None
    try:
        page = 1
        while page <= 15:
            try:
                r = requests.get(
                    "https://www.arbeitnow.com/api/job-board-api",
                    params={"page": page}, timeout=TIMEOUT
                )
                r.raise_for_status()
                jobs = r.json().get("data", [])
            except Exception as e:
                error = str(e)
                break
            if not jobs:
                break
            for j in jobs:
                title    = j.get("title", "")
                company  = j.get("company_name", "")
                location = j.get("location", "")
                desc     = strip_html(j.get("description", ""))
                tags     = " ".join(j.get("tags", []))
                combined = f"{title} {company} {location} {tags} {desc}"
                city, country, rg = parse_location(location)
                upsert_job(conn, {
                    "source": "arbeitnow", "source_type": "board",
                    "ats": "arbeitnow", "external_id": str(j.get("slug","")),
                    "company": company, "company_size": None,
                    "title": title, "location": location, "city": city,
                    "country": country, "region_group": rg,
                    "language": detect_language(combined),
                    "international_flag": detect_international(combined),
                    "recruiter_flag": 0, "direct_company_flag": 0,
                    "url": j.get("url",""),
                    "date_posted": str(j.get("created_at","")),
                    "description": desc,
                    "keywords_raw": extract_keywords(combined),
                    "normalized_text": normalize_space(combined.lower()),
                    "created_at": now_iso(),
                })
                count += 1
            if len(jobs) < 10:
                break
            page += 1
        conn.commit()
    except Exception as e:
        error = str(e)
    _log_finish(conn, lid, count, error)
    conn.close()
    _set_status(source, "done", count, error)

def _do_harvest_euraxess():
    """EURAXESS RSS — EU research, HPC, and academic positions."""
    source = "euraxess"
    _set_status(source, "running")
    conn = db.connect_db()
    lid = _log_start(conn, source)
    count, error = 0, None
    try:
        feed = feedparser.parse("https://euraxess.ec.europa.eu/jobs/rss")
        for entry in feed.entries:
            title   = entry.get("title", "")
            desc    = strip_html(entry.get("summary", ""))
            url     = entry.get("link", "")
            tags    = entry.get("tags", [])
            location = tags[0].get("term","") if tags else ""
            combined = f"{title} {location} {desc}"
            city, country, rg = parse_location(location or combined)
            upsert_job(conn, {
                "source": "euraxess", "source_type": "institution",
                "ats": "euraxess", "external_id": entry.get("id", url),
                "company": "", "company_size": None,
                "title": title, "location": location, "city": city,
                "country": country, "region_group": rg,
                "language": detect_language(combined),
                "international_flag": 1,
                "recruiter_flag": 0, "direct_company_flag": 1,
                "url": url,
                "date_posted": entry.get("published",""),
                "description": desc,
                "keywords_raw": extract_keywords(combined),
                "normalized_text": normalize_space(combined.lower()),
                "created_at": now_iso(),
            })
            count += 1
        conn.commit()
    except Exception as e:
        error = str(e)
    _log_finish(conn, lid, count, error)
    conn.close()
    _set_status(source, "done", count, error)

JOBSPY_DIR = BASE / "jobspy"
JOB_SCRAPER_DIR = BASE / "job-scraper"
if str(JOBSPY_DIR) not in sys.path:
    sys.path.insert(0, str(JOBSPY_DIR))
if str(JOB_SCRAPER_DIR) not in sys.path:
    sys.path.insert(0, str(JOB_SCRAPER_DIR))

# IT/Linux search scope (per decision: filter to IT positions, Linux as
# primary skill; other EU countries need English-only postings — enforced
# via detect_language()/region_group downstream, not baked into the query).
IT_SEARCH_LOCATIONS = [
    ("Germany",  "germany"),
    ("Spain",    "spain"),
    ("Portugal", "portugal"),
    ("Italy",    "italy"),
    ("Malta",    "malta"),
]
IT_SEARCH_TERMS = ["Linux system administrator", "DevOps engineer Linux",
                    "Site Reliability Engineer", "Platform Engineer Linux"]

def _do_harvest_jobspy():
    """speedyapply/JobSpy — reaches LinkedIn/Indeed/Glassdoor/ZipRecruiter,
    which the API-only harvesters above can't touch."""
    source = "jobspy"
    _set_status(source, "running")
    conn = db.connect_db()
    lid = _log_start(conn, source)
    count, error = 0, None
    try:
        import jobspy

        def _cell(row, key, default=""):
            # pandas NaN cells are truthy in Python (`nan or ""` -> nan),
            # so `str(row.get(key) or default)` silently produces the
            # literal string "nan" instead of falling back to default.
            val = row.get(key)
            try:
                if val is None or (isinstance(val, float) and val != val):  # NaN != NaN
                    return default
            except Exception:
                pass
            return str(val) if val else default

        sites = ["linkedin", "indeed", "glassdoor", "zip_recruiter"]
        for location_name, country_code in IT_SEARCH_LOCATIONS:
            for term in IT_SEARCH_TERMS:
                try:
                    df = jobspy.scrape_jobs(
                        site_name=sites, search_term=term,
                        location=location_name, country_indeed=country_code,
                        results_wanted=50, hours_old=336,
                        linkedin_fetch_description=True,
                    )
                except Exception as e:
                    error = f"{location_name}/{term}: {e}"
                    continue
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    title   = _cell(row, "title")
                    company = _cell(row, "company")
                    location = _cell(row, "location", location_name)
                    desc    = strip_html(_cell(row, "description"))
                    url     = _cell(row, "job_url")
                    site    = _cell(row, "site", "jobspy")
                    combined = f"{title} {company} {location} {desc}"
                    city, country, rg = parse_location(location)
                    upsert_job(conn, {
                        "source": "jobspy", "source_type": "board",
                        "ats": site, "external_id": _cell(row, "id", url),
                        "company": company, "company_size": None,
                        "title": title, "location": location, "city": city,
                        "country": country or location_name, "region_group": rg,
                        "language": detect_language(combined),
                        "international_flag": detect_international(combined),
                        "recruiter_flag": 0, "direct_company_flag": 0,
                        "url": url,
                        "date_posted": _cell(row, "date_posted"),
                        "description": desc,
                        "keywords_raw": extract_keywords(combined),
                        "normalized_text": normalize_space(combined.lower()),
                        "created_at": now_iso(),
                    })
                    count += 1
                conn.commit()
    except Exception as e:
        error = str(e)
    _log_finish(conn, lid, count, error)
    conn.close()
    _set_status(source, "done", count, error)


# LinkedIn geoIds (numeric, LinkedIn-internal, stable across their APIs)
LINKEDIN_GEO_IDS = {"Germany": 101282230, "Spain": 105646813}

def _do_harvest_linkedin_alt():
    """Secondary/fallback LinkedIn source using anandanair/job-scraper's
    scraping technique (guest jobs API + BeautifulSoup), reimplemented here
    without its Supabase coupling since this project stores jobs in SQLite.
    Deliberately throttled (small page/result caps, randomized delays
    copied from the source project) since it's a redundant path behind
    jobspy's LinkedIn coverage, not the primary one."""
    source = "linkedin_alt"
    _set_status(source, "running")
    conn = db.connect_db()
    lid = _log_start(conn, source)
    count, error = 0, None
    try:
        import random
        import time as _time
        from bs4 import BeautifulSoup
        from user_agents import USER_AGENTS

        def ua_headers():
            return {"User-Agent": random.choice(USER_AGENTS)}

        def get_with_retry(url, max_retries=2):
            headers = ua_headers()
            for attempt in range(max_retries + 1):
                try:
                    r = requests.get(url, headers=headers, timeout=TIMEOUT)
                    if r.status_code == 429 and attempt < max_retries:
                        _time.sleep(15 + random.uniform(0, 5))
                        headers = ua_headers()
                        continue
                    r.raise_for_status()
                    return r
                except requests.exceptions.HTTPError:
                    if attempt < max_retries:
                        _time.sleep(15 + random.uniform(0, 5))
                        headers = ua_headers()
                        continue
                    raise

        for location_name, geo_id in LINKEDIN_GEO_IDS.items():
            for term in IT_SEARCH_TERMS[:1]:
                try:
                    url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                           f"?keywords={term.replace(' ', '%20')}&location={location_name}"
                           f"&geoId={geo_id}&f_TPR=r604800")
                    r = get_with_retry(url)
                except Exception as e:
                    error = f"{location_name}: {e}"
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                job_ids = []
                for li in soup.find_all("li"):
                    card = li.find("div", {"class": "base-card"})
                    urn = card.get("data-entity-urn") if card else None
                    if urn and "jobPosting:" in urn:
                        job_ids.append(urn.split(":")[-1])
                for jid in job_ids[:5]:  # small cap: redundant/fallback path
                    _time.sleep(random.uniform(3.0, 10.0))
                    try:
                        detail_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"
                        dr = get_with_retry(detail_url)
                    except Exception:
                        continue
                    dsoup = BeautifulSoup(dr.text, "html.parser")

                    # title: primary selector, then the fallback the source
                    # project also falls back to
                    title_el = dsoup.find("div", {"class": "top-card-layout__entity-info"})
                    title = title_el.find("a").text.strip() if title_el and title_el.find("a") else ""
                    if not title:
                        h1 = dsoup.find("h1", {"class": "top-card-layout__title"})
                        title = h1.text.strip() if h1 else ""

                    # company: logo alt text, then org-name link, then flavor span
                    company = ""
                    card_layout = dsoup.find("div", {"class": "top-card-layout__card"})
                    img = card_layout.find("img") if card_layout else None
                    if img and img.get("alt"):
                        company = img["alt"].strip()
                    if not company:
                        company_el = dsoup.find("a", {"class": "topcard__org-name-link"})
                        company = company_el.text.strip() if company_el else ""
                    if not company:
                        flavor = dsoup.find("span", {"class": "topcard__flavor"})
                        company = flavor.text.strip() if flavor else ""

                    loc_el = dsoup.find("span", {"class": "topcard__flavor topcard__flavor--bullet"})
                    desc_el = dsoup.find("div", {"class": "show-more-less-html__markup"})
                    location = loc_el.text.strip() if loc_el else location_name
                    desc = strip_html(str(desc_el)) if desc_el else ""
                    if not title:
                        continue
                    combined = f"{title} {company} {location} {desc}"
                    city, country, rg = parse_location(location)
                    upsert_job(conn, {
                        "source": "linkedin_alt", "source_type": "board",
                        "ats": "linkedin", "external_id": jid,
                        "company": company, "company_size": None,
                        "title": title, "location": location, "city": city,
                        "country": country or location_name, "region_group": rg,
                        "language": detect_language(combined),
                        "international_flag": detect_international(combined),
                        "recruiter_flag": 0, "direct_company_flag": 0,
                        "url": f"https://www.linkedin.com/jobs/view/{jid}",
                        "date_posted": "", "description": desc,
                        "keywords_raw": extract_keywords(combined),
                        "normalized_text": normalize_space(combined.lower()),
                        "created_at": now_iso(),
                    })
                    count += 1
                conn.commit()
    except Exception as e:
        error = str(e)
    _log_finish(conn, lid, count, error)
    conn.close()
    _set_status(source, "done", count, error)


_HARVEST_FNS = {
    "greenhouse":    lambda: _do_harvest_greenhouse(str(BOARDS_FILE)),
    "arbeitnow":     _do_harvest_arbeitnow,
    "euraxess":      _do_harvest_euraxess,
    "jobspy":        _do_harvest_jobspy,
    "linkedin_alt":  _do_harvest_linkedin_alt,
}

# ── filter SQL builder ─────────────────────────────────────────────────────────

def _kw_list(val):
    return [k.strip().lower() for k in (val or "").split(",") if k.strip()]

def _quick_filter_link(args, overrides: dict, clears: tuple = ()) -> dict:
    """One-click workflow-status filter link (Unrated / Ready to Generate /
    etc.) -- toggles `overrides` on top of whatever filters are already
    applied, rather than replacing them, so a quick filter composes with
    region/language/keyword filters instead of resetting them. Clicking an
    already-active one turns it back off (same click target, no separate
    "remove" control needed). `clears` drops other params that would
    conflict with this one (e.g. applying "Unviewed" also drops a
    lingering "viewed=1" from a previous click). Always drops `offset` --
    a stale page offset from before the filter changed could otherwise
    land on a now-empty page of the new, smaller result set."""
    new_args = {k: v for k, v in args.items() if k != "offset"}
    active = all(new_args.get(k) == v for k, v in overrides.items())
    if active:
        for k in overrides:
            new_args.pop(k, None)
    else:
        for k in clears:
            new_args.pop(k, None)
        new_args.update(overrides)
    qs = urlencode(new_args)
    return {"href": f"/jobs?{qs}" if qs else "/jobs", "active": active}

def build_filter_sql(args):
    clauses, params = [], []

    regions = _kw_list(args.get("region",""))
    if regions:
        clauses.append(f"lower(region_group) IN ({','.join('?'*len(regions))})")
        params += regions

    langs = _kw_list(args.get("language",""))
    if langs:
        clauses.append(f"lower(language) IN ({','.join('?'*len(langs))})")
        params += langs

    ats_list = _kw_list(args.get("ats",""))
    if ats_list:
        clauses.append(f"lower(ats) IN ({','.join('?'*len(ats_list))})")
        params += ats_list

    sources = _kw_list(args.get("source",""))
    if sources:
        sub = []
        for s in sources:
            if s == "recruiter":     sub.append("recruiter_flag=1")
            elif s in ("company","direct"): sub.append("direct_company_flag=1")
            elif s == "institution": sub.append("source_type='institution'")
            elif s == "board":       sub.append("source_type='board'")
        if sub:
            clauses.append("(" + " OR ".join(sub) + ")")

    ms = args.get("min_company_size","")
    if ms and ms.isdigit():
        clauses.append("(company_size IS NULL OR company_size >= ?)")
        params.append(int(ms))

    if args.get("scored") == "1":
        clauses.append("llm_score IS NOT NULL")
    elif args.get("scored") == "0":
        clauses.append("llm_score IS NULL")

    if args.get("unviewed") == "1":
        clauses.append("viewed_at IS NULL")
    elif args.get("viewed") == "1":
        clauses.append("viewed_at IS NOT NULL")

    if args.get("has_notes") == "1":
        clauses.append("notes IS NOT NULL AND notes != ''")

    # cv_status: real column values are NULL / 'generated' / 'approved' --
    # "none" here means NULL (never generated), matching how the rest of
    # the app already treats an absent cv_status.
    cv_statuses = _kw_list(args.get("cv_status",""))
    if cv_statuses:
        sub = []
        for cs in cv_statuses:
            if cs == "none":
                sub.append("cv_status IS NULL")
            else:
                sub.append("cv_status = ?")
                params.append(cs)
        clauses.append("(" + " OR ".join(sub) + ")")

    profs = _kw_list(args.get("profession",""))
    if profs:
        sub = []
        for p in profs:
            kws = PROFESSION_CATEGORIES.get(p, [])
            for kw in kws:
                sub.append("lower(title) LIKE ?")
                params.append(f"%{kw}%")
        if sub:
            clauses.append("(" + " OR ".join(sub) + ")")

    # kw_and  — ALL of these must be present
    for kw in _kw_list(args.get("kw_and","")):
        clauses.append("normalized_text LIKE ?")
        params.append(f"%{kw}%")

    # kw_or  — ANY of these must be present
    kw_or = _kw_list(args.get("kw_or",""))
    if kw_or:
        clauses.append("(" + " OR ".join("normalized_text LIKE ?" for _ in kw_or) + ")")
        params += [f"%{kw}%" for kw in kw_or]

    # kw_not — NONE of these may appear
    for kw in _kw_list(args.get("kw_not","")):
        clauses.append("normalized_text NOT LIKE ?")
        params.append(f"%{kw}%")

    # free text search in title + company
    q = (args.get("q","") or "").strip().lower()
    if q:
        clauses.append("(lower(title) LIKE ? OR lower(company) LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params

# ── routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    conn = db.connect_db()
    stats = {
        "total":    conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "scored":   conn.execute("SELECT COUNT(*) FROM jobs_full WHERE llm_score IS NOT NULL").fetchone()[0],
        "unviewed": conn.execute("SELECT COUNT(*) FROM jobs_full WHERE viewed_at IS NULL").fetchone()[0],
        "today":    conn.execute("SELECT COUNT(*) FROM jobs WHERE date(created_at)=date('now')").fetchone()[0],
    }
    by_region = [dict(r) for r in conn.execute(
        "SELECT region_group, COUNT(*) n FROM jobs GROUP BY region_group ORDER BY n DESC"
    ).fetchall()]
    by_lang = [dict(r) for r in conn.execute(
        "SELECT language, COUNT(*) n FROM jobs GROUP BY language ORDER BY n DESC"
    ).fetchall()]
    by_source = [dict(r) for r in conn.execute(
        "SELECT ats, COUNT(*) n FROM jobs GROUP BY ats ORDER BY n DESC"
    ).fetchall()]
    prof_counts = Counter(classify_profession(t) for (t,) in conn.execute("SELECT title FROM jobs"))
    by_profession = [{"profession": p, "n": n} for p, n in prof_counts.most_common()]
    import cv_bank
    skill_counts = Counter()
    kw_weights = cv_bank.keyword_weights()
    for (kw_raw,) in conn.execute("SELECT keywords_raw FROM jobs WHERE keywords_raw != ''"):
        # keywords_raw is a space-joined subset of the active profile's own
        # keyword_weights keys (some of which contain their own spaces,
        # e.g. "github actions") -- re-match against the known key list
        # instead of a naive .split(), which would wrongly break
        # multi-word keywords into pieces.
        present = (kw_raw or "")
        for kw in kw_weights:
            if kw in present:
                skill_counts[kw] += 1
    by_skill = [{"skill": s, "n": n} for s, n in skill_counts.most_common(30)]
    recent_log = [dict(r) for r in conn.execute(
        "SELECT * FROM harvest_log ORDER BY started_at DESC LIMIT 12"
    ).fetchall()]
    conn.close()
    return render_template("index.html",
        stats=stats, by_region=by_region, by_lang=by_lang,
        by_source=by_source, by_profession=by_profession, by_skill=by_skill, recent_log=recent_log,
        harvest_status=dict(_harvest_status),
    )

@app.route("/jobs")
def jobs():
    conn = db.connect_db()
    where, params = build_filter_sql(request.args)

    sort = request.args.get("sort","created_at")
    if sort not in {"created_at","company","title","llm_score","region_group","language","date_posted"}:
        sort = "created_at"
    order = "DESC" if sort in ("created_at","llm_score","date_posted") else "ASC"

    limit  = min(int(request.args.get("limit",  200)), 5000)
    offset = max(int(request.args.get("offset", 0)),   0)

    total = conn.execute(f"SELECT COUNT(*) FROM jobs_full {where}", params).fetchone()[0]
    rows  = conn.execute(
        f"""SELECT id,company,title,location,city,country,region_group,
                   language,ats,source_type,url,keywords_raw,date_posted,
                   llm_score,llm_notes,created_at,viewed_at,cv_status,notes
            FROM jobs_full {where}
            ORDER BY {sort} {order}
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    presets = conn.execute("SELECT id, name, query_string FROM search_presets ORDER BY name").fetchall()
    conn.close()

    args = dict(request.args)
    # One-click workflow-status filters, matching the rate -> generate
    # batch pipeline (see jobs.html's "Workflow" quick-filter bar) --
    # computed here rather than in the template so the toggle-on/off
    # logic (and dropping a stale `offset`) lives in one place.
    quick_filters = {
        "unrated":           _quick_filter_link(args, {"scored": "0"}),
        "rated":             _quick_filter_link(args, {"scored": "1"}),
        "ready_to_generate": _quick_filter_link(args, {"scored": "1", "cv_status": "none"}),
        "not_generated":     _quick_filter_link(args, {"cv_status": "none"}),
        "generated":         _quick_filter_link(args, {"cv_status": "generated,approved"}),
        "unviewed":          _quick_filter_link(args, {"unviewed": "1"}, clears=("viewed",)),
        "has_notes":         _quick_filter_link(args, {"has_notes": "1"}),
    }

    return render_template("jobs.html",
        jobs=[dict(r) for r in rows],
        total=total, limit=limit, offset=offset,
        args=args,
        presets=[dict(r) for r in presets],
        quick_filters=quick_filters,
    )

@app.route("/jobs/presets", methods=["POST"])
def save_search_preset():
    name = (request.form.get("name") or "").strip()
    query_string = request.form.get("query_string") or ""
    if not name:
        return redirect(url_for("jobs", **dict(request.args)))
    conn = db.connect_db()
    conn.execute(
        "INSERT INTO search_presets(name, query_string, created_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET query_string=excluded.query_string, created_at=excluded.created_at",
        (name, query_string, now_iso()),
    )
    conn.commit()
    conn.close()
    return redirect(f"/jobs?{query_string}")

@app.route("/jobs/presets/delete/<int:preset_id>", methods=["POST"])
def delete_search_preset(preset_id):
    conn = db.connect_db()
    conn.execute("DELETE FROM search_presets WHERE id=?", (preset_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("jobs"))

@app.route("/jobs/new")
def new_job_form():
    return render_template("job_new.html")

@app.route("/jobs/new", methods=["POST"])
def create_job():
    title = normalize_space(request.form.get("title", ""))
    if not title:
        return render_template("job_new.html", error="Job title is required.",
                                form=request.form), 400

    company     = normalize_space(request.form.get("company", ""))
    location    = normalize_space(request.form.get("location", ""))
    description = strip_html(request.form.get("description", ""))
    url         = normalize_space(request.form.get("url", "")) or None

    combined = f"{title} {company} {location} {description}"
    city, country, region_group = parse_location(location)
    conn = db.connect_db()
    cur = upsert_job(conn, {
        "source": "self", "source_type": "manual", "ats": None, "external_id": None,
        "company": company, "company_size": None,
        "title": title, "location": location, "city": city,
        "country": country, "region_group": region_group,
        "language": detect_language(combined),
        "international_flag": detect_international(combined),
        "recruiter_flag": 0, "direct_company_flag": 1,
        "url": url, "date_posted": now_iso(), "description": description,
        "keywords_raw": extract_keywords(combined),
        "normalized_text": normalize_space(combined.lower()),
        "created_at": now_iso(),
    }, skip_filters=True)
    conn.commit()
    job_id = cur.lastrowid if cur else None
    conn.close()
    if not job_id:
        return render_template("job_new.html", error="Could not save the job -- please try again.",
                                form=request.form), 400
    return redirect(url_for("job_detail", job_id=job_id))

@app.route("/jobs/<int:job_id>")
def job_detail(job_id):
    conn = db.connect_db()
    row = conn.execute("SELECT * FROM jobs_full WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return "Not found", 404
    job = dict(row)
    already_viewed = bool(job.get("viewed_at"))
    if not already_viewed:
        db.set_job_state(conn, job_id, viewed_at=now_iso())
        conn.commit()
        job["viewed_at"] = now_iso()
    conn.close()
    import cv_bank
    jtext = " ".join(filter(None,[
        job.get("title"), job.get("company"), job.get("location"),
        job.get("description"), job.get("keywords_raw"),
    ]))
    store = cv_bank.build_bullet_store()
    scored_bullets = cv_bank.dedupe_by_similarity_group(
        cv_bank.score_bullets(jtext, store))[:20]
    return render_template("job_detail.html",
        job=job, scored_bullets=scored_bullets, bullet_bank_loaded=bool(store),
        already_viewed=already_viewed,
    )

@app.route("/jobs/<int:job_id>/notes", methods=["POST"])
def save_notes(job_id):
    notes = (request.json or {}).get("notes","")
    conn = db.connect_db()
    db.set_job_state(conn, job_id, notes=notes)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

def _rate_one_job(job: dict, cv_bank_candidates=None) -> dict:
    """Shared by the single-job route and the bulk pass. Rating draws
    only from recent CVs (curated CSV + last ~50 docs / ~90 days /
    Long_Complete, English or German) so it reflects Richard's current
    self-presentation, not years-old phrasing -- the actual fit judgment
    is still fuzzy/semantic, done by the LLM itself, since job-posting
    jargon rarely matches bullet wording verbatim."""
    import cv_bank
    import llm_plugin
    jtext = " ".join(filter(None, [job.get("title"), job.get("description"), job.get("keywords_raw")]))
    candidates = cv_bank_candidates if cv_bank_candidates is not None else cv_bank.recent_bullets()
    top_bullets = cv_bank.dedupe_by_similarity_group(cv_bank.score_bullets(jtext, candidates))[:12]
    # adapt cv_bank's richer bullet shape to the {id,title,text,keywords}
    # shape llm_plugin.py's prompt builder expects
    blocks = [{
        "id": b["id"],
        "title": b.get("position_title") or b.get("employer") or b["id"],
        "text": cv_bank.bullet_text(b),
        "keywords": b.get("skill_tags", []),
    } for b in top_bullets]
    return llm_plugin.rate_job(job, blocks)


@app.route("/jobs/<int:job_id>/rate", methods=["POST"])
def rate_job(job_id):
    try:
        import llm_plugin  # noqa: F401 -- import-checked here for the friendly error below
    except ImportError:
        return jsonify({"error": "llm_plugin.py not found"}), 500

    conn = db.connect_db()
    row = conn.execute("SELECT * FROM jobs_full WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    job = dict(row)

    try:
        result = _rate_one_job(job)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    conn = db.connect_db()
    db.set_job_state(conn, job_id, llm_score=result.get("score"),
                      llm_notes=json.dumps(result, ensure_ascii=False))
    conn.commit()
    conn.close()
    return jsonify(result)


_RATE_BULK_CAP = 300  # bound one run's duration regardless of backlog size

def _do_rate_bulk():
    source = "rate_bulk"
    _set_status(source, "running")
    conn = db.connect_db()
    lid = _log_start(conn, source)
    count, error = 0, None
    try:
        import cv_bank
        candidates = cv_bank.recent_bullets()  # computed once, reused for every job this run
        rows = conn.execute(
            "SELECT * FROM jobs_full WHERE llm_score IS NULL AND region_group != 'other' "
            "ORDER BY created_at DESC LIMIT ?", (_RATE_BULK_CAP * 3,)
        ).fetchall()
        for row in rows:
            if count >= _RATE_BULK_CAP:
                break
            job = dict(row)
            if not is_it_relevant(job):
                continue
            try:
                result = _rate_one_job(job, candidates)
            except Exception as e:
                error = str(e)
                continue
            db.set_job_state(conn, job["id"], llm_score=result.get("score"),
                              llm_notes=json.dumps(result, ensure_ascii=False))
            conn.commit()
            count += 1
    except Exception as e:
        error = str(e)
    _log_finish(conn, lid, count, error)
    conn.close()
    _set_status(source, "done", count, error)

@app.route("/jobs/rate_bulk", methods=["POST"])
def rate_bulk():
    with _harvest_lock:
        if _harvest_status.get("rate_bulk", {}).get("state") == "running":
            return jsonify({"error": "already running"}), 409
    threading.Thread(target=_do_rate_bulk, daemon=True).start()
    return jsonify({"started": "rate_bulk"})


def _do_rate_selected(job_ids):
    source = "rate_selected"
    _set_status(source, "running")
    conn = db.connect_db()
    lid = _log_start(conn, source)
    count, error = 0, None
    try:
        import cv_bank
        candidates = cv_bank.recent_bullets()  # computed once, reused for every job this run
        for jid in job_ids:
            row = conn.execute("SELECT * FROM jobs_full WHERE id=?", (jid,)).fetchone()
            if not row:
                continue
            job = dict(row)
            if job.get("llm_score") is not None:
                continue  # already rated -- selecting it again shouldn't cost another LLM call
            if not is_it_relevant(job):
                continue
            try:
                result = _rate_one_job(job, candidates)
            except Exception as e:
                error = str(e)
                continue
            db.set_job_state(conn, job["id"], llm_score=result.get("score"),
                              llm_notes=json.dumps(result, ensure_ascii=False))
            conn.commit()
            count += 1
    except Exception as e:
        error = str(e)
    _log_finish(conn, lid, count, error)
    conn.close()
    _set_status(source, "done", count, error)

@app.route("/jobs/rate_selected", methods=["POST"])
def rate_selected():
    job_ids = (request.get_json(silent=True) or {}).get("job_ids") or []
    try:
        job_ids = [int(i) for i in job_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "invalid job_ids"}), 400
    if not job_ids:
        return jsonify({"error": "no jobs selected"}), 400
    with _harvest_lock:
        if _harvest_status.get("rate_selected", {}).get("state") == "running":
            return jsonify({"error": "already running"}), 409
    threading.Thread(target=_do_rate_selected, args=(job_ids,), daemon=True).start()
    return jsonify({"started": "rate_selected"})

@app.route("/jobs/delete_selected", methods=["POST"])
def delete_selected():
    job_ids = (request.get_json(silent=True) or {}).get("job_ids") or []
    try:
        job_ids = [int(i) for i in job_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "invalid job_ids"}), 400
    if not job_ids:
        return jsonify({"error": "no jobs selected"}), 400

    conn = db.connect_db()
    deleted = 0
    for jid in job_ids:
        row = conn.execute("SELECT cv_tex_path FROM jobs_full WHERE id=?", (jid,)).fetchone()
        if row and row["cv_tex_path"]:
            job_dir = Path(row["cv_tex_path"]).resolve().parent
            if _under_generated_root(job_dir):
                shutil.rmtree(job_dir, ignore_errors=True)
        conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
        deleted += 1
    db.delete_job_state(conn, job_ids)
    conn.commit()
    conn.close()
    return jsonify({"deleted": deleted})

def _do_generate_cv(job_id):
    source = f"generate_cv_{job_id}"
    _set_status(source, "running")
    import generate_cv
    on_progress = lambda n: _set_status(source, "running", count=n)
    try:
        result = generate_cv.generate_for_job(job_id, on_progress=on_progress)
    except Exception as e:
        _set_status(source, "error", error=str(e))
        return
    conn = db.connect_db()
    if result["ok"]:
        db.set_job_state(conn, job_id, cv_status="generated", cv_tex_path=result["tex_path"],
                          cv_pdf_path=result["pdf_path"], cover_letter_path=result.get("cover_letter_path"),
                          cv_generated_at=now_iso())
        conn.commit()
    conn.close()
    if result["ok"]:
        _set_status(source, "done")
    else:
        _set_status(source, "error", error=f"LaTeX compile failed after {len(result['attempts'])} attempt(s) -- see {result['tex_path']}")

@app.route("/jobs/<int:job_id>/generate_cv", methods=["POST"])
def generate_cv_route(job_id):
    conn = db.connect_db()
    row = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    source = f"generate_cv_{job_id}"
    with _harvest_lock:
        if _harvest_status.get(source, {}).get("state") == "running":
            return jsonify({"error": "already running"}), 409
    threading.Thread(target=_do_generate_cv, args=(job_id,), daemon=True).start()
    return jsonify({"started": job_id})

def _do_generate_selected(job_ids):
    # Reuses _do_generate_cv one job at a time -- same LaTeX-compile/db-write
    # path as the single-job button, no duplicated generation logic. Each
    # job still gets its own generate_cv_<id> status entry; this "count" is
    # just how many of the batch have finished (successfully or not), for
    # the selection bar's progress readout. Sequential, not parallel -- the
    # LLM backend (esp. a single local Ollama host) isn't meant to serve
    # concurrent generations, and rate_selected already sets this same
    # one-at-a-time precedent.
    source = "generate_selected"
    total = len(job_ids)
    _set_status(source, "running", count=0)
    failed = []
    for i, jid in enumerate(job_ids):
        _do_generate_cv(jid)
        if _harvest_status.get(f"generate_cv_{jid}", {}).get("state") == "error":
            failed.append(str(jid))
        _set_status(source, "running", count=i + 1)
    _set_status(source, "done", count=total,
                error=f"{len(failed)} failed (job id(s) {', '.join(failed)})" if failed else None)

@app.route("/jobs/generate_selected", methods=["POST"])
def generate_selected():
    job_ids = (request.get_json(silent=True) or {}).get("job_ids") or []
    try:
        job_ids = [int(i) for i in job_ids]
    except (TypeError, ValueError):
        return jsonify({"error": "invalid job_ids"}), 400
    if not job_ids:
        return jsonify({"error": "no jobs selected"}), 400
    with _harvest_lock:
        if _harvest_status.get("generate_selected", {}).get("state") == "running":
            return jsonify({"error": "already running"}), 409
    threading.Thread(target=_do_generate_selected, args=(job_ids,), daemon=True).start()
    return jsonify({"started": "generate_selected", "total": len(job_ids)})

@app.route("/jobs/<int:job_id>/cv")
def cv_review(job_id):
    conn = db.connect_db()
    row = conn.execute("SELECT * FROM jobs_full WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return "Not found", 404
    job = dict(row)
    tex_content, compile_error = "", None
    if job.get("cv_tex_path") and Path(job["cv_tex_path"]).exists():
        tex_content = Path(job["cv_tex_path"]).read_text(encoding="utf-8")
    cover_letter_text = ""
    if job.get("cover_letter_path") and Path(job["cover_letter_path"]).exists():
        cover_letter_text = Path(job["cover_letter_path"]).read_text(encoding="utf-8")
    return render_template("cv_review.html", job=job, tex_content=tex_content, compile_error=compile_error,
                            cover_letter_text=cover_letter_text)

@app.route("/jobs/<int:job_id>/cover_letter", methods=["POST"])
def save_cover_letter(job_id):
    conn = db.connect_db()
    row = conn.execute("SELECT cover_letter_path FROM jobs_full WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row or not row["cover_letter_path"]:
        return "No cover letter generated yet for this job", 400
    Path(row["cover_letter_path"]).write_text(request.form.get("cover_letter_text", ""), encoding="utf-8")
    return redirect(url_for("cv_review", job_id=job_id))

@app.route("/jobs/<int:job_id>/cv/recompile", methods=["POST"])
def cv_recompile(job_id):
    import generate_cv
    conn = db.connect_db()
    row = conn.execute("SELECT cv_tex_path FROM jobs_full WHERE id=?", (job_id,)).fetchone()
    if not row or not row["cv_tex_path"]:
        conn.close()
        return "No CV generated yet for this job", 400
    tex_path = Path(row["cv_tex_path"])
    tex_path.write_text(request.form.get("tex_content", ""), encoding="utf-8")
    result = generate_cv.compile_pdf(tex_path)
    if result["ok"]:
        db.set_job_state(conn, job_id, cv_pdf_path=str(tex_path.with_suffix(".pdf")), cv_generated_at=now_iso())
        conn.commit()
    conn.close()
    if not result["ok"]:
        job = get_job_for_review(job_id)
        return render_template("cv_review.html", job=job, tex_content=tex_path.read_text(encoding="utf-8"),
                                compile_error=result["log_tail"])
    return redirect(url_for("cv_review", job_id=job_id))

@app.route("/jobs/<int:job_id>/cv/approve", methods=["POST"])
def cv_approve(job_id):
    conn = db.connect_db()
    db.set_job_state(conn, job_id, cv_status="approved")
    conn.commit()
    conn.close()
    return redirect(url_for("job_detail", job_id=job_id))

def get_job_for_review(job_id):
    conn = db.connect_db()
    row = conn.execute("SELECT * FROM jobs_full WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

@app.route("/generated/<int:job_id>/cv.pdf")
def serve_generated_pdf(job_id):
    conn = db.connect_db()
    row = conn.execute("SELECT cv_pdf_path FROM jobs_full WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row or not row["cv_pdf_path"]:
        return "Not found", 404
    pdf_path = Path(row["cv_pdf_path"]).resolve()
    if not _under_generated_root(pdf_path) or not pdf_path.exists():
        return "Not found", 404
    return Response(pdf_path.read_bytes(), mimetype="application/pdf")

@app.route("/generated/<int:job_id>/cv.tex")
def serve_generated_tex(job_id):
    conn = db.connect_db()
    row = conn.execute("SELECT cv_tex_path FROM jobs_full WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row or not row["cv_tex_path"]:
        return "Not found", 404
    tex_path = Path(row["cv_tex_path"]).resolve()
    if not _under_generated_root(tex_path) or not tex_path.exists():
        return "Not found", 404
    return Response(
        tex_path.read_bytes(), mimetype="application/x-tex",
        headers={"Content-Disposition": f'attachment; filename="{tex_path.name}"'},
    )

@app.route("/harvest/<source>", methods=["POST"])
def harvest(source):
    if source not in _HARVEST_FNS:
        return jsonify({"error": "unknown source"}), 400
    with _harvest_lock:
        if _harvest_status.get(source,{}).get("state") == "running":
            return jsonify({"error": "already running"}), 409
    cleanup_old_jobs(_load_local_config().get("job_retention_days", DEFAULT_JOB_RETENTION_DAYS))
    threading.Thread(target=_HARVEST_FNS[source], daemon=True).start()
    return jsonify({"started": source})

@app.route("/harvest/status")
def harvest_status():
    with _harvest_lock:
        return jsonify(dict(_harvest_status))

@app.route("/settings")
def settings():
    boards    = BOARDS_FILE.read_text() if BOARDS_FILE.exists() else ""

    import cv_bank
    store = cv_bank.build_bullet_store()
    bank_stats = {
        "total":     len(store),
        "csv":       sum(1 for b in store if b["source_tag"].startswith("csv:")),
        "tex":       sum(1 for b in store if b["source_tag"].startswith("tex:")),
        "odt":       sum(1 for b in store if b["source_tag"].startswith("odt:")),
        "csv_path":  str(profile.bullet_bank_path()),
        "tex_dir":   str(cv_bank.tex_source_dir()),
        "odt_dir":   str(cv_bank.odt_source_dir()),
        "csv_exists": profile.bullet_bank_path().exists(),
        "tex_exists": cv_bank.tex_source_dir().exists(),
        "odt_exists": cv_bank.odt_source_dir().exists(),
        "archive_enabled": profile.load_config().get("include_tex_odt_archive", False),
    }

    conn = db.connect_db()
    db_stats = {
        "size_kb":   int(db.DB_PATH.stat().st_size / 1024) if db.DB_PATH.exists() else 0,
        "total_jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "sources": [dict(r) for r in conn.execute(
            "SELECT ats, COUNT(*) n FROM jobs GROUP BY ats ORDER BY n DESC"
        ).fetchall()],
    }
    conn.close()

    # LLM connection settings live on the active profile (config.json via
    # profile.llm_config()), not global env vars -- so DE can use a
    # different backend/model than EN without them interfering. Raw
    # secrets never reach the template, only whether one's set.
    _cfg = profile.llm_config()
    llm_config = {
        "backend":         _cfg["llm_backend"],
        "anthropic_model": _cfg["anthropic_model"],
        "anthropic_key_set": bool(_cfg["anthropic_api_key"]),
        "openai_model":    _cfg["openai_model"],
        "openai_key_set":  bool(_cfg["openai_api_key"]),
        "ollama_host":     _cfg["ollama_host"],
        "ollama_model":    _cfg["ollama_model"],
        "custom_base_url": _cfg["custom_base_url"],
        "custom_model":    _cfg["custom_model"],
        "custom_key_set":  bool(_cfg["custom_api_key"]),
        "custom_min_tokens": _cfg["custom_min_tokens"],
    }
    retention_days = _load_local_config().get("job_retention_days", DEFAULT_JOB_RETENTION_DAYS)
    # Same 0-10 dial/default as generate_cv.GENERATION_BALANCE_DEFAULT --
    # not imported here to avoid pulling in generate_cv's heavier deps
    # (jinja2, latex helpers) just to read one config value.
    try:
        generation_balance = int(profile.load_config().get("generation_balance", 5))
    except (TypeError, ValueError):
        generation_balance = 5
    return render_template("settings.html",
        boards=boards, bank_stats=bank_stats,
        db_stats=db_stats, llm_backend=llm_config["backend"], llm_config=llm_config,
        retention_days=retention_days, cleaned=request.args.get("cleaned"),
        generation_balance=generation_balance,
    )

@app.route("/settings/boards",    methods=["POST"])
def save_boards():
    BOARDS_FILE.write_text(request.form.get("boards",""), encoding="utf-8")
    return redirect(url_for("settings"))

@app.route("/settings/cv_paths", methods=["POST"])
def save_cv_paths():
    # Per-profile: switching profiles switches which archive (if any) is
    # in play, same as everything else profile-scoped in this app.
    cfg = profile.load_config()
    for field in ("tex_dir", "odt_dir"):
        val = (request.form.get(field, "") or "").strip()
        if val:  # blank means "leave the current value alone"
            cfg[field] = val
    cfg["include_tex_odt_archive"] = bool(request.form.get("include_tex_odt_archive"))
    profile.save_config(cfg)
    return redirect(url_for("settings"))

@app.route("/settings/cleanup", methods=["POST"])
def save_cleanup_settings():
    try:
        days = max(1, int(request.form.get("retention_days", DEFAULT_JOB_RETENTION_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_JOB_RETENTION_DAYS
    _save_local_config({"job_retention_days": days})
    deleted = cleanup_old_jobs(days)
    return redirect(url_for("settings", cleaned=deleted))

@app.route("/settings/rebuild_bullets", methods=["POST"])
def rebuild_bullets():
    import importlib
    import cv_bank
    importlib.reload(cv_bank)  # re-read config.local.json path overrides
    cv_bank.build_bullet_store(force_rebuild=True)
    return redirect(url_for("settings"))

# ── bullet bank editor ───────────────────────────────────────────────────────
# Operates directly on the raw CSV grid (not the parsed bullet-store shape)
# so every literal column is editable, including quirks like the file's
# duplicate "German Variant 2 (DE)" header -- nothing gets silently
# reinterpreted or dropped.

def _read_bullet_csv_raw():
    path = profile.bullet_bank_path()
    if not path.exists():
        return [], [], []
    with open(path, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    banner = rows[0] if len(rows) > 0 else []
    header = rows[1] if len(rows) > 1 else []
    data = [r for r in rows[2:] if any(r)]
    return banner, header, data

def _write_bullet_csv_raw(banner, header, data_rows):
    path = profile.bullet_bank_path()
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([banner, header] + data_rows)

def _backup_bullet_csv():
    path = profile.bullet_bank_path()
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak-{stamp}")
    backup_path.write_bytes(path.read_bytes())

def _rebuild_bullet_cache():
    import cv_bank
    cv_bank.build_bullet_store(force_rebuild=True)

@app.route("/bullet_editor")
def bullet_editor():
    banner, header, data = _read_bullet_csv_raw()
    id_col = next((i for i, h in enumerate(header) if h.strip().lower() == "id"), 1)

    # disambiguate any duplicate header labels (e.g. two "German Variant 2 (DE)"
    # columns) for display only -- the underlying column position is unchanged
    seen = {}
    labels = []
    for h in header:
        seen[h] = seen.get(h, 0) + 1
        labels.append(h if seen[h] == 1 else f"{h} (#{seen[h]})")

    anchor_col = next((i for i, h in enumerate(header) if "anchor" in h.lower()), None)

    # Left column: keywords/categorization fields. Right column: identity
    # (ID/employer/title/period) plus the actual bullet text fields.
    left_labels = {"category", "core skill tags", "source cv family", "verb risk",
                   "jd keyword notes", "imp."}
    left_cols = [i for i, h in enumerate(header) if h.strip().lower() in left_labels or i == anchor_col]
    right_cols = [i for i in range(len(header)) if i not in left_cols]

    rows = []
    for row in data:
        row = (row + [""] * len(header))[:len(header)]
        rid = row[id_col] if id_col < len(row) else ""
        if not rid:
            continue  # stray non-bullet row (e.g. a multi-line-quoted-field artifact) -- no ID, nothing to edit
        rows.append({"id": rid, "cells": row})

    return render_template("bullet_editor.html", header=header, labels=labels,
                            rows=rows, id_col=id_col, anchor_col=anchor_col,
                            left_cols=left_cols, right_cols=right_cols,
                            saved=request.args.get("saved"), error=request.args.get("error"))

@app.route("/bullet_editor/save", methods=["POST"])
def bullet_editor_save():
    banner, header, data = _read_bullet_csv_raw()
    id_col = next((i for i, h in enumerate(header) if h.strip().lower() == "id"), 1)

    by_id = {}
    for row in data:
        row = (row + [""] * len(header))[:len(header)]
        rid = row[id_col] if id_col < len(row) else ""
        if rid:
            by_id[rid] = row

    for rid in list(by_id.keys()):
        for col in range(len(header)):
            field = request.form.get(f"cell_{rid}_{col}")
            if field is not None:
                by_id[rid][col] = field

    new_rows = list(by_id.values())
    new_ids = [r[id_col] for r in new_rows if id_col < len(r) and r[id_col].strip()]
    dupes = sorted({i for i in new_ids if new_ids.count(i) > 1})
    if dupes:
        return redirect(url_for("bullet_editor",
                                 error=f"Not saved -- duplicate ID(s): {', '.join(dupes)}. Every bullet needs a unique ID."))

    _backup_bullet_csv()
    _write_bullet_csv_raw(banner, header, new_rows)
    _rebuild_bullet_cache()
    return redirect(url_for("bullet_editor", saved=1))

@app.route("/bullet_editor/add", methods=["POST"])
def bullet_editor_add():
    banner, header, data = _read_bullet_csv_raw()
    id_col = next((i for i, h in enumerate(header) if h.strip().lower() == "id"), 1)
    new_row = [""] * len(header)
    # Placeholder only -- the real Company-Group-Unique ID depends on which
    # employer/group this bullet belongs to, which the user fills in below.
    new_row[id_col] = f"NEW-{uuid.uuid4().hex[:6]}"

    _backup_bullet_csv()
    _write_bullet_csv_raw(banner, header, data + [new_row])
    _rebuild_bullet_cache()
    return redirect(url_for("bullet_editor", saved=1))

@app.route("/bullet_editor/delete/<bullet_id>", methods=["POST"])
def bullet_editor_delete(bullet_id):
    banner, header, data = _read_bullet_csv_raw()
    id_col = next((i for i, h in enumerate(header) if h.strip().lower() == "id"), 1)
    kept = [r for r in data if not (id_col < len(r) and r[id_col] == bullet_id)]

    _backup_bullet_csv()
    _write_bullet_csv_raw(banner, header, kept)
    _rebuild_bullet_cache()
    return redirect(url_for("bullet_editor", saved=1))

@app.route("/bullet_editor/export")
def bullet_editor_export():
    path = profile.bullet_bank_path()
    if not path.exists():
        return "No bullet bank CSV found", 404
    # The stored file is plain UTF-8 (no BOM) -- correct, and what our own
    # csv parsing expects. But Excel/many spreadsheet apps ignore the
    # Content-Type charset and guess the encoding of a downloaded CSV from
    # its bytes alone, defaulting to a Windows codepage without a BOM --
    # garbling umlauts/em-dashes even though the source file is fine. A
    # UTF-8 BOM prepended to just this downloaded copy fixes that guess,
    # without putting a BOM in the file our own code reads.
    body = b"\xef\xbb\xbf" + path.read_bytes()
    # Every profile's file is locally named "bullet_bank.csv" -- fine on
    # disk (separate directories), but a downloaded copy loses that
    # separation the moment two profiles' exports land in the same
    # Downloads folder. Stamp the profile ID into the download's filename
    # so EN and DE exports can't get silently mixed up.
    download_name = f"{profile.active_profile_id() or 'bullet_bank'}_{path.name}"
    return Response(
        body, mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"',
                 "Content-Type": "text/csv; charset=utf-8"},
    )

@app.route("/bullet_editor/import", methods=["POST"])
def bullet_editor_import():
    upload = request.files.get("csv_file")
    if not upload or not upload.filename:
        return redirect(url_for("bullet_editor", error="No file selected"))

    raw = upload.read()
    # utf-8-sig strips a UTF-8 BOM if present (our own export adds one for
    # Excel's benefit -- see bullet_editor_export). If a re-saved file
    # isn't UTF-8 at all, it's almost always Excel's "CSV (Comma
    # delimited)" export defaulting to the Windows codepage instead of
    # "CSV UTF-8" -- try that before giving up, then re-store as clean
    # UTF-8 either way so umlauts/em-dashes survive round-tripping.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError as e:
            return redirect(url_for("bullet_editor", error=f"Could not parse file (unrecognized encoding): {e}"))
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        return redirect(url_for("bullet_editor", error=f"Could not parse file: {e}"))

    if len(rows) < 3:
        return redirect(url_for("bullet_editor", error="File doesn't look like the bullet bank (too few rows)"))
    header = rows[1]
    required = ["employer", "id", "job title", "period", "base bullet"]
    missing = [r for r in required if not any(r in h.lower() for h in header)]
    if missing:
        return redirect(url_for("bullet_editor", error=f"Missing expected column(s): {', '.join(missing)}"))

    _backup_bullet_csv()
    profile.bullet_bank_path().write_text(text, encoding="utf-8")
    _rebuild_bullet_cache()
    return redirect(url_for("bullet_editor", saved=1))

@app.route("/settings/llm", methods=["POST"])
def save_llm_config():
    # Saved to the ACTIVE PROFILE's own config.json (profile.save_llm_config),
    # not a shared global file -- switching profiles switches which LLM
    # setup is in play, same as everything else profile-scoped here.
    updates = {"llm_backend": request.form.get("backend", "stub")}

    # only overwrite a value if the user actually typed something -- leaves
    # the existing saved value alone on an otherwise-unrelated save
    fields = {
        "anthropic_key":   "anthropic_api_key",
        "anthropic_model": "anthropic_model",
        "openai_key":      "openai_api_key",
        "openai_model":    "openai_model",
        "ollama_host":     "ollama_host",
        "ollama_model":    "ollama_model",
        "custom_base_url": "custom_base_url",
        "custom_key":      "custom_api_key",
        "custom_model":    "custom_model",
    }
    for form_field, config_key in fields.items():
        val = (request.form.get(form_field, "") or "").strip()
        if val:
            updates[config_key] = val

    # validated separately (must be a positive int) rather than saved as
    # a raw string like the fields above -- generate_cv.py/llm_plugin.py
    # both do int(profile.llm_config()["custom_min_tokens"]) with no guard
    min_tokens_val = (request.form.get("custom_min_tokens", "") or "").strip()
    if min_tokens_val:
        try:
            updates["custom_min_tokens"] = max(1, int(min_tokens_val))
        except ValueError:
            pass

    profile.save_llm_config(updates)
    return redirect(url_for("settings"))

@app.route("/settings/generation", methods=["POST"])
def save_generation_balance():
    # Same profile-scoped config.json as everything else here -- see
    # generate_cv.generation_mode() for how the 0-10 value collapses into
    # the "rule"/"hybrid"/"llm" behavior bands.
    try:
        val = max(0, min(10, int(request.form.get("generation_balance", 5))))
    except (TypeError, ValueError):
        val = 5
    cfg = profile.load_config()
    cfg["generation_balance"] = val
    profile.save_config(cfg)
    return redirect(url_for("settings"))

@app.route("/settings/llm/test", methods=["POST"])
def test_llm_connection():
    import llm_plugin
    backend = request.form.get("backend") or profile.llm_config()["llm_backend"]
    result = llm_plugin.test_connection(backend)
    return jsonify(result)

@app.route("/settings/llm/ollama/unload", methods=["POST"])
def unload_ollama():
    import llm_plugin
    return jsonify(llm_plugin.unload_ollama_models())

@app.route("/export")
def export_csv():
    conn = db.connect_db()
    where, params = build_filter_sql(request.args)
    rows = conn.execute(
        f"""SELECT id,company,title,location,city,country,region_group,
                   language,ats,url,keywords_raw,date_posted,starred,llm_score
            FROM jobs_full {where} ORDER BY company,title""",
        params,
    ).fetchall()
    conn.close()
    cols = ["id","company","title","location","city","country","region_group",
            "language","ats","url","keywords_raw","date_posted","starred","llm_score"]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(dict(r))
    return Response(
        out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobs_export.csv"},
    )

@app.route("/export/histogram")
def export_histogram_csv():
    import cv_bank
    conn = db.connect_db()
    prof_counts = Counter(classify_profession(t) for (t,) in conn.execute("SELECT title FROM jobs"))
    skill_counts = Counter()
    kw_weights = cv_bank.keyword_weights()
    for (kw_raw,) in conn.execute("SELECT keywords_raw FROM jobs WHERE keywords_raw != ''"):
        for kw in kw_weights:
            if kw in (kw_raw or ""):
                skill_counts[kw] += 1
    conn.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["type", "value", "count"])
    for p, n in prof_counts.most_common():
        w.writerow(["profession", p, n])
    for s, n in skill_counts.most_common():
        w.writerow(["skill", s, n])
    return Response(
        out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=job_histogram.csv"},
    )

_EXTERNAL_PROMPTS = [
    ("build_bullet_bank", "Build Your Bullet Bank",         "bi-hammer",         "Turn raw career history into properly-structured bullet-bank entries, ready to transcribe into the Bullet Editor."),
    ("jd_analysis",      "Analyze a Job Description",       "bi-search",         "Paste a JD to extract what they actually need vs. what they say."),
    ("cv_block_tailor",  "Tailor a CV Block",                "bi-layout-text-sidebar", "Reword an existing CV bullet to mirror the JD's language."),
    ("cover_letter_en",  "English Cover Letter",             "bi-envelope",       "For EU/international roles."),
    ("cover_letter_de",  "Deutsches Bewerbungsschreiben",    "bi-flag",           "For roles that require German or prefer a German application."),
    ("interview_prep",   "Interview Prep",                   "bi-person-check",   "Generate likely technical questions based on a JD."),
    ("salary_research",  "Salary Research",                  "bi-currency-euro",  "Understand what to ask for before negotiating."),
    ("cold_outreach",    "Cold Outreach (LinkedIn / XING)",  "bi-person-plus",    "Message a hiring manager or team lead directly."),
    ("followup",         "Follow-up After Applying",         "bi-send",           "After 10-14 days with no response."),
]

@app.route("/prompts", methods=["GET", "POST"])
def prompts():
    prompts_dir = profile.external_prompts_dir()
    if request.method == "POST":
        for name, _label, _icon, _desc in _EXTERNAL_PROMPTS:
            text = request.form.get(name)
            if text is not None:
                prompts_dir.mkdir(parents=True, exist_ok=True)
                (prompts_dir / f"{name}.txt").write_text(text, encoding="utf-8")
        return redirect(url_for("prompts", saved=1))

    items = []
    for name, label, icon, desc in _EXTERNAL_PROMPTS:
        path = prompts_dir / f"{name}.txt"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        items.append({"name": name, "label": label, "icon": icon, "desc": desc, "text": text})
    return render_template("prompts.html", items=items, saved=request.args.get("saved"))

@app.route("/template_editor", methods=["GET", "POST"])
def template_editor():
    if not profile.active_profile_id():
        return redirect(url_for("index"))
    path = profile.template_path()
    if request.method == "POST":
        path.write_text(request.form.get("template_content", ""), encoding="utf-8")
        return redirect(url_for("template_editor", saved=1))
    return render_template("template_editor.html",
                            template_content=path.read_text(encoding="utf-8") if path.exists() else "",
                            saved=request.args.get("saved"))

@app.route("/template_editor/reset", methods=["POST"])
def reset_template():
    if not profile.active_profile_id():
        return redirect(url_for("index"))
    default_path = profile.SKELETON_DIR / "template.tex.jinja"
    if default_path.exists():
        profile.template_path().write_text(default_path.read_text(encoding="utf-8"), encoding="utf-8")
    return redirect(url_for("template_editor", saved=1))

_SYSTEM_PROMPTS = [
    ("rate_job",           "Job Rating",       "Used to score every job 0-10 (single-job rate and bulk/selected rating)."),
    ("recommend_bullets",  "CV Bullet Selection", "Picks which real bullets go into the tailored CV per job."),
    ("intro",              "CV Intro Summary",  "Writes the 2-sentence summary paragraph at the top of the CV."),
    ("cover_letter",       "Cover Letter",      "Writes the opening, body, and closing paragraphs of the cover letter."),
]

@app.route("/system_prompts", methods=["GET", "POST"])
def system_prompts():
    prompts_dir = profile.prompts_dir()
    defaults_dir = BASE / "default-settings" / "prompts"
    if request.method == "POST":
        for name, _label, _desc in _SYSTEM_PROMPTS:
            text = request.form.get(name)
            if text is not None:
                (prompts_dir / f"{name}.txt").write_text(text, encoding="utf-8")
        return redirect(url_for("system_prompts", saved=1))

    items = []
    for name, label, desc in _SYSTEM_PROMPTS:
        path = prompts_dir / f"{name}.txt"
        default_path = defaults_dir / f"{name}.txt"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        default_text = default_path.read_text(encoding="utf-8") if default_path.exists() else ""
        items.append({
            "name": name, "label": label, "desc": desc, "text": text,
            "is_customized": text != default_text,
        })
    return render_template("system_prompts.html", items=items, saved=request.args.get("saved"))

@app.route("/system_prompts/reset/<name>", methods=["POST"])
def reset_system_prompt(name):
    valid_names = {n for n, _l, _d in _SYSTEM_PROMPTS}
    if name not in valid_names:
        return "Unknown prompt", 404
    default_path = BASE / "default-settings" / "prompts" / f"{name}.txt"
    if default_path.exists():
        (profile.prompts_dir() / f"{name}.txt").write_text(
            default_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return redirect(url_for("system_prompts", saved=1))

# ── profiles ───────────────────────────────────────────────────────────────────

@app.route("/profiles/switch", methods=["POST"])
def switch_profile():
    pid = (request.form.get("pid") or "").strip()
    if pid in profile.list_profiles():
        profile.set_active_profile(pid)
    return redirect(request.referrer or url_for("index"))

@app.route("/profiles/new", methods=["POST"])
def new_profile():
    label = (request.form.get("label") or "").strip()
    if not label:
        return redirect(url_for("index"))
    pid = profile.create_profile(label)
    profile.set_active_profile(pid)
    return redirect(url_for("edit_profile"))

# List fields: each is a set of parallel form-array columns (e.g. link_label[]/
# link_url[]) reassembled into a list of dicts, one dict per submitted row.
_PROFILE_LIST_FIELDS = {
    "links":        ["label", "url"],
    "education":    ["school", "degree", "year"],
    "certificates": ["name", "id", "date"],
    "languages":    ["name", "level"],
    "projects":     ["title", "period", "location", "summary"],
}
_PROFILE_FLAT_FIELDS = [
    "name", "default_title", "location", "phone", "email",
    "nationality", "visa", "photo_path",
]

@app.route("/profiles/edit", methods=["GET", "POST"])
def edit_profile():
    if not profile.active_profile_id():
        return redirect(url_for("index"))

    if request.method == "POST":
        data = {f: (request.form.get(f, "") or "").strip() for f in _PROFILE_FLAT_FIELDS}
        for group, cols in _PROFILE_LIST_FIELDS.items():
            lists = {c: request.form.getlist(f"{group}_{c}") for c in cols}
            n = max((len(v) for v in lists.values()), default=0)
            rows = []
            for i in range(n):
                row = {c: (lists[c][i] if i < len(lists[c]) else "").strip() for c in cols}
                if any(row.values()):
                    rows.append(row)
            data[group] = rows
        profile.save_profile(data)
        return redirect(url_for("edit_profile", saved=1))

    return render_template("profile_edit.html",
                            data=profile.load_profile(),
                            list_fields=_PROFILE_LIST_FIELDS,
                            saved=request.args.get("saved"))

# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_schema()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5050))
    print(f"Job Assist running at  http://{host}:{port}")
    print("  Set HOST=0.0.0.0 to allow LAN/OpenStack access")
    print("  Set PORT=XXXX  to change port")
    app.run(host=host, port=port, debug=True)
