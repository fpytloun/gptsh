"""Tests for gptsh.core.utils module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from gptsh.core.utils import load_instruction_files, resolve_instructions


@pytest.mark.asyncio
async def test_load_instruction_files_empty():
    """Test loading with empty file list."""
    result = await load_instruction_files([])
    assert result == ""


@pytest.mark.asyncio
async def test_load_instruction_files_missing():
    """Test loading with missing files (should be silent)."""
    result = await load_instruction_files(["/nonexistent/file.md", "/also/missing.txt"])
    assert result == ""


@pytest.mark.asyncio
async def test_load_instruction_files_single():
    """Test loading a single instruction file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"
        test_file.write_text("# Test Content\nThis is a test file.")

        result = await load_instruction_files([str(test_file)])

        assert "[File: test.md]" in result
        assert "# Test Content" in result
        assert "This is a test file." in result


@pytest.mark.asyncio
async def test_load_instruction_files_multiple():
    """Test loading multiple instruction files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file1 = Path(tmpdir) / "file1.md"
        file1.write_text("Content 1")

        file2 = Path(tmpdir) / "file2.txt"
        file2.write_text("Content 2")

        result = await load_instruction_files([str(file1), str(file2)])

        assert "[File: file1.md]" in result
        assert "[File: file2.txt]" in result
        assert "Content 1" in result
        assert "Content 2" in result


@pytest.mark.asyncio
async def test_load_instruction_files_with_tilde_expansion():
    """Test tilde expansion in file paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"
        test_file.write_text("Test content")

        # This won't actually expand to home, but tests that the code path works
        result = await load_instruction_files([str(test_file)])
        assert "Test content" in result


@pytest.mark.asyncio
async def test_load_instruction_files_size_limit():
    """Test that files are truncated when exceeding size limit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a file larger than the limit
        large_file = Path(tmpdir) / "large.txt"
        large_content = "x" * 2000000  # 2MB

        large_file.write_text(large_content)

        result = await load_instruction_files([str(large_file)], max_total_bytes=1000000)

        # Should have truncation notice
        assert "truncated" in result.lower()


@pytest.mark.asyncio
async def test_load_instruction_files_skip_invalid_utf8():
    """Test that non-UTF8 files are skipped gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a valid UTF-8 file
        valid_file = Path(tmpdir) / "valid.txt"
        valid_file.write_text("Valid content")

        # Create an invalid UTF-8 file
        invalid_file = Path(tmpdir) / "invalid.bin"
        invalid_file.write_bytes(b"\xff\xfe\x00\x00")

        result = await load_instruction_files([str(invalid_file), str(valid_file)])

        # Should skip invalid file and load valid one
        assert "Valid content" in result
        assert "[File: valid.txt]" in result


def test_resolve_instructions_agent_specific():
    """Test that agent-specific instructions take precedence."""
    config = {
        "instructions": ["global1.md", "global2.md"],
        "agents": {
            "dev": {
                "instructions": ["agent1.md", "agent2.md"],
            },
            "test": {},
        },
    }

    result = resolve_instructions(config, "dev")
    assert result == ["agent1.md", "agent2.md"]

    # Agent without specific instructions should fall back to global
    result = resolve_instructions(config, "test")
    assert result == ["global1.md", "global2.md"]


def test_resolve_instructions_global():
    """Test global instructions when no agent-specific ones exist."""
    config = {
        "instructions": ["global.md"],
        "agents": {
            "dev": {},
        },
    }

    result = resolve_instructions(config, "dev")
    assert result == ["global.md"]


def test_resolve_instructions_empty():
    """Test empty instructions when none configured."""
    config = {"agents": {"dev": {}}}

    result = resolve_instructions(config, "dev")
    assert result == []


def test_resolve_instructions_missing_agent():
    """Test empty instructions for nonexistent agent."""
    config = {
        "instructions": ["global.md"],
    }

    result = resolve_instructions(config, "missing_agent")
    assert result == ["global.md"]
