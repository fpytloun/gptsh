import sys
from typing import Any, Dict, Optional, Tuple


def read_stdin_raw(max_bytes: int = 5242880) -> Optional[Tuple[bytes, bool]]:
    """Read raw bytes from stdin up to max_bytes.

    Returns (data, truncated) or None if stdin is a TTY.
    """
    if sys.stdin.isatty():
        return None
    data = sys.stdin.buffer.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return (data, truncated)


def is_probably_text(data: bytes) -> bool:
    """Check if bytes are likely plain text.

    Returns False if:
    - Contains NUL bytes
    - Cannot decode as UTF-8
    - Has too many non-printable characters (< 90% printable)
    """
    if not data:
        return True

    # NUL bytes are a strong indicator of binary
    if b"\x00" in data:
        return False

    # Must decode as valid UTF-8
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False

    # Check printable ratio
    if not text.strip():
        return True  # Empty/whitespace is okay

    printable_count = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    ratio = printable_count / len(text)

    return ratio >= 0.90


def sniff_mime(data: bytes) -> str:
    """Determine MIME type from magic bytes.

    Returns a MIME string like "image/png", "application/zip", etc.
    Returns "text/plain" only if data passes is_probably_text().
    Falls back to "application/octet-stream" for unknown binary.
    """
    if len(data) == 0:
        return "text/plain"

    # Image formats (must check before text)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"BM":
        return "image/bmp"

    # PDF
    if data[:5] == b"%PDF-":
        return "application/pdf"

    # Archives
    if data[:4] == b"PK\x03\x04":
        return "application/zip"
    if data[:2] == b"\x1f\x8b":
        return "application/gzip"
    if data[:4] == b"Rar!":
        return "application/x-rar-compressed"

    # Audio formats
    if data[:3] == b"ID3" or (len(data) >= 2 and data[:2] == b"\xff\xfb"):
        return "audio/mpeg"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"fLaC":
        return "audio/flac"

    # Video formats
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"AVI ":
        return "video/x-msvideo"
    if len(data) >= 12 and data[4:12] == b"ftypmp42":
        return "video/mp4"

    # Check if it's actually text (strict)
    if is_probably_text(data):
        return "text/plain"

    return "application/octet-stream"


def read_stdin_any(max_bytes: int = 5242880) -> Optional[Dict[str, Any]]:
    """Read stdin and auto-detect content type.

    Returns:
    - None if stdin is a TTY
    - {"kind": "text", "text": str, "truncated": bool} for text
    - {"kind": "attachment", "type": "image"|"pdf"|"file", "mime": str, "data": bytes, "truncated": bool} for binary

    Safety: NEVER returns kind="text" for binary data. Any detected binary
    format or data that fails is_probably_text() returns kind="attachment".
    """
    raw = read_stdin_raw(max_bytes)
    if raw is None:
        return None

    data, truncated = raw
    mime = sniff_mime(data)

    # CRITICAL: Only treat as text if MIME is text/plain AND it passes safety check
    if mime == "text/plain" and is_probably_text(data):
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            # Should never happen since is_probably_text checks decode, but be safe
            return {
                "kind": "attachment",
                "type": "file",
                "mime": "application/octet-stream",
                "data": data,
                "truncated": truncated,
            }
        if truncated:
            text += "\n[...STDIN truncated. Input exceeded limit.]"
        return {"kind": "text", "text": text, "truncated": truncated}

    # Everything else is binary - classify by type for multimodal routing
    att_type = "file"  # default
    if mime.startswith("image/"):
        att_type = "image"
    elif mime == "application/pdf":
        att_type = "pdf"

    return {
        "kind": "attachment",
        "type": att_type,
        "mime": mime,
        "data": data,
        "truncated": truncated,
    }


def read_stdin(max_bytes: int = 5242880) -> Optional[str]:
    """Read up to max_bytes from stdin, handle overflow with notice.

    Deprecated: use read_stdin_any for binary-aware handling.
    """
    if sys.stdin.isatty():
        return None
    data = sys.stdin.buffer.read(max_bytes + 1)
    truncated = False
    if len(data) > max_bytes:
        truncated = True
        data = data[:max_bytes]
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = str(data)
    if truncated:
        text += (
            "\n[...STDIN truncated. Input exceeded limit. See config.stdin.overflow_strategy... ]"
        )
    return text
