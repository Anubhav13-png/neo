"""LLM providers behind one interface. Nothing else in neo calls a provider SDK.

  anthropic          Claude through the official SDK (prompt caching on the briefing)
  openai_compatible  any /v1/chat/completions server: vLLM, Ollama, LM Studio, OpenRouter, Gemini, OpenAI ...
  scripted           replays canned replies (tests, offline evaluation)
  "pkg.module:Class" your own subclass of LLM
"""
from __future__ import annotations

import importlib
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional


class LLMError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLM:
    name = "base"

    def __init__(self, model: str = "", max_tokens: int = 8000, temperature: Optional[float] = None, **options):
        self.model, self.max_tokens, self.temperature, self.options = model, max_tokens, temperature, options

    def chat(self, system: str, messages: List[Dict[str, str]]) -> Reply:
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}:{self.model}"


class AnthropicLLM(LLM):
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", max_tokens: int = 16000, temperature: Optional[float] = None,
                 api_key_env: Optional[str] = None, base_url: Optional[str] = None, prompt_cache: bool = True,
                 **options):
        super().__init__(model, max_tokens, temperature, **options)
        try:
            import anthropic
        except ImportError:
            raise LLMError("pip install anthropic to use provider: anthropic")
        kwargs = {}
        if api_key_env:
            kwargs["api_key"] = os.environ.get(api_key_env)
        if base_url:
            kwargs["base_url"] = base_url
        self._anthropic = anthropic
        self.prompt_cache = prompt_cache
        try:
            self.client = anthropic.Anthropic(**kwargs)
        except Exception as e:
            raise LLMError(f"could not create the Anthropic client: {e}")

    def chat(self, system: str, messages: List[Dict[str, str]]) -> Reply:
        a = self._anthropic
        block = {"type": "text", "text": system}
        if self.prompt_cache:
            block["cache_control"] = {"type": "ephemeral"}
        params = dict(model=self.model, max_tokens=self.max_tokens, system=[block], messages=messages)
        if self.temperature is not None:
            params["temperature"] = self.temperature
        try:
            with self.client.messages.stream(**params) as stream:
                msg = stream.get_final_message()
        except a.AuthenticationError:
            raise LLMError("Anthropic authentication failed: set ANTHROPIC_API_KEY")
        except a.NotFoundError:
            raise LLMError(f"model `{self.model}` not found")
        except a.RateLimitError:
            raise LLMError("Anthropic rate limit hit (the SDK already retried)")
        except a.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}")
        except a.APIConnectionError:
            raise LLMError("could not reach the Anthropic API")
        except TypeError as e:
            if "authentication" in str(e).lower() or "api_key" in str(e).lower():
                raise LLMError("no Anthropic credentials found: set ANTHROPIC_API_KEY")
            raise
        if msg.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        u = getattr(msg, "usage", None)
        return Reply(text, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)


class OpenAICompatibleLLM(LLM):
    name = "openai_compatible"

    def __init__(self, model: str = "", max_tokens: int = 4096, temperature: Optional[float] = 0.1,
                 base_url: str = "http://localhost:11434/v1", api_key_env: Optional[str] = None, timeout: int = 900,
                 **options):
        super().__init__(model, max_tokens, temperature, **options)
        self.base_url = base_url.rstrip("/")
        self.api_key = os.environ.get(api_key_env) if api_key_env else None
        if api_key_env and not self.api_key:
            raise LLMError(f"environment variable {api_key_env} is not set")
        self.timeout = timeout

    def chat(self, system: str, messages: List[Dict[str, str]]) -> Reply:
        import httpx
        body = {"model": self.model, "messages": [{"role": "system", "content": system}] + messages,
                "max_tokens": self.max_tokens}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise LLMError(f"could not reach {self.base_url}: {e}")
        if r.status_code != 200:
            raise LLMError(f"{self.base_url} returned {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError):
            raise LLMError(f"unexpected response: {r.text[:300]}")
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        u = data.get("usage") or {}
        return Reply(text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))

    def describe(self) -> str:
        return f"{self.name}:{self.model}@{self.base_url}"


class ScriptedLLM(LLM):
    name = "scripted"

    def __init__(self, model: str = "script", max_tokens: int = 0, temperature: Optional[float] = None,
                 responses: Optional[List] = None, **options):
        super().__init__(model, max_tokens, temperature, **options)
        self.responses = [r if isinstance(r, str) else json.dumps(r) for r in (responses or [])]
        self.calls: List[dict] = []

    def chat(self, system: str, messages: List[Dict[str, str]]) -> Reply:
        self.calls.append({"system": system, "messages": messages})
        if not self.responses:
            raise LLMError("ScriptedLLM ran out of responses")
        text = self.responses.pop(0)
        return Reply(text, len(system) // 4 + sum(len(m["content"]) for m in messages) // 4, len(text) // 4)


PRESETS = {
    "ollama": {"base_url": "http://localhost:11434/v1"},
    "vllm": {"base_url": "http://localhost:8000/v1"},
    "lmstudio": {"base_url": "http://localhost:1234/v1"},
    "openai": {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api_key_env": "GEMINI_API_KEY"},
}


def make_llm(spec: dict) -> LLM:
    spec = dict(spec or {})
    provider = spec.pop("provider", "scripted")
    options = spec.pop("options", {}) or {}
    if provider in PRESETS:
        options = {**PRESETS[provider], **options}
        provider = "openai_compatible"
    classes = {"anthropic": AnthropicLLM, "openai_compatible": OpenAICompatibleLLM, "scripted": ScriptedLLM}
    if provider in classes:
        cls = classes[provider]
    elif ":" in provider:
        mod, name = provider.split(":", 1)
        cls = getattr(importlib.import_module(mod), name)
    else:
        raise LLMError(f"unknown LLM provider `{provider}`")
    return cls(**spec, **options)
