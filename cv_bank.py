#!/usr/bin/env python3
"""
Unified CV bullet store.

Sources (all factual, never invented):
  - richard_cv_master_bullet_bank_g_update.csv   structured, primary
  - LateX_Docs/*.tex                              secondary, free text
  - CVs_Job_Dos/*.odt                             secondary, free text

Every bullet carries: source tag, similarity group, a rating (strength /
wording / similarity), two ages (cv_timestamp_age from the source file's
own date, position_age from the career period the bullet describes),
placement flexibility, and a full parallel schema for German text.

Results are cached to bullet_store_cache.json (gitignored) next to this
file; call build_bullet_store(force_rebuild=True) after editing sources.
"""

import csv
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree

BASE = Path(__file__).parent
LOCAL_CONFIG_PATH = BASE / "config.local.json"


def _load_path_overrides() -> dict:
    """User-configurable source paths (settings tab), so this isn't
    hardcoded to one machine. Priority: env vars > config.local.json >
    the LateX_Docs/CVs_Job_Dos symlinks + CSV at repo root."""
    overrides = {}
    if LOCAL_CONFIG_PATH.exists():
        try:
            loaded = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                overrides = loaded
        except (OSError, json.JSONDecodeError):
            pass
    return overrides


def _override_str(overrides: dict, key: str):
    val = overrides.get(key)
    return val if isinstance(val, str) and val else None


_overrides = _load_path_overrides()
CSV_PATH = Path(os.environ.get("CV_BANK_CSV_PATH") or _override_str(_overrides, "csv_path")
                 or (BASE / "richard_cv_master_bullet_bank_g_update.csv"))
TEX_DIR = Path(os.environ.get("CV_BANK_TEX_DIR") or _override_str(_overrides, "tex_dir")
                or (BASE / "LateX_Docs"))
ODT_DIR = Path(os.environ.get("CV_BANK_ODT_DIR") or _override_str(_overrides, "odt_dir")
                or (BASE / "CVs_Job_Dos"))
CACHE_PATH = BASE / "bullet_store_cache.json"

SIMILARITY_THRESHOLD = 0.6
CURRENT_YEAR = datetime.now().year

KEYWORDS_WEIGHTED = {
    "linux": 8, "ansible": 7, "python": 6, "bash": 5, "terraform": 6,
    "jenkins": 5, "docker": 5, "kubernetes": 6, "vmware": 5, "grafana": 4,
    "graylog": 4, "icinga": 4, "loki": 4, "prometheus": 4, "nginx": 4,
    "gitlab": 4, "github actions": 4, "hpc": 9, "cluster": 8, "slurm": 8,
    "infiniband": 8, "ipmi": 7, "pxe": 7, "gpu": 7, "azure": 5, "aws": 5,
    "debian": 5, "ubuntu": 5, "rhel": 5, "alma": 4, "networking": 4,
    "routing": 4, "ci/cd": 4, "proxmox": 6, "openstack": 5, "ceph": 5,
    "zfs": 4, "lustre": 7, "gpfs": 6, "beegfs": 6, "mpi": 7, "openmpi": 7,
    "cuda": 6, "rdma": 7, "puppet": 4, "chef": 4, "saltstack": 4,
    "packer": 4, "vault": 4, "consul": 4, "nfs": 4, "iscsi": 4,
    "fiber channel": 5, "fibre channel": 5,
}

VARIANT_LABELS = ["Linux Admin", "DevOps", "Ops / SRE", "STAR"]


# ── shared helpers ───────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _file_age_days(path: Path) -> Optional[float]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return (datetime.now(timezone.utc).timestamp() - mtime) / 86400


def _position_age_years(period: str) -> Optional[float]:
    """Age (years) since the described role ended; 'present' -> 0."""
    if not period:
        return None
    if "present" in period.lower():
        return 0.0
    years = re.findall(r"\d{4}", period)
    if not years:
        return None
    end_year = int(years[-1])
    return max(CURRENT_YEAR - end_year, 0)


def _wording_score(text: str, verb_risk: str) -> float:
    """Heuristic wording quality: 0-5. Penalize low-risk-verb flags and
    very short/very long bullets; reward a reasonable action-verb length."""
    if not text:
        return 0.0
    score = 3.0
    risk = (verb_risk or "").strip().lower()
    if risk == "low":
        score += 1.5
    elif risk == "high":
        score -= 1.5
    words = len(text.split())
    if 10 <= words <= 30:
        score += 0.5
    elif words < 6 or words > 45:
        score -= 1.0
    return max(0.0, min(5.0, score))


