
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
        "{\n  \"a\": 1\n}\n",
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
