from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict

from gptsh.interfaces import LLMClient
from gptsh.llm.chunk_utils import extract_text


logger = logging.getLogger(__name__)


class LiteLLMClient(LLMClient):
    def __init__(self, base_params: Dict[str, Any] | None = None) -> None:
        self._base = dict(base_params or {})

    async def complete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from litellm import acompletion  # lazy import for testability

        merged: Dict[str, Any] = {**self._base, **(params or {})}
        return await acompletion(**merged)

    async def stream(self, params: Dict[str, Any]) -> AsyncIterator[str]:
        from litellm import acompletion  # lazy import for testability

        merged: Dict[str, Any] = {**self._base, **(params or {})}
        stream_iter = await acompletion(stream=True, **merged)
        async for chunk in stream_iter:
            # Debug: detect tool_call deltas and minimal text deltas
            try:
                if isinstance(chunk, dict) or hasattr(chunk, "get"):
                    m = chunk  # type: ignore
                    ch0 = (m.get("choices") or [{}])[0]
                    delta = (ch0.get("delta") or {})
                    tcalls = delta.get("tool_calls") or []
                    if tcalls:
                        names = []
                        for tc in tcalls:
                            fn = (tc.get("function") or {}).get("name")
                            if fn:
                                names.append(fn)
                        # Arguments are often streamed; avoid logging full content
                        logger.debug("LLM stream tool delta: names=%s", names)
                    fcall = delta.get("function_call")
                    if fcall and isinstance(fcall, dict):
                        logger.debug("LLM stream legacy function_call: name=%s", fcall.get("name"))
            except Exception:
                pass
            text = extract_text(chunk)
            if text:
                try:
                    logger.debug("LLM stream text delta: %r", text[:80])
                except Exception:
                    pass
            if text:
                yield text
