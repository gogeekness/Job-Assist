# 1. Getting Started

This guide takes you from a fresh clone to a running app with your own
profile. Allow about 15 minutes, plus however long a LaTeX install takes on
your system.

← [Back to README](../README.md) · Next: [Using the App →](02-using-the-app.md)

---

## Prerequisites

| Requirement | Why | Check |
|---|---|---|
| **Python 3.10+** | The app itself | `python3 --version` |
| **LaTeX (`pdflatex`)** | Compiling CVs to PDF | `pdflatex --version` |
| **`latexmk`** *(optional)* | Used before plain `pdflatex` when it's available | `latexmk --version` |
| **`pdfinfo`** *(optional)* | Page counting, which keeps the CV to its target length | `pdfinfo -v` |
| **Ollama** *(optional)* | Free local LLM backend | `ollama list` |

On Debian/Ubuntu:

```bash
sudo apt install python3-venv texlive-latex-recommended texlive-latex-extra texlive-fonts-extra latexmk poppler-utils
```

On macOS, install [MacTeX](https://www.tug.org/mactex/) (or BasicTeX plus
the packages listed at the top of `profiles/_skeleton/template.tex.jinja`)
and `brew install poppler`.

> Without LaTeX the app still runs: you can harvest, filter, rate, and get
> generated `.tex` source. Only the PDF step fails.

## Install

```bash
git clone git@github.com:gogeekness/Job-Assist.git
cd Job-Assist
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Always run the app with **`.venv/bin/python`**, not your system `python3`.
The `anthropic` and `openai` packages, and the dependencies of the vendored
`jobspy/` scraper, are installed only in the venv.

## Run

```bash
.venv/bin/python FindJobs.py
```

Open <http://localhost:5050>.

| Env var | Default | Effect |
|---|---|---|
| `HOST` | `127.0.0.1` | Set to `0.0.0.0` to reach the app from other machines on your LAN |
| `PORT` | `5050` | Listening port |
| `JOB_ASSIST_PROFILE` | *(unset)* | Forces the active profile for this process, overriding the UI switcher |

```bash
HOST=0.0.0.0 PORT=8080 .venv/bin/python FindJobs.py
```

> ⚠️ The app runs Flask's development server with `debug=True` and has
> **no authentication**. Bind it to a LAN only if you trust that network,
> and never expose it to the internet.

## Create your first profile

A fresh clone has no active profile. Before you do anything else:

1. Click the **profile switcher** at the top right of the nav bar and
   create a new profile. The name can be up to 15 characters (letters,
   digits, `-`, `_`), for example `JANE-EN-01`.
2. The app copies `profiles/_skeleton/` to `profiles/JANE-EN-01/` and makes
   it active. It starts with:
   - an empty bullet bank
   - the `stub` LLM backend (free keyword scoring, no API calls)
   - generic prompts and a generic CV template
3. Open **Profile** and fill in your name, contact details, education,
   certificates, and languages. These go into the CV header and sidebar.
4. Open **Bullet Bank** and enter your work history, one row per
   achievement. This is the most important step. See
   [Profiles & Content](03-profiles-and-content.md#the-bullet-bank).
   - Tip: the **Prompts** page has an external prompt called *Build Your
     Bullet Bank*. Paste it into any AI chat along with your old CV and it
     returns rows in the correct format.
5. *(Optional)* Open **Settings → LLM Connection** and choose a real LLM
   backend. See [LLM Backends](04-llm-backends.md).

All of `profiles/<your-id>/` is **gitignored**. Your career data and API
keys never enter version control.

## Smoke test

1. **Dashboard** (`/`) → **Harvest** → *Arbeitnow*. It's quick and needs no
   authentication.
2. When it finishes, open any job and click **Rate**. With `stub` you get a
   keyword-based score immediately.
3. Click **Generate CV**. You land on the CV Review page with the PDF on one
   side and the LaTeX source on the other.

If step 3 shows a LaTeX error, see [Troubleshooting](#troubleshooting).

## What lives where (and what's not in git)

```
Job-Assist/
├── jobs.db                  # harvested postings, shared by all profiles   (gitignored)
├── .active_profile          # which profile is selected                     (gitignored)
├── boards.txt               # Greenhouse board tokens to harvest            (tracked)
└── profiles/
    ├── _skeleton/           # template for new profiles                     (tracked)
    └── JANE-EN-01/          # your profile                                  (gitignored)
        ├── profile.json, config.json, domain.json, bullet_bank.csv
        ├── prompts/, external_prompts/, template.tex.jinja
        ├── job_state.db     # your ratings, notes, CV status per job
        └── generated_cvs/, generated_cover_letters/
```

Because personal data is gitignored, **`git pull` won't move it between
machines.** Copy it yourself, for example:

```bash
rsync -av profiles/JANE-EN-01/ other-host:Job-Assist/profiles/JANE-EN-01/
```

`scripts/backup_personal_data.sh` is a starting point for scheduled
backups. Check its file list first: it still references some pre-profile
paths.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: anthropic` / `pandas` / `tls_client` | You ran `python3` instead of `.venv/bin/python`, or `pip install -r requirements.txt` didn't finish. |
| CV generation: `pdflatex: not found` | Install TeX Live (see Prerequisites). |
| LaTeX error: `File 'xyz.sty' not found` | Install the missing package (`texlive-latex-extra` / `texlive-fonts-extra` on Debian), or remove that `\usepackage` from your profile's template. |
| Every job rates the same or near 0 | The bullet bank is empty, or `domain.json` has no `keyword_weights`. The stub rater scores keyword overlap with your bullets. |
| Harvest finishes with 0 jobs | Look at the error shown next to the source on the Dashboard. Scraped sites (LinkedIn/Indeed via JobSpy) rate-limit often, so retry later. |
| Settings changes don't seem to apply | Check you're on the profile you think you are. Every setting is per-profile. |
