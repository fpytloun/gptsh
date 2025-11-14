# MarkdownBuffer Analysis: List Rendering Issue

## Problem Statement
During streaming output, text after a list does not have an empty line separator, causing poor rendering in Rich Markdown. The list and following content appear without proper block separation.

## Root Cause
The `MarkdownBuffer` class (in `gptsh/core/runner.py`) uses double-newline (`\n\n`) as the flush boundary for Markdown blocks. However:

1. **List items** in Markdown typically end with a single newline:
   ```
   - item1
   - item2
   ```

2. **Following content** (like a paragraph) also starts without a preceding blank line in streamed content:
   ```
   - item1
   - item2
   Next paragraph
   ```

3. **Result**: When both are in the same block when the double-newline appears, they're rendered as:
   ```
   - item1
   - item2
   Next paragraph
   ```
   Without the blank line that Markdown renderers need to separate block elements.

## Key Logic Points
- Line 76: `par_idx = self._buf.find("\n\n")` - looks for paragraph boundaries
- Line 101-105: Flushes block when `\n\n` found and fence not in the way
- Line 152: When fenced block closes, appends `acc` (accumulated content) as-is
- Line 168: Latency guard flushes with `\n\n`

## Proposed Fix: List Detection and Separator Injection

Add a helper method to detect list items and inject blank-line separators:

```python
def _is_list_line(self, line: str) -> bool:
    """Check if a line is a list item (-, *, +, or numbered)."""
    stripped = line.lstrip()
    if not stripped:
        return False
    # Unordered lists: -, *, +
    if stripped[0] in ('-', '*', '+') and len(stripped) > 1 and stripped[1] == ' ':
        return True
    # Ordered lists: 1., 2., etc.
    import re
    if re.match(r'^\d+\.\s', stripped):
        return True
    return False

def _contains_list(self, text: str) -> bool:
    """Check if text contains list items."""
    for line in text.splitlines():
        if self._is_list_line(line):
            return True
    return False
```

Then modify the flush logic (line 101-105) to:
```python
if par_idx != -1 and (fence_start_idx == -1 or par_idx < fence_start_idx):
    block = self._buf[: par_idx + 2]
    
    # NEW: If block contains lists and next content doesn't start with blank,
    # ensure block ends with blank line for proper separation
    if self._contains_list(block):
        next_content = self._buf[par_idx + 2:]
        if next_content and not next_content.startswith('\n'):
            if not block.endswith('\n\n'):
                block = block.rstrip('\n') + '\n\n'
    
    out.append(self._ensure_trailing_newline(block))
    self._buf = self._buf[par_idx + 2:]
    continue
```

## Alternative Simpler Approach
When a block ends with exactly one newline (not `\n\n`), and it contains list items, ensure it always ends with `\n\n`:

This could be integrated into `_ensure_trailing_newline` or as a separate post-processing step.

## Test Case Needed
```python
def test_list_followed_by_paragraph():
    """Lists should be separated from following content by blank line."""
    mbuf = MarkdownBuffer()
    chunks = [
        "- item1\n",
        "- item2\n", 
        "Paragraph text\n\n"
    ]
    out = collect_blocks(mbuf, chunks)
    # List block should end with \n\n for proper separation
    assert out[0].endswith('\n\n')  # List block properly terminated
    assert len(out) >= 1  # May be one or two blocks, but properly separated
```

## Files to Modify
- `gptsh/core/runner.py` (MarkdownBuffer class)
- `gptsh/tests/test_markdown_buffer.py` (add test case)
