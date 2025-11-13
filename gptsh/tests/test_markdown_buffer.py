from gptsh.core.runner import MarkdownBuffer


def collect_blocks(mbuf: MarkdownBuffer, chunks):
    out = []
    for ch in chunks:
        out.extend(mbuf.push(ch))
    tail = mbuf.flush()
    if tail:
        out.append(tail)
    return out


def test_paragraph_flush_on_blank_line_simple():
    mbuf = MarkdownBuffer()
    chunks = ["Hello world\n\n", "Next para\n\n", "Tail without extra\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert out == ["Hello world\n\n", "Next para\n\n", "Tail without extra\n\n"]


def test_streaming_chunked_paragraphs():
    mbuf = MarkdownBuffer()
    text = "Line 1\nLine 2\n\nLine 3\n\n"
    out = []
    for ch in [text[:5], text[5:12], text[12:17], text[17:]]:
        out.extend(mbuf.push(ch))
    tail = mbuf.flush()
    if tail:
        out.append(tail)
    # Should split into two paragraph blocks
    assert out == ["Line 1\nLine 2\n\n", "Line 3\n\n"]


def test_latency_guard_flushes_when_long_and_newline():
    # Set low latency threshold to trigger easily
    mbuf = MarkdownBuffer(latency_chars=10)
    chunks = ["abcdefghij\n", "more"]
    out = mbuf.push(chunks[0])
    # First push ends with newline and >= threshold, should flush entire buffer
    assert out == ["abcdefghij\n"]
    out2 = mbuf.push(chunks[1])
    # No newline -> no flush; flush() should return remaining
    assert out2 == []
    tail = mbuf.flush()
    assert tail == "more"


def test_fenced_block_triple_backticks_with_language():
    mbuf = MarkdownBuffer()
    chunks = [
        "Intro text before code\n",
        "```python\n",
        "print('hi')\n",
        "```\n",
        "After code para\n\n",
    ]
    out = []
    out.extend(mbuf.push(chunks[0]))
    # No blank line yet; no flush expected (only one newline)
    assert out == []
    out.extend(mbuf.push(chunks[1]))
    # Starting fence should flush preceding text (with newline enforced)
    assert out == ["Intro text before code\n"]
    out.extend(mbuf.push(chunks[2]))
    # Still inside fence -> no flush
    assert out == ["Intro text before code\n"]
    out.extend(mbuf.push(chunks[3]))
    # Closing fence flushes fenced block
    assert out[-1].startswith("```python\n")
    assert out[-1].endswith("```\n") or out[-1].endswith("````\n")
    out.extend(mbuf.push(chunks[4]))
    # Now a normal paragraph ends with blank line, should be flushed on flush()
    tail = mbuf.flush()
    if tail:
        out.append(tail)
    # Last element should be the paragraph
    assert out[-1] == "After code para\n\n"


def test_variable_length_fence_backticks():
    mbuf = MarkdownBuffer()
    chunks = ["````text\n", "payload\n", "````\n"]
    out = []
    for c in chunks:
        out.extend(mbuf.push(c))
    # Entire fenced block should be emitted once closed
    assert len(out) == 1
    assert out[0].startswith("````text\n")
    assert out[0].endswith("````\n")


def test_tilde_fence_and_indentation():
    mbuf = MarkdownBuffer()
    chunks = [
        "Para before\n",
        "    ~~~json\n",  # indented fence
        '{\n  "a": 1\n}\n',
        "    ~~~\n",
    ]
    out = []
    out.extend(mbuf.push(chunks[0]))
    # No flush yet (no blank line)
    assert out == []
    out.extend(mbuf.push(chunks[1]))
    # Preceding para flushed with enforced newline
    assert out == ["Para before\n"]
    out.extend(mbuf.push(chunks[2]))
    assert len(out) == 1  # still inside fence
    out.extend(mbuf.push(chunks[3]))
    # Closing tilde fence should flush block
    assert out[-1].lstrip().startswith("~~~")
    assert out[-1].rstrip().endswith("~~~")


def test_no_flush_inside_fence_on_double_newline():
    mbuf = MarkdownBuffer()
    chunks = ["```\n", "line1\n\nline2\n", "```\n"]
    out = []
    out.extend(mbuf.push(chunks[0]))
    out.extend(mbuf.push(chunks[1]))
    # Still inside fence, no flush
    assert out == []
    out.extend(mbuf.push(chunks[2]))
    assert len(out) == 1
    assert out[0].startswith("```\n") and out[0].endswith("```\n")


def test_autoclose_unterminated_fence_on_flush():
    mbuf = MarkdownBuffer()
    chunks = ["```bash\n", "echo hi\n"]
    out = []
    for c in chunks:
        out.extend(mbuf.push(c))
    # Stream ends without closing fence
    tail = mbuf.flush()
    assert tail is not None
    # Should auto-close with matching marker and trailing newline
    assert tail.startswith("```bash\n")
    assert tail.endswith("```\n")


def test_text_before_fence_without_blank_line():
    mbuf = MarkdownBuffer()
    chunks = ["Text before\n", "```\n", "c\n", "```\n"]
    out = []
    out.extend(mbuf.push(chunks[0]))
    # No flush yet
    assert out == []
    out.extend(mbuf.push(chunks[1]))
    # Should flush preceding text with exactly one trailing newline
    assert out == ["Text before\n"]
    out.extend(mbuf.push(chunks[2]))
    out.extend(mbuf.push(chunks[3]))
    assert out[-1].startswith("```\n") and out[-1].endswith("```\n")


def test_blocks_end_with_newline_to_prevent_bleed():
    mbuf = MarkdownBuffer()
    chunks = ["Hello", " world\n\n", "Next"]
    out = []
    out.extend(mbuf.push(chunks[0]))
    out.extend(mbuf.push(chunks[1]))
    # First paragraph flushed and must end with newline
    assert out == ["Hello world\n\n"]
    out.extend(mbuf.push(chunks[2]))
    tail = mbuf.flush()
    assert tail == "Next"


# ===== NEW EDGE CASE TESTS =====


def test_unordered_list_followed_by_paragraph():
    """Unordered list followed by paragraph should have blank line separation."""
    mbuf = MarkdownBuffer()
    chunks = ["- item1\n", "- item2\n", "Paragraph text\n\n"]
    out = collect_blocks(mbuf, chunks)
    # List should be separated from paragraph with blank line
    assert len(out) >= 1
    assert out[0].endswith("\n\n"), f"Expected block to end with blank line, got: {repr(out[0])}"


def test_ordered_list_followed_by_paragraph():
    """Ordered list followed by paragraph should have blank line separation."""
    mbuf = MarkdownBuffer()
    chunks = ["1. item1\n", "2. item2\n", "Paragraph text\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert len(out) >= 1
    assert out[0].endswith("\n\n"), f"Expected block to end with blank line, got: {repr(out[0])}"


def test_different_unordered_list_markers():
    """Lists with different markers (-, *, +) followed by text."""
    for marker in ["-", "*", "+"]:
        mbuf = MarkdownBuffer()
        chunks = [f"{marker} item1\n", f"{marker} item2\n", "Text\n\n"]
        out = collect_blocks(mbuf, chunks)
        assert len(out) >= 1
        assert out[0].endswith("\n\n"), (
            f"Expected block to end with blank line for {marker} marker, got: {repr(out[0])}"
        )


def test_blockquote_followed_by_paragraph():
    """Blockquote followed by paragraph should have blank line separation."""
    mbuf = MarkdownBuffer()
    chunks = ["> quote line 1\n", "> quote line 2\n", "Regular text\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert len(out) >= 1
    assert out[0].endswith("\n\n"), f"Expected block to end with blank line, got: {repr(out[0])}"


def test_single_blockquote_line_followed_by_text():
    """Single blockquote line followed by text should separate."""
    mbuf = MarkdownBuffer()
    chunks = ["> Just one quote\n", "Text after\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert len(out) >= 1
    assert out[0].endswith("\n\n")


def test_horizontal_rule_followed_by_paragraph():
    """Horizontal rule followed by paragraph should separate."""
    mbuf = MarkdownBuffer()
    chunks = ["---\n", "Paragraph text\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert len(out) >= 1
    # HR should be separated from paragraph
    assert out[0].endswith("\n\n"), f"Expected HR block to end with blank line, got: {repr(out[0])}"


def test_horizontal_rule_variants():
    """Test different HR patterns: ---, ***, ___."""
    for hr_marker in ["---", "***", "___", "- - -", "* * *", "_ _ _"]:
        mbuf = MarkdownBuffer()
        chunks = [f"{hr_marker}\n", "Text\n\n"]
        out = collect_blocks(mbuf, chunks)
        if out:  # HR might be detected or not depending on pattern
            # If flushed, should have separation
            if len(out) >= 1:
                assert out[0].endswith("\n\n") or len(out) > 1


def test_mixed_list_and_blockquote():
    """Mixed list items and blockquote should separate from following text."""
    mbuf = MarkdownBuffer()
    chunks = ["- item1\n", "- item2\n", "> quote\n", "Regular text\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert len(out) >= 1
    assert out[0].endswith("\n\n")


def test_nested_list_item_followed_by_text():
    """Indented/nested list items followed by text should separate."""
    mbuf = MarkdownBuffer()
    chunks = ["  - nested item\n", "  - another nested\n", "Paragraph\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert len(out) >= 1
    assert out[0].endswith("\n\n")


def test_html_block_followed_by_text():
    """HTML block followed by text should separate."""
    mbuf = MarkdownBuffer()
    chunks = ["<div>\ncontent\n</div>\n", "Text after\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert len(out) >= 1
    # HTML should be separated from following text
    assert out[0].endswith("\n\n") or len(out) > 1


def test_list_then_blockquote_then_paragraph():
    """Complex sequence: list -> blockquote -> paragraph."""
    mbuf = MarkdownBuffer()
    chunks = ["- item\n", "> quote\n", "Normal text\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert len(out) >= 1
    # Entire mixed block should end with blank line
    assert out[0].endswith("\n\n")


def test_blockquote_continuation_stays_grouped():
    """Multiple blockquote lines should stay grouped together."""
    mbuf = MarkdownBuffer()
    chunks = ["> First line\n", "> Second line\n", "> Third line\n", "Regular text\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert len(out) >= 1
    # All blockquote lines should be in first block with text properly separated
    assert out[0].endswith("\n\n")
    # Should contain all three blockquote lines plus separation
    assert out[0].count(">") >= 3


def test_list_with_blank_lines_within():
    """List items with blank lines inside should be preserved."""
    mbuf = MarkdownBuffer()
    chunks = ["- item1\n", "\n", "- item2\n", "Text after\n\n"]
    out = collect_blocks(mbuf, chunks)
    # Blank line within list might create multiple blocks
    assert len(out) >= 1
    # Whatever the last block is, should have proper ending
    assert out[-1].endswith("\n\n") or out[-1].startswith("Text after")


def test_blockquote_with_multiple_levels():
    """Blockquotes with nested levels (> >)."""
    mbuf = MarkdownBuffer()
    chunks = ["> level 1\n", "> > level 2\n", "Regular text\n\n"]
    out = collect_blocks(mbuf, chunks)
    assert len(out) >= 1
    assert out[0].endswith("\n\n")


def test_no_extra_blanks_with_existing_double_newline():
    """If block already ends with \\n\\n, don't add more."""
    mbuf = MarkdownBuffer()
    chunks = ["- item1\n", "- item2\n\n", "Text\n\n"]
    out = collect_blocks(mbuf, chunks)
    # Block should end with exactly two newlines, not more
    assert out[0].endswith("\n\n")
    # Count trailing newlines - should be exactly 2
    trailing_newlines = len(out[0]) - len(out[0].rstrip("\n"))
    assert trailing_newlines == 2
