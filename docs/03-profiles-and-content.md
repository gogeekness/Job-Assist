# 3. Profiles & Content

How to make the app yours: your career data, your field's vocabulary, the
prompts, and the CV layout. **Changing any of this doesn't touch Python
code.** Everything here is a JSON, CSV, text, or LaTeX file inside your
profile directory, and most of it can be edited from the UI.

← [Using the App](02-using-the-app.md) · Next: [LLM Backends →](04-llm-backends.md)

---

## What a profile is

A profile is one directory, `profiles/<ID>/`, that holds a complete
job-search identity. Move or copy the directory and the whole identity goes
with it.

| File | Edited from | Purpose |
|---|---|---|
| `profile.json` | **Profile** page | Name, contact details, links, photo, education, certificates, languages, projects |
| `bullet_bank.csv` | **Bullet Bank** page | Your career history as individual achievements |
| `domain.json` | by hand | Your field's vocabulary: keyword weights, skill groups, per-position rules |
| `config.json` | **Settings** page | Language, LLM backend and keys, retention, generation balance |
| `prompts/*.txt` | **Prompts** page | The prompts the app sends to the LLM |
| `external_prompts/*.txt` | **Prompts** page | Prompts you copy into an outside AI chat |
| `template.tex.jinja` | **Template Editor** | The CV's LaTeX layout |
| `job_state.db` | *(automatic)* | This profile's ratings, notes, and CV status per job |

**Choosing the active profile:** the `JOB_ASSIST_PROFILE` env var wins.
Otherwise it's the ID in `.active_profile`, which the nav-bar switcher
writes.

**New profiles** are copied from `profiles/_skeleton/`. If you want every
future profile to start with something different (your own template, say),
edit the skeleton. It's tracked in git, so keep personal data out of it.

---

## `profile.json`: who you are

```json
{
  "name": "Jane Doe",
  "default_title": "Linux Systems Engineer",
  "location": "Berlin, Germany",
  "phone": "+49 ...",
  "email": "jane@example.com",
  "nationality": "Canadian",
  "visa": "EU Blue Card",
  "links": [{"label": "GitHub", "url": "https://github.com/jane"}],
  "photo_path": "photo.jpg",
  "projects":     [{"title": "...", "period": "...", "location": "...", "summary": "..."}],
  "education":    [{"school": "...", "degree": "BSc: Computer Science", "year": "2012"}],
  "certificates": [{"name": "RHCE", "id": "123-456", "date": "2023"}],
  "languages":    [{"name": "English", "level": "Native"}, {"name": "German", "level": "B1"}]
}
```

- The CV headline is the **job's own title**. `default_title` is used only
  when a job has no title.
- `photo_path` is optional. A relative path is resolved from the profile
  directory, so drop `photo.jpg` next to `profile.json`.
- `visa` is added after nationality when it's set.

---

## The bullet bank

`bullet_bank.csv` is the most important input. **Generated CVs contain only
what's in this file**: nothing is invented.

### Format

- **Row 1** is a banner and is ignored. **Row 2** is the header. Data starts
  at **row 3**.
- Columns are found by header name, so their order doesn't matter.

| Column | Required | Meaning |
|---|---|---|
| `Employer` | ✓ | Company name. Must be **identical** on every row for the same position. |
| `ID` | ✓ | Unique ID. A row without an ID is skipped. See *ID scheme* below. |
| `Job Title` | ✓ | Your title in that position |
| `Period` | ✓ | E.g. `Jan 2020 – Present`. Used for recency and tenure. |
| `Location` | | E.g. `Berlin, DE` |
| `Category` | | Free-form grouping, e.g. *Automation* or *Storage* |
| `Imp.` | | Importance from 1 to 5 (default 3) |
| `Anchor (Always Include)` | | Put `★` to mark a must-include bullet |
| `Core Skill Tags` | | Comma-separated, e.g. `ansible, terraform, linux` |
| `Source CV Family` | | Comma-separated tags noting which old CV it came from |
| `Base Bullet` | ✓ | The achievement, written plainly |
| `Variation 1..3` | | Other wordings of the same achievement. The best match for each job is used. |
| `Verb Risk` | | Note on weak or overused verbs. Affects the wording score. |
| `JD Keyword Notes` | | Notes to yourself |

### ID scheme

Use `CODE-GGG-NNN`, for example `ACME-003-002`:

- `CODE`: a short code for the employer
- `GGG`: an **achievement group**. Bullets that share a group are
  alternative versions of the same achievement, and **only one of them will
  appear in a given CV**.
- `GGG = 000` is reserved for a position's single **summary bullet**, its
  one-line description of duties.
- `NNN`: a sequence number within the group

Any other ID format still works. You just lose the grouping behavior.

### Tips

- Write **many specific bullets**, not a few general ones. The generator
  does better with more to choose from.
- Include the **exact technology names** that postings use, and add them to
  `Core Skill Tags`.
- Give every position a `-000-` summary bullet. It's what appears when the
  position is shortened to an "Earlier" line.
- On the **Prompts** page, *Build Your Bullet Bank* turns an old CV into
  correctly formatted rows using any AI chat.
- Edit in the **Bullet Bank** page, or **Export** the CSV, edit it in a
  spreadsheet, and **Import** it back. A backup is made automatically on
  save.

### Archive sources (optional)

**Settings → Include .tex/.odt archive** also mines bullet text from a
folder of old LaTeX or ODT CVs (paths are set in Settings). This is
lower-quality input than the CSV. It's useful for seeing what you've
written before, not as a replacement for the bullet bank.

