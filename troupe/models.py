"""Model presets and runtime model configuration.

The owner picks a provider in the setup wizard (or later in Vault & system). The choice is stored in
the database and the API key in the encrypted vault ("llm_api_key"), so nothing needs editing on the
server and the model can be switched without a restart. Environment variables still work and are
used until the owner saves a choice in the app.
"""
from __future__ import annotations

import json

import httpx

PRESETS = {
    "anthropic": {"label": "Claude", "by": "Anthropic", "kind": "anthropic", "base_url": "",
                  "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
                  "default": "claude-opus-5-5", "key_hint": "sk-ant-…", "keys_url": "https://console.anthropic.com/settings/keys"},
    "openai": {"label": "OpenAI", "by": "OpenAI", "kind": "openai_compat", "base_url": "https://api.openai.com/v1",
               "key_hint": "sk-…", "keys_url": "https://platform.openai.com/api-keys"},
    "qwen": {"label": "Qwen", "by": "Alibaba Cloud Model Studio", "kind": "openai_compat",
             "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
             "alt_base_urls": ["https://dashscope.aliyuncs.com/compatible-mode/v1"],
             "key_hint": "sk-…", "keys_url": "https://modelstudio.console.alibabacloud.com/"},
    "deepseek": {"label": "DeepSeek", "by": "DeepSeek", "kind": "openai_compat", "base_url": "https://api.deepseek.com/v1",
                 "key_hint": "sk-…", "keys_url": "https://platform.deepseek.com/api_keys"},
    "kimi": {"label": "Kimi", "by": "Moonshot AI", "kind": "openai_compat", "base_url": "https://api.moonshot.ai/v1",
             "alt_base_urls": ["https://api.moonshot.cn/v1"], "key_hint": "sk-…", "keys_url": "https://platform.moonshot.ai/"},
    "openrouter": {"label": "OpenRouter", "by": "Many providers, one key", "kind": "openai_compat",
                   "base_url": "https://openrouter.ai/api/v1", "key_hint": "sk-or-…", "keys_url": "https://openrouter.ai/keys"},
    "ollama": {"label": "Ollama", "by": "Local models on your server", "kind": "openai_compat",
               "base_url": "http://127.0.0.1:11434/v1", "no_key": True, "keys_url": "https://ollama.com/library"},
    "custom": {"label": "Other", "by": "Any OpenAI-compatible endpoint", "kind": "openai_compat", "base_url": "",
               "key_hint": "API key"},
}
# Keep tool-capable chat models near the top of long lists; hide embeddings, audio, images and the like.
_SKIP = ("embed", "rerank", "tts", "asr", "audio", "whisper", "image", "vision-ocr", "ocr", "moderation", "dall",
         "realtime", "transcribe", "speech", "omni", "livetranslate", "s2s", "mt-")


def current(db) -> dict | None:
    raw = db.kv_get("config:llm")
    return json.loads(raw) if raw else None


def save(db, cfg: dict) -> None:
    db.kv_set("config:llm", json.dumps(cfg))
    db.audit("system", "model_configured", {k: cfg.get(k) for k in ("preset", "base_url", "model")})


def custom_prices(db) -> dict:
    raw = db.kv_get("config:prices")
    return json.loads(raw) if raw else {}


def chat_models(ids: list[str]) -> list[str]:
    keep = [i for i in ids if not any(s in i.lower() for s in _SKIP)]
    return sorted(set(keep), key=lambda m: (not any(x in m for x in ("max", "plus", "pro", "opus", "sonnet", "gpt", "chat", "kimi", "deepseek")), m))


async def list_models(preset: str, base_url: str, api_key: str) -> list[str]:
    """Checks the key works and returns the models it can use. Raises ValueError with a readable reason."""
    p = PRESETS.get(preset)
    if not p:
        raise ValueError("unknown provider")
    if p["kind"] == "anthropic":
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=20)
        try:
            page = await client.models.list(limit=100)
        except anthropic.AuthenticationError:
            raise ValueError("Anthropic did not accept that key")
        except anthropic.APIStatusError as e:
            raise ValueError(f"Anthropic returned an error ({e.status_code})")
        except anthropic.APIConnectionError:
            raise ValueError("could not reach Anthropic from the server")
        ids = [m.id for m in page.data]
        preferred = [m for m in p["models"] if m in ids]
        return preferred + [m for m in ids if m not in preferred]
    base = (base_url or p["base_url"]).rstrip("/")
    if not base.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise ValueError("the endpoint must use https:// (or be on this server)")
    headers = {"Authorization": f"Bearer {api_key or 'none'}"}
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.get(f"{base}/models", headers=headers)
    except httpx.HTTPError:
        raise ValueError(f"could not reach {base}")
    if r.status_code in (401, 403):
        raise ValueError("the provider did not accept that key")
    if r.status_code >= 400:
        raise ValueError(f"the provider returned HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise ValueError("the endpoint did not return a model list (is the address right?)")
    ids = [m.get("id") for m in (data.get("data") or data.get("models") or []) if isinstance(m, dict) and m.get("id")]
    if not ids:
        raise ValueError("connected, but no models were listed for this key")
    return chat_models(ids)
