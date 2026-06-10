# Research spike: pluggable LLM providers

**Status:** design only — no behavior change shipped. Claude (Anthropic) remains
the single implemented "brain". This doc captures the design so a future change
can add local / alternative providers (llama.cpp, LM Studio, Ollama, ChatGPT,
Gemini) behind a config switch.

## Why

`ClaudeBrain` (`tidesync_dj/app/claude_brain.py`) is the only decision engine.
Users have asked for a local-LLM / Claude-alternative option (privacy, cost,
offline). The goal is a provider interface so the brain can be swapped without
touching the scheduler.

## Today's surface (what an adapter must implement)

`scheduler.py` calls the brain through four async methods:

| Method | Returns | Used for |
| --- | --- | --- |
| `decide(context) -> DJDecision` | structured JSON (`vibe_reading`, `next_tracks[]`, `mood_shift`, `dj_note`) | every queue refill |
| `plan_set(context) -> SetPlan` | structured JSON (`phases[]`, `arc_note`) | once per session (the long-form arc) |
| `summarize_taste(sample, previous) -> str` | plain text | household taste bootstrap/refresh |
| `summarize_person_taste(signals, previous) -> str` | plain text | per-person background learning |

Two of these (`decide`, `plan_set`) rely on **structured outputs** (a JSON
schema the model must satisfy) and on Anthropic's `output_config` + adaptive
`thinking`/`effort` (`_extra_body`). Those are the parts that differ most across
providers.

## Proposed design

1. **`Brain` protocol** (a `typing.Protocol` or ABC) declaring the four methods
   above. `ClaudeBrain` already matches it.
2. **Config switch** `llm_provider` (default `anthropic`) plus per-provider keys:
   - `anthropic`: `anthropic_api_key`, `claude_model` (exists today).
   - `openai_compatible`: `llm_base_url`, `llm_api_key`, `llm_model`. **One
     adapter covers a lot**: ChatGPT (`https://api.openai.com/v1`), **llama.cpp**
     server, **LM Studio**, and **Ollama** (`/v1` OpenAI-compatible endpoints) —
     they only differ by `base_url`/`model`.
   - `gemini`: `gemini_api_key`, `gemini_model`.
3. **Factory** in `main.py`'s lifespan: build the brain from `llm_provider`
   instead of hard-coding `ClaudeBrain`.

## Gaps to solve per provider

- **Structured output.** Anthropic uses `output_config.format` (JSON schema).
  OpenAI-compatible servers use `response_format={"type":"json_schema",…}` (or
  `json_object` + a schema in the prompt for older/local servers). Gemini uses
  `response_mime_type` + `response_schema`. The adapter must translate
  `DECISION_SCHEMA` / `SET_PLAN_SCHEMA` to the provider's mechanism, with a
  **prompt-enforced JSON fallback** for local servers that don't support schemas
  — `DJDecision`/`SetPlan` already parse defensively (`_parse` returns a safe
  default on error), so a flaky local model degrades gracefully.
- **"Thinking"/effort.** Anthropic-only; other adapters simply omit it.
- **Prompt caching.** Anthropic-specific (the cached system prefix). Other
  providers ignore `cache_control`; cost/latency characteristics differ.
- **Token limits / context.** Local models often have smaller context — the
  per-tick payload (history, queue, set plan) may need trimming for them.

## Recommended first step (future PR)

Add the `Brain` protocol + the **OpenAI-compatible adapter** only (covers
ChatGPT and every local server via `base_url`), keep Anthropic the default, and
gate it behind `llm_provider`. Defer Gemini. Validate with a local llama.cpp /
Ollama server pointed at `llm_base_url`.

## Tracking

Open a GitHub issue ("Pluggable LLM providers") linking this doc so the
investigation lives in the issue tracker alongside git history.
