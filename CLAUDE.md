# Job-Assist

End-to-end job search pipeline: harvest postings → gross-filter → LLM-rate →
generate a tailored LaTeX CV/cover letter per approved job. Owner: Richard
Eseke (rdeseke@swcp.com), sysadmin job search, Berlin/Germany/EU. Full
pipeline description: [README.md](README.md).

## Machines

- **Orion-Station** (`192.168.178.40`) — RTX 3090, runs the app (`HOST`/`PORT`
  env vars control LAN binding; defaults to localhost:5050), local Ollama
  for the LLM backend when profiles want it.
- **Laptop** (`192.168.178.25`) — thin client. No live connection between the
  two is assumed; they sync independently through `origin` on GitHub
  (`git@github.com:gogeekness/Job-Assist.git`).
- **Do not run multiple Claude sessions against the same checkout of this
  repo at once.** `git checkout`/branch state is filesystem-wide, not
  per-session — a second session doing `git checkout <other-branch>` silently
  moves the branch under whichever session is mid-task. If you need two
  sessions working here simultaneously, give each its own `git worktree`
  instead of sharing one directory.

## Branches

- **`main`** — stable trunk. (Renamed from `master` locally + on `origin`;
  **GitHub's repo-level default branch is still `master`** as of this
  writing — Settings → Branches → Default branch still needs to be flipped
  to `main` by hand, `gh` wasn't installed on Orion to do it via CLI.)
- **`dev`** — active feature work, currently: generalizing the harvest/rate
  layer beyond Germany/German (see "Open work" below).
- **`author`** — Richard's daily-use branch: personal tweaks, content
  changes, small fixes. Merge into `main` once stable; rebase onto `main`
  after `dev` work lands.

## Architecture: profiles

A *profile* (`profiles/<ID>/`, e.g. `RICHARD-DE-01`, `RICHARD-EN-01`) is a
self-contained context: personal fields, bullet bank, the four LLM prompts,
CV template, settings, per-profile job state, and its own generated output.
Path resolution lives in [profile.py](profile.py); `profiles/_skeleton/` is
what `create_profile()` copies from. Active profile is `.active_profile`
(gitignored) or the `JOB_ASSIST_PROFILE` env var.

**Intent (per Richard, 2026-09-29): profiles are a general context bucket —
language, market, prompts, LLM backend, whatever a given job search needs —
not a hardcoded DE/EN toggle.** CV/cover-letter generation
([generate_cv.py](generate_cv.py), [cv_bank.py](cv_bank.py)) already matches
this: `lang` is just a string read from the active profile's `config.json`,
and per-language content is `{lang: ...}` dicts that fall back to `"en"`.
Adding a new target language is adding a dict key, not writing code.

**What does NOT yet match this intent:** the harvest/filter/rate layer
(`FindJobs.py`, `llm_plugin.py`) is hardcoded to Germany/German/English via
module-level constants (`GERMANY_CITIES`, `IT_SEARCH_LOCATIONS`,
`IT_SEARCH_TERMS`, `LINKEDIN_GEO_IDS`, `LANGUAGE_HINTS`), plus a "non-Germany
EU postings must be English" hard-exclude rule in `upsert_job()` and a
`"German fluency required"` rating flag in `llm_plugin.py`'s stub rater.
None of it reads from a profile.

**Architectural wrinkle to keep in mind:** `jobs.db` (harvested postings) is
explicitly **shared across every profile** ([db.py](db.py)) — only
ratings/notes (`job_state.db`) are per-profile. So harvest criteria is
inherently a shared/global concern today, not a per-profile one, unlike CV
generation. `job_harvester_and_cv_mapper.py` is reference-only / not
imported at runtime — its LinkedIn technique was reimplemented directly in
`FindJobs.py`'s `_do_harvest_linkedin_alt()`.

## Open work (scoped, lives on `dev`)

Move the harvest-layer hardcoding into an editable, app-wide
`harvest_config.json` (gitignored) + tracked `harvest_config.example.json`,
seeded with the current Germany/Spain/Portugal/Italy/Malta values so nothing
changes until someone edits it:

1. New config file: `search_locations`, `search_terms`, `linkedin_geo_ids`,
   which cities count as the "home" region, language-detection hint
   phrases (primary/partial/fallback), foreign-language exclusion toggle.
2. `FindJobs.py`: the five module constants above + the `upsert_job()`
   exclusion rule read from it instead of being hardcoded.
3. `llm_plugin.py`: the `"German fluency required"` concern reads the
   active profile's target-language label instead of the literal string
   `"german"`.
4. `EU_COUNTRIES`/`ISO_COUNTRY_CODES` stay as-is — already generic (30
   countries), not Germany-specific.
5. Settings UI for editing the new config is a nice-to-have, not required
   for phase 1 (hand-editing the JSON works).

Not in scope unless asked: true per-profile independent job pools (each
profile harvesting/seeing only its own jobs) — would require partitioning
`jobs.db` by profile and reworking the shared-harvest scheduler. Only worth
it if two profiles need genuinely different simultaneous markets; today's
two profiles are the same search in two output languages.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
cp profile.example.json profile.local.json   # legacy path; profiles/<ID>/ is the real one now
```

Gitignored personal data (bullet banks, `.env`, `profile.local.json`,
`active-settings/`, `LateX_Docs`/`CVs_Job_Dos` symlink targets, `jobs.db`,
per-profile dirs under `profiles/` other than `_skeleton`) is **not** in
git and must be synced machine-to-machine by hand (rsync/scp), not by
`git pull`.
