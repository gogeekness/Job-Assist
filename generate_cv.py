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
import profile as profile_mod
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from string import Template

import jinja2

import cv_bank
import db

BASE = Path(__file__).parent


def _load_prompt(name: str) -> Template:
    return Template((profile_mod.prompts_dir() / f"{name}.txt").read_text(encoding="utf-8"))


def _active_lang() -> str:
    """Which fixed-text locale (SECTION_LABELS/LANGUAGE_NAMES/cover-letter
    wrapper) to use -- a property of the active profile, not a per-call
    choice. Defaults to English for a profile that hasn't set it, or in
    legacy (no active profile) mode."""
    return profile_mod.load_config().get("lang", "en")

def position_key(employer: str, position_title: str) -> str:
    """Stable identifier for a position, independent of language or date
    corrections -- Employer/Job Title text is identical across a profile's
    EN/DE bullet banks (unlike the Period string, which isn't: German
    month abbreviations differ for May/Oct/Dec). Used to key
    position_bullet_targets/pinned_bullets in domain.json."""
    return f"{employer}|||{position_title}"


def position_bullet_targets() -> dict:
    """The active profile's own per-position detail-bullet-count targets
    (domain.json's "position_bullet_targets", keyed by position_key) --
    matches the density of that profile owner's real saved/reference CVs,
    rather than an algorithmic relevance-rank taper. Relevance scoring
    still decides WHICH bullets fill this budget (see
    _rank_bullets_with_fallback), just not HOW MANY. A profile without
    any configured just gets DEFAULT_BULLET_TARGET everywhere -- a
    reasonable, field-agnostic starting point, not Richard's own tuning."""
    return profile_mod.load_domain_config().get("position_bullet_targets", {})


def priority_tiers() -> dict:
    """The active profile's own {tier_name: guidance_sentence} map
    (domain.json's "priority_tiers") -- lets the profile owner state their
    own calibration of which positions matter more than a pure
    recency/keyword-match score would suggest (e.g. "this older role is a
    reliably strong fit, don't bury it just because it's not the most
    recent"), and have that reach the LLM's placement prompt directly
    instead of being re-derived from scratch per job. A position's own
    tier is looked up via its position_bullet_targets() entry's
    "priority_tier" key; see llm_recommend_bullets."""
    return profile_mod.load_domain_config().get("priority_tiers", {})


GENERATION_BALANCE_DEFAULT = 5  # 0-10 dial, same scale as job llm_score -- see generation_mode()


def generation_mode() -> str:
    """Where the active profile's own 0-10 "rule-based <-> LLM-led" dial
    (config.json's "generation_balance") currently sits, collapsed to one
    of three real behaviors -- there are only three distinct code paths
    for this, not ten, so values within a band behave identically:
      "rule"   (0-2): llm_recommend_bullets() skips the LLM call entirely
                -- pure position_bullet_targets/earlier_threshold/keyword-
                score placement, deterministic and model-independent.
      "hybrid" (3-7, default): today's behavior -- the LLM drives
                placement/bullet selection, and build_position_groups's
                safety nets (priority-tier override, empty-picks fallback,
                hard max cap) stay on.
      "llm"    (8-10): LLM placement is trusted fully -- the priority-tier
                safety-net override is disabled, for a profile owner who
                trusts their backend and wants more per-job variety even
                against strong keyword evidence.
    Deliberately scoped to placement/bullet-selection only (the one part
    of generation with a real rule-based equivalent) -- generate_intro/
    generate_cover_letter have no rule-based alternative to dial toward,
    so they're unaffected and keep following llm_backend/"stub" as before."""
    try:
        val = int(profile_mod.load_config().get("generation_balance", GENERATION_BALANCE_DEFAULT))
    except (TypeError, ValueError):
        val = GENERATION_BALANCE_DEFAULT
    if val <= 2:
        return "rule"
    if val >= 8:
        return "llm"
    return "hybrid"


DEFAULT_BULLET_TARGET = {"min": 1, "max": 2}  # fallback for any position with no configured target
# Single-column style ("Earlier" list vs a full entry): a position's own
# best keyword match_score against THIS job's text decides its placement,
# not a fixed per-position property -- e.g. YOC is normally too thin to
# earn a full entry, but should get one when a job actually calls for
# Ansible; Excelgens/Intel swing the same way depending on the job. See
# render_tex_single's full_groups/earlier_groups split in generate_for_job.
# Every CSV bullet with ANY keyword overlap already gets a flat +6 boost in
# cv_bank.score_bullets, so even one generic, near-universal keyword (e.g.
# "linux", which appears in almost every position's bullets) alone clears a
# low bar -- 16 requires either one genuinely strong/specific keyword or
# more than one real match, not just incidental overlap.
EARLIER_SCORE_THRESHOLD = 16
EARLIER_PROTECTED_RECENT_COUNT = 1  # the N most recent positions are always a full entry, regardless of score -- just the current role; short-tenure recent roles (e.g. Excelgens) still go through normal scoring
WEAK_DROP_CAP = 6  # single-bullet drops tried before falling back to blunter levers
MAX_FIT_ITERATIONS = 13  # WEAK_DROP_CAP(6) + trim_level(0-5, 6) + drop_droppable(1) + headroom

# Section headers, per active profile's language (_active_lang) --
# everything else on the page (bullet text, intro, cover letter) already
# comes from that profile's own bullet bank and language-aware prompts.
SECTION_LABELS = {
    "en": {"details": "Details:", "nationality": "Nationality:", "links": "Links:",
           "skills": "Skills:", "education": "Education:", "certificates": "Certificates:",
           "languages": "Languages:", "profile": "Profile", "experience": "Experience",
           "earlier": "Earlier", "current_project": "Current Project"},
    "de": {"details": "Kontakt:", "nationality": "Staatsangehörigkeit:", "links": "Links:",
           "skills": "Kenntnisse:", "education": "Ausbildung:", "certificates": "Zertifikate:",
           "languages": "Sprachen:", "profile": "Profil", "experience": "Erfahrung",
           "earlier": "Frühere Positionen", "current_project": "Aktuelles Projekt"},
}

