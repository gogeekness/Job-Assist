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
import threading
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
LEVER_FILE   = BASE / "lever_companies.txt"
BLOCKS_FILE  = BASE / "cv_blocks_template.json"

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
    ("starred",   "INTEGER DEFAULT 0"),
    ("notes",     "TEXT"),
    ("llm_score", "REAL"),
    ("llm_notes", "TEXT"),
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

# ── helpers ────────────────────────────────────────────────────────────────────

def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def normalize_space(text):
    return re.sub(r"\s+", " ", (text or "")).strip()

def strip_html(text):
    if not text:
        return ""
    return normalize_space(html.unescape(re.sub(r"<[^>]+>", " ", text)))

def detect_language(text):
    t = text.lower()
    for level in ("german", "partial_german", "english"):
        if any(h in t for h in LANGUAGE_HINTS[level]):
            return level
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

def extract_keywords(text):
    lower = text.lower()
    return " ".join(kw for kw in KEYWORDS_WEIGHTED if kw in lower)

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

def _do_harvest_lever(companies_file):
    source = "lever"
    _set_status(source, "running")
    conn = connect_db()
    lid = _log_start(conn, source)
    count, error = 0, None
    try:
        tokens = [x.strip() for x in Path(companies_file).read_text().splitlines() if x.strip()]
        for token in tokens:
            try:
                jobs = requests.get(
                    f"https://api.lever.co/v0/postings/{token}?mode=json",
                    timeout=TIMEOUT
                ).json()
                if not isinstance(jobs, list):
                    continue
            except Exception:
                continue
            for j in jobs:
                title    = safe_get(j, ["text"], "")
                cats     = safe_get(j, ["categories"], {}) or {}
                location = cats.get("location", "") or ""
                desc     = strip_html(safe_get(j,["descriptionPlain"],"") or safe_get(j,["description"],""))
                combined = f"{title} {token} {location} {cats.get('commitment','')} {cats.get('team','')} {desc}"
                city, country, rg = parse_location(location)
                upsert_job(conn, {
                    "source": f"lever:{token}", "source_type": "company",
                    "ats": "lever", "external_id": str(safe_get(j,["id"],"")),
                    "company": token, "company_size": None,
                    "title": title, "location": location, "city": city,
                    "country": country, "region_group": rg,
                    "language": detect_language(combined),
                    "international_flag": detect_international(combined),
                    "recruiter_flag": 0, "direct_company_flag": 1,
                    "url": safe_get(j, ["hostedUrl"], ""),
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

_HARVEST_FNS = {
    "greenhouse": lambda: _do_harvest_greenhouse(str(BOARDS_FILE)),
    "lever":      lambda: _do_harvest_lever(str(LEVER_FILE)),
    "arbeitnow":  _do_harvest_arbeitnow,
    "euraxess":   _do_harvest_euraxess,
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

    if args.get("starred") == "1":
        clauses.append("starred=1")

    if args.get("scored") == "1":
        clauses.append("llm_score IS NOT NULL")

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

# ── CV block scorer ────────────────────────────────────────────────────────────

def score_blocks(job_text, blocks):
    lower = job_text.lower()
    scored = []
    for b in blocks:
        s = sum(KEYWORDS_WEIGHTED.get(kw.lower(), 3)
                for kw in b.get("keywords",[]) if kw.lower() in lower)
        if s > 0:
            scored.append({**b, "score": s})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored

# ── routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    conn = connect_db()
    stats = {
        "total":   conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "starred": conn.execute("SELECT COUNT(*) FROM jobs WHERE starred=1").fetchone()[0],
        "scored":  conn.execute("SELECT COUNT(*) FROM jobs WHERE llm_score IS NOT NULL").fetchone()[0],
        "today":   conn.execute("SELECT COUNT(*) FROM jobs WHERE date(created_at)=date('now')").fetchone()[0],
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
    recent_log = [dict(r) for r in conn.execute(
        "SELECT * FROM harvest_log ORDER BY started_at DESC LIMIT 12"
    ).fetchall()]
    conn.close()
    return render_template("index.html",
        stats=stats, by_region=by_region, by_lang=by_lang,
        by_source=by_source, recent_log=recent_log,
        harvest_status=dict(_harvest_status),
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
                   starred,llm_score,llm_notes,created_at
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
    )

@app.route("/jobs/<int:job_id>")
def job_detail(job_id):
    conn = connect_db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return "Not found", 404
    job = dict(row)
    scored_blocks, all_blocks = [], []
    if BLOCKS_FILE.exists():
        data = json.loads(BLOCKS_FILE.read_text(encoding="utf-8"))
        all_blocks = data.get("blocks",[])
        jtext = " ".join(filter(None,[
            job.get("title"), job.get("company"), job.get("location"),
            job.get("description"), job.get("keywords_raw"),
        ]))
        scored_blocks = score_blocks(jtext, all_blocks)
    return render_template("job_detail.html",
        job=job, scored_blocks=scored_blocks, all_blocks=all_blocks,
    )

@app.route("/jobs/<int:job_id>/star", methods=["POST"])
def toggle_star(job_id):
    conn = connect_db()
    row = conn.execute("SELECT starred FROM jobs WHERE id=?", (job_id,)).fetchone()
    new_val = 0
    if row:
        new_val = 0 if row["starred"] else 1
        conn.execute("UPDATE jobs SET starred=? WHERE id=?", (new_val, job_id))
        conn.commit()
    conn.close()
    return jsonify({"starred": new_val})

@app.route("/jobs/<int:job_id>/notes", methods=["POST"])
def save_notes(job_id):
    notes = (request.json or {}).get("notes","")
    conn = connect_db()
    conn.execute("UPDATE jobs SET notes=? WHERE id=?", (notes, job_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/jobs/<int:job_id>/rate", methods=["POST"])
def rate_job(job_id):
    try:
        import llm_plugin
    except ImportError:
        return jsonify({"error": "llm_plugin.py not found"}), 500

    conn = connect_db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404

    blocks = []
    if BLOCKS_FILE.exists():
        blocks = json.loads(BLOCKS_FILE.read_text(encoding="utf-8")).get("blocks",[])

    try:
        result = llm_plugin.rate_job(dict(row), blocks)
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
    companies = LEVER_FILE.read_text()  if LEVER_FILE.exists()  else ""
    cv_raw    = BLOCKS_FILE.read_text() if BLOCKS_FILE.exists() else "{}"
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
        boards=boards, companies=companies, cv_blocks=cv_raw,
        db_stats=db_stats, llm_backend=os.environ.get("LLM_BACKEND","stub"),
    )

@app.route("/settings/boards",    methods=["POST"])
def save_boards():
    BOARDS_FILE.write_text(request.form.get("boards",""), encoding="utf-8")
    return redirect(url_for("settings"))

@app.route("/settings/companies", methods=["POST"])
def save_companies():
    LEVER_FILE.write_text(request.form.get("companies",""), encoding="utf-8")
    return redirect(url_for("settings"))

@app.route("/settings/cv_blocks", methods=["POST"])
def save_cv_blocks():
    raw = request.form.get("cv_blocks","{}") or "{}"
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}", 400
    BLOCKS_FILE.write_text(raw, encoding="utf-8")
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
