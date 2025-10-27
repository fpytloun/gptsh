from __future__ import annotations

from typing import Any, Mapping, Iterable
import logging

_log = logging.getLogger(__name__)


def _to_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        try:
            return val.decode()
        except Exception:
            return val.decode(errors="replace")
    return str(val)


def _extract_from_content_field(val: Any) -> str:
    """Handle various provider content shapes:
    - string/bytes
    - list of blocks: [{type: "text", text: "..."}, ...]
    - dict with {text: "..."} or {content: ...}
    """
    if val is None:
        return ""
    if isinstance(val, (str, bytes)):
        return _to_str(val)
    if isinstance(val, Mapping):
        # Common Anthropic/others: {"text": "..."}
        if "text" in val:
            return _to_str(val.get("text"))
        if "content" in val:
            return _extract_from_content_field(val.get("content"))
        # Some providers embed plain value under different keys; join best-effort
        parts: list[str] = []
        for k in ("message", "delta", "data"):
            if k in val:
                parts.append(_extract_from_content_field(val.get(k)))
        return "".join(parts)
    if isinstance(val, Iterable) and not isinstance(val, (str, bytes)):
        parts: list[str] = []
        for item in val:
            if isinstance(item, Mapping):
                # Prefer block.text when present
                if "text" in item:
                    parts.append(_to_str(item.get("text")))
                elif "content" in item:
                    parts.append(_extract_from_content_field(item.get("content")))
                else:
                    # Fallback: stringify item
                    parts.append(_to_str(item))
            else:
                parts.append(_extract_from_content_field(item))
        return "".join(parts)
    return _to_str(val)


def extract_text(c: Any) -> str:
    """Extract human-visible text from a streamed or non-streamed LLM chunk.

    Robust to multiple provider schemas and always returns a string (never None).
    Concatenates text across all choices in a chunk when present.
    """
    # Fast path for simple payloads
    if isinstance(c, (str, bytes)):
        return _to_str(c)

    # Mapping-like
    if isinstance(c, Mapping) or hasattr(c, "get"):
        try:
            m = c  # type: ignore[assignment]
            choices = m.get("choices") if isinstance(m, Mapping) else None
            collected: list[str] = []
            if isinstance(choices, list) and choices:
                for ch in choices:
                    delta = ch.get("delta") if isinstance(ch, Mapping) else None
                    message = ch.get("message") if isinstance(ch, Mapping) else None

                    # Streaming deltas first
                    if isinstance(delta, Mapping):
                        # OpenAI-style delta.content or delta.text
                        content = delta.get("content")
                        text_val = delta.get("text")
                        if content is not None:
                            collected.append(_extract_from_content_field(content))
                        elif text_val is not None:
                            collected.append(_extract_from_content_field(text_val))

                        # Some providers put list blocks directly in content
                        if not collected and isinstance(content, list):
                            collected.append(_extract_from_content_field(content))

                    # Non-stream final message fallback
                    if isinstance(message, Mapping):
                        msg_content = message.get("content")
                        if msg_content is not None:
                            collected.append(_extract_from_content_field(msg_content))

                if collected:
                    return "".join([p for p in collected if p])

            # Top-level fallbacks
            for key in ("content", "text", "output_text", "response"):
                if isinstance(m, Mapping) and key in m:
                    return _extract_from_content_field(m.get(key))
        except Exception as e:
            _log.debug("extract_text mapping parse error: %s", e)

    # Object-like access (attrs)
    try:
        choices = getattr(c, "choices", None)
        if isinstance(choices, list) and choices:
            pieces: list[str] = []
            for ch in choices:
                delta = getattr(ch, "delta", None)
                if delta is not None:
                    content = getattr(delta, "content", None)
                    text_val = getattr(delta, "text", None)
                    if content is not None:
                        pieces.append(_extract_from_content_field(content))
                    elif text_val is not None:
                        pieces.append(_extract_from_content_field(text_val))
                else:
                    message = getattr(ch, "message", None)
                    if message is not None:
                        pieces.append(_extract_from_content_field(getattr(message, "content", None)))
            if pieces:
                return "".join(pieces)

        for key in ("content", "text", "output_text", "response"):
            val = getattr(c, key, None)
            if val is not None:
                return _extract_from_content_field(val)
    except Exception as e:
        _log.debug("extract_text object parse error: %s", e)

    # Always return a string
    return ""