# A profile can pin a specific bullet into the CV whenever the job
# posting mentions certain keywords, regardless of normal scoring -- for
# a standout achievement that's easy to under-rank by keyword overlap
# alone (Richard's own domain.json uses this for an HPC cluster detail
# bullet that only matters for HPC-flavored postings). Config-driven and
# empty by default -- no field-specific assumption baked into code.
def pinned_bullets() -> list:
    return profile_mod.load_domain_config().get("pinned_bullets", [])


def skill_categories_config() -> list:
    """The active profile's own CV "Skills" section groupings
    (domain.json's "skill_categories": [{label, keywords}, ...]) -- empty
    for a profile that hasn't configured any, in which case the Skills
    section simply has nothing to show (see build_skill_categories)."""
    return [(c["label"], c["keywords"]) for c in profile_mod.load_domain_config().get("skill_categories", [])]


# Decorative list-marker glyphs some of the source .odt files use (►, □, ∆,
# bullets, arrows, dingbats) -- pdfTeX can't render these without extra
# Unicode setup, and they're redundant with LaTeX's own \item marker anyway.
# Also covers the actual emoji block (U+1F000-U+1FFFF) -- real scraped job
# postings put these straight in the title/company text (e.g. a literal
# "🌄" in "...Hybrid in Freiburg🌄"), and plain pdflatex fatal-errors on
# them ("Unicode character ... not set up for use with LaTeX") same as any
# other glyph here, just from external job data instead of the bullet bank.
_DECORATIVE_GLYPH_RE = re.compile(
    "[←-⇿∀-⋿⌀-⏿─-◿☀-➿\U0001F000-\U0001FFFF]+\\s*"
)

# Job postings (esp. German-market ones) commonly append a gender-neutral
# disclaimer to the title -- "(m/w/d)", "(f/m/d)", "(all genders)" -- which
# is a legal/HR formality about the posting, not part of the job title
# itself, and reads oddly stitched onto a CV header.
_GENDER_MARKER_RE = re.compile(
    r"\(\s*[mwfdx]\s*(?:/\s*[mwfdx]\s*){1,3}\)|\(\s*all\s+genders?\s*\)",
    re.IGNORECASE,
)


def strip_gender_marker(text: str) -> str:
    if not text:
        return text
    return re.sub(r"\s{2,}", " ", _GENDER_MARKER_RE.sub("", text)).strip()


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


# ":" in a profile text field (school, degree, ...) is a manual line-break
# marker for the narrow sidebar column -- e.g. "Bachelor's Degree: Computer
# Science" prints as two lines, colon dropped. Applied AFTER latex_escape()
# so this inserts a real LaTeX line break (\\), not raw data -- a literal
# "\\" typed into the source JSON would just get escaped back into visible
# \textbackslash{} text like everything else.
def _colon_break(escaped_text: str) -> str:
    if ":" not in escaped_text:
        return escaped_text
    before, after = escaped_text.split(":", 1)
    return f"{before.strip()} \\\\ {after.strip()}"


