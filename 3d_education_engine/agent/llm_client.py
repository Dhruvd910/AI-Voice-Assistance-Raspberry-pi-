"""One client for any OpenAI-compatible chat-completions API.

OpenRouter, Groq, OpenAI, a local llama.cpp or Ollama server: they all accept
the same request, so switching is a change of LLM_BASE_URL / LLM_MODEL, not
of code. Plain `requests`, so the Pi carries no SDK.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import requests

from app.config import LLMSettings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMReply:
    content: str
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    raw_message: dict = field(default_factory=dict)


class LLMClient:
    def __init__(self, settings: LLMSettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply:
        if not self.configured:
            raise LLMError("no LLM is configured (set LLM_API_KEY / OPENROUTER_API_KEY)")
        body: dict = {"model": self.settings.model, "messages": messages,
                      "temperature": self.settings.temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        if "openrouter.ai" in self.settings.base_url:
            headers["X-Title"] = "3D Education Engine"
        try:
            resp = self.session.post(f"{self.settings.base_url}/chat/completions", json=body, headers=headers,
                                     timeout=self.settings.timeout_s)
        except requests.RequestException as exc:
            raise LLMError(f"cannot reach the LLM: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"LLM returned {resp.status_code}: {resp.text[:300]}")
        try:
            message = resp.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"unexpected LLM response: {resp.text[:300]}") from exc
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__unparseable__": fn.get("arguments")}
            calls.append(LLMToolCall(tc.get("id", ""), fn.get("name", ""), args if isinstance(args, dict) else {}))
        return LLMReply(message.get("content") or "", calls, message)
