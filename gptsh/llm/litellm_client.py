from __future__ import annotations

from typing import Any, AsyncIterator, Dict

from gptsh.interfaces import LLMClient
from gptsh.llm.chunk_utils import extract_text


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
            text = extract_text(chunk)
            if text:
                yield text
