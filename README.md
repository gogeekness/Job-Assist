# Job-Assist

This is a job search pipeline that: scrapes postings from open sourced job sites, with thousands of jobs. then you can rate the job against the user's (your) information using Key-words or AI (LLM).  Then generate a tailored, CV and Cover letter for the job you like.  Lastly, review, edit, and save PDF/TEX files. 

- You can add as much or as little information as CVs, Cover  letters, Accolades in a form that your or LLM can read.


Everything (bullet bank, LLM settings, CV template, vocabulary) are saved in a **profile**, so you can run entirely separate job-search
identities (e.g. an English and a German language) without each interfering with each other.  Yes, I did use Claude Code for much of the application.

## What It Does

1. **Harvests jobs from multiple sources** 
The application harvests job posting from multiple site.  As the I live in Berlin Germany the focus is European centric.  The Job site are:

- Greenhouse
- Arbeitnow (public APIs)
- EURAXESS (RSS)
- JobSpy (LinkedIn/Indeed/Glassdoor/ZipRecruiter)
- LinkedIn-guest-API -
- Blank Job, to add a job manually 

Well over 5000 jobs will be pulled so the filters are as important as the job sites.  From the image below your can see the sources and Language (Living in Berlin, I need to filter for that).  While I am looking for IT position, many other professions can be searched.
- You can always add more.

![image caption](/home/reseke/Pictures/Find-Job-1.png)

2. **Filters and searches the job list** 
This is where you can find, filter, and rate jobs.
Filtering is by:

- Posting
- Company name
- Already rated or looked at
- Region
- Language
- Profession 
- Has Notes (your notes)
- ATS source (job site)
- Key words
    * multiple words (or/and), excluding words(not)
    
**Workflow filter bar** Create and save any filter combination as a named preset. Filters you apply on the Jobs page persist when you switch to another page and come back — no need to re-filter every time.
**Batch rating** Select multiple jobs to rate to process many jobs to find the best fit for your needs.
**Blank Job** Add a job not listed to rate and generate a CV from.

![image caption](/home/reseke/Pictures/Find-Job-2-jobs_list.png)

3. **Rates every posting against your real CV content** — a 0-10 fit score,
   summary, highlights, and concerns, generated per-job or in bulk. Pick the
   rating backend per profile: free keyword-only `stub` (no API calls at
   all), Anthropic, OpenAI, a local/LAN Ollama install, or any other
   OpenAI-compatible endpoint (self-hosted, a proxy, a third-party
   provider) via the free-form **custom** backend.  When you find the job you what to look at closer, You can select it see the more information about that job.  From here you can see where it is located, language, and from what job site.  There is more information as ID and keywords matching.  There is an option to rate the job by LLM and what it thinks is a good match from your past CVs and information.  From this pane you can go to the website where the job is posted and generate a new CV by keywords or by LLM.  In the settings you can select the ratio between keyword or LLM CV generation.  The CV intro and a basic cover letter is also generated.  

![image caption](/home/reseke/Pictures/Find-Job-3-rated-job.png)

5. **Generates CV & matching Cover Letter** The goal of this application is to create a CV "good enough" to be of use.  You can be modify many settings to get close to the perfect CV, but I have "Good enough" is much more attainable. Instead of taking hours to craft the perfect CV this application gets you 85 - 90% of the way there.  You only need to edit or tweak the output to the specific job's needs. Thus a lot of time is saved and more tailored CVs are written.  The Bullet Bank has all of your jobs and bullet points (work title, what you did, successes, tools used, your accomplishments, etc.), so the new generated CV has your "voice", ordered to match the job you are looking at. In the example below I might change the CV intro.  

- This application uses **LateX** for it generating the CV
    * You don't need to be an expert at Latex, but some knowledge will help (There is a quick and dirty guide here in this repo also).
-  A cover letter is also generated, but it is less refined.  It can serve as a draft to start as it will have a lot of your information in a cover letter structure (intro, body, end).
- There is a CV template that is fully editable as the base for the CV generation.
- the LateX side (right) is fully editable and you can generate the PDF any time.  You can copy the LateX text into another Latex tool if you have a prefered editor. 

![image caption](/home/reseke/Pictures/Job-Finder_CV-generation-4.png)

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
- Claude Code
