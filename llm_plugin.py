"""
LLM Plugin — job rating and analysis.

Default backend: "stub" (keyword scoring, no external API needed).

To switch backends, set the LLM_BACKEND environment variable before
starting app.py:

    export LLM_BACKEND=anthropic    # needs ANTHROPIC_API_KEY
    export LLM_BACKEND=openai       # needs OPENAI_API_KEY
    export LLM_BACKEND=ollama       # needs Ollama running somewhere on your network
                                    # set OLLAMA_HOST=http://<vm-ip>:11434
                                    # set OLLAMA_MODEL=llama3.2  (or any pulled model)

All backends return the same dict:
    {
        "score":        float 0-10,
        "backend":      str,
        "summary":      str,
        "highlights":   [str],   # matched skills / positives
        "block_matches":[str],   # CV block titles that fit
        "concerns":     [str],   # language reqs, recruiter flags, etc.
    }
"""

import json
import os
import re

def current_backend() -> str:
    # read fresh each call (not cached at import) so a settings-page save
    # that updates os.environ takes effect immediately, no restart needed
    return os.environ.get("LLM_BACKEND", "stub")

KEYWORDS_WEIGHTED = {
    "linux":8,"ansible":7,"python":6,"bash":5,"terraform":6,"jenkins":5,
    "docker":5,"kubernetes":6,"vmware":5,"grafana":4,"graylog":4,"icinga":4,
    "loki":4,"prometheus":4,"nginx":4,"gitlab":4,"github actions":4,
    "hpc":9,"cluster":8,"slurm":8,"infiniband":8,"ipmi":7,"pxe":7,"gpu":7,
    "azure":5,"aws":5,"debian":5,"ubuntu":5,"rhel":5,"alma":4,"networking":4,
    "routing":4,"ci/cd":4,"proxmox":6,"openstack":5,"ceph":5,"zfs":4,
    "lustre":7,"gpfs":6,"beegfs":6,"mpi":7,"openmpi":7,"cuda":6,"rdma":7,
    "puppet":4,"chef":4,"saltstack":4,"packer":4,"vault":4,"consul":4,
}


def rate_job(job: dict, cv_blocks: list) -> dict:
    """Rate a job against the candidate's CV blocks."""
    backend = current_backend()
    if backend == "stub":
        return _stub_rate(job, cv_blocks)
    if backend == "anthropic":
        return _anthropic_rate(job, cv_blocks)
    if backend == "openai":
        return _openai_rate(job, cv_blocks)
    if backend == "ollama":
        return _ollama_rate(job, cv_blocks)
    raise ValueError(f"Unknown LLM_BACKEND={backend!r}. Valid: stub, anthropic, openai, ollama")


