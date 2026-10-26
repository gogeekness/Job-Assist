# Setup

To setup and start using Job-Assist.  You need to clone the Repo, install the dependence (There are a few), LateX support.  Optionally: More CVs to pull data from, more the better. To maximize its accuracy having a LLM accessible by API is needed.

If you intend to use **pip** then you will need a Python Environment "enve".  

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python FindJobs.py
```

## Starting out
Start the program `python3 FindJobs.py &` and then access the web-interface `http://localhost:5050`. On first run there's no active profile yet. 

### Profiles 
Profile switcher (top-right of the nav bar) to create one; it use the scaffold from `profiles/_skeleton/` (empty bullet bank, generic domain config, `stub` LLM backend settings).  In the application you can create a new profile. Here is the skeleton version of the profile file.

```json
{
  "language": "English",
  "lang": "en",
  "llm_backend": "stub",
  "ollama_model": "",
  "ollama_host": "http://localhost:11434",
  "job_retention_days": 45,
  "include_tex_odt_archive": false
}
```
Additional keywords:

* job_retention_days: 30 (default)
* include_tex_odt_archive: false
* open_api_key:
* anthropic_api_key:
* custom_base_url:
* custom_api_key:
* custom_model:

An example profile directory tree
```bash
< profile name >
├── external_prompts
├── generated_cover_letters
├── generated_cvs
└── prompts
```

### Domain file
Then fill in your real details from the Settings and Bullet Bank
pages. Everything under `profiles/<id>/` is gitignored — real bullet
content, career history, and any API keys you save there never enter
version control.  The `anthropic`/`openai` packages (needed for those two rating/generation
backends) only live in the venv.