def get_job(job_id: int) -> dict:
    conn = db.connect_db()
    row = conn.execute("SELECT * FROM jobs_full WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"job {job_id} not found")
    return dict(row)


def job_text(job: dict) -> str:
    return " ".join(filter(None, [
        job.get("title"), job.get("description"), job.get("keywords_raw"),
    ]))


def pick_variant_text(bullet: dict, jtext: str) -> str:
    """Picks a bullet's best phrasing angle for this job. Language-agnostic
    by construction: each bullet's variants come from exactly one
    language's CSV (bullet_bank_<lang>.csv, never merged -- see
    cv_bank.load_csv_bullets), so whichever language's text is in
    `variants` is what a CV in that language uses; no en/de branching
    needed here."""
    lower = jtext.lower()
    if "site reliability" in lower or " sre" in lower:
        label_priority = ["Ops / SRE", "DevOps", "Linux Admin", "Base"]
    elif "devops" in lower:
        label_priority = ["DevOps", "Linux Admin", "Ops / SRE", "Base"]
    else:
        label_priority = ["Linux Admin", "DevOps", "Ops / SRE", "Base"]

    by_label = {v["label"]: v["text"] for v in bullet["variants"]}
    base_text = by_label.get("Base")

    chosen = None
    for label in label_priority:
        if label in by_label:
            chosen = by_label[label]
            break
    if chosen is None and by_label:
        chosen = next(iter(by_label.values()))
    if chosen is None:
        return ""

    # No real alternative -- if the picked phrasing-variant is the same
    # content as Base (never differentiated, or simply the same text),
    # prefer showing Base rather than a redundant differently-labeled copy.
    if base_text and chosen.strip().lower() == base_text.strip().lower():
        return base_text
    return chosen


def _rank_bullets_with_fallback(bullets: list, jtext: str) -> list:
    """Like cv_bank.score_bullets(), but never drops a bullet just
    because it scores 0 keyword overlap with this specific job -- within
    one position's own (small) bullet set, a zero-scoring bullet is still
    real content that should fill the detail-bullet budget when nothing
    more relevant is available, ranked after anything that does match."""
    lower = (jtext or "").lower()
    kw_weights = cv_bank.keyword_weights()
    scored = []
    for b in bullets:
        hay = " ".join([
            b.get("category", ""), " ".join(b.get("skill_tags", [])),
            b.get("jd_keyword_notes", ""), cv_bank.bullet_text(b),
        ]).lower()
        s = sum(kw_weights.get(kw, 3) for kw in kw_weights if kw in hay and kw in lower)
        scored.append({**b, "match_score": s})
    scored.sort(key=lambda b: (
        -b["match_score"],
        -b.get("rating", {}).get("strength", 0),
        -b.get("rating", {}).get("wording", 0),
    ))
    return scored


def _position_recency_tenure_hint(period: str) -> str:
    """Best-effort recency/tenure gloss for the LLM prompt, computed from
    the CSV's free-text period string ('2025–present', '2023–2024',
    '2018') -- gives the model concrete numbers instead of making it infer
    "how recent" / "how short" from the raw string itself."""
    if not period:
        return ""
    years = [int(y) for y in re.findall(r"\d{4}", period)]
    if not years:
        return ""
    is_present = "present" in period.lower()
    now = datetime.now().year
    end_year = now if is_present else years[-1]
    start_year = years[0]
    age = max(now - end_year, 0)
    tenure = max(end_year - start_year, 0) if len(years) > 1 else 0
    recency = "current position" if is_present else (
        f"ended {age} year{'s' if age != 1 else ''} ago" if age else "ended this year")
    tenure_str = f"~{tenure} year{'s' if tenure != 1 else ''} tenure" if tenure else "under 1 year tenure"
    return f"{recency}, {tenure_str}"


def llm_recommend_bullets(job: dict, timeline: list, on_progress=None):
    """Ask the LLM to pick which real bullets go into the CV for this
    specific job, AND how to place each position (full entry vs. the
    compact "Earlier" list) -- the target density Richard described
    (recent/relevant roles get more detail, minor roles get just a
    summary) is passed as guidance, not a hard rule, so the LLM can use
    judgment per job rather than a rigid per-employer count. Returns
    {period: {...}} or None on the stub backend / any failure / the
    "rule" generation_mode(), in which case the caller falls back to the
    algorithmic position_bullet_targets()/EARLIER_SCORE_THRESHOLD system
    below."""
    if profile_mod.llm_config()["llm_backend"] == "stub" or generation_mode() == "rule":
        return None

    bullet_targets = position_bullet_targets()
    tier_guidance = priority_tiers()
    blocks = []
    for pos in timeline:
        hint = _position_recency_tenure_hint(pos["period"])
        header = f"Position: {pos['employer']} — {pos['position_title']} ({pos['period']})"
        if hint:
            header += f"  [{hint}]"
        target = bullet_targets.get(position_key(pos["employer"], pos["position_title"]), {})
        # Human-supplied calibration, not re-derived from recency/keywords --
        # e.g. an older role the profile owner considers a reliably strong
        # fit shouldn't get bumped to "earlier" just for being older. A
        # per-position "priority_note" overrides the tier's generic
        # sentence when this position needs its own nuance.
        note = target.get("priority_note") or tier_guidance.get(target.get("priority_tier"))
        if note:
            header += f"\n  Priority guidance: {note}"
        lines = [header]
        for b in pos["bullets"]:
            # Surface the bullet bank's own Category/Anchor metadata --
            # without it the LLM was judging relevance from bullet prose
            # alone, blind to the structured signal Richard already put
            # in the CSV. ★ marks the position's single always-include
            # bullet (see cv_bank.py's "anchor" field).
            tag = f" ({b['category']})" if b.get("category") else ""
            anchor_mark = " ★" if b.get("anchor") else ""
            lines.append(f"  [{b['id']}]{anchor_mark}{tag} {cv_bank.bullet_text(b)}")
        blocks.append("\n".join(lines))

    prompt = _load_prompt("generate_cv").safe_substitute(
        title=job.get("title"), company=job.get("company"),
        description=(job.get("description") or "")[:1500],
        blocks="\n".join("---\n" + b for b in blocks),
    )

    try:
        raw = _call_llm(prompt, max_tokens=1500, on_progress=on_progress)
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)
        return {p["period"]: p for p in data.get("positions", []) if p.get("period")}
    except Exception:
        return None


