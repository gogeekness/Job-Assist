# 2. Using the App

The daily workflow, page by page. It assumes you've finished
[Getting Started](01-getting-started.md) and have a profile with a filled-in
bullet bank.

← [Getting Started](01-getting-started.md) · Next: [Profiles & Content →](03-profiles-and-content.md)

---

## The pipeline at a glance

```
 Harvest ──► Filter ──► Rate (0–10) ──► Generate CV + cover letter ──► Review/edit ──► Approve
  jobs.db     sidebar     LLM or stub      bullet bank + template         PDF ⇄ .tex
```

Each step is deliberately cheaper than the next. Harvesting is free,
filtering is instant, rating costs one LLM call per job, and generation
costs several. Narrow the list before you spend anything.

## 1. Harvest

**Dashboard (`/`) → Harvest buttons.** Each source runs in the background, and
its status and count show live.

| Source | What it reaches | Notes |
|---|---|---|
| **Greenhouse** | Every company listed in `boards.txt` | Official public API. Add one board token per line, e.g. `gitlab` for `boards.greenhouse.io/gitlab`. Edit it in Settings. |
| **Arbeitnow** | Arbeitnow's job board (Germany/EU focus) | Free API, no authentication. |
| **EURAXESS** | EU research and academic jobs | RSS feed. |
| **JobSpy** | LinkedIn, Indeed, Glassdoor, ZipRecruiter | Scraping, so it's slower and can be rate-limited. Search terms and countries are set in code; see [Customizing Harvest](05-customizing-harvest.md). |
| **LinkedIn (alt)** | LinkedIn guest API | Throttled backup in case JobSpy's LinkedIn results come back thin. |

**Can't find a posting?** Click **Blank Job** and paste in the title,
company, location, and description. A manual job is rated and generated
like any other, and it skips the automatic language exclusion.

Old postings are purged automatically after a retention window (Settings,
7–60 days, default 45), along with their per-profile state.

## 2. Filter

The Jobs sidebar filters by:

- **Region / country / city**: Germany vs. other EU vs. elsewhere
- **Language**: posting detected as English, German, partial German, or
  foreign
- **ATS / source**: where the posting came from
- **Profession**: a coarse title-based category
- **Keywords**: `AND` / `OR` / `NOT` lists over title and description

The **workflow filter bar** has one-click views that follow the order you
work in: *Unrated → Rated → Ready to Generate → Not Generated → Generated*,
plus *Unviewed* and *Has Notes*.

Click **Save preset** to store any filter combination under a name. Filters
stay in place while you move between pages.

## 3. Rate

Each rating returns a **0–10 fit score**, a summary, highlights (matched
skills), and concerns (language requirements, recruiter postings, seniority
mismatch, and so on).

- **One job**: open it and click **Rate**.
- **Several jobs**: tick checkboxes in the list and click **Rate selected**.
- **Everything unrated in the current filter**: **Rate all**, capped at
  300 per run.

Jobs that already have a rating are always skipped, so a batch never pays
for the same job twice.

**Overnight rating**: save a filter preset, then:

```bash
scripts/install_nightly_rate_cron.sh "My Preset Name" 23:00
```

This rates unrated jobs in that preset each night, working in
45-minutes-on / 15-minutes-off cycles so the GPU isn't busy all night. The
log goes to `backups/nightly_rate.log`.

## 4. Generate

Click **Generate CV** on a job, or select several and click **Generate
selected**. For each job the app:

1. Scores every bullet in your bank against the posting's text.
2. Decides, position by position, whether that job gets a **full entry**
   (several bullets) or a one-line **"Earlier"** mention.
3. Picks which bullets to include, and which wording variant of each.
4. Asks the LLM for a two-sentence intro and a three-paragraph cover letter.
5. Renders your profile's `template.tex.jinja` and compiles it with
   `pdflatex`. If the result is too long, it trims and recompiles.

**Generation never invents content.** Every bullet comes from your bank.
The LLM only chooses among your bullets and writes the intro and cover
letter.

**Settings → Generation Balance** (0–10) controls how much of step 2–3 is
left to the LLM:

| Dial | Mode | Behavior |
|---|---|---|
| 0–2 | **Rule-based** | No LLM call for placement. Uses your `domain.json` targets and keyword scores. Deterministic, so the same job always gives the same CV. |
| 3–7 | **Hybrid** *(default)* | The LLM chooses, and safety nets apply: priority tiers, a fallback when the LLM picks nothing, and max-bullet caps. |
| 8–10 | **LLM-led** | The LLM's placement is used as-is and the priority-tier override is turned off. Gives more variety from job to job. |

## 5. Review, edit, approve

The **CV Review** page shows the compiled PDF beside the editable `.tex`
source.

- Fix wording directly in the source and click **Recompile**.
- Edit the cover letter text and save it.
- **Copy .tex** to finish the CV in your own editor, such as VS Code or
  Overleaf.
- Click **Approve** when it's ready to send. The workflow bar tracks
  approval status.

Output files are under `profiles/<id>/generated_cvs/` and
`generated_cover_letters/`.

## Other pages

| Page | Purpose |
|---|---|
| **Dashboard** (`/`) | Harvest buttons, counts, sources, recent harvest log |
| **Bullet Bank** | Spreadsheet-style editor for your bullets, with CSV import and export |
| **Profile** | Name, contact details, education, certificates, languages, links |
| **Prompts** | Edit the LLM prompts the app uses, plus *external* prompts to copy into any AI chat (JD analysis, interview prep, salary research, cold outreach, follow-up, …) |
| **Template Editor** | Edit this profile's LaTeX CV template, with a reset to the skeleton version |
| **Settings** | LLM connection, generation balance, Greenhouse boards, retention, archive paths |
| **Export** | Filtered jobs as CSV; skill-frequency histogram as CSV |

## Working in two languages

To apply in both English and German, create **two profiles**, e.g.
`JANE-EN-01` and `JANE-DE-01`. Each has its own bullet bank in its own
language, its own prompts, and its own `"lang"` in `config.json`. Both see
the **same harvested jobs** (`jobs.db` is shared), but ratings, notes, and
generated CVs are separate per profile. Switch between them from the nav
bar.
