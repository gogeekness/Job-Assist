# Job-Assist

An end-to-end job search pipeline: scrape postings → LLM-rate them for fit →
generate a tailored, factual CV per job you approve. Built for a Linux/HPC/DevOps
sysadmin job search focused on Berlin/Germany/EU, by extending a working
Flask prototype (originally "JobFinder") rather than rewriting it.

## Layout

This repo root is the actual application — **not** a wrapper around anything
in a subdirectory. The two subdirectories below are vendored third-party
projects, kept as plain (non-submodule) copies so they're easy to read and
adapt, each with their own README/LICENSE:

```
FindJobs.py           Flask app: dashboard, job list, job detail, settings
cv_bank.py             Unified CV bullet store (CSV + LaTeX CVs + .odt archive)
generate_cv.py          Tailored CV generator (LaTeX -> PDF)
llm_plugin.py            Job-rating backends (stub / Anthropic / OpenAI / Ollama)
tex_templates/             Jinja2-templated LaTeX CV template
templates/                  Flask/Jinja2 HTML templates

jobspy/                        Vendored: github.com/speedyapply/JobSpy
                                 (LinkedIn/Indeed/Glassdoor/ZipRecruiter scraping)
job-scraper/                     Vendored: github.com/anandanair/job-scraper
                                   (source reference only -- its LinkedIn technique
                                   was reimplemented directly in FindJobs.py's
                                   _do_harvest_linkedin_alt(), targeting Germany/
                                   Spain, not this repo's original Singapore
                                   defaults, and without its Supabase dependency)
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # edit as needed; LLM_BACKEND=stub needs no API key
cp profile.example.json profile.local.json   # fill in your real (gitignored) details
```

Personal data (bullet-bank CSV, CV archive symlinks, generated output,
`.env`, `profile.local.json`) is gitignored and never enters version control.
See `.gitignore` for the full list.

Run the app:

```bash
.venv/bin/python FindJobs.py
```

## Pipeline

1. **Harvest** — Greenhouse, Arbeitnow, EURAXESS (public APIs), plus JobSpy and
   a LinkedIn-guest-API harvester for LinkedIn/Indeed/Glassdoor/ZipRecruiter
   coverage the API-only sources can't reach.
2. **Gross filter** — cheap pre-check (`is_it_relevant`) drops obviously
   non-IT postings before spending LLM calls on them.
3. **Rate** — LLM-scored (or free keyword-only `stub` backend) fit rating,
   run per-job or in bulk (`Rate unrated jobs` on the dashboard).
4. **Generate** — pick an approved job, generate a tailored two-page LaTeX
   CV from your real career history and bullet bank, review/edit the LaTeX
   inline with a live PDF preview, recompile, approve.

## Credits

- [JobSpy](https://github.com/speedyapply/JobSpy) by speedyapply
- [job-scraper](https://github.com/anandanair/job-scraper) by anandanair
