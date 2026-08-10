#!/usr/bin/env python3
"""
Job Portal — local web UI for job harvesting and CV mapping.

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
import sqlite3
import sys
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import feedparser
import requests
from flask import (Flask, Response, jsonify, redirect,
                   render_template, request, url_for)

BASE         = Path(__file__).parent
DB_PATH      = BASE / "jobs.db"
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

KEYWORDS_WEIGHTED = {
    "linux":8,"ansible":7,"python":6,"bash":5,"terraform":6,"jenkins":5,
    "docker":5,"kubernetes":6,"vmware":5,"grafana":4,"graylog":4,"icinga":4,
    "loki":4,"prometheus":4,"nginx":4,"gitlab":4,"github actions":4,
    "hpc":9,"cluster":8,"slurm":8,"infiniband":8,"ipmi":7,"pxe":7,"gpu":7,
    "azure":5,"aws":5,"debian":5,"ubuntu":5,"rhel":5,"alma":4,"networking":4,
    "routing":4,"ci/cd":4,"proxmox":6,"openstack":5,"ceph":5,"zfs":4,
    "lustre":7,"gpfs":6,"beegfs":6,"mpi":7,"openmpi":7,"cuda":6,"rdma":7,
    "puppet":4,"chef":4,"saltstack":4,"packer":4,"vault":4,"consul":4,
    "nfs":4,"iscsi":4,"fiber channel":5,"fibre channel":5,
}

# Broader-than-KEYWORDS_WEIGHTED terms for the gross IT-relevance pre-filter --
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
    "devops_sre":     ["devops", "site reliability", " sre", "platform engineer"],
    "cloud":          ["cloud engineer", "cloud architect", "aws engineer", "azure engineer", "cloud"],
    "linux_sysadmin": ["linux", "sysadmin", "systemadministrator", "system administrator", "systemtechniker"],
    "network":        ["network", "netzwerk"],
    "security":       ["security", "cyber", "sicherheit"],
    "data_db":        ["data engineer", "database administrator", "dba", "data scientist"],
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

_KEYWORD_PATTERNS = {
    kw: re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")
    for kw in KEYWORDS_WEIGHTED
}

def extract_keywords(text):
    lower = text.lower()
    return " ".join(kw for kw, pat in _KEYWORD_PATTERNS.items() if pat.search(lower))

def safe_get(obj, path, default=None):
    cur = obj
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur

# ── database ───────────────────────────────────────────────────────────────────

def connect_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def ensure_schema():
    conn = connect_db()
    conn.executescript(SCHEMA_SQL)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col, coldef in EXTRA_COLS:
        if col not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coldef}")
    conn.commit()
    conn.close()

def upsert_job(conn, rec: dict):
    # Locked-in filtering rule: non-Germany EU postings must be English --
    # a posting confidently detected as written in another language is
    # hard-excluded (not just flagged) here, uniformly across every
    # harvester, rather than relying on each one to remember the rule.
    if rec.get("region_group") == "eu" and rec.get("language") == "foreign":
        return
    cols = [
        "source","source_type","ats","external_id","company","company_size",
        "title","location","city","country","region_group","language",
        "international_flag","recruiter_flag","direct_company_flag","url",
        "date_posted","description","keywords_raw","normalized_text","created_at",
    ]
    vals = [rec.get(c) for c in cols]
    placeholders = ",".join("?" * len(cols))
    conn.execute(
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
    conn = connect_db()
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
    conn = connect_db()
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
    conn = connect_db()
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
IT_SEARCH_TERMS = ["Linux system administrator", "DevOps engineer Linux"]

def _do_harvest_jobspy():
    """speedyapply/JobSpy — reaches LinkedIn/Indeed/Glassdoor/ZipRecruiter,
    which the API-only harvesters above can't touch."""
    source = "jobspy"
    _set_status(source, "running")
    conn = connect_db()
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
                        results_wanted=15, hours_old=336,
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
    conn = connect_db()
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

    if args.get("unviewed") == "1":
        clauses.append("viewed_at IS NULL")

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
    conn = connect_db()
    stats = {
        "total":    conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "scored":   conn.execute("SELECT COUNT(*) FROM jobs WHERE llm_score IS NOT NULL").fetchone()[0],
        "unviewed": conn.execute("SELECT COUNT(*) FROM jobs WHERE viewed_at IS NULL").fetchone()[0],
        "today":    conn.execute("SELECT COUNT(*) FROM jobs WHERE date(created_at)=date('now')").fetchone()[0],
    }
    llm_backend = os.environ.get("LLM_BACKEND", "stub")
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
    skill_counts = Counter()
    for (kw_raw,) in conn.execute("SELECT keywords_raw FROM jobs WHERE keywords_raw != ''"):
        # keywords_raw is a space-joined subset of KEYWORDS_WEIGHTED's keys
        # (some of which contain their own spaces, e.g. "github actions") --
        # re-match against the known key list instead of a naive .split(),
        # which would wrongly break multi-word keywords into pieces.
        present = (kw_raw or "")
        for kw in KEYWORDS_WEIGHTED:
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
        harvest_status=dict(_harvest_status), llm_backend=llm_backend,
    )

