"""LLM providers.

Internally everything uses the Anthropic Messages format (content blocks). History is append-only:
assistant content is stored exactly as returned (thinking blocks included) and replayed unchanged.

Providers:
  anthropic      - Claude via the official SDK (default; adaptive thinking, prompt caching, fallbacks)
  openai_compat  - any OpenAI-compatible endpoint (Ollama, vLLM, OpenRouter, ...) for local/other models
  mock           - scripted responses for tests
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

# $ per 1M tokens: (input, output, cache_read). Cache writes are billed ~1.25x input.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
# Models that take `fallbacks: "default"` (server-side refusal fallback).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# Older models that do not take adaptive thinking / effort.
LEGACY_MODELS = {"claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5"}


class LLMError(Exception):
    pass


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    refusal: str = ""
    raw_usage: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


def estimate_cost(model: str, inp: int, out: int, cache_read: int, cache_write: int) -> float:
    p = PRICES.get(model)
    if not p:
        return 0.0
    return (inp * p[0] + out * p[1] + cache_read * p[2] + cache_write * p[0] * 1.25) / 1_000_000


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, effort: str, enable_fallbacks: bool, enable_web_search: bool):
        import anthropic  # imported lazily so the mock/openai paths don't need it

        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic()
        self.effort = effort
        self.enable_fallbacks = enable_fallbacks
        self.enable_web_search = enable_web_search

    def server_tools(self, model: str) -> list[dict]:
        if self.enable_web_search and model not in LEGACY_MODELS:
            return [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
        return []

    async def complete(self, model: str, system: str, messages: list[dict], tools: list[dict],
                       max_tokens: int = 16000) -> LLMResponse:
        a = self._anthropic
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools + self.server_tools(model),
            "cache_control": {"type": "ephemeral"},  # automatic prefix caching
        }
        if model not in LEGACY_MODELS:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": self.effort}
        try:
            if self.enable_fallbacks and model in FALLBACK_MODELS:
                resp = await self.client.beta.messages.create(
                    **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            else:
                resp = await self.client.messages.create(**kwargs)
        except a.RateLimitError as e:
            raise LLMError(f"rate limited: {e}") from e
        except a.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"connection error: {e}") from e

        u = resp.usage
        inp = u.input_tokens or 0
        out = u.output_tokens or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        refusal = ""
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            refusal = getattr(details, "explanation", "") or "The model declined this request."
        return LLMResponse(
            content=[b.model_dump(mode="json", exclude_none=True) for b in resp.content],
            stop_reason=resp.stop_reason or "end_turn",
            input_tokens=inp, output_tokens=out, cache_read_tokens=cr, cache_write_tokens=cw,
            cost_usd=estimate_cost(model, inp, out, cr, cw),
            refusal=refusal,
        )


class OpenAICompatProvider:
    """For local models (Ollama) or other OpenAI-compatible APIs. Converts to/from Anthropic format."""

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http = httpx.AsyncClient(timeout=300)

    def server_tools(self, model: str) -> list[dict]:
        return []

    @staticmethod
    def _flatten(content: Any) -> str:
        if isinstance(content, str):
            return content
        parts = []
        for b in content or []:
            if b.get("type") == "text":
                parts.append(b["text"])
            elif b.get("type") == "image":
                parts.append("[image omitted]")
        return "\n".join(parts)

    def _convert_messages(self, system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            c = m["content"]
            if m["role"] == "user":
                if isinstance(c, str):
                    out.append({"role": "user", "content": c})
                    continue
                texts = []
                for b in c:
                    if b.get("type") == "tool_result":
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                    "content": self._flatten(b.get("content", ""))})
                    elif b.get("type") == "text":
                        texts.append(b["text"])
                if texts:
                    out.append({"role": "user", "content": "\n".join(texts)})
            elif m["role"] == "assistant":
                blocks = c if isinstance(c, list) else [{"type": "text", "text": c}]
                text = "\n".join(b["text"] for b in blocks if b.get("type") == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                         for b in blocks if b.get("type") == "tool_use"]
                msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
        return out

    async def complete(self, model: str, system: str, messages: list[dict], tools: list[dict],
                       max_tokens: int = 8000) -> LLMResponse:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": self._convert_messages(system, messages),
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}}
                for t in tools]
        try:
            r = await self.http.post(f"{self.base_url}/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"openai_compat error: {e}") from e
        data = r.json()
        msg = data["choices"][0]["message"]
        content: list[dict] = []
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
        for i, call in enumerate(msg.get("tool_calls") or []):
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            content.append({"type": "tool_use", "id": call.get("id") or f"call_{i}",
                            "name": call["function"]["name"], "input": args})
        usage = data.get("usage") or {}
        return LLMResponse(
            content=content or [{"type": "text", "text": ""}],
            stop_reason="tool_use" if msg.get("tool_calls") else "end_turn",
            input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
        )


MockHandler = Callable[[str, list[dict], list[dict]], Awaitable[dict] | dict]


class MockProvider:
    """Test provider: a handler returns {"content": [...], "stop_reason": "..."}."""

    name = "mock"

    def __init__(self, handler: MockHandler | None = None):
        self.handler = handler or (lambda s, m, t: {"content": [{"type": "text", "text": "ok"}],
                                                    "stop_reason": "end_turn"})
        self.calls: list[dict] = []

    def server_tools(self, model: str) -> list[dict]:
        return []

    async def complete(self, model: str, system: str, messages: list[dict], tools: list[dict],
                       max_tokens: int = 1000) -> LLMResponse:
        self.calls.append({"system": system, "messages": json.loads(json.dumps(messages)), "tools": tools})
        r = self.handler(system, messages, tools)
        if hasattr(r, "__await__"):
            r = await r
        return LLMResponse(content=r["content"], stop_reason=r.get("stop_reason", "end_turn"),
                           input_tokens=100, output_tokens=20, cost_usd=r.get("cost_usd", 0.001))


def make_provider(settings) -> Any:
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings.effort, settings.enable_fallbacks, settings.enable_web_search)
    if settings.llm_provider == "openai_compat":
        return OpenAICompatProvider(settings.openai_base_url, settings.openai_api_key)
    if settings.llm_provider == "mock":
        return MockProvider()
    raise ValueError(f"unknown TROUPE_LLM_PROVIDER: {settings.llm_provider}")
