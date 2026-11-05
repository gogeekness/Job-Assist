# 6. Development

How the code fits together, how to run the checks, and how to extend it.
For contributors, and for anyone forking the app to change its behavior
rather than its content.

← [Customizing Harvest](05-customizing-harvest.md) · [Back to README](../README.md)

---

## Architecture

```
                    ┌────────────────────── FindJobs.py (Flask) ──────────────────────┐
  Greenhouse ─┐     │ harvesters ─► upsert_job() ─► jobs.db (shared)                  │
  Arbeitnow  ─┤     │                                   │                             │
  EURAXESS   ─┼────►│ routes / filters ◄── db.jobs_full view (ATTACH) ◄── job_state.db │
  JobSpy     ─┤     │      │                                              (per profile)│
  LinkedIn   ─┘     │      ├─ rate ───► llm_plugin.py ──► stub / ollama / anthropic /...│
                    │      └─ generate ► generate_cv.py ─► cv_bank.py (bullet store)   │
                    │                         │                                        │
                    │                         └─► template.tex.jinja ─► pdflatex ─► PDF│
                    └──────────────────────────────── profile.py resolves every path ──┘
```

### Module map

| File | Responsibility |
|---|---|
| `FindJobs.py` | Flask app: every route, the harvesters, region/language detection, filter SQL, the settings pages. The entry point. |
| `profile.py` | **Every path to per-profile data goes through here.** Active-profile lookup, `create_profile()`, `load_config()`/`llm_config()`/`load_domain_config()`. |
| `db.py` | `connect_db()` opens `jobs.db`, ATTACHes the profile's `job_state.db`, and builds the `jobs_full` TEMP VIEW. `set_job_state()` upserts per-profile state. |
| `llm_plugin.py` | `rate_job()` dispatches to a backend; `test_connection()`; Ollama load/unload helpers |
| `cv_bank.py` | Loads the bullet bank (CSV, plus the optional .tex/.odt archive), deduplicates and groups by similarity, scores bullets against a job, builds the position timeline |
| `generate_cv.py` | Places positions (full vs. "Earlier"), selects bullets, writes intro and cover letter via `_call_llm()`, renders the template, compiles, and trims until it fits. Also runs from the CLI: `python generate_cv.py <job_id>`. |
| `templates/` | Jinja2 HTML (Bootstrap 5 + Bootstrap Icons). `base.html` holds the nav and profile switcher. |
| `profiles/_skeleton/` | What new profiles are copied from |
| `default-settings/prompts/` | Factory prompts, used by the **reset** buttons |
| `scripts/` | Cron helpers and **one-off migrations** (`migrate_*`, `split_*`). Don't run the migrations on a fresh install. |
| `jobspy/`, `job-scraper/` | **Vendored** third-party code, kept as plain copies so they're easy to patch. Excluded from lint. |
| `job_harvester_and_cv_mapper.py` | Reference only; nothing imports it |

### Key design rules

1. **Never hardcode personal or field-specific content in code.** It goes
   in the profile (`domain.json`, prompts, template). `cv_bank.py` and
   `generate_cv.py` fall back to generic defaults when a key is missing.
2. **Every per-profile path goes through `profile.py`.** Don't build
   `profiles/<id>/...` paths yourself.
3. **Read config fresh on every call.** Don't cache it at import. That's
   what lets profile switches and settings saves apply without a restart.
4. **Query `jobs_full`, write with `db.set_job_state()`.** Scraped columns
   live in `jobs`; per-profile state lives in `state.job_state`. The `jobs`
   table still contains stale state columns from before the split, so
   never read state from `jobs` directly.
5. **Generated CVs contain only content from the bullet bank.** The LLM may
   choose and order bullets, never write them. Every LLM call has a
   template or rule-based fallback if it fails.

