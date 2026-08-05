
#!/usr/bin/env python3
"""
Local-first job harvesting and CV block mapping tool.

What it does:
1. Harvest public jobs from Greenhouse boards
2. Harvest public jobs from Lever postings
3. Import additional jobs from CSV/manual exports
4. Normalize records into SQLite
5. Filter for EU / Germany / Berlin + language + company type + source
6. Score jobs against your skill blocks
7. Build a draft CV JSON/text bundle from the best matching blocks

Usage examples:

  python job_harvester_and_cv_mapper.py init-db jobs.db

  python job_harvester_and_cv_mapper.py harvest-greenhouse jobs.db boards.txt
  python job_harvester_and_cv_mapper.py harvest-lever jobs.db lever_companies.txt

  python job_harvester_and_cv_mapper.py import-csv jobs.db exported_jobs.csv

  python job_harvester_and_cv_mapper.py filter jobs.db --region germany --city berlin \
      --language english,partial_german --min-company-size 100 --source company,direct,recruiter

  python job_harvester_and_cv_mapper.py map-job jobs.db 42 cv_blocks_template.json

Notes:
- This script intentionally favors public ATS endpoints and manual imports.
- It avoids relying on private or brittle endpoints as the backbone.
- For LinkedIn/StepStone/Indeed, use alerts or manual exports, then import CSV.
"""

import argparse
import csv
import html
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

TIMEOUT = 20

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_type TEXT,              -- company / recruiter / board / institution
    ats TEXT,                      -- greenhouse / lever / manual / euraxess / linkedin / stepstone
    external_id TEXT,
    company TEXT,
    company_size INTEGER,
    title TEXT,
    location TEXT,
    city TEXT,
    country TEXT,
    region_group TEXT,             -- berlin / germany / eu / other
    language TEXT,                 -- english / partial_german / german / unknown
    international_flag INTEGER DEFAULT 0,
    recruiter_flag INTEGER DEFAULT 0,
    direct_company_flag INTEGER DEFAULT 0,
    url TEXT,
    date_posted TEXT,
    description TEXT,
    keywords_raw TEXT,
    normalized_text TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source, external_id, url)
);