def build_position_groups(job: dict, jtext: str, trim_level: int = 0, drop_droppable: bool = False,
                           llm_recommendation: dict = None, llm_placement: dict = None,
                           excluded_ids: set = None, bullets: list = None):
    """Every real position from the CSV career timeline, in chronological
    order (most recent first) -- not filtered down to whichever employers
    happen to score well against this job. Each position always shows its
    one-line summary. Detail-bullet selection prefers `llm_recommendation`
    (see llm_recommend_bullets) when given; otherwise falls back to a
    fixed (min, max) target per position (position_bullet_targets()),
    matching the density of Richard's own saved CVs. `trim_level` and
    `drop_droppable` only apply to the fallback path (the shrink-to-fit
    retry loop degrades to it if the LLM's first pass doesn't fit).

    `llm_placement` (period -> "full"/"earlier") is deliberately a
    SEPARATE parameter from `llm_recommendation`, and the caller keeps
    passing it on every fit-retry attempt even once it stops passing
    `llm_recommendation` (see generate_for_job): which position gets a
    full entry vs. a one-line "Earlier" mention is a bigger, more
    considered call than which bullets fill it, so an overflowing page
    should degrade to the algorithmic budget system for bullet counts
    without also throwing out the LLM's placement reasoning.

    `bullets` should be the active profile's store (cv_bank.build_bullet_store())
    -- defaults to loading it fresh if not given, so this stays usable for
    any caller that doesn't already have a store handy."""
    timeline = cv_bank.build_position_timeline(bullets)
    bullet_targets = position_bullet_targets()  # resolved once, not once per position
    bullet_pins = pinned_bullets()
    mode = generation_mode()  # resolved once -- see its docstring for the "rule"/"hybrid"/"llm" bands

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
    score_by_position = {
        (pos["employer"], pos["position_title"], pos["period"]): score
        for pos, score in scored_positions
    }

    groups = []
    for chron_idx, pos in enumerate(timeline):  # chronological order, most recent first
        pos_key = (pos["employer"], pos["position_title"], pos["period"])
        rank = rank_by_position[pos_key]
        pos_score = score_by_position[pos_key]
        bullets_by_id = {b["id"]: b for b in pos["bullets"]}
        rec = llm_recommendation.get(pos["period"]) if llm_recommendation else None
        excluded = excluded_ids or set()
        target = bullet_targets.get(position_key(pos["employer"], pos["position_title"]), DEFAULT_BULLET_TARGET)

        if rec is not None:
            summary_bullet = bullets_by_id.get(rec.get("summary_bullet_id")) or \
                bullets_by_id.get(pos["summary_bullet_id"])
            summary_text = cv_bank.bullet_text(summary_bullet) if summary_bullet else pos["summary_text"]
            detail_texts, detail_texts_raw, detail_ids = [], [], []
            for bid in rec.get("detail_bullet_ids", []):
                # target["max"] is a hard ceiling, not just prompt guidance
                # the LLM might overshoot -- stop taking picks once hit,
                # same as the algorithmic fallback path below always did.
                if len(detail_texts) >= target["max"]:
                    break
                b = bullets_by_id.get(bid)  # never invent -- skip any ID the LLM hallucinated
                if not b or b is summary_bullet or bid in excluded:
                    continue
                raw = pick_variant_text(b, jtext)
                text = latex_escape(raw)
                if text:
                    detail_texts.append(text)
                    detail_texts_raw.append(raw)
                    # score_bullets() only returns a bullet that scores > 0 or
                    # is anchored -- a zero-score, non-anchored bullet (a
                    # valid LLM pick, just one with no keyword overlap) comes
                    # back as an empty list, not a 0.
                    _scored = cv_bank.score_bullets(jtext, [b])
                    detail_ids.append((bid, _scored[0]["match_score"] if _scored else 0))
        else:
            if drop_droppable and target.get("droppable"):
                continue
            budget = max(target["min"], target["max"] - trim_level)
            summary_text = pos["summary_text"]
            ranked_bullets = _rank_bullets_with_fallback(pos["bullets"], jtext)
            detail_texts, detail_texts_raw, detail_ids = [], [], []
            for b in ranked_bullets:
                if b["id"] == pos["summary_bullet_id"] or b["id"] in excluded:
                    continue
                if len(detail_texts) >= budget:
                    break
                raw = pick_variant_text(b, jtext)
                text = latex_escape(raw)
                if text:
                    detail_texts.append(text)
                    detail_texts_raw.append(raw)
                    detail_ids.append((b["id"], b.get("match_score", 0)))

        this_pos_key = position_key(pos["employer"], pos["position_title"])
        jtext_lower = (jtext or "").lower()
        for pin in bullet_pins:
            if pin.get("position_key") != this_pos_key:
                continue
            if not any(kw in jtext_lower for kw in pin.get("trigger_keywords", [])):
                continue
            bid = pin.get("bullet_id")
            pinned = bullets_by_id.get(bid)
            if pinned and bid not in excluded:
                raw = pick_variant_text(pinned, jtext)
                text = latex_escape(raw)
                if text and text not in detail_texts:
                    detail_texts.insert(0, text)
                    detail_texts_raw.insert(0, raw)
                    detail_ids.insert(0, (bid, 999))  # never the "weakest" pick

        # Full-entry vs. "Earlier" placement: prefer the LLM's own reasoned
        # call (it weighs relevance/recency/tenure together per job, see
        # generate_cv.txt) when the recommendation gave one; fall
        # back to the older pure keyword-score threshold only when there's
        # no LLM placement to use (stub backend, or a failed call). Looked
        # up from `llm_placement`, NOT `rec`/`llm_recommendation` -- the
        # caller keeps `llm_placement` populated across every fit-retry
        # attempt even after it stops passing `llm_recommendation` for
        # bullet selection, so an overflowing page doesn't also throw
        # away the LLM's placement reasoning (see build_position_groups's
        # docstring and generate_for_job).
        placement_rec = llm_placement.get(pos["period"]) if llm_placement else None
        pos_placement = placement_rec.get("placement") if placement_rec else None
        if pos_placement not in ("full", "earlier"):
            pos_placement = None
        if pos_placement is not None:
            is_earlier = pos_placement == "earlier"
        else:
            is_earlier = pos_score < target.get("earlier_threshold", EARLIER_SCORE_THRESHOLD)

        # Priority-tier safety net: an "anchor"/"core" position is the
        # profile owner's own explicit calibration that it's a reliably
        # strong fit whenever genuinely relevant (domain.json's
        # priority_tiers/priority_tier -- also passed to the LLM as
        # "Priority guidance" text in generate_cv.txt). A single
        # LLM placement call doesn't reliably weigh that subtle a hint
        # against the rest of the prompt -- so when the LLM says "earlier"
        # for one of these positions but this job's own keyword evidence
        # against it already clears its earlier_threshold (real, objective
        # relevance, not just recency), trust the human calibration +
        # evidence over that one call's compliance and keep it full. Off
        # in "llm" generation_mode() -- that band means the profile owner
        # wants the model's own call trusted as-is, guardrail included.
        if (is_earlier and pos_placement == "earlier" and mode != "llm"
                and target.get("priority_tier") in ("anchor", "core")
                and pos_score >= target.get("earlier_threshold", EARLIER_SCORE_THRESHOLD)):
            is_earlier = False

        final_earlier = chron_idx >= EARLIER_PROTECTED_RECENT_COUNT and is_earlier

        # Safety net: the LLM sometimes places a position "full" but still
        # returns zero detail_bullet_ids (e.g. treating the bullet it used
        # as the summary as if that alone satisfied the pick) -- rather
        # than ship a bare, single-bullet "full" entry, fall back to the
        # algorithmic top picks so a compliance slip on the LLM's part
        # doesn't produce a visibly broken CV.
        if rec is not None and not final_earlier and not detail_texts:
            budget = max(1, target.get("max", DEFAULT_BULLET_TARGET["max"]))
            for b in _rank_bullets_with_fallback(pos["bullets"], jtext):
                if b is summary_bullet or b["id"] in excluded:
                    continue
                if len(detail_texts) >= budget:
                    break
                raw = pick_variant_text(b, jtext)
                text = latex_escape(raw)
                if text:
                    detail_texts.append(text)
                    detail_texts_raw.append(raw)
                    detail_ids.append((b["id"], b.get("match_score", 0)))

        groups.append({
            "employer": latex_escape(pos["employer"]),
            "employer_raw": pos["employer"],
            "position_title": latex_escape(pos["position_title"]),
            "period": latex_escape(pos["period"]),
            "location": latex_escape(pos.get("location", "")),
            "summary": latex_escape(summary_text),
            "summary_raw": summary_text,
            "bullets": detail_texts,
            "bullets_raw": detail_texts_raw,
            "detail_bullet_scores": detail_ids,  # [(bullet_id, match_score), ...] -- for the fit-retry loop's weakest-bullet drop
            "relevance_rank": rank,
            "placement_source": "llm" if pos_placement is not None else "score_threshold",
            "placement_reasoning": placement_rec.get("reasoning") if placement_rec else None,
            # dynamic per job -- but never the most recent
            # EARLIER_PROTECTED_RECENT_COUNT positions, regardless of
            # placement: a low keyword-overlap score (or a borderline LLM
            # call) on the CURRENT job just means the job posting text
            # didn't happen to repeat much CV jargon, not that the
            # position itself is stale.
            "earlier": final_earlier,
        })
    return groups