# ── CSV source (primary) ────────────────────────────────────────────────

def load_csv_bullets(csv_path: Path = CSV_PATH) -> List[dict]:
    if not csv_path.exists():
        return []

    cv_age_days = _file_age_days(csv_path)

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if len(rows) < 3:
        return []

    header = rows[1]

    def col(row, name_substr, occurrence=0):
        matches = [i for i, h in enumerate(header) if name_substr.lower() in h.lower()]
        if occurrence >= len(matches):
            return ""
        return row[matches[occurrence]] if matches[occurrence] < len(row) else ""

    bullets = []
    for row in rows[2:]:
        if not row or not any(row):
            continue
        rid = col(row, "ID")
        if not rid:
            continue

        base = _normalize(col(row, "Base Bullet"))
        en_variants = []
        for i, label in enumerate(VARIANT_LABELS):
            text = _normalize(col(row, f"Variation {i+1}"))
            if text:
                en_variants.append({"label": label, "text": text, "lang": "en"})
        if base:
            en_variants.insert(0, {"label": "Base", "text": base, "lang": "en"})

        de_cols = [i for i, h in enumerate(header) if "german variant" in h.lower()]
        de_variants = []
        for i, ci in enumerate(de_cols):
            text = _normalize(row[ci]) if ci < len(row) else ""
            label = VARIANT_LABELS[i] if i < len(VARIANT_LABELS) else f"Variant {i+1}"
            if text:
                de_variants.append({"label": label, "text": text, "lang": "de"})

        family = [s.strip() for s in col(row, "Source CV Family").split(",") if s.strip()]
        skills = [s.strip() for s in col(row, "Core Skill Tags").split(",") if s.strip()]
        period = _normalize(col(row, "Period"))
        try:
            importance = int(col(row, "Imp."))
        except ValueError:
            importance = 3

        bullets.append({
            "id": rid,
            "employer": _normalize(col(row, "Employer")),
            "position_title": _normalize(col(row, "Job Title")),
            "period": period,
            "category": _normalize(col(row, "Category")),
            "importance": importance,
            "anchor": col(row, "Anchor").strip() == "★",
            "skill_tags": skills,
            "source_cv_family": family,
            "impact_outcome": _normalize(col(row, "Impact / Outcome")),
            "verb_risk": _normalize(col(row, "Verb Risk")),
            "jd_keyword_notes": _normalize(col(row, "JD Keyword Notes")),
            "variants": en_variants + de_variants,
            "source_tag": f"csv:{rid}",
            "cv_timestamp_age_days": cv_age_days,
            "position_age_years": _position_age_years(period),
        })
    return bullets


# ── .tex source (secondary) ─────────────────────────────────────────────

_TEX_ITEM_RE = re.compile(r"\\item\s*(?:\[[^\]]*\])?\s*(.+)")
_TEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?(\{[^{}]*\})?")
_TEX_COMMENT_RE = re.compile(r"(?<!\\)%.*")


def _strip_tex_markup(text: str) -> str:
    text = _TEX_COMMENT_RE.sub("", text)
    # pull the argument text out of common wrapping commands (\textbf{x} -> x)
    for _ in range(3):
        text = re.sub(r"\\(?:textbf|textit|emph|section|subsection)\{([^{}]*)\}", r"\1", text)
    text = _TEX_CMD_RE.sub(" ", text)
    text = text.replace("{", "").replace("}", "")
    return _normalize(text)


def _extract_skill_tags(text: str) -> List[str]:
    """Real, recognized technology keywords found in free text -- used for
    tex/odt bullets, which (unlike the CSV) have no authored skill-tag
    column of their own."""
    lower = text.lower()
    return [kw for kw in KEYWORDS_WEIGHTED if kw in lower]


def load_tex_bullets(tex_dir: Path = TEX_DIR) -> List[dict]:
    if not tex_dir.exists():
        return []
    bullets = []
    for path in sorted(tex_dir.glob("*.tex")):
        age_days = _file_age_days(path)
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lang = "de" if "-de" in path.stem.lower() or "_de" in path.stem.lower() else "en"
        for i, m in enumerate(_TEX_ITEM_RE.finditer(raw)):
            text = _strip_tex_markup(m.group(1))
            if len(text.split()) < 4:
                continue
            bullets.append({
                "id": f"tex:{path.stem}:{i}",
                "employer": "",
                "position_title": "",
                "period": "",
                "category": "",
                "importance": 3,
                "anchor": False,
                "skill_tags": _extract_skill_tags(text),
                "source_cv_family": [path.stem],
                "impact_outcome": "",
                "verb_risk": "",
                "jd_keyword_notes": "",
                "variants": [{"label": "Source", "text": text, "lang": lang}],
                "source_tag": f"tex:{path.name}",
                "cv_timestamp_age_days": age_days,
                "position_age_years": None,
            })
    return bullets