CREATE TABLE IF NOT EXISTS filters_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    args_json TEXT NOT NULL
);
"""

GERMANY_CITIES = {
    "berlin", "hamburg", "munich", "münchen", "frankfurt", "stuttgart", "karlsruhe",
    "cologne", "köln", "bremen", "hannover", "bonn", "heidelberg", "dresden",
    "leipzig", "jülich", "juelich", "ulm", "mannheim", "nuremberg", "nürnberg",
    "böblingen", "boeblingen"
}

EU_COUNTRIES = {
    "germany", "deutschland", "austria", "france", "netherlands", "belgium", "ireland",
    "spain", "portugal", "italy", "poland", "czech republic", "czechia", "denmark",
    "sweden", "finland", "slovakia", "slovenia", "croatia", "hungary", "romania",
    "bulgaria", "luxembourg", "latvia", "lithuania", "estonia", "malta", "cyprus",
    "greece"
}

LANGUAGE_HINTS = {
    "english": [
        "english", "international team", "working language is english",
        "german is a plus", "german desirable", "business english"
    ],
    "partial_german": [
        "german is a plus", "basic german", "good written german", "b1", "a2", "a2-b1"
    ],
    "german": [
        "fluent german", "verhandlungssicher deutsch", "fließend deutsch",
        "sehr gute deutschkenntnisse", "german required"
    ]
}

KEYWORDS_WEIGHTED = {
    "linux": 8,
    "ansible": 7,
    "python": 6,
    "bash": 5,
    "terraform": 6,
    "jenkins": 5,
    "docker": 5,
    "kubernetes": 6,
    "vmware": 5,
    "grafana": 4,
    "graylog": 4,
    "icinga": 4,
    "loki": 4,
    "prometheus": 4,
    "nginx": 4,
    "gitlab": 4,
    "github actions": 4,
    "hpc": 9,
    "cluster": 8,
    "slurm": 8,
    "infiniband": 8,
    "ipmi": 7,
    "pxe": 7,
    "gpu": 7,
    "azure": 5,
    "aws": 5,
    "debian": 5,
    "ubuntu": 5,
    "rhel": 5,
    "alma": 4,
    "networking": 4,
    "routing": 4,
    "ci/cd": 4,
}

def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()

def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_space(html.unescape(text))

def detect_language(text: str) -> str:
    t = text.lower()
    if any(h in t for h in LANGUAGE_HINTS["german"]):
        return "german"
    if any(h in t for h in LANGUAGE_HINTS["partial_german"]):
        return "partial_german"
    if any(h in t for h in LANGUAGE_HINTS["english"]):
        return "english"
    return "unknown"

def detect_international(text: str) -> int:
    t = text.lower()
    flags = ["international team", "global team", "worldwide", "distributed team", "english"]
    return 1 if any(f in t for f in flags) else 0

def parse_location(location: str) -> Tuple[str, str, str]:
    loc = (location or "").strip()
    lower = loc.lower()
    city = ""
    country = ""
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

    if "berlin" in lower:
        city, country, region_group = "Berlin", "Germany", "berlin"
    elif "germany" in lower or "deutschland" in lower:
        if not city:
            city = ""
        country = "Germany"
        if region_group == "other":
            region_group = "germany"
    elif country and region_group == "other":
        region_group = "eu"

    return city, country, region_group

def extract_keywords(text: str) -> str:
    lower = text.lower()
    found = []
    for kw in KEYWORDS_WEIGHTED:
        if kw in lower:
            found.append(kw)
    return " ".join(found)

def safe_get(obj, path, default=None):
    cur = obj
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur

def connect_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str):
    conn = connect_db(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()

def upsert_job(conn, rec: Dict):
    cols = [
        "source", "source_type", "ats", "external_id", "company", "company_size", "title",
        "location", "city", "country", "region_group", "language", "international_flag",
        "recruiter_flag", "direct_company_flag", "url", "date_posted", "description",
        "keywords_raw", "normalized_text", "created_at"
    ]
    values = [rec.get(c) for c in cols]
    placeholders = ",".join("?" for _ in cols)
    sql = f"""
    INSERT OR IGNORE INTO jobs ({",".join(cols)})
    VALUES ({placeholders})
    """
    conn.execute(sql, values)

def greenhouse_fetch_board(board_token: str) -> List[Dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("jobs", [])

def lever_fetch_company(company_token: str) -> List[Dict]:
    # Public postings endpoint for many Lever-hosted boards
    url = f"https://api.lever.co/v0/postings/{company_token}?mode=json"
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []

def harvest_greenhouse(db_path: str, boards_file: str):
    boards = [x.strip() for x in Path(boards_file).read_text(encoding="utf-8").splitlines() if x.strip()]
    conn = connect_db(db_path)
    count = 0
    for token in boards:
        try:
            jobs = greenhouse_fetch_board(token)
        except Exception as e:
            print(f"[greenhouse] failed {token}: {e}", file=sys.stderr)
            continue
        for j in jobs:
            title = safe_get(j, ["title"], "")
            company = token
            location = ", ".join([l.get("name","") for l in safe_get(j, ["location"], [])]) if isinstance(safe_get(j, ["location"]), list) else safe_get(j, ["location","name"], "")
            url = safe_get(j, ["absolute_url"], "")
            description = strip_html(safe_get(j, ["content"], ""))
            combined = f"{title} {company} {location} {description}"
            city, country, region_group = parse_location(location)
            rec = {
                "source": f"greenhouse:{token}",
                "source_type": "company",
                "ats": "greenhouse",
                "external_id": str(safe_get(j, ["id"], "")),
                "company": company,
                "company_size": None,
                "title": title,
                "location": location,
                "city": city,
                "country": country,
                "region_group": region_group,
                "language": detect_language(combined),
                "international_flag": detect_international(combined),
                "recruiter_flag": 0,
                "direct_company_flag": 1,
                "url": url,
                "date_posted": None,
                "description": description,
                "keywords_raw": extract_keywords(combined),
                "normalized_text": normalize_space(combined.lower()),
                "created_at": now_iso(),
            }
            upsert_job(conn, rec)
            count += 1
    conn.commit()
    conn.close()
    print(json.dumps({"insert_attempts": count, "ats": "greenhouse"}, indent=2))

def harvest_lever(db_path: str, companies_file: str):
    companies = [x.strip() for x in Path(companies_file).read_text(encoding="utf-8").splitlines() if x.strip()]
    conn = connect_db(db_path)
    count = 0
    for token in companies:
        try:
            jobs = lever_fetch_company(token)
        except Exception as e:
            print(f"[lever] failed {token}: {e}", file=sys.stderr)
            continue
        for j in jobs:
            title = safe_get(j, ["text"], "")
            company = token
            categories = safe_get(j, ["categories"], {}) or {}
            location = categories.get("location", "") or ""
            description = strip_html(safe_get(j, ["descriptionPlain"], "") or safe_get(j, ["description"], ""))
            commitment = categories.get("commitment", "")
            team = categories.get("team", "")
            url = safe_get(j, ["hostedUrl"], "")
            combined = f"{title} {company} {location} {commitment} {team} {description}"
            city, country, region_group = parse_location(location)
            rec = {
                "source": f"lever:{token}",
                "source_type": "company",
                "ats": "lever",
                "external_id": str(safe_get(j, ["id"], "")),
                "company": company,
                "company_size": None,
                "title": title,
                "location": location,
                "city": city,
                "country": country,
                "region_group": region_group,
                "language": detect_language(combined),
                "international_flag": detect_international(combined),
                "recruiter_flag": 0,
                "direct_company_flag": 1,
                "url": url,
                "date_posted": None,
                "description": description,
                "keywords_raw": extract_keywords(combined),
                "normalized_text": normalize_space(combined.lower()),
                "created_at": now_iso(),
            }
            upsert_job(conn, rec)
            count += 1
    conn.commit()
    conn.close()
    print(json.dumps({"insert_attempts": count, "ats": "lever"}, indent=2))

def import_csv(db_path: str, csv_path: str):
    """
    Expected CSV columns (flexible, case-insensitive where possible):
      company, title, location, url, description, language, source, source_type,
      ats, company_size, recruiter_flag, direct_company_flag, date_posted
    """
    conn = connect_db(db_path)
    count = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lower = {k.lower().strip(): (v or "").strip() for k, v in row.items()}
            company = lower.get("company", "")
            title = lower.get("title", "")
            location = lower.get("location", "")
            description = lower.get("description", "")
            language = lower.get("language", "") or detect_language(f"{title} {location} {description}")
            city, country, region_group = parse_location(location)
            combined = f"{title} {company} {location} {description}"
            rec = {
                "source": lower.get("source", "manual"),
                "source_type": lower.get("source_type", "board"),
                "ats": lower.get("ats", "manual"),
                "external_id": lower.get("external_id", ""),
                "company": company,
                "company_size": int(lower["company_size"]) if lower.get("company_size", "").isdigit() else None,
                "title": title,
                "location": location,
                "city": city,
                "country": country,
                "region_group": region_group,
                "language": language,
                "international_flag": 1 if lower.get("international_flag", "") in ("1","true","True","yes","YES") else detect_international(combined),
                "recruiter_flag": 1 if lower.get("recruiter_flag", "") in ("1","true","True","yes","YES") else 0,
                "direct_company_flag": 1 if lower.get("direct_company_flag", "") in ("1","true","True","yes","YES") else 0,
                "url": lower.get("url", ""),
                "date_posted": lower.get("date_posted", ""),
                "description": description,
                "keywords_raw": lower.get("keywords_raw", "") or extract_keywords(combined),
                "normalized_text": normalize_space(combined.lower()),
                "created_at": now_iso(),
            }
            upsert_job(conn, rec)
            count += 1
    conn.commit()
    conn.close()
    print(json.dumps({"imported_rows": count, "source": csv_path}, indent=2))

def build_where(args):
    clauses = []
    params = []

    if args.region:
        wanted = [x.strip().lower() for x in args.region.split(",") if x.strip()]
        placeholders = ",".join("?" for _ in wanted)
        clauses.append(f"lower(region_group) IN ({placeholders})")
        params.extend(wanted)

    if args.city:
        clauses.append("lower(city) = ?")
        params.append(args.city.lower())

    if args.country:
        clauses.append("lower(country) = ?")
        params.append(args.country.lower())

    if args.language:
        wanted = [x.strip().lower() for x in args.language.split(",") if x.strip()]
        placeholders = ",".join("?" for _ in wanted)
        clauses.append(f"lower(language) IN ({placeholders})")
        params.extend(wanted)

    if args.min_company_size is not None:
        clauses.append("(company_size IS NULL OR company_size >= ?)")
        params.append(args.min_company_size)

    if args.source:
        source_parts = [x.strip().lower() for x in args.source.split(",") if x.strip()]
        source_sub = []
        for s in source_parts:
            if s == "recruiter":
                source_sub.append("recruiter_flag = 1")
            elif s in ("company", "direct"):
                source_sub.append("direct_company_flag = 1")
            elif s == "institution":
                source_sub.append("source_type = 'institution'")
            elif s == "board":
                source_sub.append("source_type = 'board'")
        if source_sub:
            clauses.append("(" + " OR ".join(source_sub) + ")")

    if args.include_keywords:
        kws = [x.strip().lower() for x in args.include_keywords.split(",") if x.strip()]
        for kw in kws:
            clauses.append("normalized_text LIKE ?")
            params.append(f"%{kw}%")

    if args.exclude_keywords:
        kws = [x.strip().lower() for x in args.exclude_keywords.split(",") if x.strip()]
        for kw in kws:
            clauses.append("normalized_text NOT LIKE ?")
            params.append(f"%{kw}%")

    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params

def filter_jobs(db_path: str, args):
    conn = connect_db(db_path)
    where_sql, params = build_where(args)
    sql = f"""
    SELECT id, company, title, location, language, company_size, source_type, ats, url, keywords_raw
    FROM jobs
    {where_sql}
    ORDER BY company, title
    """
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.execute("INSERT INTO filters_run(run_at, args_json) VALUES (?,?)", (now_iso(), json.dumps(vars(args), ensure_ascii=False)))
    conn.commit()
    conn.close()
    print(json.dumps(rows[:args.limit], indent=2, ensure_ascii=False))

def score_blocks(job_text: str, blocks: List[Dict]) -> List[Dict]:
    lower = job_text.lower()
    scored = []
    for block in blocks:
        score = 0
        for kw in block.get("keywords", []):
            kw_l = kw.lower()
            if kw_l in lower:
                score += KEYWORDS_WEIGHTED.get(kw_l, 3)
        if score > 0:
            scored.append({
                "block_id": block.get("id"),
                "title": block.get("title"),
                "score": score,
                "text": block.get("text"),
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored

def map_job(db_path: str, job_id: int, blocks_file: str):
    conn = connect_db(db_path)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not row:
        raise SystemExit(f"job id {job_id} not found")

    data = json.loads(Path(blocks_file).read_text(encoding="utf-8"))
    blocks = data.get("blocks", [])
    job_text = " ".join([
        row["title"] or "",
        row["company"] or "",
        row["location"] or "",
        row["description"] or "",
        row["keywords_raw"] or "",
    ])
    scored = score_blocks(job_text, blocks)

    intro_guidance = {
        "company": row["company"],
        "title": row["title"],
        "language_hint": row["language"],
        "top_keywords": (row["keywords_raw"] or "").split(),
        "note": "Use the top 3-5 matching blocks below to assemble the body of the CV. Rewrite only the intro and summary by hand."
    }

    out = {
        "job": {
            "id": row["id"],
            "company": row["company"],
            "title": row["title"],
            "location": row["location"],
            "language": row["language"],
            "url": row["url"],
        },
        "intro_guidance": intro_guidance,
        "recommended_blocks": scored[:8],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-db")
    p_init.add_argument("db")

    p_gh = sub.add_parser("harvest-greenhouse")
    p_gh.add_argument("db")
    p_gh.add_argument("boards_file")

    p_lv = sub.add_parser("harvest-lever")
    p_lv.add_argument("db")
    p_lv.add_argument("companies_file")

    p_csv = sub.add_parser("import-csv")
    p_csv.add_argument("db")
    p_csv.add_argument("csv_path")

    p_filter = sub.add_parser("filter")
    p_filter.add_argument("db")
    p_filter.add_argument("--region", default="")
    p_filter.add_argument("--city", default="")
    p_filter.add_argument("--country", default="")
    p_filter.add_argument("--language", default="")
    p_filter.add_argument("--min-company-size", type=int, default=None)
    p_filter.add_argument("--source", default="")
    p_filter.add_argument("--include-keywords", default="")
    p_filter.add_argument("--exclude-keywords", default="")
    p_filter.add_argument("--limit", type=int, default=100)

    p_map = sub.add_parser("map-job")
    p_map.add_argument("db")
    p_map.add_argument("job_id", type=int)
    p_map.add_argument("blocks_file")

    args = p.parse_args()

    if args.cmd == "init-db":
        init_db(args.db)
    elif args.cmd == "harvest-greenhouse":
        harvest_greenhouse(args.db, args.boards_file)
    elif args.cmd == "harvest-lever":
        harvest_lever(args.db, args.companies_file)
    elif args.cmd == "import-csv":
        import_csv(args.db, args.csv_path)
    elif args.cmd == "filter":
        filter_jobs(args.db, args)
    elif args.cmd == "map-job":
        map_job(args.db, args.job_id, args.blocks_file)

if __name__ == "__main__":
    main()
