from __future__ import annotations

from typing import Any, Mapping


def extract_text(c: Any) -> str:
    """Robustly extract text from various provider stream chunk shapes."""
    if isinstance(c, (str, bytes)):
        return c.decode() if isinstance(c, bytes) else c
    if isinstance(c, Mapping) or hasattr(c, "get"):
        try:
            m = c  # type: ignore
            content = ((m.get("choices", [{}])[0].get("delta", {}) or {}).get("content"))
            if content:
                return str(content)
            delta = (m.get("choices", [{}])[0].get("delta", {}) or {})
            text_val = delta.get("text") if isinstance(delta, Mapping) else None
            if text_val:
                return str(text_val)
            message = (m.get("choices", [{}])[0].get("message", {}) or {})
            content = message.get("content") if isinstance(message, Mapping) else None
            if content:
                return str(content)
            if m.get("content"):
                return str(m.get("content"))
            if m.get("text"):
                return str(m.get("text"))
        except Exception:
            pass
    try:
        choices = getattr(c, "choices", None)
        if choices:
            first = choices[0] if len(choices) > 0 else None
            if first is not None:
                delta = getattr(first, "delta", None)
                if delta is not None:
                    content = getattr(delta, "content", None)
                    if content:
                        return str(content)
                    text_val = getattr(delta, "text", None)
                    if text_val:
                        return str(text_val)
        content_attr = getattr(c, "content", None)
        if content_attr:
            return str(content_attr)
        text_attr = getattr(c, "text", None)
        if text_attr:
            return str(text_attr)
    except Exception:
        pass
    return ""

