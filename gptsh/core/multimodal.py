"""Multimodal message building and capability checking for LLM interactions.

This module provides utilities for:
- Checking model capabilities (vision, PDF support)
- Building content arrays with text and attachments
- Converting binary data to appropriate formats (data URLs, etc.)
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)


def check_model_capabilities(model: str) -> Dict[str, bool]:
    """Check what modalities a model supports.

    Returns: {"vision": bool, "pdf": bool}
    """
    try:
        from litellm.utils import supports_pdf_input, supports_vision

        return {
            "vision": supports_vision(model=model),
            "pdf": supports_pdf_input(model=model),
        }
    except Exception as e:
        _log.debug("Failed to check model capabilities for %s: %s", model, e)
        return {"vision": False, "pdf": False}


def make_image_content_part(data: bytes, mime: str) -> Dict[str, Any]:
    """Create an image_url content part from binary data.

    Returns: {"type": "image_url", "image_url": {"url": "data:...;base64,..."}}
    """
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def make_text_content_part(text: str) -> Dict[str, Any]:
    """Create a text content part.

    Returns: {"type": "text", "text": "..."}
    """
    return {"type": "text", "text": text}


def make_attachment_marker(mime: str, size: int, truncated: bool = False) -> str:
    """Create a text marker for unsupported attachments.

    Returns: "[Attached: <mime>, <size> bytes]" or with (truncated)
    """
    trunc_note = " (truncated)" if truncated else ""
    return f"[Attached: {mime}, {size} bytes{trunc_note}]"


def build_user_message(
    text: Optional[str],
    attachments: Optional[List[Dict[str, Any]]],
    model: str,
) -> Dict[str, Any]:
    """Build a user message with text and optional attachments.

    Args:
        text: User prompt text (optional if attachments present)
        attachments: List of {"type": "image"|"pdf", "mime": str, "data": bytes}
        model: Model name for capability checking

    Returns:
        {"role": "user", "content": <str or list>}

    If model supports multimodal and attachments are provided, content will be
    a list of content parts. Otherwise, content is plain text with markers.
    """
    if not attachments:
        # Simple text message
        return {"role": "user", "content": text or ""}

    capabilities = check_model_capabilities(model)
    content_parts: List[Dict[str, Any]] = []
    fallback_markers: List[str] = []

    # Add text first if present
    if text:
        content_parts.append(make_text_content_part(text))

    # Process attachments
    for att in attachments:
        att_type = att.get("type", "")
        mime = att.get("mime", "application/octet-stream")
        data = att.get("data", b"")
        size = len(data)
        truncated = att.get("truncated", False)

        if att_type == "image" and mime.startswith("image/") and capabilities["vision"]:
            # Model supports vision - add image content part
            content_parts.append(make_image_content_part(data, mime))
        elif att_type == "pdf" and mime == "application/pdf" and capabilities["pdf"]:
            # Model supports PDF - add file content part (future: implement file upload)
            # For now, fall back to marker
            fallback_markers.append(make_attachment_marker(mime, size, truncated))
        else:
            # Unsupported attachment - use text marker
            fallback_markers.append(make_attachment_marker(mime, size, truncated))

    # Decide on final content format
    if len(content_parts) > 1 or (
        len(content_parts) == 1 and content_parts[0]["type"] == "image_url"
    ):
        # We have multimodal content - use content array
        if fallback_markers:
            # Append markers as text part
            marker_text = "\n".join(fallback_markers)
            if text:
                # Update existing text part
                content_parts[0]["text"] += f"\n\n{marker_text}"
            else:
                # Add new text part with markers
                content_parts.insert(0, make_text_content_part(marker_text))
        return {"role": "user", "content": content_parts}
    else:
        # Plain text with markers only
        all_text_parts = [text] if text else []
        all_text_parts.extend(fallback_markers)
        return {"role": "user", "content": "\n\n".join(all_text_parts)}


def message_to_text(message: Dict[str, Any]) -> str:
    """Convert a message (with possible content array) to plain text for persistence.

    Replaces binary content parts with concise markers.
    """
    content = message.get("content", "")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts: List[str] = []
        for part in content:
            part_type = part.get("type", "")
            if part_type == "text":
                text_parts.append(part.get("text", ""))
            elif part_type == "image_url":
                # Replace with marker
                text_parts.append("[Attached: image (base64 data)]")
            elif part_type == "file":
                text_parts.append("[Attached: file]")
        return "\n\n".join(text_parts)

    return str(content)