@app.route("/jobs")
def jobs():
    conn = connect_db()
    where, params = build_filter_sql(request.args)

    sort = request.args.get("sort","created_at")
    if sort not in {"created_at","company","title","llm_score","region_group","language","date_posted"}:
        sort = "created_at"
    order = "DESC" if sort in ("created_at","llm_score","date_posted") else "ASC"

    limit  = min(int(request.args.get("limit",  200)), 5000)
    offset = max(int(request.args.get("offset", 0)),   0)

    total = conn.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]
    rows  = conn.execute(
        f"""SELECT id,company,title,location,city,country,region_group,
                   language,ats,source_type,url,keywords_raw,date_posted,
                   llm_score,llm_notes,created_at,viewed_at
            FROM jobs {where}
            ORDER BY {sort} {order}
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    conn.close()

    return render_template("jobs.html",
        jobs=[dict(r) for r in rows],
        total=total, limit=limit, offset=offset,
        args=dict(request.args),
        llm_backend=os.environ.get("LLM_BACKEND", "stub"),
    )

@app.route("/jobs/<int:job_id>")
def job_detail(job_id):
    conn = connect_db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return "Not found", 404
    job = dict(row)
    already_viewed = bool(job.get("viewed_at"))
    if not already_viewed:
        conn.execute("UPDATE jobs SET viewed_at=? WHERE id=?", (now_iso(), job_id))
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
    conn = connect_db()
    conn.execute("UPDATE jobs SET notes=? WHERE id=?", (notes, job_id))
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

    conn = connect_db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    job = dict(row)

    try:
        result = _rate_one_job(job)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    conn = connect_db()
    conn.execute(
        "UPDATE jobs SET llm_score=?, llm_notes=? WHERE id=?",
        (result.get("score"), json.dumps(result, ensure_ascii=False), job_id),
    )
    conn.commit()
    conn.close()
    return jsonify(result)


_RATE_BULK_CAP = 300  # bound one run's duration regardless of backlog size

def _do_rate_bulk():
    source = "rate_bulk"
    _set_status(source, "running")
    conn = connect_db()
    lid = _log_start(conn, source)
    count, error = 0, None
    try:
        import cv_bank
        candidates = cv_bank.recent_bullets()  # computed once, reused for every job this run
        rows = conn.execute(
            "SELECT * FROM jobs WHERE llm_score IS NULL AND region_group != 'other' "
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
            conn.execute(
                "UPDATE jobs SET llm_score=?, llm_notes=? WHERE id=?",
                (result.get("score"), json.dumps(result, ensure_ascii=False), job["id"]),
            )
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

@app.route("/jobs/<int:job_id>/generate_cv", methods=["POST"])
def generate_cv_route(job_id):
    import generate_cv
    conn = connect_db()
    row = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return "Not found", 404
    try:
        result = generate_cv.generate_for_job(job_id)
    except Exception as e:
        conn.close()
        return f"CV generation failed: {e}", 500
    if result["ok"]:
        conn.execute(
            "UPDATE jobs SET cv_status='generated', cv_tex_path=?, cv_pdf_path=?, cv_generated_at=? WHERE id=?",
            (result["tex_path"], result["pdf_path"], now_iso(), job_id),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("cv_review", job_id=job_id))

@app.route("/jobs/<int:job_id>/cv")
def cv_review(job_id):
    conn = connect_db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return "Not found", 404
    job = dict(row)
    tex_content, compile_error = "", None
    if job.get("cv_tex_path") and Path(job["cv_tex_path"]).exists():
        tex_content = Path(job["cv_tex_path"]).read_text(encoding="utf-8")
    return render_template("cv_review.html", job=job, tex_content=tex_content, compile_error=compile_error)

@app.route("/jobs/<int:job_id>/cv/recompile", methods=["POST"])
def cv_recompile(job_id):
    import generate_cv
    conn = connect_db()
    row = conn.execute("SELECT cv_tex_path FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or not row["cv_tex_path"]:
        conn.close()
        return "No CV generated yet for this job", 400
    tex_path = Path(row["cv_tex_path"])
    tex_path.write_text(request.form.get("tex_content", ""), encoding="utf-8")
    result = generate_cv.compile_pdf(tex_path)
    if result["ok"]:
        conn.execute(
            "UPDATE jobs SET cv_pdf_path=?, cv_generated_at=? WHERE id=?",
            (str(tex_path.with_suffix(".pdf")), now_iso(), job_id),
        )
        conn.commit()
    conn.close()
    if not result["ok"]:
        job = get_job_for_review(job_id)
        return render_template("cv_review.html", job=job, tex_content=tex_path.read_text(encoding="utf-8"),
                                compile_error=result["log_tail"])
    return redirect(url_for("cv_review", job_id=job_id))

@app.route("/jobs/<int:job_id>/cv/approve", methods=["POST"])
def cv_approve(job_id):
    conn = connect_db()
    conn.execute("UPDATE jobs SET cv_status='approved' WHERE id=?", (job_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("job_detail", job_id=job_id))

def get_job_for_review(job_id):
    conn = connect_db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

@app.route("/generated/<int:job_id>/cv.pdf")
def serve_generated_pdf(job_id):
    conn = connect_db()
    row = conn.execute("SELECT cv_pdf_path FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row or not row["cv_pdf_path"]:
        return "Not found", 404
    pdf_path = Path(row["cv_pdf_path"]).resolve()
    generated_root = (BASE / "generated").resolve()
    if generated_root not in pdf_path.parents or not pdf_path.exists():
        return "Not found", 404
    return Response(pdf_path.read_bytes(), mimetype="application/pdf")

@app.route("/harvest/<source>", methods=["POST"])
def harvest(source):
    if source not in _HARVEST_FNS:
        return jsonify({"error": "unknown source"}), 400
    with _harvest_lock:
        if _harvest_status.get(source,{}).get("state") == "running":
            return jsonify({"error": "already running"}), 409
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
        "csv_path":  str(cv_bank.CSV_PATH),
        "tex_dir":   str(cv_bank.TEX_DIR),
        "odt_dir":   str(cv_bank.ODT_DIR),
        "csv_exists": cv_bank.CSV_PATH.exists(),
        "tex_exists": cv_bank.TEX_DIR.exists(),
        "odt_exists": cv_bank.ODT_DIR.exists(),
    }

    conn = connect_db()
    db_stats = {
        "size_kb":   int(DB_PATH.stat().st_size / 1024) if DB_PATH.exists() else 0,
        "total_jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "sources": [dict(r) for r in conn.execute(
            "SELECT ats, COUNT(*) n FROM jobs GROUP BY ats ORDER BY n DESC"
        ).fetchall()],
    }
    conn.close()
    return render_template("settings.html",
        boards=boards, bank_stats=bank_stats,
        db_stats=db_stats, llm_backend=os.environ.get("LLM_BACKEND","stub"),
    )

@app.route("/settings/boards",    methods=["POST"])
def save_boards():
    BOARDS_FILE.write_text(request.form.get("boards",""), encoding="utf-8")
    return redirect(url_for("settings"))

@app.route("/settings/cv_paths", methods=["POST"])
def save_cv_paths():
    cfg = {}
    for field in ("csv_path", "tex_dir", "odt_dir"):
        val = (request.form.get(field, "") or "").strip()
        if val:
            cfg[field] = val
    LOCAL_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return redirect(url_for("settings"))

@app.route("/settings/rebuild_bullets", methods=["POST"])
def rebuild_bullets():
    import importlib
    import cv_bank
    importlib.reload(cv_bank)  # re-read config.local.json path overrides
    cv_bank.build_bullet_store(force_rebuild=True)
    return redirect(url_for("settings"))

@app.route("/export")
def export_csv():
    conn = connect_db()
    where, params = build_filter_sql(request.args)
    rows = conn.execute(
        f"""SELECT id,company,title,location,city,country,region_group,
                   language,ats,url,keywords_raw,date_posted,starred,llm_score
            FROM jobs {where} ORDER BY company,title""",
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
    conn = connect_db()
    prof_counts = Counter(classify_profession(t) for (t,) in conn.execute("SELECT title FROM jobs"))
    skill_counts = Counter()
    for (kw_raw,) in conn.execute("SELECT keywords_raw FROM jobs WHERE keywords_raw != ''"):
        for kw in KEYWORDS_WEIGHTED:
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

@app.route("/prompts")
def prompts():
    return render_template("prompts.html")

# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_schema()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5050))
    print(f"Job Portal running at  http://{host}:{port}")
    print("  Set HOST=0.0.0.0 to allow LAN/OpenStack access")
    print("  Set PORT=XXXX  to change port")
    app.run(host=host, port=port, debug=True)