---

## `domain.json`: your field's vocabulary

None of the app's code assumes a field. Tuning for *your* field lives here.
Every key is optional, and an empty file works, just without tuning.

```json
{
  "keyword_weights": {
    "kubernetes": 9, "ansible": 8, "linux": 5, "python": 6, "slurm": 9
  },
  "skill_categories": [
    {"label": "Automation", "keywords": ["ansible", "terraform", "python"]},
    {"label": "Platforms",  "keywords": ["linux", "kubernetes", "openstack"]}
  ],
  "skill_display_overrides": { "openstack": "OpenStack", "hpc": "HPC" },
  "position_bullet_targets": {
    "ACME GmbH|||Senior SRE":  {"min": 3, "max": 5},
    "OldCo|||Helpdesk Tech":   {"min": 0, "max": 1, "earlier_threshold": 25,
                                 "droppable": true, "priority_tier": "legacy"}
  },
  "priority_tiers": {
    "legacy": "Older and less relevant: include only if the job specifically needs it."
  },
  "pinned_bullets": [
    {"bullet_id": "ACME-007-001", "position_key": "ACME GmbH|||Senior SRE",
     "trigger_keywords": ["hpc", "slurm"]}
  ]
}
```

| Key | Effect |
|---|---|
| `keyword_weights` | `{keyword: 0–10}`. **This drives rating and bullet scoring.** Start with 20–40 terms from your field. |
| `skill_categories` | Groups for the CV's Skills section. A category appears only when one of its keywords matches the job. |
| `skill_display_overrides` | Correct capitalization for display: `aws` → `AWS` |
| `position_bullet_targets` | Per position (`"Employer\|\|\|Job Title"`, matching the CSV exactly): bullet `min`/`max`, plus optional `earlier_threshold`, `droppable`, `priority_tier`, `priority_note` |
| `priority_tiers` | `{tier: sentence}`. Your own assessment of a group of positions, passed to the LLM. |
| `pinned_bullets` | Always include a particular bullet when the job mentions any of the trigger keywords |

---

## `config.json`: per-profile settings

Usually edited from **Settings**. The keys that matter:

| Key | Example | Notes |
|---|---|---|
| `lang` | `"en"`, `"de"` | Sets the CV section headings and the language the prompts ask for. See [Development → Adding a language](06-development.md#adding-a-cv-language). |
| `language` | `"English"` | Human-readable label |
| `llm_backend` + backend keys | | See [LLM Backends](04-llm-backends.md) |
| `generation_balance` | `0`–`10` | Rule-based ↔ LLM-led (see [Using the App](02-using-the-app.md#4-generate)) |
| `job_retention_days` | `45` | 7–60 |
| `include_tex_odt_archive`, `tex_dir`, `odt_dir` | | Archive mining |

> 🔑 API keys are saved in this file **in plain text**. It's gitignored, but
> treat the profile directory as a secret.

---

## Prompts

### The app's own LLM prompts (`prompts/`)

| File | Used for | Variables |
|---|---|---|
| `rate_job.txt` | Scoring a job from 0 to 10 | `$title $company $location $description $language $blocks_text` |
| `recommend_bullets.txt` | Choosing which positions are full entries and which bullets they get | `$title $company $description $blocks` |
| `generate_cv.txt` | Selecting bullets | `$title $company $description $blocks` |
| `intro.txt` | Two-sentence CV summary | `$title $company $highlights $language` |
| `cover_letter.txt` | Three-paragraph cover letter | `$title $company $description $achievements $language` |

Variables use Python `string.Template` syntax (`$name`). To include a
literal dollar sign, write `$$`.

**Don't change the JSON output shape** a prompt asks for unless you also
change the code that parses it. The wording, tone, and criteria are yours to
change. Each prompt has a **reset** button on the Prompts page.

### External prompts (`external_prompts/`)

These are for copying into ChatGPT, Claude, or any other chat tool. The app
never sends them anywhere itself. Add, remove, or rewrite them however you
like. The list on the page is defined in `_EXTERNAL_PROMPTS` in
`FindJobs.py`.

---

## The CV template

`template.tex.jinja` is ordinary LaTeX with Jinja2, using **LaTeX-friendly
delimiters** so curly braces don't clash:

| Jinja normally | Here |
|---|---|
| `{{ x }}` | `\VAR{x}` |
| `{% for ... %}` | `\BLOCK{for ...}` |
| `{# comment #}` | `\#{comment}` |

**Available variables:**

| Variable | Contents |
|---|---|
| `name`, `title`, `location`, `phone`, `email` | Header fields |
| `nationality_line`, `languages_line` | Ready-formatted lines |
| `photo_path` | Absolute path, or empty |
| `links` | `[{label, url}]` |
| `intro` | LLM-written summary |
| `skill_categories` | `[{label, skills: [...]}]` |
| `full_groups` | Positions shown in full: `{position_title, employer, period, location, summary, bullets}` |
| `earlier_groups` | Positions shortened to one line, same fields |
| `projects`, `education`, `certificates` | From `profile.json` |
| `labels` | Section headings in the profile's language, e.g. `labels.experience` |

All text values are **already LaTeX-escaped**. Don't escape them again.

The template must compile with **plain `pdflatex`**. Use packages that work
with pdfLaTeX, not XeLaTeX/LuaLaTeX-only ones such as `fontspec`. Edit it in
the **Template Editor** (which has a reset) or in any text editor, then
regenerate a CV to check the result.