def test_connection(backend: str = None) -> dict:
    """Cheap, minimal-token connectivity check for the settings page's
    "Test access" button -- does NOT run a real job rating."""
    backend = backend or current_backend()
    try:
        if backend == "stub":
            return {"ok": True, "message": "Stub backend needs no external connection."}

        if backend == "anthropic":
            import anthropic
            if "ANTHROPIC_API_KEY" not in os.environ:
                return {"ok": False, "message": "ANTHROPIC_API_KEY not set."}
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
            msg = client.messages.create(
                model=model, max_tokens=10,
                messages=[{"role": "user", "content": "Reply with just the word: OK"}],
            )
            return {"ok": True, "message": f"Connected to {model}. Reply: {msg.content[0].text.strip()}"}

        if backend == "openai":
            from openai import OpenAI
            if "OPENAI_API_KEY" not in os.environ:
                return {"ok": False, "message": "OPENAI_API_KEY not set."}
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
            resp = client.chat.completions.create(
                model=model, max_tokens=10,
                messages=[{"role": "user", "content": "Reply with just the word: OK"}],
            )
            return {"ok": True, "message": f"Connected to {model}. Reply: {resp.choices[0].message.content.strip()}"}

        if backend == "ollama":
            import requests as req
            host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
            model = os.environ.get("OLLAMA_MODEL", "llama3.2")
            resp = req.get(f"{host}/api/tags", timeout=10)
            resp.raise_for_status()
            tags = [m.get("name") for m in resp.json().get("models", [])]
            if model not in tags and not any(t.startswith(model) for t in tags):
                return {"ok": False, "message": f"Connected to {host}, but model '{model}' isn't pulled there. Available: {', '.join(tags) or '(none)'}"}
            return {"ok": True, "message": f"Connected to {host}. Model '{model}' is available."}

        return {"ok": False, "message": f"Unknown backend: {backend}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ── stub ───────────────────────────────────────────────────────────────────────

def _stub_rate(job: dict, cv_blocks: list) -> dict:
    text = " ".join(filter(None, [
        job.get("title"), job.get("description"), job.get("keywords_raw")
    ])).lower()

    matched, total_w = [], 0
    max_w = sum(KEYWORDS_WEIGHTED.values())
    for kw, w in KEYWORDS_WEIGHTED.items():
        if kw in text:
            matched.append(kw)
            total_w += w

    # Scale to 0-10 with a generous multiplier so even partial matches score well
    score = min(10.0, round(total_w / max(max_w, 1) * 10 * 4.0, 1))

    block_hits = []
    for b in cv_blocks:
        bs = sum(KEYWORDS_WEIGHTED.get(kw.lower(), 2)
                 for kw in b.get("keywords",[]) if kw.lower() in text)
        if bs > 0:
            block_hits.append(b.get("title",""))

    concerns = []
    if job.get("language") == "german":
        concerns.append("German fluency required")
    if job.get("recruiter_flag"):
        concerns.append("Recruiter posting — company identity unclear")
    if not job.get("url"):
        concerns.append("No URL — may be stale")

    return {
        "score":         score,
        "backend":       "stub",
        "summary":       f"Matched {len(matched)} keywords ({total_w} weight pts). Stub scoring — no LLM used.",
        "highlights":    matched[:10],
        "block_matches": block_hits[:5],
        "concerns":      concerns,
    }


# ── shared prompt ──────────────────────────────────────────────────────────────

def _build_prompt(job: dict, cv_blocks: list) -> str:
    blocks_text = "\n".join(
        f"  [{b['id']}] {b['title']}: {b['text']}" for b in cv_blocks
    )
    desc = (job.get("description") or "")[:2500]
    return f"""You are a career coach helping a senior Linux/HPC Administrator (Richard Eseke) find jobs in Germany and the EU.

Job posting:
  Title:    {job.get('title')}
  Company:  {job.get('company')}
  Location: {job.get('location')}
  Language: {job.get('language')}
  Description:
{desc}

Candidate's CV blocks:
{blocks_text}

Rate this job for the candidate on a scale of 0–10. Consider:
- Technical overlap: Linux, HPC, Ansible, Terraform, Docker, Kubernetes, Python, Bash, Slurm, InfiniBand, IPMI, PXE
- Location / language fit: Germany or EU English-first roles preferred; partial German acceptable
- Company type: research institutions, tech companies, cloud providers
- Career growth and stability

Be a discerning filter, not an encouraging cheerleader -- most real postings should land in the
middle of the scale, not the top. Use these bands as calibration, not just a vague 0-10 feel:
- 0-2: fundamentally mismatched (wrong field entirely, or a hard blocker like required fluent German
  for a non-Germany EU role)
- 3-4: weak fit -- only a couple of skills overlap, most of the role is unrelated to the candidate's background
- 5-6: reasonable fit -- solid technical overlap but notable gaps or a role that's more senior/junior/
  different in shape than the candidate's actual experience
- 7-8: strong fit -- most core requirements are directly covered by real experience, location/language fine
- 9-10: reserve for an exceptional, close-to-perfect match -- should be rare, not the default outcome

Respond with ONLY valid JSON, no markdown fences:
{{
  "score":         <float 0-10>,
  "summary":       "<2-3 sentence assessment>",
  "highlights":    ["<matched skill or positive>", ...],
  "block_matches": ["<cv block id>", ...],
  "concerns":      ["<concern or red flag>", ...]
}}"""


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    # Strip ```json ... ``` fences if present
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    raise ValueError(f"No JSON object found in LLM response: {text[:300]}")


# ── Anthropic (Claude) ─────────────────────────────────────────────────────────

def _anthropic_rate(job: dict, cv_blocks: list) -> dict:
    """
    Requires:
        pip3.12 install anthropic --break-system-packages
        export ANTHROPIC_API_KEY=sk-ant-...
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("Run: pip3.12 install anthropic --break-system-packages")
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("Set ANTHROPIC_API_KEY to use LLM_BACKEND=anthropic")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=600,
        messages=[{"role": "user", "content": _build_prompt(job, cv_blocks)}],
    )
    result = _parse_json_response(msg.content[0].text)
    result["backend"] = "anthropic"
    return result


# ── OpenAI ─────────────────────────────────────────────────────────────────────

def _openai_rate(job: dict, cv_blocks: list) -> dict:
    """
    Requires:
        pip3.12 install openai --break-system-packages
        export OPENAI_API_KEY=sk-...
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("Run: pip3.12 install openai --break-system-packages")
    if "OPENAI_API_KEY" not in os.environ:
        raise RuntimeError("Set OPENAI_API_KEY to use LLM_BACKEND=openai")

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        max_tokens=600,
        messages=[{"role": "user", "content": _build_prompt(job, cv_blocks)}],
    )
    result = _parse_json_response(resp.choices[0].message.content)
    result["backend"] = "openai"
    return result


# ── Ollama (local / OpenStack VM) ──────────────────────────────────────────────

def _ollama_rate(job: dict, cv_blocks: list) -> dict:
    """
    Runs against any Ollama instance on your network, including an OpenStack VM.

    Setup on the Ollama host:
        curl -fsSL https://ollama.ai/install.sh | sh
        ollama pull llama3.2        # or mistral, gemma3, etc.
        # Expose the API (binds to 0.0.0.0 on port 11434 by default)

    Environment variables:
        OLLAMA_HOST=http://<vm-ip>:11434   (default: http://localhost:11434)
        OLLAMA_MODEL=llama3.2              (default: llama3.2)
    """
    import requests as req

    host  = os.environ.get("OLLAMA_HOST",  "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "llama3.2")

    resp = req.post(
        f"{host}/api/generate",
        json={"model": model, "prompt": _build_prompt(job, cv_blocks), "stream": False},
        timeout=int(os.environ.get("OLLAMA_TIMEOUT", 180)),
    )
    resp.raise_for_status()
    result = _parse_json_response(resp.json().get("response","{}"))
    result["backend"] = f"ollama/{model}"
    return result
