"""Utility functions for core gptsh operations."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

_log = logging.getLogger(__name__)


async def load_instruction_files(
    file_paths: List[str],
    max_total_bytes: int = 1048576,  # 1MB total limit
) -> str:
    """Load multiple instruction files and combine into a single context block.

    Features:
    - Expands ~ and resolves relative paths
    - Silently skips missing files (logs at DEBUG level)
    - Truncates total content if exceeds max_total_bytes
    - Returns formatted text block with file markers
    - Returns empty string if no files found/readable

    Args:
        file_paths: List of file paths to load (supports ~ expansion)
        max_total_bytes: Maximum total bytes to load (default 1MB)

    Returns:
        Formatted string with file contents and markers, or empty string if no files loaded
    """
    if not file_paths:
        return ""

    parts: List[str] = []
    total_bytes = 0
    limit_exceeded = False

    for file_path in file_paths:
        try:
            path = Path(file_path).expanduser()

            # Skip missing files silently
            if not path.is_file():
                _log.debug("Instruction file not found: %s (resolved to %s)", file_path, path)
                continue

            # Check file size
            size = path.stat().st_size
            if total_bytes + size > max_total_bytes:
                _log.warning(
                    "Instruction file %s would exceed total limit (current: %s, file: %s, max: %s)",
                    file_path,
                    total_bytes,
                    size,
                    max_total_bytes,
                )
                limit_exceeded = True
                # Add truncation notice if we have some content already
                if parts:
                    parts.append(
                        f"[...Instruction files truncated. Total exceeded {max_total_bytes} bytes.]"
                    )
                break

            # Read file content
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()

                # Add file marker
                parts.append(f"[File: {path.name}]")
                parts.append(content)
                parts.append("")  # Blank line between files

                total_bytes += size
            except UnicodeDecodeError:
                _log.debug("Instruction file is not valid UTF-8: %s", file_path)
                continue
            except OSError as e:
                _log.debug("Failed to read instruction file %s: %s", file_path, e)
                continue

        except Exception as e:
            _log.debug("Error processing instruction file %s: %s", file_path, e)
            continue

    if not parts:
        # Check if we hit the size limit
        if limit_exceeded:
            return f"[...Instruction files truncated. Total exceeded {max_total_bytes} bytes.]"
        return ""

    # Join all parts and strip trailing whitespace
    result = "\n".join(parts).strip()

    # Add truncation notice if total exceeded (and not already added)
    if limit_exceeded and "[...Instruction files truncated" not in result:
        result += f"\n[...Instruction files truncated. Total exceeded {max_total_bytes} bytes.]"

    return result


def resolve_instructions(
    config: dict,
    agent_name: str,
) -> List[str]:
    """Resolve which instruction files to load based on config hierarchy.

    Precedence:
    1. Agent-specific instructions (agents.<name>.instructions)
    2. Global instructions (instructions)
    3. Empty list (no instructions)

    Args:
        config: Global configuration dict
        agent_name: Name of the active agent

    Returns:
        List of file paths to load, or empty list if none configured
    """
    # Check agent-specific instructions first
    agents_config = config.get("agents") or {}
    agent_config = agents_config.get(agent_name) or {}
    agent_instructions = agent_config.get("instructions")

    if isinstance(agent_instructions, list):
        return agent_instructions

    # Fall back to global instructions
    global_instructions = config.get("instructions")
    if isinstance(global_instructions, list):
        return global_instructions

    # No instructions configured
    return []
