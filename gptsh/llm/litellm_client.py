from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional, TypedDict

import litellm

from gptsh.interfaces import LLMClient

litellm.include_cost_in_streaming_usage = True

# Shared session for litellm (prompt caching / connection reuse)
try:
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None  # type: ignore

# Module logger (respects project logging config)
_log = logging.getLogger(__name__)


class StreamToolCall(TypedDict, total=False):
    id: Optional[str]
    name: Optional[str]
    arguments: str  # accumulated JSON string


class LiteLLMClient(LLMClient):
    def __init__(self, base_params: Dict[str, Any] | None = None) -> None:
        self._base = dict(base_params or {})
        # Track stream metadata for decision making in the session
        self._last_stream_info: Dict[str, Any] = {
            "saw_tool_delta": False,
            "tool_names": [],
            "finish_reason": None,
            "saw_text": False,
        }
        self._last_stream_calls: List["StreamToolCall"] = []
        # Lazily-created shared aiohttp session (used by litellm.shared_session)
        self._shared_session: Optional["aiohttp.ClientSession"] = None  # type: ignore[name-defined]
        _log.debug("LiteLLMClient initialized; base_params keys=%s", list(self._base.keys()))

    async def complete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Perform a non-streamed chat completion via LiteLLM acompletion."""
        from litellm import acompletion  # lazy import for testability

        merged: Dict[str, Any] = {**self._base, **(params or {})}
        # Attach shared session for connection reuse / provider prompt caching
        sess = await self._ensure_shared_session()
        if sess is not None:
            merged["shared_session"] = sess
        _log.debug(
            "LLM complete(): model=%s stream=%s len(messages)=%s shared_session=%s",
            merged.get("model"),
            merged.get("stream", False),
            len(merged.get("messages") or []),
            (hex(id(sess)) if sess is not None else None),
        )
        return await acompletion(
            cache_control_injection_points=[
                {
                    "location": "message",
                    "role": "system",
                },
            ],
            **merged,
        )

    async def stream(self, params: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """Stream a chat completion using LiteLLM acompletion(stream=True).

        Accumulates OpenAI-format streamed tool_calls by index and tracks basic
        telemetry (saw text, saw tool deltas, finish_reason, tool names).
        Yields raw chunks for higher-level text extraction/printing.
        """
        from litellm import acompletion  # lazy import for testability

        merged: Dict[str, Any] = {**self._base, **(params or {})}
        # Attach shared session for connection reuse / provider prompt caching
        sess = await self._ensure_shared_session()
        if sess is not None:
            merged["shared_session"] = sess
        _log.debug(
            "LLM stream(): model=%s len(messages)=%s shared_session=%s",
            merged.get("model"),
            len(merged.get("messages") or []),
            (hex(id(sess)) if sess is not None else None),
        )
        stream_iter = await acompletion(
            stream=True,
            stream_options={"include_usage": True},
            cache_control_injection_points=[
                {
                    "location": "message",
                    "role": "system",
                },
            ],
            **merged,
        )
        # Reset stream info at start
        self._last_stream_info = {"saw_tool_delta": False, "tool_names": [], "finish_reason": None, "saw_text": False}
        self._last_stream_calls = []
        # Accumulate tool_calls by index to reconstruct full arguments
        calls_acc: Dict[int, StreamToolCall] = {}
        last_finish_reason: Optional[str] = None
        async for chunk in stream_iter:
            # Support dict-like or object-like chunks
            if isinstance(chunk, dict):
                choices = chunk.get("choices")
            else:
                choices = getattr(chunk, "choices", None)

            if not (isinstance(choices, list) and choices):
                # Yield raw chunk even if there are no choices (provider variations)
                yield chunk
                continue

            # Iterate all choices to support n > 1 and capture all deltas
            for ch in choices:
                if isinstance(ch, dict):
                    delta = ch.get("delta")
                    finish_reason = ch.get("finish_reason")
                else:
                    delta = getattr(ch, "delta", None)
                    finish_reason = getattr(ch, "finish_reason", None)

                if isinstance(delta, dict):
                    # Track visible text content
                    if delta.get("content"):
                        self._last_stream_info["saw_text"] = True

                    # OpenAI-style tool_calls deltas
                    tcalls = delta.get("tool_calls") or []
                    if isinstance(tcalls, list) and tcalls:
                        for tc in tcalls:
                            if not isinstance(tc, dict):
                                continue
                            idx_val = tc.get("index", 0)
                            idx = int(idx_val) if isinstance(idx_val, int) or (isinstance(idx_val, str) and str(idx_val).isdigit()) else 0
                            acc = calls_acc.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                            if tc.get("id"):
                                acc["id"] = str(tc.get("id"))  # type: ignore[assignment]
                            fn = tc.get("function") or {}
                            if isinstance(fn, dict):
                                if fn.get("name"):
                                    acc["name"] = str(fn.get("name"))  # type: ignore[assignment]
                                arg_val = fn.get("arguments")
                                if arg_val is not None:
                                    acc["arguments"] += str(arg_val)
                        self._last_stream_info["saw_tool_delta"] = True

                    # Legacy function_call
                    fcall = delta.get("function_call")
                    if isinstance(fcall, dict):
                        acc = calls_acc.setdefault(0, {"id": None, "name": None, "arguments": ""})
                        if fcall.get("name"):
                            acc["name"] = str(fcall.get("name"))  # type: ignore[assignment]
                        arg_val = fcall.get("arguments")
                        if arg_val is not None:
                            acc["arguments"] += str(arg_val)
                        self._last_stream_info["saw_tool_delta"] = True

                if finish_reason:
                    last_finish_reason = str(finish_reason)

            # Yield the raw chunk once per received provider chunk
            yield chunk
        # Snapshot accumulated calls and names, preserving call order by index
        self._last_stream_calls = [v for _, v in sorted(calls_acc.items(), key=lambda kv: kv[0])]
        names = [c.get("name") for c in self._last_stream_calls if c.get("name")]
        self._last_stream_info["tool_names"] = names
        if last_finish_reason is not None:
            self._last_stream_info["finish_reason"] = last_finish_reason

    def get_last_stream_info(self) -> Dict[str, Any]:
        """Return telemetry about the last stream call (copy)."""
        return dict(self._last_stream_info)

    def get_last_stream_calls(self) -> List[StreamToolCall]:
        """Return reconstructed tool calls from the last stream (copy)."""
        return list(self._last_stream_calls)

    async def aclose(self) -> None:
        """Close the shared aiohttp session if it was created."""
        sess = getattr(self, "_shared_session", None)
        self._shared_session = None
        if sess is not None:
            try:
                await sess.close()
            except Exception:
                pass

    async def _ensure_shared_session(self) -> Optional["aiohttp.ClientSession"]:
        """Create (once) and return the shared aiohttp session; None if aiohttp is unavailable."""
        if aiohttp is None:
            return None
        if self._shared_session is None or self._shared_session.closed:  # type: ignore[union-attr]
            self._shared_session = aiohttp.ClientSession()
        return self._shared_session
