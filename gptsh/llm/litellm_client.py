from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict

from gptsh.interfaces import LLMClient

logger = logging.getLogger(__name__)


class LiteLLMClient(LLMClient):
    def __init__(self, base_params: Dict[str, Any] | None = None) -> None:
        self._base = dict(base_params or {})
        self._last_stream_info: Dict[str, Any] = {"saw_tool_delta": False, "tool_names": []}

    async def complete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from litellm import acompletion  # lazy import for testability

        merged: Dict[str, Any] = {**self._base, **(params or {})}
        return await acompletion(**merged)

    async def stream(self, params: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        from litellm import acompletion  # lazy import for testability

        merged: Dict[str, Any] = {**self._base, **(params or {})}
        stream_iter = await acompletion(stream=True, **merged)
        # Reset stream info at start
        self._last_stream_info = {"saw_tool_delta": False, "tool_names": []}
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
                        try:
                            self._last_stream_info["saw_tool_delta"] = True
                            existing = self._last_stream_info.get("tool_names") or []
                            self._last_stream_info["tool_names"] = list({*existing, *names})
                        except Exception:
                            pass
                    fcall = delta.get("function_call")
                    if fcall and isinstance(fcall, dict):
                        logger.debug("LLM stream legacy function_call: name=%s", fcall.get("name"))
                        try:
                            self._last_stream_info["saw_tool_delta"] = True
                            name = fcall.get("name")
                            if name:
                                existing = self._last_stream_info.get("tool_names") or []
                                self._last_stream_info["tool_names"] = list({*existing, name})
                        except Exception:
                            pass
            except Exception:
                pass
            # Yield raw chunk; the session handles text extraction and rendering
            yield chunk

    def get_last_stream_info(self) -> Dict[str, Any]:
        return dict(self._last_stream_info)
