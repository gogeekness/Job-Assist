# Job-Assist

An end-to-end job search pipeline: scrape postings → AI-rate them for fit →
generate a tailored, factual CV + cover letter per job → review, edit, and
approve. Everything (bullet bank, LLM settings, CV template, vocabulary) is
scoped to a **profile**, so you can run entirely separate job-search
identities (e.g. an English and a German CV/persona) side by side without
them interfering with each other.

## What It Does

1. **Harvests jobs from multiple sources** — Greenhouse and Arbeitnow (public
   APIs), EURAXESS (RSS), JobSpy (LinkedIn/Indeed/Glassdoor/ZipRecruiter),
   and a LinkedIn-guest-API fallback. Can't find a posting through any of
   those? Add it by hand from the Jobs page's "Blank Job" button — paste in
   the title/company/location/description and it's rated and CV-generated
   exactly like a scraped one.

2. **Filters and searches the job list** — region, language, ATS source,
   profession, and AND/OR/NOT keyword filters in the sidebar, plus a
   one-click **workflow filter bar** (Unrated, Rated, Ready to Generate,
   Not Generated, Generated, Unviewed, Has Notes) matching the rate-then-
   generate way you actually work through the list. Save any filter
   combination as a named preset. Filters you apply on the Jobs page persist
   when you switch to another page and come back — no need to re-filter
   every time.

3. **Rates every posting against your real CV content** — a 0-10 fit score,
   summary, highlights, and concerns, generated per-job or in bulk. Pick the
   rating backend per profile: free keyword-only `stub` (no API calls at
   all), Anthropic, OpenAI, a local/LAN Ollama install, or any other
   OpenAI-compatible endpoint (self-hosted, a proxy, a third-party
   provider) via the freeform **custom** backend.

4. **Generates a tailored CV** — real content only, nothing invented. For
   each position in your career history it decides whether that job earns a
   full, detailed entry or a compact one-line "Earlier" mention, and which
   of your real bullets to include, weighing relevance to *this* posting
   against recency and tenure. A **Generation Balance** dial (Settings page)
   lets you push that decision toward pure rule-based (your own configured
   targets, deterministic, no LLM call) or fully LLM-led, with a sane hybrid
   default in between.

5. **Generates a matching cover letter** — a real three-paragraph letter
   (opening / body / closing) built from the same job and CV content, not a
   generic template.

6. **Lets you review and fix the output before it goes out** — the CV
   Review page shows the compiled PDF next to the editable LaTeX source
   side by side. Edit factual details directly, recompile in place, copy
   the full `.tex` to your clipboard if you'd rather finish edits in
   VS Code, then mark the CV approved once it's ready.

7. **Batches the repetitive parts** — select many jobs at once from the
   list and either rate all of them or generate CVs for all of them in one
   go, instead of clicking through each job individually. Already-rated
   jobs are skipped automatically so a batch rate never re-spends an LLM
   call on something you already scored.

8. **Keeps everything profile-scoped** — bullet bank, LLM backend/keys,
   domain vocabulary (skill categories, keyword weights, per-position
   detail targets and priority tiers), prompts, and the CV's LaTeX template
   all live inside `profiles/<PROFILE-ID>/`, switchable from the nav bar.
   Nothing about one profile's setup leaks into another's, and a brand-new
   profile starts from a generic, non-personal skeleton.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python FindJobs.py
```

Open `http://localhost:5050`. On first run there's no active profile yet —
use the profile switcher (top-right of the nav bar) to create one; it
scaffolds from `profiles/_skeleton/` (empty bullet bank, generic domain
config, `stub` LLM backend) so it's immediately usable without hand-editing
JSON, then fill in your real details from the Settings and Bullet Bank
pages. Everything under `profiles/<id>/` is gitignored — real bullet
content, career history, and any API keys you save there never enter
version control.

Always run the app with `.venv/bin/python`, not a bare system `python3` —
the `anthropic`/`openai` packages (needed for those two rating/generation
backends) only live in the venv.

## Layout

This repo root is the actual application — **not** a wrapper around
anything in a subdirectory. `jobspy/` and `job-scraper/` are vendored
third-party scrapers, kept as plain (non-submodule) copies so they're easy
to read and adapt, each with their own README/LICENSE. See the project's
own notes (ask in-session, or read each file's module docstring) for a
fuller file-by-file breakdown of the main app's own code.

```
FindJobs.py         Flask app -- routes, harvesters, filters, settings
generate_cv.py       Tailored CV + cover letter generation, LaTeX rendering
cv_bank.py            Bullet bank: CSV + .tex/.odt archive, keyword scoring
llm_plugin.py           Job-rating backends (stub/Anthropic/OpenAI/Ollama/custom)
profile.py                Per-profile path resolution and config I/O
db.py                       Shared job-postings DB + per-profile job-state DB
templates/                    Flask/Jinja2 HTML templates
profiles/                       Per-profile data (gitignored except _skeleton/)

jobspy/                            Vendored: github.com/speedyapply/JobSpy
job-scraper/                         Vendored: github.com/anandanair/job-scraper
```

## Credits

- [JobSpy](https://github.com/speedyapply/JobSpy) by speedyapply
- [job-scraper](https://github.com/anandanair/job-scraper) by anandanair
