"""Thin LLM abstraction layer — routes calls to Anthropic or Ollama.

Supports two backends:
  - anthropic: Claude Haiku via Anthropic API (production, paid)
  - ollama: Local model via Ollama OpenAI-compatible API (development, free)

Backend selection: LLM_BACKEND env var or config.LLM_BACKEND.
"""

import json
import logging
import re

import config as cfg

logger = logging.getLogger(__name__)

# ── Backend dispatch ──────────────────────────────────────────────────


def chat_json(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    api_key: str | None = None,
    model: str | None = None,
    cache_system: bool = False,
) -> dict | None:
    """Send prompt, get parsed JSON response. Routes to configured backend.

    `model` overrides the backend's default model — used for per-call-site
    pinning (see config.LLM_MODELS). Ignored by the Ollama/OpenRouter dev
    backends, which always use their own configured model.
    `cache_system` marks the system prompt as an Anthropic ephemeral cache
    breakpoint. No effect below Anthropic's minimum cacheable prompt length
    (1024 tokens for Sonnet/Opus, 2048 for Haiku) or on non-Anthropic backends.

    Returns parsed dict on success, None on failure.
    """
    backend = getattr(cfg, "LLM_BACKEND", "anthropic")
    if backend == "ollama":
        return _chat_ollama(prompt, system, max_tokens)
    elif backend == "openrouter":
        return _chat_openrouter(prompt, system, max_tokens)
    else:
        return _chat_anthropic(prompt, system, max_tokens, api_key, model, cache_system)


def chat_json_async(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    api_key: str | None = None,
    model: str | None = None,
    cache_system: bool = False,
):
    """Async version — returns a coroutine. For llm_parser.py compatibility.

    See chat_json() for `model`/`cache_system` semantics.
    """
    import asyncio
    backend = getattr(cfg, "LLM_BACKEND", "anthropic")
    if backend == "ollama":
        return _chat_ollama_async(prompt, system, max_tokens)
    elif backend == "openrouter":
        return _chat_openrouter_async(prompt, system, max_tokens)
    else:
        return _chat_anthropic_async(prompt, system, max_tokens, api_key, model, cache_system)


# ── Anthropic backend ────────────────────────────────────────────────


def _system_param(system: str, cache_system: bool):
    """Build the `system` kwarg for the Anthropic SDK, optionally marking
    it as an ephemeral cache breakpoint."""
    if system and cache_system:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    return system


def _chat_anthropic(
    prompt: str,
    system: str,
    max_tokens: int,
    api_key: str | None,
    model: str | None = None,
    cache_system: bool = False,
) -> dict | None:
    """Call Claude via Anthropic API (sync)."""
    import anthropic

    key = api_key or cfg.ANTHROPIC_API_KEY
    if not key:
        logger.warning("No Anthropic API key — skipping LLM call")
        return None

    resolved_model = model or getattr(cfg, "LLM_MODEL", "claude-haiku-4-5-20251001")
    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=resolved_model,
            max_tokens=max_tokens,
            system=_system_param(system, cache_system),
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()
        return _parse_json(result_text)
    except Exception as e:
        logger.warning("Anthropic LLM call failed: %s", e)
        return None


async def _chat_anthropic_async(
    prompt: str,
    system: str,
    max_tokens: int,
    api_key: str | None,
    model: str | None = None,
    cache_system: bool = False,
) -> dict | None:
    """Call Claude via Anthropic API (async)."""
    import anthropic

    key = api_key or cfg.ANTHROPIC_API_KEY
    if not key:
        logger.warning("No Anthropic API key — skipping LLM call")
        return None

    resolved_model = model or getattr(cfg, "LLM_MODEL", "claude-haiku-4-5-20251001")
    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        response = await client.messages.create(
            model=resolved_model,
            max_tokens=max_tokens,
            system=_system_param(system, cache_system),
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()
        return _parse_json(result_text)
    except Exception as e:
        logger.warning("Anthropic async LLM call failed: %s", e)
        return None


# ── Ollama backend ───────────────────────────────────────────────────


def _chat_ollama(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call local Ollama model via OpenAI-compatible API (sync)."""
    from openai import OpenAI

    base_url = getattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434/v1/")
    model = getattr(cfg, "OLLAMA_MODEL", "qwen2.5:7b")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = OpenAI(base_url=base_url, api_key="ollama")
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            # Retry once with explicit JSON instruction appended
            logger.debug("Ollama JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("Ollama LLM call failed: %s", e)
        return None


async def _chat_ollama_async(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call local Ollama model via OpenAI-compatible API (async)."""
    from openai import AsyncOpenAI

    base_url = getattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434/v1/")
    model = getattr(cfg, "OLLAMA_MODEL", "qwen2.5:7b")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = AsyncOpenAI(base_url=base_url, api_key="ollama")
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("Ollama async JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("Ollama async LLM call failed: %s", e)
        return None


# ── OpenRouter backend ──────────────────────────────────────────────


def _chat_openrouter(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call OpenRouter model via OpenAI-compatible API (sync)."""
    from openai import OpenAI

    api_key = getattr(cfg, "OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("No OpenRouter API key — skipping LLM call")
        return None

    base_url = getattr(cfg, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = getattr(cfg, "OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("OpenRouter JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("OpenRouter LLM call failed: %s", e)
        return None


async def _chat_openrouter_async(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call OpenRouter model via OpenAI-compatible API (async)."""
    from openai import AsyncOpenAI

    api_key = getattr(cfg, "OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("No OpenRouter API key — skipping LLM call")
        return None

    base_url = getattr(cfg, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = getattr(cfg, "OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("OpenRouter async JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("OpenRouter async LLM call failed: %s", e)
        return None


# ── JSON parsing ─────────────────────────────────────────────────────


def _parse_json(text: str) -> dict | None:
    """Parse JSON from LLM response, stripping markdown fences."""
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return {"items": result}  # Wrap list in dict for consistency
        return None
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.debug("Failed to parse JSON from LLM response: %.200s", text)
        return None
