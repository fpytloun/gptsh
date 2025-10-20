from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, TypedDict

from gptsh.interfaces import LLMClient


class StreamToolCall(TypedDict, total=False):
    id: Optional[str]
    name: Optional[str]
    arguments: str  # accumulated JSON string



class LiteLLMClient(LLMClient):
    def __init__(self, base_params: Dict[str, Any] | None = None) -> None:
        self._base = dict(base_params or {})
        self._last_stream_info: Dict[str, Any] = {"saw_tool_delta": False, "tool_names": []}
        self._last_stream_calls: List["StreamToolCall"] = []

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
        self._last_stream_calls = []
        # Accumulate tool_calls by index to reconstruct full arguments
        calls_acc: Dict[int, StreamToolCall] = {}
        async for chunk in stream_iter:
            # Support dict-like or object-like chunks
            if isinstance(chunk, dict):
                choices = chunk.get("choices")
            else:
                choices = getattr(chunk, "choices", None)
            if isinstance(choices, list) and choices:
                ch0 = choices[0]
            else:
                ch0 = {}
            if isinstance(ch0, dict):
                delta = ch0.get("delta") or {}
            else:
                delta = getattr(ch0, "delta", None) or {}
                if isinstance(delta, dict):
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
            # Yield raw chunk; the session handles text extraction and rendering
            yield chunk
        # Snapshot accumulated calls and names
        # Sort by index
        self._last_stream_calls = [v for _, v in sorted(calls_acc.items(), key=lambda kv: kv[0])]
        names = [c.get("name") for c in self._last_stream_calls if c.get("name")]
        existing = self._last_stream_info.get("tool_names") or []
        self._last_stream_info["tool_names"] = list({*existing, *names})

    def get_last_stream_info(self) -> Dict[str, Any]:
        return dict(self._last_stream_info)

    def get_last_stream_calls(self) -> List[StreamToolCall]:
        return list(self._last_stream_calls)
