"""
LLM Plugin — job rating and analysis.

Default backend: "stub" (keyword scoring, no external API needed).

Which backend is active, and every backend's credentials/model/host, are
all part of the ACTIVE PROFILE's own settings (Settings page -> LLM
Connection, saved to that profile's config.json via profile.llm_config()/
profile.save_llm_config()) -- not global process env vars, so different
profiles can use entirely different LLM setups without interfering with
each other:
    anthropic  -- needs an API key
    openai     -- needs an API key
    ollama     -- needs Ollama running somewhere on your network (host + model)
    custom     -- any OpenAI-compatible endpoint: self-hosted, a proxy, a
                  third-party provider (base URL + optional key + model)

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
import profile as profile_mod
import re
from pathlib import Path
from string import Template

import cv_bank

BASE = Path(__file__).parent

def current_backend() -> str:
    # read fresh each call (not cached at import) so a settings-page save
    # or a profile switch takes effect immediately, no restart needed
    return profile_mod.llm_config()["llm_backend"]

# Was its own hardcoded copy of cv_bank's keyword-weight vocabulary --
# had quietly drifted out of sync (missing nfs/iscsi/fiber channel/fibre
# channel). Now the same active-profile-scoped source cv_bank.py uses,
# so job rating and CV generation always agree on what a keyword is
# worth and neither depends on Linux/DevOps vocabulary being hardcoded
# in two different files.
def _keywords_weighted() -> dict:
    return cv_bank.keyword_weights()


CUSTOM_BACKEND_TIMEOUT = 600  # seconds -- generous on purpose, matches a
# reasoning model's real completion time on a slow/shared self-hosted host


def custom_chat_completion(messages: list, max_tokens: int, cfg: dict = None) -> dict:
    """Raw HTTP POST to a "custom" backend's OpenAI-compatible
    /chat/completions endpoint -- deliberately NOT the `openai` Python
    package. That SDK is just a thin wrapper around this exact JSON-over-
    HTTP contract (the same one every self-hosted/proxy endpoint this
    backend targets already speaks), and importing it purely to reach a
    server that has nothing to do with OpenAI meant a missing `openai`
    pip package broke this backend with an "openai" error even though it
    was never talking to OpenAI at all. Mirrors how the "ollama" backend
    already avoids any SDK dependency, just with `requests` directly.

    Returns the parsed JSON response body (OpenAI chat-completions
    shape) -- callers read response["choices"][0]["message"]["content"]
    and ["finish_reason"] themselves, same fields client.chat.completions.
    create(...).choices[0] used to hand back."""
    import requests as req
    cfg = cfg or profile_mod.llm_config()
    base_url = (cfg["custom_base_url"] or "").rstrip("/")
    resp = req.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {cfg['custom_api_key']}", "Content-Type": "application/json"},
        json={"model": cfg["custom_model"], "max_tokens": max_tokens, "messages": messages},
        timeout=CUSTOM_BACKEND_TIMEOUT,
    )
    if not resp.ok:
        # Surface the endpoint's own error body when it has one (most
        # OpenAI-compatible servers return {"error": {"message": ...}})
        # instead of just requests' generic "401 Client Error" text.
        detail = None
        try:
            detail = resp.json().get("error", {}).get("message")
        except Exception:
            pass
        if detail:
            raise RuntimeError(f"{resp.status_code} from {base_url}: {detail}")
        resp.raise_for_status()
    return resp.json()


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
    if backend == "custom":
        return _custom_rate(job, cv_blocks)
    raise ValueError(f"Unknown LLM_BACKEND={backend!r}. Valid: stub, anthropic, openai, ollama, custom")


def test_connection(backend: str = None) -> dict:
    """Cheap, minimal-token connectivity check for the settings page's
    "Test access" button -- does NOT run a real job rating. Reads every
    backend's credentials/model/host from the active profile's own saved
    settings (profile.llm_config()), same as every real call does -- so
    this always tests exactly what a real generation/rating call would
    actually use, never a different profile's or a stale env var's."""
    cfg = profile_mod.llm_config()
    backend = backend or cfg["llm_backend"]
    try:
        if backend == "stub":
            return {"ok": True, "message": "Stub backend needs no external connection."}

        if backend == "anthropic":
            import anthropic
            if not cfg["anthropic_api_key"]:
                return {"ok": False, "message": "Anthropic API key not set."}
            client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
            model = cfg["anthropic_model"]
            msg = client.messages.create(
                model=model, max_tokens=10,
                messages=[{"role": "user", "content": "Reply with just the word: OK"}],
            )
            return {"ok": True, "message": f"Connected to {model}. Reply: {msg.content[0].text.strip()}"}

        if backend == "openai":
            from openai import OpenAI
            if not cfg["openai_api_key"]:
                return {"ok": False, "message": "OpenAI API key not set."}
            client = OpenAI(api_key=cfg["openai_api_key"])
            model = cfg["openai_model"]
            resp = client.chat.completions.create(
                model=model, max_tokens=10,
                messages=[{"role": "user", "content": "Reply with just the word: OK"}],
            )
            return {"ok": True, "message": f"Connected to {model}. Reply: {resp.choices[0].message.content.strip()}"}

        if backend == "ollama":
            import requests as req
            host = cfg["ollama_host"]
            model = cfg["ollama_model"]
            resp = req.get(f"{host}/api/tags", timeout=10)
            resp.raise_for_status()
            tags = [m.get("name") for m in resp.json().get("models", [])]
            if model not in tags and not any(t.startswith(model) for t in tags):
                return {"ok": False, "message": f"Connected to {host}, but model '{model}' isn't pulled there. Available: {', '.join(tags) or '(none)'}"}
            return {"ok": True, "message": f"Connected to {host}. Model '{model}' is available."}

        if backend == "custom":
            base_url = cfg["custom_base_url"]
            if not base_url:
                return {"ok": False, "message": "Custom base URL not set."}
            model = cfg["custom_model"]
            if not model:
                return {"ok": False, "message": "Custom model not set."}
            # A reasoning model spends tokens on an internal chain-of-thought
            # (returned separately as message.reasoning_content, not part of
            # this raw JSON response's "content" field) before it ever
            # starts the visible answer -- a small max_tokens can cut
            # generation off entirely mid-reasoning (finish_reason="length"),
            # leaving content=None even though the call itself succeeded.
            # "Min. tokens per response" (settings page) is the same floor
            # every other custom-backend call uses -- see generate_cv.py's
            # _call_llm for why extending the budget is the fix here
            # (reasoning-disable params tested against a real reasoning
            # model and none were honored by this kind of proxy).
            data = custom_chat_completion(
                [{"role": "user", "content": "Reply with just the word: OK"}],
                cfg["custom_min_tokens"], cfg,
            )
            choice = data["choices"][0]
            content = choice.get("message", {}).get("content")
            if not content:
                finish = choice.get("finish_reason")
                return {"ok": False, "message": f"Connected to {base_url} ({model}), but got no answer text back "
                        f"(finish_reason={finish!r}) -- if this is a reasoning model, try raising "
                        f"\"Min. tokens per response\" in Settings."}
            return {"ok": True, "message": f"Connected to {base_url} ({model}). Reply: {content.strip()}"}

        return {"ok": False, "message": f"Unknown backend: {backend}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ── stub ───────────────────────────────────────────────────────────────────────

def _stub_rate(job: dict, cv_blocks: list) -> dict:
    text = " ".join(filter(None, [
        job.get("title"), job.get("description"), job.get("keywords_raw")
    ])).lower()

    kw_weights = _keywords_weighted()
    matched, total_w = [], 0
    max_w = sum(kw_weights.values())
    for kw, w in kw_weights.items():
        if kw in text:
            matched.append(kw)
            total_w += w

    # Scale to 0-10 with a generous multiplier so even partial matches score well
    score = min(10.0, round(total_w / max(max_w, 1) * 10 * 4.0, 1))

    block_hits = []
    for b in cv_blocks:
        bs = sum(kw_weights.get(kw.lower(), 2)
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
    template = Template((profile_mod.prompts_dir() / "rate_job.txt").read_text(encoding="utf-8"))
    return template.safe_substitute(
        title=job.get("title"), company=job.get("company"),
        location=job.get("location"), language=job.get("language"),
        description=desc, blocks_text=blocks_text,
    )


def _parse_json_response(text: str) -> dict:
    if not text:
        # A reasoning model can burn its whole max_tokens budget on its
        # internal chain-of-thought (returned separately, not part of this
        # field) and get cut off before ever writing the visible answer --
        # content comes back None/empty, not malformed JSON. Distinct,
        # actionable message rather than an AttributeError on .strip().
        raise ValueError("LLM returned no answer text (empty/None content) -- "
                          "if this is a reasoning model, it may need a larger max_tokens budget.")
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
    """Requires: pip3.12 install anthropic --break-system-packages, and an
    API key saved on the active profile (Settings -> LLM Connection)."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("Run: pip3.12 install anthropic --break-system-packages")
    cfg = profile_mod.llm_config()
    if not cfg["anthropic_api_key"]:
        raise RuntimeError("Set an Anthropic API key (Settings -> LLM Connection) to use LLM_BACKEND=anthropic")

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    msg = client.messages.create(
        model=cfg["anthropic_model"],
        max_tokens=600,
        messages=[{"role": "user", "content": _build_prompt(job, cv_blocks)}],
    )
    result = _parse_json_response(msg.content[0].text)
    result["backend"] = "anthropic"
    return result