def _display_skill_name(kw: str) -> str:
    """Display-only capitalization for cv_bank.keyword_weights() entries
    (all lowercase internally, for matching) -- acronyms and official
    stylizations that plain .title() would get wrong (domain.json's
    "skill_display_overrides", e.g. "hpc" -> "HPC"). Anything not listed
    there just falls back to .title() (e.g. "linux" -> "Linux")."""
    overrides = profile_mod.load_domain_config().get("skill_display_overrides", {})
    return overrides.get(kw, kw.title())


def _skill_redundant_with_label(skill: str, label: str) -> bool:
    """True if the skill name is just the sidebar category heading
    restated, e.g. "linux" under "Linux Administration" -- redundant on
    the printed CV, so it gets dropped from that category's item list."""
    label_words = re.findall(r"[a-z0-9/]+", label.lower())
    return skill.lower() in label_words


def build_skill_categories(bullets: list) -> list:
    matched_tags = set()
    for b in bullets:
        matched_tags.update(t.lower() for t in b.get("skill_tags", []))

    categories = []
    used = set()
    for label, keywords in skill_categories_config():
        matched = [kw for kw in keywords if kw in matched_tags]
        used.update(matched)  # accounted for even if dropped below as redundant -- must not leak into "Other"
        items = [kw for kw in matched if not _skill_redundant_with_label(kw, label)]
        if items:
            categories.append({"label": latex_escape(label), "skills": [latex_escape(_display_skill_name(i)) for i in items]})
    # only show leftover tags that are real recognized tech keywords --
    # the CSV's "Core Skill Tags" column also carries internal
    # classification words (e.g. "ownership", "proxies") never meant to
    # be printed as if they were skills
    leftover = sorted((matched_tags - used) & set(cv_bank.keyword_weights()))
    if leftover:
        other_label = "Sonstiges" if _active_lang() == "de" else "Other"
        categories.append({"label": other_label, "skills": [latex_escape(_display_skill_name(i)) for i in leftover[:8]]})
    return categories


