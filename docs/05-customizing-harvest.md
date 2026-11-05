# 5. Customizing Harvest

Out of the box, harvesting is set up for one particular search: **Linux,
DevOps, and SRE roles in Germany, with English-language postings from
Spain, Portugal, Italy, and Malta**. This page explains how to point it at
your own market.

← [LLM Backends](04-llm-backends.md) · Next: [Development →](06-development.md)

---

> **Status:** unlike everything in [Profiles & Content](03-profiles-and-content.md),
> harvest settings are **still Python constants in `FindJobs.py`**. Moving
> them into an editable `harvest_config.json` is planned on the `dev`
> branch. Until that lands, you'll be editing code, but only constants, as
> described below.

## Harvest is shared across profiles

`jobs.db` (harvested postings) is **shared by every profile**. Only
ratings, notes, and generated output are per-profile. Harvest settings are
therefore global: changing the search terms changes them for every
profile.

## Knobs you can turn without coding

| What | Where |
|---|---|
| Which companies the Greenhouse harvester checks | `boards.txt`, or **Settings → Greenhouse boards**. One token per line: `gitlab` for `boards.greenhouse.io/gitlab`. |
| How long postings are kept | **Settings → Retention**, 7–60 days |
| Which jobs you look at | Filters and presets on the Jobs page |

Finding Greenhouse tokens: open a company's careers page. If links go to
`boards.greenhouse.io/<token>` or `job-boards.greenhouse.io/<token>`, that
`<token>` is what to add.

## Changing search terms and countries (JobSpy)

In `FindJobs.py`:

```python
IT_SEARCH_LOCATIONS = [
    ("Germany",  "germany"),     # (location text for the search, Indeed country code)
    ("Spain",    "spain"),
    ...
]
IT_SEARCH_TERMS = ["Linux system administrator", "DevOps engineer Linux",
                   "Site Reliability Engineer", "Platform Engineer Linux"]
```

JobSpy runs **every term × every location × 4 sites** with 50 results
each. Each one you add multiplies run time, so a few precise terms beat
many broad ones. Indeed country codes are listed in
`jobspy/jobspy/model.py` (`class Country`).

## Changing the LinkedIn (alt) harvester

```python
LINKEDIN_GEO_IDS = {"Germany": 101282230, "Spain": 105646813}
```

To find the geoId for a country or city, search LinkedIn Jobs for that
location in a browser and copy the `geoId=` value from the URL.

## Region and language classification

Each posting is tagged with a region, a detected language, and a
profession. These constants control the tagging:

| Constant | Purpose |
|---|---|
| `GERMANY_CITIES` | Cities that count as the "home" region (`region_group = germany`) |
| `EU_COUNTRIES`, `ISO_COUNTRY_CODES` | EU membership. Already general; leave as is. |
| `LANGUAGE_HINTS` | Phrases that mark a posting as requiring German, partially German, or English-friendly |
| `ENGLISH_STOPWORDS`, `FOREIGN_STOPWORDS` | Detect what language the posting itself is written in |
| `IT_TITLE_HINTS` | Rough "is this an IT job?" pre-filter |
| `PROFESSION_CATEGORIES` | Title → profession buckets for the sidebar filter |

**Hard exclusion rule** (`upsert_job()`): a posting in the EU but outside
the home region **that is written in a language other than English is
dropped at harvest time**, not just flagged. If you're searching in your
own language in another country, edit or remove this rule:

```python
if not skip_filters and rec.get("region_group") == "eu" and rec.get("language") == "foreign":
    return None
```

### Example: Netherlands, Dutch-speaking

1. Replace the city names in `GERMANY_CITIES` with Dutch cities (the name
   is historical; it means "home region").
2. Set `IT_SEARCH_LOCATIONS = [("Netherlands", "netherlands")]`.
3. Add `"Netherlands": <geoId>` to `LINKEDIN_GEO_IDS`.
4. Rewrite `LANGUAGE_HINTS["german"]` / `["partial_german"]` as Dutch
   phrases (`"vloeiend nederlands"`, …).
5. Remove Dutch words from `FOREIGN_STOPWORDS` so Dutch postings aren't
   treated as foreign.
6. Edit the `"german fluency"` concern in `llm_plugin.py`'s `_stub_rate`
   and the wording in your profile's `rate_job.txt` prompt.

## Adding a new job source

Every harvester follows the same pattern. Copy `_do_harvest_arbeitnow()`
as a starting point:

```python
def _do_harvest_mysource():
    source = "mysource"
    _set_status(source, "running")
    conn = db.connect_db()
    lid = _log_start(conn, source)
    count, error = 0, None
    try:
        for item in fetch_somehow():
            combined = f"{item['title']} {item['company']} {item['location']} {item['desc']}"
            city, country, rg = parse_location(item["location"])
            upsert_job(conn, {
                "source": "mysource", "source_type": "board", "ats": "mysource",
                "external_id": str(item["id"]),
                "company": item["company"], "company_size": None,
                "title": item["title"], "location": item["location"],
                "city": city, "country": country, "region_group": rg,
                "language": detect_language(combined),
                "international_flag": detect_international(combined),
                "recruiter_flag": 0, "direct_company_flag": 1,
                "url": item["url"], "date_posted": item.get("date"),
                "description": strip_html(item["desc"]),
                "keywords_raw": extract_keywords(combined),
                "normalized_text": normalize_space(combined.lower()),
                "created_at": now_iso(),
            })
            count += 1
        conn.commit()
    except Exception as e:
        error = str(e)
    _log_finish(conn, lid, count, error)
    conn.close()
    _set_status(source, "done", count, error)
```

Then:

1. Register it in `_HARVEST_FNS`: `"mysource": _do_harvest_mysource,`
2. Add a button to the Dashboard (`templates/index.html`): copy an existing
   `.harvest-btn` element and set `data-source="mysource"`. Clicking it
   `POST`s to `/harvest/mysource`.

Duplicate postings are ignored automatically (`INSERT OR IGNORE`, keyed on
source + external ID + URL), so running a harvester again is safe.

**Choosing sources:** official or public APIs and RSS (Greenhouse, Lever,
Ashby, Personio, Recruitee, company feeds) are reliable and allowed.
Scraping job boards breaks often and may violate their terms, so keep it
secondary.