## Dev setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # app deps + pytest + ruff
```

Run against a throwaway profile so your real data isn't touched:

```bash
JOB_ASSIST_PROFILE=DEV-TEST .venv/bin/python FindJobs.py
```

Create `DEV-TEST` from the UI first, or `cp -r profiles/_skeleton profiles/DEV-TEST`.

> Note: the profile is per-profile, but `jobs.db` is shared. For a fully
> isolated sandbox, use a separate clone or `git worktree`.

## Tests & lint

```bash
.venv/bin/pytest -q
```

```bash
.venv/bin/ruff check .
```

```bash
.venv/bin/ruff check . --fix
```

- Tests live in `tests/`. `test_profile.py` shows the pattern: monkeypatch
  `profile_mod.ACTIVE_MARKER` / `PROFILES_DIR` into `tmp_path` so tests
  never touch real profiles.
- Ruff config is in `pyproject.toml`: py310 target, rules `E,F,W,I`
  (import order is enforced), and the vendored directories are excluded.

### CI

`.github/workflows/ci.yml` runs **lint → test → package** on every push and
PR. The package step builds a versioned `git archive` tarball as a build
artifact. Jobs run on **self-hosted runners** labelled
`[self-hosted, dustoff, job-assist]`. If you fork the repo, change
`runs-on:` to `ubuntu-latest` or to your own runner labels.

## Branches

| Branch | Use |
|---|---|
| `main` | Stable trunk |
| `dev` | Larger feature work, currently moving harvest settings into config |
| `author` | The maintainer's day-to-day tweaks; merged into `main` once stable |

Before working on something that touches harvesting, read `CLAUDE.md` →
*Open work* and `OPS_NOTES.md`.

---

## Extension recipes

### Adding a CV language

The CV/cover-letter side is language-agnostic. A language is a dictionary
key, not new code.

1. `generate_cv.py`: add an entry to `SECTION_LABELS`, e.g. `"fr": {...}`,
   using the same keys as `"en"`.
2. `generate_cv.py`: add an entry to `LANGUAGE_NAMES` (`"fr": "Français"`)
   and to `_COVER_LETTER_WRAPPER` (salutation and sign-off).
3. Create a profile, set `"lang": "fr"` in its `config.json`, and translate
   its `prompts/*.txt` and `bullet_bank.csv`.

A missing key falls back to `"en"`, so a half-finished translation still
produces a CV.

### Adding an LLM backend

1. `profile.py`: add the new settings and their defaults to
   `_LLM_CONFIG_DEFAULTS`.
2. `llm_plugin.py`: write `_mybackend_rate(job, cv_blocks)`. Build the
   prompt with `_build_prompt()`, parse with `_parse_json_response()`, and
   return the standard dict (`score, backend, summary, highlights,
   block_matches, concerns`). Add a branch in `rate_job()` and in
   `test_connection()`.
3. `generate_cv.py`: add a branch in `_call_llm()`. It returns plain text
   and raises on failure.
4. `FindJobs.py` → `save_llm_config()` and `templates/settings.html`: add
   the form fields.

### Adding a harvester

See [Customizing Harvest → Adding a new job source](05-customizing-harvest.md#adding-a-new-job-source).

### Adding a page

1. Add a route in `FindJobs.py` that returns `render_template("mypage.html", ...)`.
2. Create `templates/mypage.html` with `{% extends "base.html" %}`.
3. Add a nav link in `base.html`.

The context processor `inject_profile_nav()` gives every template the
profile list and the active profile.

### Adding a system prompt

1. Add `prompts/myprompt.txt` to `profiles/_skeleton/` and to
   `default-settings/prompts/` (the reset source).
2. Register it in `_SYSTEM_PROMPTS` in `FindJobs.py` so it appears on the
   Prompts page.
3. Load it with `generate_cv._load_prompt("myprompt").substitute(...)`.

Existing profiles **don't** get the new file automatically. Copy it into
`profiles/*/prompts/` yourself, or make the code fall back to
`default-settings/` when the file is missing.

### Schema changes

- **`jobs` (shared)**: add the column to `SCHEMA_SQL` and to `EXTRA_COLS`.
  `ensure_schema()` runs `ALTER TABLE` on existing databases at startup.
  Also add it to `db._JOBS_OWN_COLS` so it appears in `jobs_full`.
- **`job_state` (per profile)**: add it to `STATE_SCHEMA` **and**
  `STATE_COLS` in `db.py`. `CREATE TABLE IF NOT EXISTS` won't add columns
  to databases that already exist, so include an `ALTER TABLE` migration.

## Security notes

- There is no authentication, and Flask runs with `debug=True`. The
  Werkzeug debugger allows code execution, so **never** bind to anything
  except localhost or a LAN you trust.
- API keys are stored in plain text in `profiles/<id>/config.json`.
- `.gitignore` keeps personal data out of git. Run `git status` before
  `git add -A`, because files dropped in the repo root (keys, exports) are
  only ignored if they match a pattern.
