
# Job Harvester + CV Mapper

## What this is for
This setup is meant for:
1. Building a better local job pipeline for EU / Germany / Berlin
2. Filtering jobs by language, skills, source, and company type
3. Mapping a job description to reusable CV blocks so you can assemble a tailored CV with less rewriting

## Why this approach
The strong sources are:
- Greenhouse public job boards
- Lever public postings
- EURAXESS
- direct company career pages
- manual imports from job alerts

Do **not** make LinkedIn or StepStone scraping your backbone. Use them for alerts and manual import.

## Basic workflow

### 1. Initialize database
```bash
python job_harvester_and_cv_mapper.py init-db jobs.db
```

### 2. Create board lists
Create `boards.txt` for Greenhouse:
```text
openai
canonical
siemens
datadog
```

Create `lever_companies.txt` for Lever:
```text
nvidia
docker
```

### 3. Harvest public ATS jobs
```bash
python job_harvester_and_cv_mapper.py harvest-greenhouse jobs.db boards.txt
python job_harvester_and_cv_mapper.py harvest-lever jobs.db lever_companies.txt
```

### 4. Import manual CSV exports
Use this for EURAXESS alerts, direct company exports, or job boards:
```bash
python job_harvester_and_cv_mapper.py import-csv jobs.db jobs_export.csv
```

### 5. Filter for your target
Example:
```bash
python job_harvester_and_cv_mapper.py filter jobs.db   --region berlin,germany,eu   --language english,partial_german   --min-company-size 100   --source company,direct,institution   --include-keywords linux,ansible,hpc,terraform   --exclude-keywords salesforce,frontend
```

### 6. Map a single job to your CV blocks
```bash
python job_harvester_and_cv_mapper.py map-job jobs.db 42 cv_blocks_template.json
```

That prints the best matching blocks to use in a tailored CV.

## Suggested filters for you

### Linux / Institutional
```bash
--region berlin,germany,eu
--language english,partial_german
--min-company-size 100
--source company,direct,institution
--include-keywords linux,ansible,bash,python,vmware,jenkins
```

### HPC / Research
```bash
--region germany,eu
--language english,partial_german
--source company,direct,institution
--include-keywords hpc,cluster,linux,pxe,ipmi,infiniband,slurm,gpu
```

### Light DevOps / Platform
```bash
--region berlin,germany,eu
--language english,partial_german
--min-company-size 100
--source company,direct
--include-keywords linux,ansible,terraform,docker,jenkins,github actions,python
```

## Important
The point of the block system is not to auto-write everything.
Use it to:
- reuse proven grammar from your existing CV blocks
- pick the best 4–8 blocks for each job
- rewrite only the intro and summary by hand
