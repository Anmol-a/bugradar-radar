"""Provider-agnostic LLM client. Standard library only (no SDK dependency).

The LLM is never called on a passing run and NEVER decides pass/fail. It is used for:
  1. healing   pick the right element when every known locator and the heuristic failed
               (code then re-checks the outcome, e.g. /cart.js really has the product)
  2. triage    after a CONFIRMED failure: real store problem, or Radar's own mistake? (with the
               failure screenshot). Radar's mistakes get one re-check with LLM help switched on.
  3. assist    during that re-check only: find the product name, close an overlay Radar missed.

Providers (switching = one line in .env; prompts and code stay the same):
  openai          OpenAI. Default model gpt-5-mini (llm-check 15/15 vs gpt-4o-mini 14/15, 5 Oct; reads
                  screenshots; ~$0.00036 per triage call). Key: OPENAI_API_KEY.
  anthropic       Claude Messages API. Default model claude-haiku-4-5-20251001. Key: ANTHROPIC_API_KEY.
  openai_compat   Any OpenAI-compatible /chat/completions endpoint (Gemini's compat endpoint,
                  DeepSeek, local models). Needs RADAR_LLM_BASE_URL + RADAR_LLM_API_KEY.
  none            Healing stays heuristic-only, no triage.
  auto (default)  openai if OPENAI_API_KEY is set, else anthropic if ANTHROPIC_API_KEY, else
                  openai_compat if configured, else none.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from radar.core.config import Settings

Transport = Callable[[str, dict, dict], dict]   # (url, headers, body) -> parsed JSON response


def _http_post(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


DEFAULT_MODEL = {"openai": "gpt-5-mini", "anthropic": "claude-haiku-4-5-20251001", "openai_compat": ""}
OPENAI_BASE = "https://api.openai.com/v1"

# USD per 1M tokens (input, output), list prices checked 5 Oct 2026. Only used for the cost line
# in reports; unknown models show tokens without a cost.
PRICES = {"gpt-4o-mini": (0.15, 0.60), "gpt-5-mini": (0.25, 2.00), "gpt-5-nano": (0.05, 0.40),
          "gpt-4.1-nano": (0.10, 0.40), "claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-4-5": (3.00, 15.00),
          "gemini-2.5-flash-lite": (0.10, 0.40), "deepseek-chat": (0.28, 0.42)}


def est_usd(model: str, tin: int, tout: int) -> float | None:
    key = next((k for k in PRICES if (model or "").startswith(k)), None)
    if not key:
        return None
    pin, pout = PRICES[key]
    return round(tin / 1e6 * pin + tout / 1e6 * pout, 6)


def extract_json(text: str) -> dict:
    """Models sometimes wrap JSON in prose or ```json fences. Take the first {...} block."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError(f"no JSON object in model output: {text[:120]!r}")
    return json.loads(m.group(0))


@dataclass
class LLMClient:
    provider: str
    model: str
    max_calls: int
    base_url: str = ""
    api_key: str = ""
    transport: Transport = _http_post
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.provider != "none"

    def usage(self) -> dict:
        return {"provider": self.provider, "model": self.model if self.enabled else None,
                "calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "est_usd": est_usd(self.model, self.input_tokens, self.output_tokens) if self.enabled else None,
                "errors": self.errors[:5]}

    def complete_json(self, system: str, user: str, max_tokens: int = 300,
                      images: list[bytes] | None = None) -> dict | None:
        """Returns parsed JSON or None (disabled, over budget, or failed; failure is recorded).
        images: JPEG bytes (screenshots), sent at low detail to keep cost down."""
        if not self.enabled or self.calls >= self.max_calls:
            return None
        self.calls += 1
        imgs = [base64.b64encode(b).decode() for b in (images or []) if b]
        try:
            if self.provider == "anthropic":
                content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": d}}
                           for d in imgs] + [{"type": "text", "text": user}]
                resp = self.transport(
                    "https://api.anthropic.com/v1/messages",
                    {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                    {"model": self.model, "max_tokens": max_tokens, "system": system,
                     "messages": [{"role": "user", "content": content if imgs else user}]})
                text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
                u = resp.get("usage", {})
                self.input_tokens += u.get("input_tokens", 0)
                self.output_tokens += u.get("output_tokens", 0)
            else:
                content = [{"type": "text", "text": user}] + [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{d}", "detail": "low"}}
                    for d in imgs]
                body = {"model": self.model,
                        "messages": [{"role": "system", "content": system},
                                     {"role": "user", "content": content if imgs else user}]}
                if re.match(r"(gpt-5|o\d)", self.model or ""):     # reasoning models: no temperature
                    body.update(max_completion_tokens=max_tokens * 4, reasoning_effort="minimal")
                else:
                    body.update(max_tokens=max_tokens, temperature=0)
                if "api.openai.com" in self.base_url:
                    body["response_format"] = {"type": "json_object"}
                resp = self.transport(self.base_url.rstrip("/") + "/chat/completions",
                                      {"authorization": f"Bearer {self.api_key}"}, body)
                text = resp["choices"][0]["message"]["content"]
                u = resp.get("usage", {})
                self.input_tokens += u.get("prompt_tokens", 0)
                self.output_tokens += u.get("completion_tokens", 0)
            return extract_json(text)
        except Exception as e:  # noqa: BLE001  network, auth, parse: never crash a run over the LLM
            self.errors.append(f"{type(e).__name__}: {str(e)[:150]}")
            return None


def make_client(s: Settings, transport: Transport | None = None) -> LLMClient:
    provider = s.llm_provider
    oa_key = os.environ.get("OPENAI_API_KEY", "")
    a_key = os.environ.get("ANTHROPIC_API_KEY", "")
    c_key = os.environ.get("RADAR_LLM_API_KEY", "")
    if provider == "auto":
        provider = ("openai" if oa_key else "anthropic" if a_key
                    else "openai_compat" if (c_key and s.llm_base_url) else "none")
    key = {"openai": oa_key, "anthropic": a_key}.get(provider, c_key)
    if provider != "none" and not key:
        provider = "none"
    base = s.llm_base_url or (OPENAI_BASE if provider == "openai" else "")
    model = s.llm_model or DEFAULT_MODEL.get(provider, "")
    c = LLMClient(provider=provider, model=model, max_calls=s.llm_max_calls_per_run,
                  base_url=base, api_key=key)
    if transport:
        c.transport = transport
    return c
