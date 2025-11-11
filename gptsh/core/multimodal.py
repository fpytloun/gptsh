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

    Returns: {"vision": bool, "pdf": bool, "audio": bool}
    """
    try:
        from litellm.utils import (
            supports_audio_input,
            supports_pdf_input,
            supports_vision,
        )

        return {
            "vision": supports_vision(model=model),
            "pdf": supports_pdf_input(model=model),
            "audio": supports_audio_input(model=model),
        }
    except Exception as e:
        _log.debug("Failed to check model capabilities for %s: %s", model, e)
        return {"vision": False, "pdf": False, "audio": False}


def make_image_content_part(data: bytes, mime: str) -> Dict[str, Any]:
    """Create an image_url content part from binary data.

    Returns: {"type": "image_url", "image_url": {"url": "data:...;base64,..."}}
    """
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def make_pdf_content_part(data: bytes) -> Dict[str, Any]:
    """Create a PDF file content part.

    Returns: {"type": "file", "file": {"file_data": "data:application/pdf;base64,..."}}
    """
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "file", "file": {"file_data": f"data:application/pdf;base64,{b64}"}}


def make_audio_content_part(data: bytes, mime: str) -> Dict[str, Any]:
    """Create an audio content part for models that support audio input.

    Args:
        data: Raw audio file bytes
        mime: MIME type (e.g., "audio/mp3", "audio/wav")

    Returns: {"type": "input_audio", "input_audio": {"data": "base64...", "format": "wav"}}
    """
    # Map MIME types to audio format names
    mime_to_format = {
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/ogg": "ogg",
        "audio/flac": "flac",
        "audio/m4a": "m4a",
        "audio/aac": "aac",
        "audio/webm": "webm",
    }
    format_name = mime_to_format.get(mime, mime.split("/")[-1] or "wav")

    b64 = base64.b64encode(data).decode("ascii")
    return {
        "type": "input_audio",
        "input_audio": {
            "data": b64,
            "format": format_name,
        },
    }


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
        attachments: List of {"type": "image"|"pdf"|"file", "mime": str, "data": bytes, "truncated": bool}
        model: Model name for capability checking

    Returns:
        {"role": "user", "content": <str or list>}

    If model supports multimodal and attachments are provided, content will be
    a list of content parts. Otherwise, content is plain text with markers.

    Supported attachment types:
    - image: Sent as image_url content part if model supports vision
    - pdf: Sent as file content part if model supports PDF
    - audio: Sent as input_audio content part if model supports audio
    - file: Other binaries fall back to text markers
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
            # Model supports PDF - send as data URL via image_url type
            content_parts.append(make_pdf_content_part(data))
        elif att_type == "audio" and mime.startswith("audio/") and capabilities["audio"]:
            # Model supports audio input - add audio content part
            content_parts.append(make_audio_content_part(data, mime))
        else:
            # Unsupported attachment - use text marker
            fallback_markers.append(make_attachment_marker(mime, size, truncated))

    # Decide on final content format
    has_multimodal = False
    if len(content_parts) > 1:
        has_multimodal = True
    elif len(content_parts) == 1:
        part_type = content_parts[0].get("type")
        if part_type in ("image_url", "file"):
            has_multimodal = True

    if has_multimodal:
        # We have multimodal content (images, PDFs, or multiple parts) - use content array
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
                # Images use image_url type
                text_parts.append("[Attached: image (base64 data)]")
            elif part_type == "file":
                # PDFs and other files use file type
                file_data = part.get("file", {}).get("file_data", "")
                if "application/pdf" in file_data:
                    text_parts.append("[Attached: PDF document (base64 data)]")
                else:
                    text_parts.append("[Attached: file]")
            elif part_type == "input_audio":
                # Audio content
                audio_info = part.get("input_audio", {})
                format_name = audio_info.get("format", "audio")
                text_parts.append(f"[Attached: audio ({format_name}, base64 data)]")
        return "\n\n".join(text_parts)

    return str(content)