def _call_llm(prompt: str, max_tokens: int = 200, on_progress=None) -> str:
    """Shared backend dispatch for llm_recommend_bullets/generate_intro/
    generate_cover_letter. Returns the raw (un-escaped) model text, or
    raises on failure -- callers fall back to a template on any exception.

    `on_progress`, if given, is called with an incremental token count
    (a delta, not a running total -- callers accumulate) as the response
    arrives. Ollama streams token-by-token so this is a real live signal
    that data is actually moving, not just a stalled connection; the
    anthropic/openai backends aren't streamed here, so they report their
    whole response as one lump delta once it lands."""
    cfg = profile_mod.llm_config()
    backend = cfg["llm_backend"]
    if backend == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        msg = client.messages.create(
            model=cfg["anthropic_model"],
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if on_progress:
            on_progress(len(text.split()))
        return text
    if backend == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=cfg["openai_api_key"])
        resp = client.chat.completions.create(
            model=cfg["openai_model"],
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.choices[0].message.content
        if not text:
            raise RuntimeError(f"openai returned no answer text (finish_reason={resp.choices[0].finish_reason!r})")
        text = text.strip()
        if on_progress:
            on_progress(len(text.split()))
        return text
    if backend == "custom":
        # Any OpenAI-compatible endpoint -- self-hosted, a proxy, a
        # third-party provider -- base_url/key/model are all freeform,
        # user-supplied settings (settings page), not tied to one provider.
        # Raw HTTP via llm_plugin.custom_chat_completion(), deliberately
        # not the `openai` package -- see that function's docstring: it's
        # just a wrapper around this same JSON-over-HTTP contract, and
        # importing it to reach a non-OpenAI endpoint meant a missing
        # `openai` pip package broke this backend with a confusing,
        # wrong-provider error.
        #
        # A reasoning model spends tokens on an internal chain-of-thought
        # (returned separately as message.reasoning_content, not part of
        # this response's "content" field) before writing the visible
        # answer -- without headroom it can get cut off entirely mid-
        # thought (finish_reason="length", content=None), which is what
        # empirically happened here even at max_tokens+300 on a complex
        # prompt. Tried the usual ways to just turn reasoning off for a
        # request instead (extra_body enable_thinking=False,
        # reasoning_effort="low", a "/no_think" suffix in the prompt) --
        # this particular server/proxy honored none of them, still
        # returning a full reasoning_content every time. So: give it
        # room instead. "Min. tokens per response" (settings page) is a
        # per-call FLOOR, not an addition -- effectively "reasoning
        # budget," since local/self-hosted use has no per-token cost to
        # weigh against reliability. Callers still fall back to their
        # own template/algorithmic path on the RuntimeError below if
        # even that isn't enough for a given call.
        import llm_plugin
        data = llm_plugin.custom_chat_completion(
            [{"role": "user", "content": prompt}],
            max(max_tokens, cfg["custom_min_tokens"]), cfg,
        )
        choice = data["choices"][0]
        text = choice.get("message", {}).get("content")
        if not text:
            raise RuntimeError(f"custom backend returned no answer text (finish_reason={choice.get('finish_reason')!r}) "
                                f"-- if this is a reasoning model, try raising \"Min. tokens per response\" in Settings.")
        text = text.strip()
        if on_progress:
            on_progress(len(text.split()))
        return text
    if backend == "ollama":
        import requests as req
        host = cfg["ollama_host"]
        model = cfg["ollama_model"]
        timeout = cfg["ollama_timeout"]
        resp = req.post(f"{host}/api/generate",
                         json={"model": model, "prompt": prompt, "stream": True},
                         timeout=timeout, stream=True)
        resp.raise_for_status()
        pieces = []
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            piece = chunk.get("response", "")
            if piece:
                pieces.append(piece)
                if on_progress:
                    on_progress(1)  # one streamed chunk ~= one token
            if chunk.get("done"):
                break
        return "".join(pieces).strip()
    raise ValueError(f"no real LLM for backend={backend!r}")


LANGUAGE_NAMES = {"en": "English", "de": "Deutsch"}  # native self-name -- the prompt text itself is in that language too

_DEFAULT_OPEN_QUESTION = {
    "en": ("I'd welcome the chance to talk through how that experience applies here -- "
           "what's the biggest priority for whoever takes this on in the first few months?"),
    "de": ("Ich würde mich freuen, in einem Gespräch zu erläutern, wie diese Erfahrung hier "
           "einfließen kann -- was ist die größte Priorität für diese Rolle in den ersten Monaten?"),
}

_COVER_LETTER_WRAPPER = {
    "en": {"greeting": "Dear Hiring Team at {company},", "opening": "I'm writing about the {title} opening.",
           "signoff": "Best regards,\n{name}"},
    "de": {"greeting": "Sehr geehrtes Team von {company},", "opening": "ich schreibe Ihnen bezüglich der Position als {title}.",
           "signoff": "Mit freundlichen Grüßen,\n{name}"},
}


def generate_cover_letter(job: dict, groups: list, profile: dict, on_progress=None) -> str:
    """Fixed template (guaranteed shape/reliability) with three small LLM-
    filled paragraph-sized gaps -- OPENING/BODY/CLOSING, roughly 3/4/3
    sentences -- rather than asking the model to compose a whole letter
    freeform. A smaller/local model (e.g. Ollama) is much more reliable at
    one focused block at a time than at holding together a whole coherent
    letter; each gap also degrades independently (a bad/missing block
    doesn't cost the others) instead of an all-or-nothing fallback to a
    generic template on any failure. Language is a property of the active
    profile (see _active_lang), not a per-call choice -- the achievement
    bullets it draws from already come from that profile's own bullet
    bank (see generate_for_job), so the whole letter stays one language."""
    lang = _active_lang()
    wrapper = _COVER_LETTER_WRAPPER.get(lang, _COVER_LETTER_WRAPPER["en"])
    by_relevance = sorted(groups, key=lambda g: g["relevance_rank"])
    top = by_relevance[:2]
    company = job.get("company") or "your team"
    title = job.get("title") or "this role"
    name = profile.get("name", "")

    # factual fallbacks -- used whole-cloth if the LLM call fails entirely,
    # or per-block if only that block's regex match comes up empty
    fallback_opening = f"{wrapper['opening'].format(title=title)}".strip()
    fallback_body = top[0]["summary_raw"] if top else ""
    fallback_closing = _DEFAULT_OPEN_QUESTION.get(lang, _DEFAULT_OPEN_QUESTION["en"])

    opening, body, closing = fallback_opening, fallback_body, fallback_closing

    if profile_mod.llm_config()["llm_backend"] != "stub":
        achievements = "\n".join(
            ["- " + g["summary_raw"] for g in top] +
            ["- " + b for g in top for b in g["bullets_raw"][:2]]
        )
        prompt = _load_prompt("cover_letter").safe_substitute(
            achievements=achievements, title=title, company=company,
            description=(job.get("description") or "")[:800],
            language=LANGUAGE_NAMES.get(lang, "English"),
        )

        try:
            raw = _call_llm(prompt, max_tokens=500, on_progress=on_progress)
            opening_match = re.search(r"OPENING:\s*(.+?)(?=\n[A-Z]+:|\Z)", raw, re.DOTALL)
            body_match = re.search(r"BODY:\s*(.+?)(?=\n[A-Z]+:|\Z)", raw, re.DOTALL)
            closing_match = re.search(r"CLOSING:\s*(.+?)(?=\n[A-Z]+:|\Z)", raw, re.DOTALL)
            if opening_match and opening_match.group(1).strip():
                opening = opening_match.group(1).strip()
            if body_match and body_match.group(1).strip():
                body = body_match.group(1).strip()
            if closing_match and closing_match.group(1).strip():
                closing = closing_match.group(1).strip()
        except Exception:
            pass  # keep the factual fallback values for whichever block(s) didn't fill

    return (
        f"{wrapper['greeting'].format(company=company)}\n\n"
        f"{opening}\n\n"
        f"{body}\n\n"
        f"{closing}\n\n"
        f"{wrapper['signoff'].format(name=name)}"
    )


def generate_intro(job: dict, groups: list, profile: dict, on_progress=None) -> str:
    """One small LLM call for a factual intro paragraph, prompted to use
    only the selected bullets/skills (which already come from the active
    profile's bullet bank, see generate_for_job) -- falls back to a
    template sentence on the stub backend (default, no API calls)."""
    backend = profile_mod.llm_config()["llm_backend"]
    lang = _active_lang()
    # Only ever draw from positions actually shown as full entries in the
    # body -- groups sorted by pure keyword relevance_rank could otherwise
    # surface a position's bullet here while build_position_groups placed
    # that same position "earlier" (a separate, LLM-reasoned call weighing
    # recency/tenure too, not just keyword overlap), producing an intro
    # that name-drops or paraphrases a role the CV then buries as a
    # one-line mention right below it.
    full_by_relevance = sorted((g for g in groups if not g.get("earlier")), key=lambda g: g["relevance_rank"])
    by_relevance = full_by_relevance or sorted(groups, key=lambda g: g["relevance_rank"])
    top_employers_raw = [g["employer_raw"] for g in by_relevance[:3]]

    if backend == "stub":
        base = profile.get("default_title", "Senior Linux Administrator")
        if lang == "de":
            employers_txt = ", ".join(top_employers_raw) if top_employers_raw else "produktiven Linux-Umgebungen"
            return latex_escape(
                f"{base} mit praktischer Erfahrung bei {employers_txt}, mit Fokus auf zuverlässige, "
                f"automatisierte Infrastruktur, relevant für diese Rolle."
            )
        employers_txt = ", ".join(top_employers_raw) if top_employers_raw else "production Linux environments"
        return latex_escape(
            f"{base} with hands-on experience across {employers_txt}, focused on reliable, "
            f"automated infrastructure relevant to this role."
        )

    highlights = "\n".join(
        ["- " + g["summary_raw"] for g in by_relevance[:3]] +
        ["- " + b for g in by_relevance[:3] for b in g["bullets_raw"][:2]]
    )
    prompt = _load_prompt("intro").safe_substitute(
        title=job.get("title"), company=job.get("company"), highlights=highlights,
        language=LANGUAGE_NAMES.get(lang, "English"),
    )

    try:
        return latex_escape(_call_llm(prompt, max_tokens=200, on_progress=on_progress))
    except Exception:
        pass

    base = profile.get("default_title", "Senior Linux Administrator")
    employers_txt = ", ".join(top_employers_raw) if top_employers_raw else "production Linux environments"
    return latex_escape(f"{base} with hands-on experience across {employers_txt}.")


def render_tex_single(job: dict, profile: dict, groups: list,
                       skill_categories: list, intro: str) -> str:
    """Single-column style: skills near the top, education/certificates at
    the bottom. Each position's "earlier" flag (build_position_groups,
    driven by its own keyword relevance to THIS job vs
    EARLIER_SCORE_THRESHOLD -- not a fixed property of the position) sends
    it to a compact one-line list at the end of the experience section
    instead of a full jobheader+bullets entry -- everything else stays
    chronological within its own group."""
    labels = SECTION_LABELS.get(_active_lang(), SECTION_LABELS["en"])
    full_groups = [g for g in groups if not g.get("earlier")]
    earlier_groups = [g for g in groups if g.get("earlier")]

    template_path = profile_mod.template_path()
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_path.parent)),
        block_start_string=r"\BLOCK{", block_end_string="}",
        variable_start_string=r"\VAR{", variable_end_string="}",
        comment_start_string=r"\#{", comment_end_string="}",
        trim_blocks=True, lstrip_blocks=True,
        autoescape=False,
    )
    template = env.get_template(template_path.name)

    languages = [{"name": latex_escape(l["name"]), "level": latex_escape(l["level"])}
                 for l in profile.get("languages", [])]
    languages_line = " / ".join(f"{l['name']} {l['level']}" for l in languages)

    nationality = latex_escape(profile.get("nationality", ""))
    visa = latex_escape(profile.get("visa", ""))
    nationality_line = f"{nationality}, {visa}" if visa else nationality

    # Optional -- omitted entirely (no error) if unset or the file doesn't
    # exist. A relative path resolves against the profile's own directory
    # (not the repo root), so a photo travels with the profile like
    # everything else it owns.
    photo_path = ""
    raw_photo = profile.get("photo_path", "")
    if raw_photo:
        p = Path(raw_photo)
        if not p.is_absolute():
            profile_dir = profile_mod.active_profile_dir()
            p = (profile_dir / p) if profile_dir else (BASE / p)
        if p.exists():
            photo_path = str(p)

    return template.render(
        photo_path=photo_path,
        name=latex_escape(profile.get("name", "")),
        title=latex_escape(strip_gender_marker(job.get("title")) or profile.get("default_title", "")),
        location=latex_escape(profile.get("location", "")),
        phone=latex_escape(profile.get("phone", "")),
        email=latex_escape(profile.get("email", "")),
        nationality_line=nationality_line,
        languages_line=languages_line,
        links=[{"label": latex_escape(l["label"]), "url": latex_escape_url(l["url"])} for l in profile.get("links", [])],
        # no _colon_break here (unlike the sidebar style) -- this layout has
        # a full-width column, no need to force "Degree: Major" onto two lines
        education=[{"school": latex_escape(e["school"]),
                    "degree": latex_escape(e["degree"]), "year": latex_escape(e["year"])}
                   for e in profile.get("education", [])],
        certificates=[{"name": latex_escape(c["name"]), "id": latex_escape(c["id"]), "date": latex_escape(c["date"])}
                      for c in profile.get("certificates", [])],
        projects=[{"title": latex_escape(p["title"]), "period": latex_escape(p["period"]),
                   "location": latex_escape(p["location"]), "summary": latex_escape(p["summary"])}
                  for p in profile.get("projects", [])],
        skill_categories=skill_categories,
        full_groups=full_groups,
        earlier_groups=earlier_groups,
        intro=intro,
        labels=labels,
    )


