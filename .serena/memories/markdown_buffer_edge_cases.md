# MarkdownBuffer: Edge Cases Analysis

## Critical Edge Cases NOT Currently Covered

### 1. Lists Followed by Content
- **Issue**: Unordered (`- item`), ordered (`1. item`), nested lists without blank lines before next content
- **Example**: `- item1\n- item2\nParagraph text\n\n`
- **Result**: Gets bundled as one block, no separation between list and paragraph
- **Test Status**: NOT TESTED

### 2. Blockquotes Followed by Content
- **Issue**: Blockquote lines (`> quote`) followed immediately by regular text
- **Example**: `> quote1\n> quote2\nRegular text\n\n`
- **Result**: Blockquote and paragraph rendered without blank line separation
- **Test Status**: NOT TESTED

### 3. Horizontal Rules Without Separation
- **Issue**: Horizontal rules (`---`, `***`, `___`) followed by content
- **Example**: `---\nParagraph text\n\n`
- **Result**: HR may be treated as underline or list separator
- **Test Status**: NOT TESTED

### 4. HTML Blocks Without Separation
- **Issue**: Raw HTML followed by content
- **Example**: `<div>content</div>\nParagraph\n\n`
- **Result**: HTML block not recognized as separate element
- **Test Status**: NOT TESTED

### 5. Mixed Block Elements
- **Issue**: Combination of lists, blockquotes, and other block elements
- **Example**: `- item\n> quote\nRegular text\n\n`
- **Result**: Entire mixed block bundled together
- **Test Status**: NOT TESTED

## Medium Priority Edge Cases

### 6. Blockquote Continuation
- Multiple lines starting with `>` should stay grouped
- Currently not specially handled, but might work by accident
- **Test Status**: NOT TESTED

### 7. Complex Indentation
- Mixed tabs/spaces in fences and lists
- Deeply indented lists with mixed markers
- **Test Status**: PARTIALLY TESTED (test_tilde_fence_and_indentation)

### 8. Fence with Special Content
- Fence containing text that looks like fence markers
- Unequal-length closing fence
- **Test Status**: MOSTLY HANDLED

## Root Cause

MarkdownBuffer only recognizes 3 boundary types:
1. Paragraph: `\n\n` (blank line)
2. Fence: code block start/end
3. Latency: buffer size + newline

**Missing**: Block-level element detection (lists, blockquotes, HR, HTML)

These elements end with single `\n`, not `\n\n`. When followed by other content without blank line, they get bundled into one block sent to Rich Markdown, causing rendering issues.

## Comprehensive Fix Strategy

Add helper to detect all block-level elements:

```python
def _is_block_element_line(self, line: str) -> bool:
    stripped = line.lstrip()
    if not stripped:
        return False
    
    # Lists
    if stripped[0] in ('-', '*', '+') and len(stripped) > 1 and stripped[1] == ' ':
        return True
    if stripped[0].isdigit():
        # Simple check for "1. ", "2. " etc.
        import re
        if re.match(r'^\d+\.\s', stripped):
            return True
    
    # Blockquotes
    if stripped[0] == '>':
        return True
    
    # Horizontal rules
    import re
    if re.match(r'^([-*_])(\s*\1){2,}\s*$', stripped):
        return True
    
    # HTML block start
    if stripped[0] == '<':
        return True
    
    return False
```

Then when flushing a block:
- Check if block ends with block element
- If yes and next content exists without blank line, ensure block ends with `\n\n`

## Required Test Cases

1. `test_list_followed_by_paragraph()` - unordered list
2. `test_ordered_list_followed_by_paragraph()` - numbered list
3. `test_blockquote_followed_by_paragraph()` - blockquote
4. `test_horizontal_rule_followed_by_paragraph()` - HR
5. `test_blockquote_continuation()` - multi-line blockquote stays grouped
6. `test_mixed_block_elements()` - list + blockquote + text
7. `test_nested_list_followed_by_text()` - indented list
8. `test_html_block_followed_by_text()` - raw HTML separation

## Files to Modify

- `gptsh/core/runner.py` - MarkdownBuffer class
- `gptsh/tests/test_markdown_buffer.py` - add 8+ new test cases