# ── OpenAI ─────────────────────────────────────────────────────────────────────

def _openai_rate(job: dict, cv_blocks: list) -> dict:
    """Requires: pip3.12 install openai --break-system-packages, and an
    API key saved on the active profile (Settings -> LLM Connection)."""
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("Run: pip3.12 install openai --break-system-packages")
    cfg = profile_mod.llm_config()
    if not cfg["openai_api_key"]:
        raise RuntimeError("Set an OpenAI API key (Settings -> LLM Connection) to use LLM_BACKEND=openai")

    client = OpenAI(api_key=cfg["openai_api_key"])
    resp = client.chat.completions.create(
        model=cfg["openai_model"],
        max_tokens=600,
        messages=[{"role": "user", "content": _build_prompt(job, cv_blocks)}],
    )
    result = _parse_json_response(resp.choices[0].message.content)
    result["backend"] = "openai"
    return result


# ── Custom (any OpenAI-compatible endpoint) ────────────────────────────────────

def _custom_rate(job: dict, cv_blocks: list) -> dict:
    """A freeform slot for any self-hosted or third-party server that
    speaks the OpenAI chat-completions API shape (POST /v1/chat/completions,
    Bearer auth) -- a proxy, a local inference server, anything that isn't
    literally api.openai.com. Base URL/key/model are all user-supplied
    (settings page), nothing about this is provider-specific -- see
    custom_chat_completion() for why this doesn't use the `openai` package."""
    cfg = profile_mod.llm_config()
    base_url = cfg["custom_base_url"]
    if not base_url:
        raise RuntimeError("Set a custom base URL (Settings -> LLM Connection) to use LLM_BACKEND=custom")
    model = cfg["custom_model"]
    if not model:
        raise RuntimeError("Set a custom model (Settings -> LLM Connection) to use LLM_BACKEND=custom")

    # "Min. tokens per response" floor (settings page, default 4000) -- a
    # reasoning model (this backend's whole point is being provider-
    # agnostic, including self-hosted reasoning models) spends tokens on
    # chain-of-thought before the actual JSON answer, so it needs real
    # headroom to not get cut off mid-reasoning (see
    # _parse_json_response's None guard for what happens if a model
    # still exhausts this, and generate_cv.py's _call_llm for the same
    # floor used everywhere else on this backend).
    data = custom_chat_completion(
        [{"role": "user", "content": _build_prompt(job, cv_blocks)}],
        cfg["custom_min_tokens"], cfg,
    )
    result = _parse_json_response(data["choices"][0].get("message", {}).get("content"))
    result["backend"] = "custom"
    return result


