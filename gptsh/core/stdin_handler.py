import sys
from typing import Optional


def read_stdin(max_bytes: int = 5242880) -> Optional[str]:
    """Read up to max_bytes from stdin, handle overflow with notice."""
    if sys.stdin.isatty():
        return None
    data = sys.stdin.buffer.read(max_bytes + 1)
    truncated = False
    if len(data) > max_bytes:
        truncated = True
        data = data[:max_bytes]
    try:
        text = data.decode('utf-8', errors='replace')
    except Exception:
        text = str(data)
    if truncated:
        text += "\n[...STDIN truncated. Input exceeded limit. See config.stdin.overflow_strategy... ]"
    return text
