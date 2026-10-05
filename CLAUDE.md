# Job-Assist

End-to-end job search pipeline: harvest postings → gross-filter → LLM-rate →
generate a tailored LaTeX CV/cover letter per approved job. Owner: Richard
Eseke (rdeseke@swcp.com). Full pipeline description: [README.md](README.md).

## Architecture: LLM/API backends

Every LLM/API call in the app goes through the **active profile's** own
settings (`profile.llm_config()`/`profile.save_llm_config()`, saved to that
profile's `config.json` via the Settings → LLM Connection page) — never
global env vars or a hardcoded provider. Config is read fresh on every call
(not cached at import), so a settings save or a profile switch takes effect
immediately, no restart needed.

Five backends, selected by `llm_config()["llm_backend"]`:

- `stub` — default; keyword scoring only, no external call, no credentials needed.
- `anthropic` — official `anthropic` SDK; needs `anthropic_api_key` + `anthropic_model`.
- `openai` — official `openai` SDK; needs `openai_api_key` + `openai_model`.
- `ollama` — raw `requests` calls (no SDK) against any Ollama instance on
  the network (`ollama_host` + `ollama_model` + `ollama_timeout`).
- `custom` — any OpenAI-compatible `/chat/completions` endpoint (self-hosted,
  a proxy, a third-party provider) via raw `requests`, deliberately **not**
  the `openai` SDK — see `llm_plugin.custom_chat_completion()`'s docstring:
  that SDK is just a JSON-over-HTTP wrapper, and importing it to reach a
  server that has nothing to do with OpenAI meant a missing `openai` pip
  package broke a backend that never talked to OpenAI. `custom_min_tokens`
  is a floor (default 4000), not a ceiling — self-hosted reasoning models
  spend tokens on chain-of-thought before the visible answer and can get
  cut off mid-reasoning (`finish_reason="length"`, empty content) if it's
  set too low.

All five backends' defaults live in one place: `profile.py`'s
`_LLM_CONFIG_DEFAULTS`.

**Two call sites, same backend switch, different contracts:**

- [llm_plugin.py](llm_plugin.py) — `rate_job()` scores a posting against
  the candidate's CV blocks; every non-stub branch returns a parsed JSON
  object (`_parse_json_response()` strips ` ```json ` fences and extracts
  the `{...}` body) in a shared shape (`score`/`backend`/`summary`/
  `highlights`/`block_matches`/`concerns`). `test_connection()` mirrors the
  same backend list for the Settings page's cheap "Test access" check —
  minimal tokens, no real rating.
- [generate_cv.py](generate_cv.py) — `_call_llm()` (same five-way backend
  dispatch) returns **raw text**, not JSON — used for cover-letter
  paragraphs and CV-block-tailoring gaps. Streams token-by-token for
  Ollama (`on_progress` callback); Anthropic/OpenAI/custom report progress
  in one shot since their SDKs/HTTP calls here aren't wired for streaming.

**Adding a new backend** means touching all of: `profile.py`'s
`_LLM_CONFIG_DEFAULTS` (new config keys with sane defaults) +
`llm_plugin.py`'s `rate_job()`/`test_connection()` + `generate_cv.py`'s
`_call_llm()` + the Settings page template. There's no shared base class —
each backend is a plain `if backend == "...":` branch in each of those
three places; keep that pattern rather than introducing an abstraction for
what's currently five linear branches in three functions.

## Prompts

Each profile (`profiles/<ID>/prompts/`) carries four templates the app
itself calls: `rate_job.txt`, `cover_letter.txt`, `intro.txt`,
`recommend_bullets.txt` (plain `string.Template` `$substitution`, not
f-strings — see `_build_prompt()`). Separately, `external_prompts/` holds
prompts meant to be copy-pasted into an external AI chat tool by hand (JD
analysis, cold outreach, interview prep, salary research, cover letters per
language) — the app never calls these itself.

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
