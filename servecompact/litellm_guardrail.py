"""LiteLLM guardrail adapter (v2 distribution).

Registers ServeCompact as a LiteLLM pre-call guardrail so any
LiteLLM proxy user gets compaction without touching their client.
Register in the proxy config:

    guardrails:
      - guardrail_name: "servecompact"
        litellm_params:
          mode: "pre_call"
          guardrail: servecompact.litellm_guardrail.ServeCompactGuardrail
          default_on: true
          summarizer_model: "openai/gpt-4o-mini"   # optional; omit for v1-only
          trigger_tokens: 2000                     # optional

Notes, all verified against litellm 1.108.1 (see tests):
- The hook that can rewrite the request is async_pre_call_hook; its
  returned dict becomes the request data. It runs for both sync and
  async chat completions, whose call_type values are "completion" and
  "acompletion" respectively; every other route is returned untouched.
- This hook NEVER raises: any failure falls back to the v1 output,
  and if that fails too the request goes through unchanged.
- The summarizer, when configured, calls the provider directly over
  HTTP (summarizer.py), never through LiteLLM: a summarizer call
  routed through the same proxy would hit this hook again.
- Without summarizer_model the guardrail is v1-only (deterministic
  compaction + verifier, no model call, $0).
- Per-request opt-out / tuning via metadata:
  data["metadata"]["servecompact"] = {"enabled": false}
  or {"trigger_tokens": 5000}.
"""

import asyncio
import logging
import os
from functools import partial

try:
    from litellm.integrations.custom_guardrail import CustomGuardrail
except ImportError as exc:  # pragma: no cover - litellm not installed
    raise ImportError(
        "litellm is required for the guardrail adapter: "
        "pip install litellm") from exc

from .pipeline import V2Config, compact_and_verify_v2
from .summarizer import OpenAISummarizer
from .tokens import Counter

logger = logging.getLogger("servecompact.guardrail")

_CHAT_CALL_TYPES = ("completion", "acompletion")


class ServeCompactGuardrail(CustomGuardrail):
    def __init__(self, summarizer_model=None, trigger_tokens=2000,
                 summarizer_base_url=None, summarizer_api_key=None,
                 summarizer_timeout_s=10.0, **kwargs):
        kwargs.setdefault("guardrail_name", "servecompact")
        super().__init__(**kwargs)
        self.summarizer_model = summarizer_model
        self.trigger_tokens = trigger_tokens
        self.summarizer_base_url = summarizer_base_url or os.environ.get(
            "SERVECOMPACT_SUMMARIZER_BASE_URL", "https://api.openai.com/v1")
        self.summarizer_api_key = summarizer_api_key or os.environ.get(
            "SERVECOMPACT_SUMMARIZER_API_KEY", "")
        self.summarizer_timeout_s = summarizer_timeout_s

    def _build_summarizer(self):
        if not self.summarizer_model:
            return None
        if not self.summarizer_api_key:
            logger.warning("summarizer_model set but no API key; v1-only")
            return None
        return OpenAISummarizer(
            self.summarizer_base_url, self.summarizer_api_key,
            self.summarizer_model, timeout_s=self.summarizer_timeout_s)

    def _config_for(self, data):
        overrides = (data.get("metadata") or {}).get("servecompact") or {}
        trigger = overrides.get("trigger_tokens", self.trigger_tokens)
        main_model = data.get("model") or ""
        return V2Config(
            trigger_tokens=int(trigger),
            summarizer_timeout_s=self.summarizer_timeout_s,
            counter=Counter.for_model(main_model))

    async def async_pre_call_hook(self, user_api_key_dict, cache, data,
                                  call_type):
        if call_type not in _CHAT_CALL_TYPES:
            return data
        messages = data.get("messages")
        if not isinstance(messages, list):
            return data
        overrides = (data.get("metadata") or {}).get("servecompact") or {}
        if overrides.get("enabled") is False:
            return data
        try:
            config = self._config_for(data)
            summarizer = self._build_summarizer()
            to_send, _final, report = await asyncio.to_thread(
                partial(compact_and_verify_v2, messages,
                        summarizer=summarizer, config=config))
            data["messages"] = to_send
            data.setdefault("metadata", {})["servecompact_stats"] = {
                "blocks_summarized": report["blocks_summarized"],
                "blocks_fallback_v1": report["blocks_fallback_v1"],
                "checklist_items_total": report["checklist_items_total"],
                "summarizer_calls": report["summarizer_calls"],
                "fallback_tier": report["fallback_tier"],
                "token_method": report["token_method"],
                "tokens_v1": report["tokens_v1"],
                "tokens_final": report["tokens_final"],
            }
        except Exception:  # noqa: BLE001 - hook must never raise
            logger.exception("servecompact guardrail failed; "
                             "request passes through unchanged")
        return data
