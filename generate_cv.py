#!/usr/bin/env python3
"""
Generate a tailored CV (.tex + compiled PDF) for one job, using only real
bullet-bank content -- never invented skills or experience.

Usage:
    python3 generate_cv.py <job_id>

Output: generated/<company>_<title>/cv.tex and cv.pdf

Cover-letter generation is deliberately out of scope for this pass (per
project decision: get CV generation solid first).
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import jinja2

import cv_bank

BASE = Path(__file__).parent
DB_PATH = BASE / "jobs.db"
TEMPLATE_PATH = BASE / "tex_templates" / "cv_template.tex.jinja"
PROFILE_PATH = BASE / "profile.local.json"
PROFILE_EXAMPLE_PATH = BASE / "profile.example.json"
OUTPUT_DIR = BASE / "generated"

# Per-position detail-bullet targets (min, max), keyed by the position's
# `period` string from the CSV timeline (unique per position here) --
# matches the density of Richard's own saved/reference CVs exactly,
# rather than an algorithmic relevance-rank taper. Relevance scoring
# still decides WHICH bullets fill this budget (see
# _rank_bullets_with_fallback), just not HOW MANY. "droppable": True
# marks the sole position allowed to disappear entirely (not just lose
# detail bullets) if page 2 is still too tight after every other budget
# has been trimmed to its floor.
POSITION_BULLET_TARGETS = {
    "2025–present": {"min": 4, "max": 5},   # Peer Network PSU
    "2024":         {"min": 0, "max": 1},   # Excelgens / Native Teams
    "2023–2024":    {"min": 3, "max": 5},   # Herbst Datentechnik GmbH
    "2019–2023":    {"min": 3, "max": 5},   # Operational Services GmbH / Modus
    "2018":         {"min": 0, "max": 0},   # YOC -- summary only
    "2016–2017":    {"min": 3, "max": 5},   # Mindtree onsite at Microsoft
    "2015–2016":    {"min": 3, "max": 4},   # CompuCom onsite at Amazon
    "2013–2014":    {"min": 0, "max": 1},   # Abacus Service Corporation on Intel (HPC)
    "2005–2013":    {"min": 0, "max": 1, "droppable": True},  # CompuCom onsite at Intel
}
DEFAULT_BULLET_TARGET = {"min": 1, "max": 2}  # fallback for any position not in the table above
PAGE1_POSITION_COUNT = 3  # Peer + Excelgens + Herbst, per the reference split
MAX_FIT_ITERATIONS = 6

SKILL_CATEGORIES = [
    ("Linux Administration", ["linux", "debian", "ubuntu", "rhel", "alma"]),
    ("Cloud Platforms", ["aws", "azure", "vmware", "openstack", "proxmox"]),
    ("Networking", ["networking", "routing", "nfs", "iscsi", "fiber channel", "fibre channel"]),
    ("Scripting & Automation", ["ansible", "terraform", "bash", "python", "puppet", "chef", "saltstack", "packer"]),
    ("Monitoring & Logging", ["grafana", "graylog", "icinga", "loki", "prometheus"]),
    ("DevOps & CI/CD", ["docker", "kubernetes", "jenkins", "gitlab", "github actions", "ci/cd", "nginx"]),
    ("HPC & Storage", ["hpc", "cluster", "slurm", "infiniband", "ipmi", "pxe", "gpu", "ceph", "zfs",
                        "lustre", "gpfs", "beegfs", "mpi", "openmpi", "cuda", "rdma"]),
]


# Decorative list-marker glyphs some of the source .odt files use (►, □, ∆,
# bullets, arrows, dingbats) -- pdfTeX can't render these without extra
# Unicode setup, and they're redundant with LaTeX's own \item marker anyway.
_DECORATIVE_GLYPH_RE = re.compile(
    "[←-⇿∀-⋿⌀-⏿─-◿☀-➿]+\\s*"
)


def latex_escape(text: str) -> str:
    if not text:
        return ""
    text = _DECORATIVE_GLYPH_RE.sub("", text).strip()
    replacements = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
        "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    pattern = re.compile("|".join(re.escape(k) for k in replacements))
    return pattern.sub(lambda m: replacements[m.group()], text)


def latex_escape_url(url: str) -> str:
    """Minimal escaping for \\href{} URL arguments -- unlike body text,
    a URL shouldn't get the full latex_escape() treatment (that would
    corrupt the URL itself), but '%' and '#' are dangerous at the raw
    TeX-parsing level even inside a macro argument and will otherwise
    break compilation (or silently swallow the rest of the line)."""
    if not url:
        return ""
    return url.replace("%", r"\%").replace("#", r"\#")


def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        raise RuntimeError(
            f"{PROFILE_PATH.name} not found. Copy {PROFILE_EXAMPLE_PATH.name} to "
            f"{PROFILE_PATH.name} and fill in your real (gitignored) details."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def get_job(job_id: int) -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"job {job_id} not found")
    return dict(row)


def job_text(job: dict) -> str:
    return " ".join(filter(None, [
        job.get("title"), job.get("description"), job.get("keywords_raw"),
    ]))


def pick_variant_text(bullet: dict, jtext: str) -> str:
    lower = jtext.lower()
    label_priority = ["Base"]
    if "site reliability" in lower or " sre" in lower:
        label_priority = ["Ops / SRE", "DevOps", "Linux Admin", "Base"]
    elif "devops" in lower:
        label_priority = ["DevOps", "Linux Admin", "Ops / SRE", "Base"]
    else:
        label_priority = ["Linux Admin", "DevOps", "Ops / SRE", "Base"]

    en_variants = {v["label"]: v["text"] for v in bullet["variants"] if v["lang"] == "en"}
    for label in label_priority:
        if label in en_variants:
            return en_variants[label]
    if en_variants:
        return next(iter(en_variants.values()))
    de_variants = [v["text"] for v in bullet["variants"] if v["lang"] == "de"]
    return de_variants[0] if de_variants else ""


def _rank_bullets_with_fallback(bullets: list, jtext: str) -> list:
    """Like cv_bank.score_bullets(), but never drops a bullet just
    because it scores 0 keyword overlap with this specific job -- within
    one position's own (small) bullet set, a zero-scoring bullet is still
    real content that should fill the detail-bullet budget when nothing
    more relevant is available, ranked after anything that does match."""
    lower = (jtext or "").lower()
    scored = []
    for b in bullets:
        hay = " ".join([
            b.get("category", ""), " ".join(b.get("skill_tags", [])),
            b.get("jd_keyword_notes", ""), cv_bank.bullet_text(b),
        ]).lower()
        s = sum(cv_bank.KEYWORDS_WEIGHTED.get(kw, 3) for kw in cv_bank.KEYWORDS_WEIGHTED if kw in hay and kw in lower)
        scored.append({**b, "match_score": s})
    scored.sort(key=lambda b: (
        -b["match_score"],
        -b.get("rating", {}).get("strength", 0),
        -b.get("rating", {}).get("wording", 0),
    ))
    return scored


def llm_recommend_bullets(job: dict, timeline: list):
    """Ask the LLM to pick which real bullets go into the CV for this
    specific job -- the target density Richard described (recent/relevant
    roles get more detail, minor roles get just a summary) is passed as
    guidance, not a hard rule, so the LLM can use judgment per job rather
    than a rigid per-employer count. Returns {period: {...}} or None on
    the stub backend / any failure, in which case the caller falls back
    to the algorithmic POSITION_BULLET_TARGETS system below."""
    if os.environ.get("LLM_BACKEND", "stub") == "stub":
        return None

    blocks = []
    for pos in timeline:
        lines = [f"Position: {pos['employer']} — {pos['position_title']} ({pos['period']})"]
        for b in pos["bullets"]:
            lines.append(f"  [{b['id']}] {cv_bank.bullet_text(b)}")
        blocks.append("\n".join(lines))

    prompt = f"""You are selecting which real, factual bullet points to include in a tailored CV for this job.