# ── .odt source (secondary) ─────────────────────────────────────────────

_ODT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


def _extract_odt_paragraphs(path: Path) -> List[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            xml_bytes = zf.read("content.xml")
    except (OSError, KeyError, zipfile.BadZipFile):
        return []
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError:
        return []
    paragraphs = []
    for p in root.iter(f"{_ODT_NS}p"):
        # join with a space, not "" -- ODT splits text across multiple
        # runs/line-breaks within one <p>, and "".join() mashes words
        # from different lines together (e.g. "AlmaLinuxCloud-Plattformen")
        text = _normalize(" ".join(p.itertext()))
        if text:
            paragraphs.append(text)
    return paragraphs


def _looks_like_label_list(text: str) -> bool:
    """Skills-dump paragraphs ('Kenntnisse: X, Y, ZCloud: A, B...') read as
    dense comma lists rather than sentences -- filter them out so they
    don't get selected as if they were achievement bullets."""
    return text.count(",") >= 5


def load_odt_bullets(odt_dir: Path = ODT_DIR) -> List[dict]:
    if not odt_dir.exists():
        return []
    bullets = []
    for path in sorted(odt_dir.glob("*.odt")):
        age_days = _file_age_days(path)
        lang = "de" if re.search(r"[_-]de\b", path.stem.lower()) else "en"
        for i, text in enumerate(_extract_odt_paragraphs(path)):
            words = text.split()
            if not (6 <= len(words) <= 45):
                continue
            if not any(kw in text.lower() for kw in KEYWORDS_WEIGHTED):
                continue
            if _looks_like_label_list(text):
                continue
            bullets.append({
                "id": f"odt:{path.stem}:{i}",
                "employer": "",
                "position_title": "",
                "period": "",
                "category": "",
                "importance": 3,
                "anchor": False,
                "skill_tags": _extract_skill_tags(text),
                "source_cv_family": [path.stem],
                "impact_outcome": "",
                "verb_risk": "",
                "jd_keyword_notes": "",
                "variants": [{"label": "Source", "text": text, "lang": lang}],
                "source_tag": f"odt:{path.name}",
                "cv_timestamp_age_days": age_days,
                "position_age_years": None,
            })
    return bullets


# ── derived fields: similarity, rating, placement flexibility ──────────

def bullet_text(b: dict, lang: str = "en") -> str:
    for v in b["variants"]:
        if v["lang"] == lang:
            return v["text"]
    return b["variants"][0]["text"] if b["variants"] else ""


def _dedupe_exact(bullets: List[dict]) -> List[dict]:
    """Collapse exact-text duplicates (common: the same bullet reused
    verbatim across many of the ~300 tailored CVs) before any pairwise
    comparison, merging their source_cv_family lists into the survivor."""
    by_text: Dict[str, dict] = {}
    ordered: List[dict] = []
    for b in bullets:
        key = bullet_text(b).lower()
        if key and key in by_text:
            survivor = by_text[key]
            for fam in b.get("source_cv_family", []):
                if fam not in survivor["source_cv_family"]:
                    survivor["source_cv_family"].append(fam)
            continue
        if key:
            by_text[key] = b
        ordered.append(b)
    return ordered


def _bucket_keys(text: str) -> List[str]:
    """Coarse keys used to only compare bullets that plausibly overlap,
    instead of every bullet against every other bullet."""
    lower = text.lower()
    keys = [kw for kw in KEYWORDS_WEIGHTED if kw in lower]
    if not keys:
        words = [w for w in re.findall(r"[a-zA-Z]{4,}", lower)][:3]
        keys = words
    return keys or ["_none_"]


def _assign_similarity_groups(bullets: List[dict]) -> None:
    """Union-find clustering on normalized-text similarity ratio, limited
    to candidate pairs sharing a keyword/word bucket so this stays
    roughly linear instead of comparing every bullet to every other."""
    n = len(bullets)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    texts = [bullet_text(b).lower() for b in bullets]
    best_sim = [0.0] * n

    buckets: Dict[str, List[int]] = {}
    for i, text in enumerate(texts):
        for key in _bucket_keys(text):
            buckets.setdefault(key, []).append(i)

    MAX_BUCKET = 120  # cap pairwise work inside any one bucket (e.g. "linux")

    seen_pairs = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        if len(members) > MAX_BUCKET:
            members = members[:MAX_BUCKET]
        for a in range(len(members)):
            i = members[a]
            for bidx in range(a + 1, len(members)):
                j = members[bidx]
                pair = (i, j) if i < j else (j, i)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                ratio = SequenceMatcher(None, texts[i], texts[j]).quick_ratio()
                if ratio > best_sim[i]:
                    best_sim[i] = ratio
                if ratio > best_sim[j]:
                    best_sim[j] = ratio
                if ratio >= SIMILARITY_THRESHOLD:
                    union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    for gid, members in groups.items():
        label = f"grp_{gid}"
        for idx in members:
            bullets[idx]["similarity_group"] = label
            bullets[idx]["similarity_group_size"] = len(members)
            bullets[idx]["similarity_score"] = round(best_sim[idx], 3)


def _assign_rating_and_flexibility(bullets: List[dict]) -> None:
    for b in bullets:
        strength = float(b.get("importance") or 3)
        text = bullet_text(b)
        wording = _wording_score(text, b.get("verb_risk", ""))
        similarity = b.get("similarity_score", 0.0)
        b["rating"] = {
            "strength": round(strength, 2),
            "wording": round(wording, 2),
            "similarity": similarity,
        }
        family = b.get("source_cv_family") or []
        b["placement_flexible"] = bool(b.get("anchor")) is False and (
            len(family) > 1 or b["source_tag"].startswith(("tex:", "odt:"))
        )


# ── build / cache / sort / score ────────────────────────────────────────

def build_bullet_store(force_rebuild: bool = False,
                        csv_path: Path = CSV_PATH,
                        tex_dir: Path = TEX_DIR,
                        odt_dir: Path = ODT_DIR,
                        cache_path: Path = CACHE_PATH) -> List[dict]:
    if not force_rebuild and cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    bullets = load_csv_bullets(csv_path) + load_tex_bullets(tex_dir) + load_odt_bullets(odt_dir)
    bullets = _dedupe_exact(bullets)
    _assign_similarity_groups(bullets)
    _assign_rating_and_flexibility(bullets)

    try:
        cache_path.write_text(json.dumps(bullets, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return bullets


def sort_bullets(bullets: List[dict], prefer_lang: str = "en") -> List[dict]:
    """Sort by strength desc, wording desc, low similarity-redundancy
    (dedupe within a cluster keeping the strongest), then freshest
    position_age, then freshest cv_timestamp_age."""
    def key(b):
        r = b.get("rating", {})
        return (
            -r.get("strength", 0),
            -r.get("wording", 0),
            r.get("similarity", 0),
            b.get("position_age_years") if b.get("position_age_years") is not None else 999,
            b.get("cv_timestamp_age_days") if b.get("cv_timestamp_age_days") is not None else 99999,
        )
    return sorted(bullets, key=key)


def dedupe_by_similarity_group(bullets: List[dict]) -> List[dict]:
    """Given a sorted list, keep only the strongest bullet per similarity
    group so near-duplicate phrasing doesn't repeat in one CV."""
    seen_groups = set()
    kept = []
    for b in bullets:
        gid = b.get("similarity_group")
        if gid and gid in seen_groups:
            continue
        if gid:
            seen_groups.add(gid)
        kept.append(b)
    return kept


def _recent_file_stems(directories, max_files: int = 50, max_days: int = 90) -> set:
    files = []
    now = datetime.now(timezone.utc).timestamp()
    for d in directories:
        if not d.exists():
            continue
        for p in list(d.glob("*.tex")) + list(d.glob("*.odt")):
            try:
                files.append((p.stat().st_mtime, p.stem))
            except OSError:
                continue
    files.sort(key=lambda t: -t[0])
    within_days = {stem for mtime, stem in files if (now - mtime) / 86400 <= max_days}
    top_n = {stem for _, stem in files[:max_files]}
    # "Long_Complete" CVs are comprehensive/detailed versions kept around
    # as reference material -- valuable regardless of when they were last
    # touched, so include them even if they fall outside the recency window
    long_form = {stem for _, stem in files if "long" in stem.lower()}
    return within_days | top_n | long_form


def recent_bullets(bullets: Optional[List[dict]] = None,
                    max_files: int = 50, max_days: int = 90) -> List[dict]:
    """CSV bullets (evergreen, curated -- not tied to one dated document)
    plus tex/odt bullets sourced from a recent CV document: among the
    most-recently-modified max_files documents, within max_days, or a
    "Long_Complete" comprehensive CV (see _recent_file_stems) -- across
    both English and German files (recency only -- language is never a
    filter here). Job *rating* should be grounded in Richard's current
    self-presentation, not phrasing from years-old CVs; terms/jargon
    drift, which is exactly why rating goes through an LLM for fuzzy
    matching rather than exact keyword overlap."""
    if bullets is None:
        bullets = build_bullet_store()
    recent_stems = _recent_file_stems([TEX_DIR, ODT_DIR], max_files, max_days)
    out = []
    for b in bullets:
        if b["source_tag"].startswith("csv:"):
            out.append(b)
            continue
        stem = b["source_cv_family"][0] if b.get("source_cv_family") else None
        if stem in recent_stems:
            out.append(b)
    return out


def score_bullets(job_text: str, bullets: Optional[List[dict]] = None) -> List[dict]:
    """Rank bullets against a job description, keyword-weighted like
    FindJobs.py's score_blocks(), then broken by the sort criteria."""
    if bullets is None:
        bullets = build_bullet_store()
    lower = (job_text or "").lower()

    scored = []
    for b in bullets:
        hay = " ".join([
            b.get("category", ""), " ".join(b.get("skill_tags", [])),
            b.get("jd_keyword_notes", ""), bullet_text(b),
        ]).lower()
        s = sum(KEYWORDS_WEIGHTED.get(kw, 3) for kw in KEYWORDS_WEIGHTED if kw in hay and kw in lower)
        if s > 0:
            # CSV is the curated, non-duplicative primary source (vs. the
            # much larger pool of near-duplicate bullets pulled from ~300
            # archived CVs) -- boost it so it isn't crowded out on raw
            # keyword-count alone when it's actually relevant.
            if b["source_tag"].startswith("csv:"):
                s += 6
        if s > 0 or b.get("anchor"):
            scored.append({**b, "match_score": s})

    scored.sort(key=lambda b: (
        -b["match_score"],
        -b.get("rating", {}).get("strength", 0),
        -b.get("rating", {}).get("wording", 0),
    ))
    return scored


def build_position_timeline(bullets: Optional[List[dict]] = None) -> List[dict]:
    """Canonical, deduplicated career timeline derived from the CSV bullets
    (the structured, factual source) -- every real position, chronological,
    independent of any single job's relevance ranking. A CV generator
    should show every position here (even minor ones get their one-line
    summary), not just whichever employers happen to score well against a
    given posting. Each entry carries a summary line (its anchor bullet,
    or highest-importance bullet as a fallback) plus all of that
    position's CSV bullets, for the generator to rank as detail points."""
    if bullets is None:
        bullets = build_bullet_store()
    csv_bullets = [b for b in bullets if b["source_tag"].startswith("csv:")]

    groups: Dict[tuple, dict] = {}
    order = []
    for b in csv_bullets:
        key = (b["employer"], b["position_title"], b["period"])
        if key not in groups:
            groups[key] = {
                "employer": b["employer"],
                "position_title": b["position_title"],
                "period": b["period"],
                "position_age_years": b.get("position_age_years"),
                "bullets": [],
            }
            order.append(key)
        groups[key]["bullets"].append(b)

    timeline = []
    for key in order:
        g = groups[key]
        anchor_bullets = [b for b in g["bullets"] if b.get("anchor")]
        summary_source = anchor_bullets[0] if anchor_bullets else max(
            g["bullets"], key=lambda b: b.get("importance", 0)
        )
        g["summary_text"] = bullet_text(summary_source)
        g["summary_bullet_id"] = summary_source["id"]
        timeline.append(g)

    timeline.sort(key=lambda g: g["position_age_years"] if g["position_age_years"] is not None else 999)
    return timeline


if __name__ == "__main__":
    store = build_bullet_store(force_rebuild=True)
    print(f"Loaded {len(store)} bullets "
          f"(csv={sum(1 for b in store if b['source_tag'].startswith('csv:'))}, "
          f"tex={sum(1 for b in store if b['source_tag'].startswith('tex:'))}, "
          f"odt={sum(1 for b in store if b['source_tag'].startswith('odt:'))})")

    sample_job = ("Senior Linux Systems Administrator - HPC cluster, Slurm, "
                   "Proxmox, OpenStack, Ansible automation, Berlin, English speaking team")
    ranked = score_bullets(sample_job, store)
    print(f"\nTop matches for sample job ({len(ranked)} scored):")
    for b in ranked[:8]:
        print(f"  [{b['match_score']:3d}] {b['id']:20s} strength={b['rating']['strength']} "
              f"wording={b['rating']['wording']:.1f} sim_grp={b.get('similarity_group')} "
              f"flex={b['placement_flexible']}  {bullet_text(b)[:70]}")
