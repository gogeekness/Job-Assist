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

# Detail-bullet budget per position, indexed by relevance rank (0 = best
# match for this job). Every position still gets its one-line summary
# regardless of rank -- only itemized detail bullets get trimmed for
# less-relevant (or, on overflow, any) positions. Positions past the end
# of this list get 0 detail bullets (summary only).
DETAIL_BUDGET_BY_RANK = [3, 3, 2, 2, 1, 1, 0, 0, 0, 0]
PAGE1_POSITION_COUNT = 4  # matches the reference design's page1/page2 split
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


def build_position_groups(job: dict, jtext: str, detail_budget_by_rank=DETAIL_BUDGET_BY_RANK):
    """Every real position from the CSV career timeline, in chronological
    order (most recent first) -- not filtered down to whichever employers
    happen to score well against this job. Each position always shows its
    one-line summary; how many additional itemized detail bullets it gets
    depends on its relevance rank for *this* job, so a minor/old position
    still appears (summary only) instead of disappearing entirely."""
    timeline = cv_bank.build_position_timeline()

    # relevance rank per position = best keyword match among its own bullets
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
        budget = detail_budget_by_rank[rank] if rank < len(detail_budget_by_rank) else 0

        ranked_bullets = cv_bank.score_bullets(jtext, pos["bullets"])
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
            "summary": latex_escape(pos["summary_text"]),
            "summary_raw": pos["summary_text"],
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
            categories.append({"label": label, "skills": [latex_escape(i) for i in items]})
    leftover = sorted(matched_tags - used)
    if leftover:
        categories.append({"label": "Other", "skills": [latex_escape(i) for i in leftover[:8]]})
    return categories


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
        if backend == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            msg = client.messages.create(
                model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return latex_escape(msg.content[0].text.strip())
        elif backend == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            resp = client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return latex_escape(resp.choices[0].message.content.strip())
        elif backend == "ollama":
            import requests as req
            host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
            model = os.environ.get("OLLAMA_MODEL", "llama3.2")
            resp = req.post(f"{host}/api/generate",
                             json={"model": model, "prompt": prompt, "stream": False},
                             timeout=int(os.environ.get("OLLAMA_TIMEOUT", 180)))
            resp.raise_for_status()
            return latex_escape(resp.json().get("response", "").strip())
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

    budget = list(DETAIL_BUDGET_BY_RANK)
    page1_count = PAGE1_POSITION_COUNT
    attempts = []
    intro = None

    for attempt in range(1, MAX_FIT_ITERATIONS + 1):
        groups = build_position_groups(job, jtext, detail_budget_by_rank=budget)
        if intro is None:  # only generate once (short summary text, unaffected by bullet trimming) -- avoids repeat LLM calls across retries
            intro = generate_intro(job, groups, profile)
        tex_content = render_tex(job, profile, groups, skill_categories, intro, page1_count=page1_count)
        tex_path.write_text(tex_content, encoding="utf-8")

        result = compile_pdf(tex_path)
        attempts.append({"attempt": attempt, "budget": list(budget), "page1_count": page1_count,
                          "page_count": result["page_count"], "overfull_vbox": result["overfull_vbox"]})

        if not result["ok"]:
            break  # a hard LaTeX error isn't something shrinking content fixes
        if result["page_count"] == 2 and result["overfull_vbox"] == 0:
            break

        # shrink and retry: trim detail bullets first (cheapest content to
        # lose), then rebalance the page1/page2 split if still overflowing
        if any(budget):
            budget = [max(0, n - 1) for n in budget]
        elif page1_count > 1:
            page1_count -= 1

    return {
        "ok": result["ok"],
        "fit_ok": result["ok"] and result["page_count"] == 2 and result["overfull_vbox"] == 0,
        "page_count": result["page_count"],
        "overfull_vbox": result["overfull_vbox"],
        "attempts": attempts,
        "tex_path": str(tex_path),
        "pdf_path": str(tex_path.with_suffix(".pdf")) if result["ok"] else None,
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
