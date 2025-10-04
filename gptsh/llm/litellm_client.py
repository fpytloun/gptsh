from __future__ import annotations

from typing import Any, AsyncIterator, Dict
from gptsh.interfaces import LLMClient


class LiteLLMClient(LLMClient):
    async def complete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from litellm import acompletion  # lazy import for testability

        return await acompletion(**params)

    async def stream(self, params: Dict[str, Any]) -> AsyncIterator[str]:
        from gptsh.llm.session import _extract_text  # reuse robust extractor
        from litellm import acompletion  # lazy import for testability

        stream_iter = await acompletion(stream=True, **params)
        async for chunk in stream_iter:
            text = _extract_text(chunk)
            if text:
                yield text