# ── Ollama (local / OpenStack VM) ──────────────────────────────────────────────

def _ollama_rate(job: dict, cv_blocks: list) -> dict:
    """
    Runs against any Ollama instance on your network, including an OpenStack VM.

    Setup on the Ollama host:
        curl -fsSL https://ollama.ai/install.sh | sh
        ollama pull llama3.2        # or mistral, gemma3, etc.
        # Expose the API (binds to 0.0.0.0 on port 11434 by default)

    Host/model are set on the active profile (Settings -> LLM Connection),
    not environment variables.
    """
    import requests as req

    cfg = profile_mod.llm_config()
    host  = cfg["ollama_host"]
    model = cfg["ollama_model"]

    resp = req.post(
        f"{host}/api/generate",
        json={"model": model, "prompt": _build_prompt(job, cv_blocks), "stream": False},
        timeout=cfg["ollama_timeout"],
    )
    resp.raise_for_status()
    result = _parse_json_response(resp.json().get("response","{}"))
    result["backend"] = f"ollama/{model}"
    return result


def list_ollama_loaded() -> dict:
    """What's currently resident in the active profile's Ollama host's
    VRAM right now (GET /api/ps) -- each entry's own "size_vram" is a
    live read of actual memory use, useful for spotting exactly the kind
    of contention (multiple models, or another process entirely, splitting
    the GPU) that silently turns a normal-length generation into a
    multi-minute one."""
    import requests as req
    host = profile_mod.llm_config()["ollama_host"]
    resp = req.get(f"{host}/api/ps", timeout=10)
    resp.raise_for_status()
    return resp.json().get("models", [])


def unload_ollama_models() -> dict:
    """Evicts every model currently loaded on the active profile's Ollama
    host, freeing its VRAM for the next generation -- Ollama's own
    unload mechanism (POST /api/generate with keep_alive=0 and no
    prompt unloads that one model immediately, per-model since there's
    no single "unload everything" endpoint). Only touches models Ollama
    itself loaded; can't see or affect an unrelated process (e.g. a
    stray python3/PyTorch job) also using that GPU -- that's only
    fixable from a terminal on the host itself, no HTTP API reaches it."""
    import requests as req
    host = profile_mod.llm_config()["ollama_host"]
    try:
        loaded = list_ollama_loaded()
    except Exception as e:
        return {"ok": False, "message": f"Couldn't reach {host}: {e}"}
    if not loaded:
        return {"ok": True, "message": f"Nothing loaded on {host} -- VRAM already clear (from Ollama's side)."}

    unloaded, failed = [], []
    for m in loaded:
        name = m.get("name") or m.get("model")
        try:
            resp = req.post(f"{host}/api/generate", json={"model": name, "keep_alive": 0}, timeout=15)
            resp.raise_for_status()
            unloaded.append(name)
        except Exception as e:
            failed.append(f"{name} ({e})")

    if failed:
        return {"ok": False, "message": f"Unloaded {', '.join(unloaded) or '(none)'}; failed: {', '.join(failed)}"}
    return {"ok": True, "message": f"Unloaded from {host}: {', '.join(unloaded)}."}