def _pdf_page_count(pdf_path: Path) -> int:
    try:
        proc = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
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
            cwd=str(outdir), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except FileNotFoundError:
        proc = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
             f"-output-directory={outdir}", str(tex_path)],
            cwd=str(outdir), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
             f"-output-directory={outdir}", str(tex_path)],
            cwd=str(outdir), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
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


def generate_for_job(job_id: int, on_progress=None) -> dict:
    """Generates a CV entirely in the active profile's language: bullets
    come from that profile's own bullet_bank.csv (cv_bank.build_bullet_store,
    never mixed with another language), the LLM is instructed to write the
    intro/cover-letter text in that language, and the section headers
    switch too (SECTION_LABELS, via _active_lang) -- no English/German
    mixing within one generated CV. Layout is always the single-column
    template (render_tex_single) -- each profile owns its own
    template.tex.jinja (profile.template_path()).

    `on_progress`, if given, is called with a running total token count
    across every LLM call this generation makes (bullet recommendation,
    intro, cover letter) -- a live signal that the model is actually
    streaming data back, not just hung (see _call_llm)."""
    token_total = 0

    def _bump(delta):
        nonlocal token_total
        token_total += delta
        if on_progress:
            on_progress(token_total)

    profile_data = profile_mod.load_profile()
    job = get_job(job_id)
    jtext = job_text(job)
    bullets = cv_bank.build_bullet_store()

    all_bullets_for_skills = cv_bank.dedupe_by_similarity_group(cv_bank.score_bullets(jtext, bullets))
    skill_categories = build_skill_categories(all_bullets_for_skills[:24])

    dir_name = f"{slugify(job.get('company'))}_{slugify(job.get('title'))}_{job_id}"
    cv_outdir = profile_mod.generated_cv_dir() / dir_name
    cv_outdir.mkdir(parents=True, exist_ok=True)
    tex_path = cv_outdir / "cv.tex"

    timeline = cv_bank.build_position_timeline(bullets)
    llm_recommendation = llm_recommend_bullets(job, timeline, on_progress=_bump)  # None on stub backend / failure
    if llm_recommendation:
        print(f"[generate_cv] LLM placement reasoning for job {job_id} ({job.get('company')} -- {job.get('title')}):")
        for period, rec in llm_recommendation.items():
            print(f"  {period}: {rec.get('placement', '?')} -- {rec.get('reasoning', '(no reasoning given)')}")

    trim_level = 0
    drop_droppable = False
    excluded_ids = set()
    weak_drop_count = 0
    attempts = []
    intro = None

    for attempt in range(1, MAX_FIT_ITERATIONS + 1):
        # the LLM's bullet picks get exactly one shot (attempt 1); if it
        # doesn't fit, retries degrade to the algorithmic fixed-target
        # system for WHICH bullets to show rather than trying to
        # renegotiate the LLM's selection. Its full/earlier PLACEMENT
        # call is a separate, stickier decision -- stays in effect on
        # every attempt (see build_position_groups's docstring) so a
        # page-overflow retry doesn't quietly undo carefully-reasoned
        # placement just because the bullet budget needed to shrink.
        use_llm = llm_recommendation if attempt == 1 else None
        groups = build_position_groups(job, jtext, trim_level=trim_level, drop_droppable=drop_droppable,
                                        llm_recommendation=use_llm, llm_placement=llm_recommendation,
                                        excluded_ids=excluded_ids, bullets=bullets)
        if intro is None:  # only generate once (short summary text, unaffected by bullet trimming) -- avoids repeat LLM calls across retries
            intro = generate_intro(job, groups, profile_data, on_progress=_bump)
        tex_content = render_tex_single(job, profile_data, groups, skill_categories, intro)
        tex_path.write_text(tex_content, encoding="utf-8")

        result = compile_pdf(tex_path)
        attempts.append({"attempt": attempt, "used_llm_recommendation": use_llm is not None,
                          "trim_level": trim_level, "drop_droppable": drop_droppable,
                          "excluded_count": len(excluded_ids),
                          "page_count": result["page_count"], "overfull_vbox": result["overfull_vbox"]})

        if not result["ok"]:
            break  # a hard LaTeX error isn't something shrinking content fixes
        if result["page_count"] == 2 and result["overfull_vbox"] == 0:
            break

        # On overflow, first drop the single weakest currently-included
        # bullet CV-wide (cheapest, most surgical cut -- one bullet, not a
        # whole position's budget), up to WEAK_DROP_CAP times. Once that's
        # exhausted (or nothing left to rank), fall back to shrinking every
        # position's budget toward its own floor, then dropping the sole
        # "droppable" position entirely. Once every lever is exhausted,
        # further iterations would just re-render the same content.
        weakest = None
        if weak_drop_count < WEAK_DROP_CAP:
            weakest = min(
                ((bid, score) for g in groups for bid, score in g.get("detail_bullet_scores", [])),
                key=lambda t: t[1], default=None,
            )
        if weakest is not None:
            excluded_ids.add(weakest[0])
            weak_drop_count += 1
        elif trim_level < 5:
            trim_level += 1
        elif not drop_droppable:
            drop_droppable = True
        else:
            break

    cover_letter_dir = profile_mod.generated_cover_letter_dir() / dir_name
    cover_letter_dir.mkdir(parents=True, exist_ok=True)
    cover_letter_path = cover_letter_dir / "cover_letter.txt"
    cover_letter_path.write_text(generate_cover_letter(job, groups, profile_data, on_progress=_bump), encoding="utf-8")

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
