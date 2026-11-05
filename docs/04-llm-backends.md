# 4. LLM Backends

The app uses an LLM for four things: **rating** jobs, deciding **bullet
placement**, writing the **CV intro**, and writing the **cover letter**.
You choose the backend per profile, under **Settings → LLM Connection**.

← [Profiles & Content](03-profiles-and-content.md) · Next: [Customizing Harvest →](05-customizing-harvest.md)

---

## Choosing a backend

| Backend | Cost | Quality | Privacy | Setup |
|---|---|---|---|---|
| `stub` | Free | Keyword overlap only. The intro and cover letter come from fixed templates. | Nothing leaves your machine | None. This is the default. |
| `ollama` | Free (your hardware) | Good with 8B+ models, better with larger ones | Stays on your machine or LAN | Install Ollama and pull a model |
| `anthropic` | Pay per token | Excellent | Sent to Anthropic | API key |
| `openai` | Pay per token | Excellent | Sent to OpenAI | API key |
| `custom` | Varies | Varies | Depends on the endpoint | Base URL, model, optional key |

A practical setup is **`stub` while you build your bullet bank**, then
**`ollama`** for bulk overnight rating, and a hosted model if you want the
best cover letters.

Settings are stored in the profile's `config.json` and read **on every
call**. Saving a change or switching profiles takes effect immediately,
with no restart. Two profiles can use different backends at the same
time.

Click **Test access** after saving. It sends a tiny request (around 10
tokens) and reports whether the backend works.

## stub

No setup. Rating is a weighted keyword match between the job text and your
bullet bank, using `keyword_weights` from `domain.json`. It also flags
language requirements and recruiter postings. **The score is only as good
as your `keyword_weights`.**

## Ollama (local)

```bash
# on the GPU machine
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2          # small and fast
ollama pull qwen2.5:14b       # better quality if you have the VRAM
```

| Setting | Default | Notes |
|---|---|---|
| Host | `http://localhost:11434` | For a GPU box on your LAN, use `http://<gpu-host>:11434` and start Ollama with `OLLAMA_HOST=0.0.0.0` |
| Model | `llama3.2` | Must match `ollama list`; a tag prefix is enough |
| Timeout | `180` s | Raise it for large models or a shared GPU |

**Unload model** in Settings frees the GPU's VRAM when you're done.

## Anthropic

| Setting | Default |
|---|---|
| API key | Get one from <https://console.anthropic.com/> |
| Model | `claude-haiku-4-5-20251001`: cheap and fast, good for rating at volume. Use a Sonnet- or Opus-class model for better cover letters. |

## OpenAI

| Setting | Default |
|---|---|
| API key | Get one from <https://platform.openai.com/> |
| Model | `gpt-4o-mini` |

## Custom (any OpenAI-compatible endpoint)

For self-hosted servers (vLLM, llama.cpp server, LM Studio, LiteLLM), API
gateways, or third-party providers (OpenRouter, Together, Groq, …).

| Setting | Example |
|---|---|
| Base URL | `http://gpu-box:8000/v1`. The app appends `/chat/completions`. |
| API key | Optional; sent as a Bearer token |
| Model | Whatever the server expects |
| Min. tokens per response | `4000` |

> **Reasoning models** spend tokens thinking before they answer. If **Test
> access** says *"got no answer text back (finish_reason='length')"*, raise
> **Min. tokens per response**.

Requests time out after 600 s, which is deliberately generous for slow
self-hosted hardware.

## Cost control

- **Filter before you rate.** Harvesting returns hundreds of postings, and
  rating everything is the most expensive mistake you can make.
- Jobs that already have a rating are **always skipped** in batch rating.
- A single bulk run is capped at 300 jobs.
- Set **Generation Balance** to 0–2 (rule-based) to skip the placement LLM
  call during CV generation. The intro and cover letter still use the LLM.
- Use cheaper models for rating and keep the larger ones for generating
  CVs for jobs you're actually applying to. Switch models in Settings
  between the two steps.

## Adding a new backend

See [Development → Adding an LLM backend](06-development.md#adding-an-llm-backend).
Often you don't need to: if the provider has an OpenAI-compatible API, use
`custom`.