Job: {job.get('title')} at {job.get('company')}
Description excerpt: {(job.get('description') or '')[:1500]}

Candidate's full real career history and available bullets, most recent first. EVERY position below
MUST appear in your output -- never omit a position entirely, even if you pick zero detail bullets
for it (a bare one-line summary is fine for minor/short-tenure/old roles).

{chr(10).join('---' + chr(10) + b for b in blocks)}

General shape to aim for (guidance based on Richard's own real CVs, not a hard rule -- use judgment
for THIS specific job): the 1-3 most recent/relevant roles typically get 3-5 detail bullets each;
short-tenure or less relevant roles typically get 0-2. Page 1 should hold roughly the 3 most recent
positions; the rest go on page 2. Never invent a bullet -- only use the exact [ID]s listed above.

Respond with ONLY valid JSON, no markdown fences, exactly one entry per position above, in this shape:
{{"positions": [
  {{"period": "<period exactly as given above>", "summary_bullet_id": "<ID to use as the one-line summary>",
    "detail_bullet_ids": ["<ID>", "..."]}}
]}}"""

    try:
        raw = _call_llm(prompt, max_tokens=1500)
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)
        return {p["period"]: p for p in data.get("positions", []) if p.get("period")}
    except Exception:
        return None


def build_position_groups(job: dict, jtext: str, trim_level: int = 0, drop_droppable: bool = False,
                           llm_recommendation: dict = None):
    """Every real position from the CSV career timeline, in chronological
    order (most recent first) -- not filtered down to whichever employers
    happen to score well against this job. Each position always shows its
    one-line summary. Detail-bullet selection prefers `llm_recommendation`
    (see llm_recommend_bullets) when given; otherwise falls back to a
    fixed (min, max) target per position (POSITION_BULLET_TARGETS),
    matching the density of Richard's own saved CVs. `trim_level` and
    `drop_droppable` only apply to the fallback path (the shrink-to-fit
    retry loop degrades to it if the LLM's first pass doesn't fit)."""
    timeline = cv_bank.build_position_timeline()

    # relevance rank per position = best keyword match among its own bullets
    # (used for intro generation, and by the fallback path to pick WHICH
    # bullets fill its fixed budget)
    scored_positions = []
    for pos in timeline:
        best_score = 0
        for b in pos["bullets"]:
            s = cv_bank.score_bullets(jtext, [b])
            if s:
                best_score = max(best_score, s[0]["match_score"])
        scored_positions.append((pos, best_score))
    scored_positions.sort(key=lambda t: -t[1])
    rank_by_position = {
        (pos["employer"], pos["position_title"], pos["period"]): rank
        for rank, (pos, _score) in enumerate(scored_positions)
    }

    groups = []
    for pos in timeline:  # chronological order for the actual rendered CV
        rank = rank_by_position[(pos["employer"], pos["position_title"], pos["period"])]
        bullets_by_id = {b["id"]: b for b in pos["bullets"]}
        rec = llm_recommendation.get(pos["period"]) if llm_recommendation else None

        if rec is not None:
            summary_bullet = bullets_by_id.get(rec.get("summary_bullet_id")) or \
                bullets_by_id.get(pos["summary_bullet_id"])
            summary_text = cv_bank.bullet_text(summary_bullet) if summary_bullet else pos["summary_text"]
            detail_texts, detail_texts_raw = [], []
            for bid in rec.get("detail_bullet_ids", []):
                b = bullets_by_id.get(bid)  # never invent -- skip any ID the LLM hallucinated
                if not b or b is summary_bullet:
                    continue
                raw = pick_variant_text(b, jtext)
                text = latex_escape(raw)
                if text:
                    detail_texts.append(text)
                    detail_texts_raw.append(raw)
        else:
            target = POSITION_BULLET_TARGETS.get(pos["period"], DEFAULT_BULLET_TARGET)
            if drop_droppable and target.get("droppable"):
                continue
            budget = max(target["min"], target["max"] - trim_level)
            summary_text = pos["summary_text"]
            ranked_bullets = _rank_bullets_with_fallback(pos["bullets"], jtext)
            detail_texts, detail_texts_raw = [], []
            for b in ranked_bullets:
                if b["id"] == pos["summary_bullet_id"]:
                    continue
                if len(detail_texts) >= budget:
                    break
                raw = pick_variant_text(b, jtext)
                text = latex_escape(raw)
                if text:
                    detail_texts.append(text)
                    detail_texts_raw.append(raw)

        groups.append({
            "employer": latex_escape(pos["employer"]),
            "employer_raw": pos["employer"],
            "position_title": latex_escape(pos["position_title"]),
            "period": latex_escape(pos["period"]),
            "summary": latex_escape(summary_text),
            "summary_raw": summary_text,
            "bullets": detail_texts,
            "bullets_raw": detail_texts_raw,
            "relevance_rank": rank,
        })
    return groups


def build_skill_categories(bullets: list) -> list:
    matched_tags = set()
    for b in bullets:
        matched_tags.update(t.lower() for t in b.get("skill_tags", []))

    categories = []
    used = set()
    for label, keywords in SKILL_CATEGORIES:
        items = [kw for kw in keywords if kw in matched_tags]
        if items:
            used.update(items)
            categories.append({"label": latex_escape(label), "skills": [latex_escape(i) for i in items]})
    # only show leftover tags that are real recognized tech keywords --
    # the CSV's "Core Skill Tags" column also carries internal
    # classification words (e.g. "ownership", "proxies") never meant to
    # be printed as if they were skills
    leftover = sorted((matched_tags - used) & set(cv_bank.KEYWORDS_WEIGHTED))
    if leftover:
        categories.append({"label": "Other", "skills": [latex_escape(i) for i in leftover[:8]]})
    return categories


def _call_llm(prompt: str, max_tokens: int = 200) -> str:
    """Shared backend dispatch for generate_intro/generate_cover_letter.
    Returns the raw (un-escaped) model text, or raises on failure --
    callers fall back to a template on any exception."""
    backend = os.environ.get("LLM_BACKEND", "stub")
    if backend == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        msg = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    if backend == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        resp = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()
    if backend == "ollama":
        import requests as req
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        model = os.environ.get("OLLAMA_MODEL", "llama3.2")
        resp = req.post(f"{host}/api/generate",
                         json={"model": model, "prompt": prompt, "stream": False},
                         timeout=int(os.environ.get("OLLAMA_TIMEOUT", 180)))
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    raise ValueError(f"no real LLM for backend={backend!r}")


_DEFAULT_OPEN_QUESTION = (
    "I'd welcome the chance to talk through how that experience applies here -- "
    "what's the biggest priority for whoever takes this on in the first few months?"
)


def generate_cover_letter(job: dict, groups: list, profile: dict) -> str:
    """Fixed template (guaranteed shape/reliability) with two small LLM-
    filled gaps -- a pitch sentence and an open question -- rather than
    asking the model to compose a whole letter freeform. A smaller/local
    model (e.g. Ollama+Mistral) is much more reliable at one focused
    sentence at a time than at holding together a whole coherent letter;
    each gap also degrades independently (a bad/missing pitch sentence
    doesn't cost the question, and vice versa) instead of an all-or-
    nothing fallback to a generic template on any failure."""
    by_relevance = sorted(groups, key=lambda g: g["relevance_rank"])
    top = by_relevance[:2]
    company = job.get("company") or "your team"
    title = job.get("title") or "this role"
    name = profile.get("name", "")
    fallback_highlight = top[0]["summary_raw"] if top else ""

    pitch_sentence, open_question = fallback_highlight, _DEFAULT_OPEN_QUESTION

    if os.environ.get("LLM_BACKEND", "stub") != "stub":
        prompt = f"""Fill in exactly two blanks for a cover letter. Output ONLY these two lines, nothing else:
PITCH: <one confident, natural first-person sentence pitching the candidate for this job, using ONLY
the real facts below -- paraphrase naturally, don't just copy a fact verbatim>
QUESTION: <one open question specific to this job/company that invites a reply -- part of the pitch,
not small talk>

Real facts to draw from (factual only, never invent anything beyond this):
{chr(10).join('- ' + g['summary_raw'] for g in top)}
{chr(10).join('- ' + b for g in top for b in g['bullets_raw'][:2])}

Job: {title} at {company}
Job description excerpt: {(job.get('description') or '')[:800]}"""

        try:
            raw = _call_llm(prompt, max_tokens=200)
            pitch_match = re.search(r"PITCH:\s*(.+)", raw)
            question_match = re.search(r"QUESTION:\s*(.+)", raw)
            if pitch_match and pitch_match.group(1).strip():
                pitch_sentence = pitch_match.group(1).strip()
            if question_match and question_match.group(1).strip():
                open_question = question_match.group(1).strip()
        except Exception:
            pass  # keep the factual fallback values for whichever gap(s) didn't fill

    return (
        f"Dear Hiring Team at {company},\n\n"
        f"I'm writing about the {title} opening. {pitch_sentence}\n\n"
        f"{open_question}\n\n"
        f"Best regards,\n{name}"
    )


def generate_intro(job: dict, groups: list, profile: dict) -> str:
    """One small LLM call for a factual intro paragraph, prompted to use
    only the selected bullets/skills -- falls back to a template
    sentence on the stub backend (default, no API calls)."""
    backend = os.environ.get("LLM_BACKEND", "stub")
    by_relevance = sorted(groups, key=lambda g: g["relevance_rank"])
    top_employers_raw = [g["employer_raw"] for g in by_relevance[:3]]

    if backend == "stub":
        base = profile.get("default_title", "Senior Linux Administrator")
        employers_txt = ", ".join(top_employers_raw) if top_employers_raw else "production Linux environments"
        return latex_escape(
            f"{base} with hands-on experience across {employers_txt}, focused on reliable, "
            f"automated infrastructure relevant to this role."
        )

    prompt = f"""Write a 2-sentence factual professional summary for a CV, tailored to this job posting.
Use ONLY the facts below -- never invent skills, employers, or achievements not listed.

Job title: {job.get('title')}
Job company: {job.get('company')}

Candidate's relevant experience (factual, from their real CV bullet bank):
{chr(10).join('- ' + g['summary_raw'] for g in by_relevance[:3])}
{chr(10).join('- ' + b for g in by_relevance[:3] for b in g['bullets_raw'][:2])}

Respond with ONLY the 2-sentence summary text, no preamble, no quotes."""

    try:
        return latex_escape(_call_llm(prompt, max_tokens=200))
    except Exception:
        pass

    base = profile.get("default_title", "Senior Linux Administrator")
    employers_txt = ", ".join(top_employers_raw) if top_employers_raw else "production Linux environments"
    return latex_escape(f"{base} with hands-on experience across {employers_txt}.")


def render_tex(job: dict, profile: dict, groups: list,
               skill_categories: list, intro: str,
               page1_count: int = PAGE1_POSITION_COUNT) -> str:
    page1_groups, page2_groups = groups[:page1_count], groups[page1_count:]

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_PATH.parent)),
        block_start_string=r"\BLOCK{", block_end_string="}",
        variable_start_string=r"\VAR{", variable_end_string="}",
        comment_start_string=r"\#{", comment_end_string="}",
        trim_blocks=True, lstrip_blocks=True,
        autoescape=False,
    )
    template = env.get_template(TEMPLATE_PATH.name)

    photo_path = ""
    raw_photo = profile.get("photo_path", "")
    if raw_photo:
        p = Path(raw_photo)
        p = p if p.is_absolute() else (BASE / p)
        if p.exists():
            photo_path = str(p)

    return template.render(
        name=latex_escape(profile.get("name", "")),
        title=latex_escape(job.get("title") or profile.get("default_title", "")),
        location=latex_escape(profile.get("location", "")),
        phone=latex_escape(profile.get("phone", "")),
        email=latex_escape(profile.get("email", "")),
        nationality=latex_escape(profile.get("nationality", "")),
        links=[{"label": latex_escape(l["label"]), "url": latex_escape_url(l["url"])} for l in profile.get("links", [])],
        photo_path=photo_path,
        education=[{"school": latex_escape(e["school"]), "degree": latex_escape(e["degree"]), "year": latex_escape(e["year"])}
                   for e in profile.get("education", [])],
        certificates=[{"name": latex_escape(c["name"]), "id": latex_escape(c["id"]), "date": latex_escape(c["date"])}
                      for c in profile.get("certificates", [])],
        languages=[{"name": latex_escape(l["name"]), "level": latex_escape(l["level"])}
                   for l in profile.get("languages", [])],
        projects=[{"title": latex_escape(p["title"]), "period": latex_escape(p["period"]),
                   "location": latex_escape(p["location"]), "summary": latex_escape(p["summary"])}
                  for p in profile.get("projects", [])],
        skill_categories=skill_categories,
        page1_groups=page1_groups,
        page2_groups=page2_groups,
        intro=intro,
    )


def _pdf_page_count(pdf_path: Path) -> int:
    try:
        proc = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, timeout=20)
        m = re.search(r"^Pages:\s+(\d+)", proc.stdout, re.MULTILINE)
        return int(m.group(1)) if m else -1
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1


def compile_pdf(tex_path: Path) -> dict:
    outdir = tex_path.parent
    try:
        proc = subprocess.run(
            ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error",
             f"-output-directory={outdir}", str(tex_path)],
            cwd=str(outdir), capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        proc = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
             f"-output-directory={outdir}", str(tex_path)],
            cwd=str(outdir), capture_output=True, text=True, timeout=120,
        )
        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
             f"-output-directory={outdir}", str(tex_path)],
            cwd=str(outdir), capture_output=True, text=True, timeout=120,
        )
    pdf_path = tex_path.with_suffix(".pdf")
    ok = pdf_path.exists() and proc.returncode == 0
    log_path = tex_path.with_suffix(".log")
    log_text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
    overfull_vbox = len(re.findall(r"Overfull \\vbox", log_text))
    return {
        "ok": ok,
        "page_count": _pdf_page_count(pdf_path) if ok else -1,
        "overfull_vbox": overfull_vbox,
        "log_tail": (proc.stdout + proc.stderr)[-4000:],
    }


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text or "").strip("_")
    return text or "job"


def generate_for_job(job_id: int) -> dict:
    profile = load_profile()
    job = get_job(job_id)
    jtext = job_text(job)

    all_bullets_for_skills = cv_bank.dedupe_by_similarity_group(cv_bank.score_bullets(jtext))
    skill_categories = build_skill_categories(all_bullets_for_skills[:24])

    outdir = OUTPUT_DIR / f"{slugify(job.get('company'))}_{slugify(job.get('title'))}_{job_id}"
    outdir.mkdir(parents=True, exist_ok=True)
    tex_path = outdir / "cv.tex"

    timeline = cv_bank.build_position_timeline()
    llm_recommendation = llm_recommend_bullets(job, timeline)  # None on stub backend / failure

    trim_level = 0
    drop_droppable = False
    page1_count = PAGE1_POSITION_COUNT
    attempts = []
    intro = None

    for attempt in range(1, MAX_FIT_ITERATIONS + 1):
        # the LLM's picks get exactly one shot (attempt 1); if it doesn't
        # fit, retries degrade to the algorithmic fixed-target system
        # rather than trying to renegotiate the LLM's selection
        use_llm = llm_recommendation if attempt == 1 else None
        groups = build_position_groups(job, jtext, trim_level=trim_level, drop_droppable=drop_droppable,
                                        llm_recommendation=use_llm)
        if intro is None:  # only generate once (short summary text, unaffected by bullet trimming) -- avoids repeat LLM calls across retries
            intro = generate_intro(job, groups, profile)
        tex_content = render_tex(job, profile, groups, skill_categories, intro, page1_count=page1_count)
        tex_path.write_text(tex_content, encoding="utf-8")

        result = compile_pdf(tex_path)
        attempts.append({"attempt": attempt, "used_llm_recommendation": use_llm is not None,
                          "trim_level": trim_level, "drop_droppable": drop_droppable,
                          "page1_count": page1_count,
                          "page_count": result["page_count"], "overfull_vbox": result["overfull_vbox"]})

        if not result["ok"]:
            break  # a hard LaTeX error isn't something shrinking content fixes
        if result["page_count"] == 2 and result["overfull_vbox"] == 0:
            break

        # shrink and retry: trim every position's detail-bullet budget
        # toward its own floor first (cheapest content to lose), then drop
        # the sole "droppable" position entirely, then rebalance the
        # page1/page2 split as a last resort
        if trim_level < 5:
            trim_level += 1
        elif not drop_droppable:
            drop_droppable = True
        elif page1_count > 1:
            page1_count -= 1

    cover_letter_path = outdir / "cover_letter.txt"
    cover_letter_path.write_text(generate_cover_letter(job, groups, profile), encoding="utf-8")

    return {
        "ok": result["ok"],
        "fit_ok": result["ok"] and result["page_count"] == 2 and result["overfull_vbox"] == 0,
        "page_count": result["page_count"],
        "overfull_vbox": result["overfull_vbox"],
        "attempts": attempts,
        "tex_path": str(tex_path),
        "pdf_path": str(tex_path.with_suffix(".pdf")) if result["ok"] else None,
        "cover_letter_path": str(cover_letter_path),
        "log_tail": result["log_tail"],
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 generate_cv.py <job_id>")
        sys.exit(1)
    result = generate_for_job(int(sys.argv[1]))
    if not result["ok"]:
        print(f"FAILED to compile. Log tail:\n{result['log_tail']}")
        sys.exit(1)
    status = "fits cleanly" if result["fit_ok"] else \
        f"page_count={result['page_count']} overfull_vbox={result['overfull_vbox']} (best effort after {len(result['attempts'])} attempts)"
    print(f"OK ({status}): {result['pdf_path']}")
